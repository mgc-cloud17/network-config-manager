"""
test_net_automation.py - Pruebas unitarias para net_automation.py usando mocks.
Valida:
1. Conexión exitosa, timeout, fallo de autenticación y errores de socket/OS en test_connection().
2. Extracción de configuración, sanitización de cabeceras volátiles y manejo de excepciones en fetch_running_config().
3. Descifrado de contraseñas cifradas con Fernet en memoria volátil.
4. Cálculo y formato estricto de diffs con difflib.unified_diff (marcadores +, -, @@, ---, +++).
Gobernado por RULES.md y SPEC-02-NET-AUTOMATION.
"""

import unittest
from unittest.mock import MagicMock, patch

from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException
from paramiko.ssh_exception import SSHException

from crypto import encrypt_password
from net_automation import (
    apply_configuration,
    calculate_diff,
    fetch_running_config,
    test_connection,
)


class TestNetAutomation(unittest.TestCase):

    def setUp(self):
        # Dispositivo con contraseña y secret en texto plano
        self.device_plain = {
            "hostname": "R1-IOSv",
            "ip_address": "192.168.76.10",
            "username": "admin",
            "password": "CiscoSecretPassword123!",
            "secret": "CiscoEnableSecret123!",
            "device_type": "cisco_ios",
        }
        # Dispositivo con contraseña y secret cifrados con Fernet (como viene de la BD)
        self.device_encrypted = {
            "hostname": "R1-IOSv",
            "ip_address": "192.168.76.10",
            "username": "admin",
            "encrypted_password": encrypt_password("CiscoSecretPassword123!"),
            "encrypted_secret": encrypt_password("CiscoEnableSecret123!"),
            "device_type": "cisco_ios",
        }

    # ==========================================
    # 1. Pruebas de test_connection() con Mocks
    # ==========================================

    @patch("net_automation.ConnectHandler")
    def test_connection_success_plain_password(self, mock_connect):
        """Caso 1: Conexión exitosa con contraseña directa retorna True."""
        mock_conn = MagicMock()
        mock_conn.find_prompt.return_value = "R1#"
        mock_connect.return_value.__enter__.return_value = mock_conn

        result = test_connection(self.device_plain)
        self.assertTrue(result)
        mock_conn.clear_buffer.assert_called_once()
        mock_conn.find_prompt.assert_called_once()

    @patch("net_automation.ConnectHandler")
    def test_connection_success_encrypted_password(self, mock_connect):
        """Caso 2: Conexión exitosa descifrando password/secret, disabled_algorithms y enable()."""
        mock_conn = MagicMock()
        mock_conn.check_enable_mode.return_value = False
        mock_conn.find_prompt.return_value = "R1#"
        mock_connect.return_value.__enter__.return_value = mock_conn

        result = test_connection(self.device_encrypted)
        self.assertTrue(result)
        # Verificar que ConnectHandler recibió credenciales descifradas y compatibilidad IOSv
        _, kwargs = mock_connect.call_args
        self.assertEqual(kwargs["password"], "CiscoSecretPassword123!")
        self.assertEqual(kwargs["secret"], "CiscoEnableSecret123!")
        self.assertEqual(kwargs["disabled_algorithms"], {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]})
        self.assertEqual(kwargs["fast_cli"], False)
        self.assertGreaterEqual(kwargs["timeout"], 30)
        self.assertGreaterEqual(kwargs["conn_timeout"], 30)
        self.assertGreaterEqual(kwargs["auth_timeout"], 30)
        self.assertGreaterEqual(kwargs["banner_timeout"], 30)
        self.assertGreaterEqual(kwargs["read_timeout_override"], 30)
        self.assertEqual(kwargs["global_delay_factor"], 2.0)
        mock_conn.check_enable_mode.assert_called_once()
        mock_conn.enable.assert_called_once()

    @patch("net_automation.ConnectHandler")
    def test_connection_privilege_15_skips_enable(self, mock_connect):
        """Caso 2b: Con privilege 15 (prompt '#') no se invoca enable() ni se espera password."""
        mock_conn = MagicMock()
        mock_conn.check_enable_mode.return_value = True
        mock_conn.find_prompt.return_value = "R1#"
        mock_connect.return_value.__enter__.return_value = mock_conn

        self.assertTrue(test_connection(self.device_encrypted))
        mock_conn.check_enable_mode.assert_called_once()
        mock_conn.enable.assert_not_called()

    @patch("net_automation.ConnectHandler")
    def test_connection_auth_failure(self, mock_connect):
        """Caso 3: NetmikoAuthenticationException capturada explícitamente retorna False."""
        mock_connect.side_effect = NetmikoAuthenticationException("Auth failed")

        result = test_connection(self.device_plain)
        self.assertFalse(result)

    @patch("net_automation.ConnectHandler")
    def test_connection_timeout(self, mock_connect):
        """Caso 4: NetmikoTimeoutException capturada explícitamente retorna False."""
        mock_connect.side_effect = NetmikoTimeoutException("Connection timed out")

        result = test_connection(self.device_plain)
        self.assertFalse(result)

    @patch("net_automation.ConnectHandler")
    def test_connection_socket_and_os_errors(self, mock_connect):
        """Caso 5: Errores de socket/OS capturados explícitamente retornan False."""
        for error in [OSError("Connection refused"), OSError(10061, "Host unreachable"), EOFError()]:
            mock_connect.side_effect = error
            self.assertFalse(test_connection(self.device_plain))

    # ===============================================
    # 2. Pruebas de fetch_running_config() con Mocks
    # ===============================================

    @patch("net_automation.ConnectHandler")
    def test_fetch_running_config_success_and_sanitization(self, mock_connect):
        """Caso 6: Extracción exitosa con enable(), disabled_algorithms y sanitización de cabeceras."""
        mock_conn = MagicMock()
        mock_conn.check_enable_mode.return_value = False
        raw_ios_output = (
            "Building configuration...\n\n"
            "Current configuration : 1520 bytes\n"
            "!\n"
            "version 15.9\n"
            "hostname R1\n"
            "!\n"
            "interface GigabitEthernet0/0\n"
            " ip address 192.168.76.10 255.255.255.0\n"
            " no shutdown\n"
            "!\n"
            "end\n"
        )
        mock_conn.send_command.return_value = raw_ios_output
        mock_connect.return_value.__enter__.return_value = mock_conn

        config = fetch_running_config(self.device_encrypted)

        # Verificar elevación de privilegios y parámetros de Netmiko
        _, kwargs = mock_connect.call_args
        self.assertEqual(kwargs["secret"], "CiscoEnableSecret123!")
        self.assertEqual(kwargs["disabled_algorithms"], {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]})
        mock_conn.check_enable_mode.assert_called_once()
        mock_conn.enable.assert_called_once()

        # Verificar parámetros defensivos en send_command (timeout calibrado para IOSv real)
        mock_conn.send_command.assert_called_once_with(
            "show running-config",
            read_timeout=30,
            strip_prompt=True,
            strip_command=True,
        )

        # Verificar sanitización estricta de cabeceras volátiles
        self.assertNotIn("Building configuration...", config)
        self.assertNotIn("Current configuration :", config)
        self.assertIn("hostname R1", config)
        self.assertTrue(config.endswith("end"))

    @patch("net_automation.ConnectHandler")
    def test_fetch_running_config_timeout_reraised(self, mock_connect):
        """Caso 7: NetmikoTimeoutException relanzada explícitamente con contexto."""
        mock_connect.side_effect = NetmikoTimeoutException("Read timeout")

        with self.assertRaises(NetmikoTimeoutException):
            fetch_running_config(self.device_plain)

    @patch("net_automation.ConnectHandler")
    def test_fetch_running_config_auth_reraised(self, mock_connect):
        """Caso 8: NetmikoAuthenticationException relanzada explícitamente con contexto."""
        mock_connect.side_effect = NetmikoAuthenticationException("Auth error")

        with self.assertRaises(NetmikoAuthenticationException):
            fetch_running_config(self.device_plain)

    @patch("net_automation.ConnectHandler")
    def test_fetch_running_config_socket_error(self, mock_connect):
        """Caso 9: socket.error/OSError relanzado como ConnectionError estructurado."""
        mock_connect.side_effect = OSError("No route to host")

        with self.assertRaises(ConnectionError):
            fetch_running_config(self.device_plain)

    @patch("net_automation.ConnectHandler")
    def test_fetch_running_config_privilege_15_and_ssh_negotiation_error(self, mock_connect):
        """Caso 9b: Con prompt '#' no se llama enable(); un SSHException de Paramiko se mapea a ConnectionError."""
        mock_conn = MagicMock()
        mock_conn.check_enable_mode.return_value = True
        mock_conn.send_command.return_value = "!\nhostname R1\nend"
        mock_connect.return_value.__enter__.return_value = mock_conn
        fetch_running_config(self.device_encrypted)
        mock_conn.enable.assert_not_called()

        mock_connect.side_effect = SSHException("Incompatible ssh peer (no acceptable kex algorithm)")
        with self.assertRaises(ConnectionError) as ctx:
            fetch_running_config(self.device_plain)
        self.assertIn("no acceptable kex algorithm", str(ctx.exception))
        self.assertNotIn("CiscoSecretPassword123!", str(ctx.exception))

    @patch("net_automation.ConnectHandler")
    def test_fetch_running_config_timeout_keeps_original_detail(self, mock_connect):
        """Caso 7b: El mensaje de timeout conserva el detalle original de Netmiko para diagnóstico."""
        mock_connect.side_effect = NetmikoTimeoutException("TCP connection to device failed.")
        with self.assertRaises(NetmikoTimeoutException) as ctx:
            fetch_running_config(self.device_plain)
        self.assertIn("TCP connection to device failed.", str(ctx.exception))

    # ==========================================
    # 3. Pruebas de calculate_diff()
    # ==========================================

    def test_calculate_diff_changes_detected(self):
        """Caso 10: Detección exacta de líneas añadidas (+) y eliminadas (-) con formato unificado."""
        old_cfg = "!\nhostname R1\n!\ninterface FastEthernet0/0\n ip address 192.168.56.101 255.255.255.0\n!\nend"
        new_cfg = "!\nhostname R1-CORE\n!\ninterface FastEthernet0/0\n description LAN-LINK\n ip address 192.168.56.101 255.255.255.0\n!\nend"

        diff = calculate_diff(old_cfg, new_cfg, fromfile="anterior", tofile="actual")

        self.assertIsInstance(diff, list)
        self.assertTrue(len(diff) > 0)
        self.assertTrue(diff[0].startswith("--- anterior"))
        self.assertTrue(diff[1].startswith("+++ actual"))

        # Validar marcadores de línea
        has_subtraction = any(line.startswith("-hostname R1") for line in diff)
        has_addition_host = any(line.startswith("+hostname R1-CORE") for line in diff)
        has_addition_desc = any(line.startswith("+ description LAN-LINK") for line in diff)

        self.assertTrue(has_subtraction, "Debe contener la línea eliminada -hostname R1")
        self.assertTrue(has_addition_host, "Debe contener la línea añadida +hostname R1-CORE")
        self.assertTrue(has_addition_desc, "Debe contener la línea añadida + description LAN-LINK")

        # Asegurar que ninguna línea contiene saltos residuales
        for line in diff:
            self.assertFalse(line.endswith("\n"))
            self.assertFalse(line.endswith("\r"))

    def test_calculate_diff_identical_configurations(self):
        """Caso 11: Configuraciones idénticas retornan lista vacía []."""
        cfg_a = "!\nhostname R1\ninterface FastEthernet0/0\n no shutdown\nend"
        cfg_b = "!\r\nhostname R1\r\ninterface FastEthernet0/0\r\n no shutdown\r\nend"

        diff = calculate_diff(cfg_a, cfg_b)
        self.assertEqual(diff, [])

    # ==========================================
    # 4. Pruebas de apply_configuration()
    # ==========================================

    @patch("net_automation.ConnectHandler")
    def test_apply_configuration_success(self, mock_connect):
        """Caso 12: apply_configuration() eleva privilegios, filtra comentarios, aplica config y guarda."""
        mock_conn = MagicMock()
        mock_conn.check_enable_mode.return_value = False
        mock_conn.send_config_set.return_value = "R1(config)#hostname R1-NEW\nR1(config)#"
        mock_conn.save_config.return_value = "Building configuration...\n[OK]"
        mock_connect.return_value.__enter__.return_value = mock_conn

        config_payload = (
            "Building configuration...\n"
            "Current configuration : 900 bytes\n"
            "!\n"
            "hostname R1-NEW\n"
            "interface Loopback0\n"
            " ip address 1.1.1.1 255.255.255.255\n"
            "!\n"
            "end\n"
        )
        result = apply_configuration(self.device_encrypted, config_payload)

        expected_commands = [
            "hostname R1-NEW",
            "interface Loopback0",
            " ip address 1.1.1.1 255.255.255.255",
        ]
        _, kwargs = mock_connect.call_args
        self.assertEqual(kwargs["password"], "CiscoSecretPassword123!")
        self.assertEqual(kwargs["secret"], "CiscoEnableSecret123!")
        self.assertEqual(kwargs["disabled_algorithms"], {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]})

        mock_conn.clear_buffer.assert_called_once()
        mock_conn.check_enable_mode.assert_called_once()
        mock_conn.enable.assert_called_once()
        mock_conn.send_config_set.assert_called_once_with(expected_commands, read_timeout=60)
        mock_conn.save_config.assert_called_once()

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["host"], "192.168.76.10")
        self.assertEqual(result["commands_applied"], 3)
        self.assertIn("[OK]", result["output"])

    def test_apply_configuration_empty_raises_value_error(self):
        """Caso 13: apply_configuration() rechaza configuraciones vacías o sin comandos válidos."""
        with self.assertRaises(ValueError):
            apply_configuration(self.device_plain, "!\nBuilding configuration...\nend\n")
        with self.assertRaises(ValueError):
            apply_configuration(self.device_plain, "")

    @patch("net_automation.ConnectHandler")
    def test_apply_configuration_exceptions(self, mock_connect):
        """Caso 14: apply_configuration() maneja explícitamente Timeout, Auth y Socket sin filtrar secretos."""
        mock_connect.side_effect = NetmikoTimeoutException("Timeout applying config")
        with self.assertRaises(NetmikoTimeoutException):
            apply_configuration(self.device_plain, "hostname R1")

        mock_connect.side_effect = NetmikoAuthenticationException("Enable secret invalid")
        with self.assertRaises(NetmikoAuthenticationException) as auth_ctx:
            apply_configuration(self.device_plain, "hostname R1")
        self.assertNotIn("CiscoEnableSecret123!", str(auth_ctx.exception))
        self.assertNotIn("CiscoSecretPassword123!", str(auth_ctx.exception))

        mock_connect.side_effect = OSError("Socket closed")
        with self.assertRaises(ConnectionError):
            apply_configuration(self.device_plain, "hostname R1")


if __name__ == "__main__":
    unittest.main()
