"""
test_db_crypto.py - Pruebas de aceptación y verificación de seguridad
según SPEC-01-DB-INVENTORY y RULES.md.
"""

import os
import sqlite3
import tempfile
import unittest

from cryptography.fernet import InvalidToken

# Importar módulos a testear
from crypto import decrypt_password, encrypt_password
from database import (
    add_device,
    delete_device,
    get_backup_by_id,
    get_connection,
    get_device_by_id,
    get_devices,
    init_db,
    update_backup_s3_metadata,
)


class TestDatabaseAndCrypto(unittest.TestCase):

    def setUp(self):
        # Crear base de datos temporal aislada para cada test
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            self.db_path = tmp.name
        init_db(self.db_path)

    def tearDown(self):
        # Limpiar base de datos temporal
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_case_1_zero_plaintext_verification(self):
        """Caso 1: Comprobar que la contraseña en texto claro NUNCA se persiste en disco ni en BD."""
        plain_secret = "Cisco7200_Sup3rS3cr3t!"
        device = add_device(
            hostname="R1",
            ip_address="192.168.56.101",
            username="admin",
            plain_password=plain_secret,
            db_path=self.db_path,
        )

        # 1. Consultar registro crudo en SQLite
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT encrypted_password FROM devices WHERE id = ?", (device["id"],))
            row = cursor.fetchone()

        self.assertIsNotNone(row)
        encrypted_val = row["encrypted_password"]

        # Aserciones lógicas
        self.assertNotEqual(encrypted_val, plain_secret)
        self.assertNotIn(plain_secret, encrypted_val)
        self.assertTrue(encrypted_val.startswith("gAAAAA"))

        # Inspección binaria del archivo .db en disco
        with open(self.db_path, "rb") as f:
            disk_bytes = f.read()
        self.assertNotIn(
            plain_secret.encode("utf-8"),
            disk_bytes,
            "FALLO DE SEGURIDAD: La contraseña en texto plano existe en los bytes en disco de la base de datos.",
        )

    def test_case_2_round_trip_integrity(self):
        """Caso 2: Recuperación y descifrado íntegro (Round-trip Integrity)."""
        original_password = "CiscoComplex#Pass2026!$%&"
        device = add_device(
            hostname="R2",
            ip_address="192.168.56.102",
            username="netadmin",
            plain_password=original_password,
            db_path=self.db_path,
        )

        record = get_device_by_id(device["id"], include_encrypted_password=True, db_path=self.db_path)
        self.assertIsNotNone(record)
        self.assertIn("encrypted_password", record)

        recovered_password = decrypt_password(record["encrypted_password"])
        self.assertEqual(recovered_password, original_password)

    def test_case_3_credentials_exclusion_in_inventory(self):
        """Caso 3: Ocultamiento de credenciales en consultas de inventario."""
        device = add_device(
            hostname="R3",
            ip_address="192.168.56.103",
            username="operator",
            plain_password="SecretPassword123",
            db_path=self.db_path,
        )

        devices_list = get_devices(db_path=self.db_path)
        single_device = get_device_by_id(device["id"], include_encrypted_password=False, db_path=self.db_path)

        self.assertTrue(len(devices_list) > 0)
        for dev in devices_list:
            self.assertNotIn("encrypted_password", dev)
            self.assertNotIn("password", dev)

        self.assertIsNotNone(single_device)
        self.assertNotIn("encrypted_password", single_device)
        self.assertNotIn("password", single_device)

    def test_case_4_hmac_tamper_and_invalid_token(self):
        """Caso 4: Rechazo por manipulación de token o clave inválida."""
        token = encrypt_password("SomePassword")
        tampered_token = token[:-4] + "AAAA"

        with self.assertRaises(InvalidToken):
            decrypt_password(tampered_token)

    def test_case_5_sql_injection_neutralization(self):
        """Caso 5: Resistencia a inyección SQL mediante consultas parametrizadas."""
        malicious_hostname = "R1'); DROP TABLE devices; --"
        device = add_device(
            hostname=malicious_hostname,
            ip_address="192.168.56.250",
            username="admin",
            plain_password="Password123!",
            db_path=self.db_path,
        )

        # La tabla devices DEBE seguir existiendo
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='devices'")
            self.assertIsNotNone(cursor.fetchone())

        # El hostname malicioso se guardó literalmente como texto
        fetched = get_device_by_id(device["id"], db_path=self.db_path)
        self.assertEqual(fetched["hostname"], malicious_hostname)

    def test_case_6_referential_integrity_cascade_delete(self):
        """Caso 6: Integridad referencial y borrado en cascada de backups al eliminar dispositivo."""
        device = add_device(
            hostname="R4",
            ip_address="192.168.56.104",
            username="admin",
            plain_password="Password456!",
            db_path=self.db_path,
        )
        dev_id = device["id"]

        # Insertar dos backups simulados vinculados a R4
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO backups (device_id, raw_config) VALUES (?, ?)",
                (dev_id, "hostname R4\ninterface fa0/0\n"),
            )
            cursor.execute(
                "INSERT INTO backups (device_id, raw_config) VALUES (?, ?)",
                (dev_id, "hostname R4\ninterface fa0/0\n ip address 10.0.0.1 255.255.255.0\n"),
            )
            conn.commit()

        # Verificar que existen 2 backups
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM backups WHERE device_id = ?", (dev_id,))
            self.assertEqual(cursor.fetchone()["cnt"], 2)

        # Eliminar el dispositivo
        deleted = delete_device(dev_id, db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertIsNone(get_device_by_id(dev_id, db_path=self.db_path))

        # Verificar que los backups fueron eliminados en cascada
        with get_connection(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM backups WHERE device_id = ?", (dev_id,))
            self.assertEqual(cursor.fetchone()["cnt"], 0)

    def test_case_7_encrypted_secret_storage_and_round_trip(self):
        """Caso 7: Almacenamiento cifrado, ocultamiento público y descifrado íntegro de encrypted_secret."""
        plain_pw = "UserPass_123!"
        plain_sec = "EnableSecret_Cisco123!"
        device = add_device(
            hostname="IOSv-R1",
            ip_address="192.168.76.10",
            username="admin",
            plain_password=plain_pw,
            plain_secret=plain_sec,
            db_path=self.db_path,
        )

        # 1. Validar que ni password ni secret se exponen en consultas públicas
        self.assertNotIn("encrypted_secret", device)
        self.assertNotIn("secret", device)
        public_single = get_device_by_id(device["id"], include_encrypted_password=False, db_path=self.db_path)
        self.assertNotIn("encrypted_secret", public_single)
        for dev in get_devices(db_path=self.db_path):
            self.assertNotIn("encrypted_secret", dev)
            self.assertNotIn("secret", dev)

        # 2. Validar almacenamiento cifrado en disco y en BD
        record = get_device_by_id(device["id"], include_encrypted_password=True, db_path=self.db_path)
        self.assertIsNotNone(record)
        self.assertIn("encrypted_secret", record)
        self.assertIsNotNone(record["encrypted_secret"])
        self.assertNotEqual(record["encrypted_secret"], plain_sec)
        self.assertTrue(record["encrypted_secret"].startswith("gAAAAA"))
        self.assertEqual(decrypt_password(record["encrypted_secret"]), plain_sec)

        with open(self.db_path, "rb") as f:
            disk_bytes = f.read()
        self.assertNotIn(plain_sec.encode("utf-8"), disk_bytes)

    def test_case_8_optional_empty_secret_and_legacy_migration(self):
        """Caso 8: Secret opcional/vacío y migración idempotente de tabla devices legacy."""
        # A. Secret omitido o vacío se almacena como None
        dev_no_secret = add_device(
            hostname="R-NoSec",
            ip_address="192.168.76.11",
            username="admin",
            plain_password="PassOnly123!",
            plain_secret="   ",
            db_path=self.db_path,
        )
        rec = get_device_by_id(dev_no_secret["id"], include_encrypted_password=True, db_path=self.db_path)
        self.assertIsNone(rec["encrypted_secret"])

        # B. Migración desde tabla legacy sin columna encrypted_secret
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as legacy_db:
            pass
        try:
            conn = sqlite3.connect(legacy_db.name)
            try:
                conn.execute("""
                    CREATE TABLE devices (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        hostname TEXT NOT NULL,
                        ip_address TEXT NOT NULL UNIQUE,
                        device_type TEXT NOT NULL DEFAULT 'cisco_ios',
                        username TEXT NOT NULL,
                        encrypted_password TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.commit()
            finally:
                conn.close()

            # Ejecutar init_db() debe migrar limpiamente sin perder datos
            init_db(legacy_db.name)
            conn = get_connection(legacy_db.name)
            try:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(devices);").fetchall()}
            finally:
                conn.close()
            self.assertIn("encrypted_secret", cols)

            # Verificar que se puede insertar con plain_secret tras migrar
            migrated_dev = add_device(
                hostname="Migrated-R1",
                ip_address="192.168.76.12",
                username="admin",
                plain_password="Cisco123!",
                plain_secret="Secret123!",
                db_path=legacy_db.name,
            )
            migrated_rec = get_device_by_id(migrated_dev["id"], include_encrypted_password=True, db_path=legacy_db.name)
            self.assertEqual(decrypt_password(migrated_rec["encrypted_secret"]), "Secret123!")
        finally:
            if os.path.exists(legacy_db.name):
                try:
                    os.remove(legacy_db.name)
                except OSError:
                    pass

    def test_case_9_s3_columns_migration_and_metadata_update(self):
        """Caso 9: Migración idempotente de backups (s3_key, s3_version_id) y update_backup_s3_metadata()."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as legacy_db:
            pass
        try:
            conn = sqlite3.connect(legacy_db.name)
            try:
                conn.execute("""
                    CREATE TABLE devices (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        hostname TEXT NOT NULL,
                        ip_address TEXT NOT NULL UNIQUE,
                        device_type TEXT NOT NULL DEFAULT 'cisco_ios',
                        username TEXT NOT NULL,
                        encrypted_password TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                """)
                conn.execute("""
                    CREATE TABLE backups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        device_id INTEGER NOT NULL,
                        raw_config TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
                    );
                """)
                conn.execute(
                    "INSERT INTO devices (hostname, ip_address, username, encrypted_password) VALUES (?, ?, ?, ?)",
                    ("R-Legacy", "10.0.0.1", "admin", "token"),
                )
                conn.execute("INSERT INTO backups (device_id, raw_config) VALUES (?, ?)", (1, "hostname R-Legacy"))
                conn.commit()
            finally:
                conn.close()

            # La migración es idempotente: se puede ejecutar varias veces sin error
            init_db(legacy_db.name)
            init_db(legacy_db.name)

            conn = get_connection(legacy_db.name)
            try:
                cols = {row["name"] for row in conn.execute("PRAGMA table_info(backups);").fetchall()}
            finally:
                conn.close()
            self.assertTrue({"s3_key", "s3_version_id"} <= cols)

            # El respaldo previo se conserva intacto y sin metadatos S3
            legacy_backup = get_backup_by_id(1, db_path=legacy_db.name)
            self.assertEqual(legacy_backup["raw_config"], "hostname R-Legacy")
            self.assertIsNone(legacy_backup["s3_key"])
            self.assertIsNone(legacy_backup["s3_version_id"])

            # Actualización con y sin VersionId; id inexistente retorna False
            key = "backups/R-Legacy/20261125T201000Z_backup_1.enc"
            self.assertTrue(update_backup_s3_metadata(1, key, "ver-9", db_path=legacy_db.name))
            stored = get_backup_by_id(1, db_path=legacy_db.name)
            self.assertEqual((stored["s3_key"], stored["s3_version_id"]), (key, "ver-9"))
            self.assertTrue(update_backup_s3_metadata(1, key, None, db_path=legacy_db.name))
            self.assertIsNone(get_backup_by_id(1, db_path=legacy_db.name)["s3_version_id"])
            self.assertFalse(update_backup_s3_metadata(999, key, "x", db_path=legacy_db.name))
        finally:
            if os.path.exists(legacy_db.name):
                try:
                    os.remove(legacy_db.name)
                except OSError:
                    pass


if __name__ == "__main__":
    unittest.main()

