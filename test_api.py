"""
test_api.py - Pruebas de integración para el servidor Flask (app.py) y sus 10 rutas REST.
Cubre inventario CRUD, prueba SSH, extracción y listado de respaldos,
consulta individual, cálculo de diff y ejecución de rollback.
Gobernado por RULES.md y ARCHITECTURE.md.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from app import app
from database import add_backup, get_backup_by_id, init_db


class TestFlaskAPI(unittest.TestCase):

    def setUp(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            self.db_path = tmp.name
        init_db(self.db_path)

        app.config["TESTING"] = True
        app.config["DB_PATH"] = self.db_path
        self.client = app.test_client()

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def _create_sample_device(self, ip="192.168.76.10", hostname="R1-IOSv"):
        return self.client.post(
            "/api/devices",
            json={
                "hostname": hostname,
                "ip_address": ip,
                "device_type": "cisco_ios",
                "port": 22,
                "username": "admin",
                "password": "CiscoPassword123!",
                "secret": "CiscoSecret123!",
            },
        )

    def test_index_renders_html5(self):
        """GET / retorna 200 OK y contiene la plantilla index.html con Tailwind CDN."""
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Network Config Manager", html)
        self.assertIn("https://cdn.tailwindcss.com", html)

    def test_create_list_and_delete_device_without_exposing_secrets(self):
        """POST /api/devices, GET /api/devices y DELETE /api/devices/<id>."""
        # Alta exitosa
        create_resp = self._create_sample_device()
        self.assertEqual(create_resp.status_code, 201)
        payload = create_resp.get_json()
        self.assertEqual(payload["status"], "success")
        dev = payload["data"]
        self.assertEqual(dev["hostname"], "R1-IOSv")
        self.assertEqual(dev["port"], 22)
        self.assertNotIn("password", dev)
        self.assertNotIn("encrypted_password", dev)
        self.assertNotIn("secret", dev)
        self.assertNotIn("encrypted_secret", dev)

        # Rechazo de IP duplicada (409)
        dup_resp = self._create_sample_device()
        self.assertEqual(dup_resp.status_code, 409)

        # Rechazo de IP inválida (400)
        bad_ip_resp = self._create_sample_device(ip="999.999.1.1", hostname="BadIP")
        self.assertEqual(bad_ip_resp.status_code, 400)

        # Listado (GET /api/devices)
        list_resp = self.client.get("/api/devices")
        self.assertEqual(list_resp.status_code, 200)
        devices = list_resp.get_json()["data"]
        self.assertEqual(len(devices), 1)
        self.assertNotIn("encrypted_password", devices[0])
        self.assertNotIn("encrypted_secret", devices[0])
        self.assertNotIn("password", devices[0])
        self.assertNotIn("secret", devices[0])

        # Eliminación (DELETE /api/devices/<id>)
        del_resp = self.client.delete(f"/api/devices/{dev['id']}")
        self.assertEqual(del_resp.status_code, 200)
        self.assertEqual(self.client.delete(f"/api/devices/{dev['id']}").status_code, 404)

    @patch("app.test_connection")
    def test_ssh_connectivity_endpoint(self, mock_test_conn):
        """POST /api/devices/<id>/test verifica conectividad SSH y retorna 200 o 502."""
        dev_id = self._create_sample_device().get_json()["data"]["id"]

        mock_test_conn.return_value = True
        ok_resp = self.client.post(f"/api/devices/{dev_id}/test")
        self.assertEqual(ok_resp.status_code, 200)
        self.assertTrue(ok_resp.get_json()["connected"])
        # Confirmar que recibió credenciales cifradas desde DB para descifrarlas en net_automation
        passed_dev = mock_test_conn.call_args[0][0]
        self.assertIn("encrypted_password", passed_dev)
        self.assertIn("encrypted_secret", passed_dev)

        mock_test_conn.return_value = False
        fail_resp = self.client.post(f"/api/devices/{dev_id}/test")
        self.assertEqual(fail_resp.status_code, 502)
        self.assertFalse(fail_resp.get_json()["connected"])

        self.assertEqual(self.client.post("/api/devices/9999/test").status_code, 404)

    @patch("app.fetch_running_config")
    def test_backup_creation_listing_and_detail(self, mock_fetch):
        """POST /api/devices/<id>/backup, GET /api/devices/<id>/backups y GET /api/backups/<id>."""
        dev_id = self._create_sample_device().get_json()["data"]["id"]

        sample_cfg = "!\nhostname R1-IOSv\ninterface GigabitEthernet0/0\n ip address 192.168.76.10 255.255.255.0\n!\nend"
        mock_fetch.return_value = sample_cfg

        resp = self.client.post(f"/api/devices/{dev_id}/backup")
        self.assertEqual(resp.status_code, 201)
        b_data = resp.get_json()["data"]
        backup_id = b_data["id"]
        self.assertEqual(b_data["config_text"], sample_cfg)

        # Manejo explícito de errores Netmiko -> códigos HTTP
        mock_fetch.side_effect = NetmikoAuthenticationException("Auth error")
        self.assertEqual(self.client.post(f"/api/devices/{dev_id}/backup").status_code, 401)

        mock_fetch.side_effect = NetmikoTimeoutException("Timeout")
        self.assertEqual(self.client.post(f"/api/devices/{dev_id}/backup").status_code, 504)

        # Listar respaldos del dispositivo
        list_b_resp = self.client.get(f"/api/devices/{dev_id}/backups")
        self.assertEqual(list_b_resp.status_code, 200)
        self.assertEqual(len(list_b_resp.get_json()["data"]), 1)

        # Detalle de respaldo específico
        detail_resp = self.client.get(f"/api/backups/{backup_id}")
        self.assertEqual(detail_resp.status_code, 200)
        self.assertEqual(detail_resp.get_json()["data"]["config_text"], sample_cfg)
        self.assertEqual(self.client.get("/api/backups/99999").status_code, 404)

    @patch("app.upload_encrypted_backup")
    @patch("app.fetch_running_config")
    def test_backup_reports_cloud_sync_and_persists_s3_metadata(self, mock_fetch, mock_upload):
        """POST /backup incluye cloud_sync y registra s3_key/s3_version_id en SQLite."""
        dev_id = self._create_sample_device().get_json()["data"]["id"]
        cfg = "!\nhostname R1-IOSv\nend"
        s3_key = "backups/R1-IOSv/20261125T201000Z_backup_1.enc"
        mock_fetch.return_value = cfg
        mock_upload.return_value = {"synced": True, "s3_key": s3_key, "version_id": "v1", "error": None}

        resp = self.client.post(f"/api/devices/{dev_id}/backup")
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()["data"]
        self.assertTrue(data["cloud_sync"]["synced"])
        self.assertEqual(data["s3_key"], s3_key)
        self.assertEqual(data["s3_version_id"], "v1")
        mock_upload.assert_called_once_with("R1-IOSv", data["id"], cfg)

        stored = get_backup_by_id(data["id"], db_path=self.db_path)
        self.assertEqual(stored["s3_key"], s3_key)
        self.assertEqual(stored["s3_version_id"], "v1")
        detail = self.client.get(f"/api/backups/{data['id']}").get_json()["data"]
        self.assertEqual(detail["s3_key"], s3_key)
        history = self.client.get(f"/api/devices/{dev_id}/backups").get_json()["data"]
        self.assertEqual(history[0]["s3_key"], s3_key)

    @patch("app.upload_encrypted_backup")
    @patch("app.fetch_running_config")
    def test_backup_keeps_local_copy_when_s3_fails(self, mock_fetch, mock_upload):
        """Si S3 falla, la API responde 201, conserva el respaldo local y reporta el error cloud."""
        dev_id = self._create_sample_device().get_json()["data"]["id"]
        mock_fetch.return_value = "!\nhostname R1-IOSv\nend"
        mock_upload.return_value = {
            "synced": False,
            "s3_key": None,
            "version_id": None,
            "error": "AWS S3 rechazó la subida (AccessDenied): Access Denied",
        }

        resp = self.client.post(f"/api/devices/{dev_id}/backup")
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertFalse(body["data"]["cloud_sync"]["synced"])
        self.assertIn("AccessDenied", body["data"]["cloud_sync"]["error"])
        self.assertIn("Advertencia", body["message"])

        stored = get_backup_by_id(body["data"]["id"], db_path=self.db_path)
        self.assertEqual(stored["raw_config"], "!\nhostname R1-IOSv\nend")
        self.assertIsNone(stored["s3_key"])

    @patch("cloud_storage.boto3.client")
    @patch("app.fetch_running_config")
    def test_backup_with_s3_disabled_never_contacts_aws(self, mock_fetch, mock_boto):
        """Con ENABLE_S3_BACKUP=false el respaldo es solo local y cloud_sync no reporta error."""
        dev_id = self._create_sample_device().get_json()["data"]["id"]
        mock_fetch.return_value = "!\nhostname R1-IOSv\nend"

        with patch.dict(os.environ, {"ENABLE_S3_BACKUP": "false"}):
            resp = self.client.post(f"/api/devices/{dev_id}/backup")

        self.assertEqual(resp.status_code, 201)
        cloud = resp.get_json()["data"]["cloud_sync"]
        self.assertFalse(cloud["synced"])
        self.assertIsNone(cloud["error"])
        mock_boto.assert_not_called()

    def test_diff_endpoint(self):
        """GET /api/devices/<id>/diff?v1=<id>&v2=<id> calcula unified_diff correctamente."""
        dev_id = self._create_sample_device().get_json()["data"]["id"]
        b1 = add_backup(dev_id, "!\nhostname R1\n!\nend", db_path=self.db_path)
        b2 = add_backup(dev_id, "!\nhostname R1-CORE\ninterface Loopback0\n!\nend", db_path=self.db_path)

        resp = self.client.get(f"/api/devices/{dev_id}/diff?v1={b1['id']}&v2={b2['id']}")
        self.assertEqual(resp.status_code, 200)
        diff_data = resp.get_json()["data"]
        self.assertTrue(diff_data["has_changes"])
        self.assertIn("-hostname R1", diff_data["diff_lines"])
        self.assertIn("+hostname R1-CORE", diff_data["diff_lines"])
        self.assertIn("+interface Loopback0", diff_data["diff_lines"])

        # Errores de validación
        self.assertEqual(self.client.get(f"/api/devices/{dev_id}/diff").status_code, 400)
        self.assertEqual(self.client.get(f"/api/devices/{dev_id}/diff?v1=abc&v2=1").status_code, 400)
        self.assertEqual(self.client.get(f"/api/devices/{dev_id}/diff?v1={b1['id']}&v2=9999").status_code, 404)

    @patch("app.apply_configuration")
    def test_rollback_endpoint(self, mock_apply):
        """POST /api/devices/<id>/rollback/<backup_id> aplica el respaldo y devuelve el resultado."""
        dev_id = self._create_sample_device().get_json()["data"]["id"]
        cfg_v1 = "!\nhostname R1-RESTORED\ninterface Loopback0\n ip address 1.1.1.1 255.255.255.255\n!\nend"
        b1 = add_backup(dev_id, cfg_v1, db_path=self.db_path)

        mock_apply.return_value = {
            "status": "success",
            "host": "192.168.76.10",
            "commands_applied": 3,
            "output": "R1(config)#hostname R1-RESTORED\n[OK]",
        }

        resp = self.client.post(f"/api/devices/{dev_id}/rollback/{b1['id']}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()["data"]
        self.assertEqual(data["backup_id"], b1["id"])
        self.assertEqual(data["commands_applied"], 3)
        self.assertIn("[OK]", data["output"])

        # Verificar que apply_configuration recibió la config exacta del respaldo
        self.assertEqual(mock_apply.call_args[0][1], cfg_v1)

        # Rollback con timeout
        mock_apply.side_effect = NetmikoTimeoutException("Timeout during rollback")
        self.assertEqual(self.client.post(f"/api/devices/{dev_id}/rollback/{b1['id']}").status_code, 504)


if __name__ == "__main__":
    unittest.main()
