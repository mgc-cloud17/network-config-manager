"""
test_cloud_storage.py - Pruebas unitarias (unittest.mock) de cloud_storage.py.
Valida el respaldo híbrido en S3: payload cifrado con Fernet (sin texto plano),
ServerSideEncryption="AES256", captura de VersionId, ENABLE_S3_BACKUP=false,
autenticación solo por cadena IAM de boto3 y tolerancia a fallos (ClientError, etc.).
No contacta a AWS en ningún momento.
"""

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError

from cloud_storage import is_s3_backup_enabled, upload_encrypted_backup
from crypto import decrypt_password

S3_ENV = {"ENABLE_S3_BACKUP": "true", "AWS_S3_BUCKET": "bucket-test", "AWS_REGION": "us-east-1"}
CONFIG = "!\nhostname R1\nenable secret 5 $1$abcd$SecretoDeLaConfig\nend"
DISABLED_RESULT = {"synced": False, "s3_key": None, "version_id": None, "error": None}


def _mock_client(response=None, error=None):
    """Cliente S3 simulado: put_object devuelve `response` (VersionId por defecto) o lanza `error`."""
    client = MagicMock()
    client.put_object.return_value = {"VersionId": "ver-123"} if response is None else response
    if error is not None:
        client.put_object.side_effect = error
    return client


def _upload(env, hostname="R1", backup_id=1, client=None, remove=()):
    """Ejecuta upload_encrypted_backup con boto3 simulado y variables de entorno controladas."""
    client = client or _mock_client()
    with (
        patch("cloud_storage.boto3.client", return_value=client) as mock_boto,
        patch.dict(os.environ, env),
    ):
        for name in remove:
            os.environ.pop(name, None)
        result = upload_encrypted_backup(hostname, backup_id, CONFIG)
    return result, mock_boto, client


class TestCloudStorage(unittest.TestCase):

    # ---------- Deshabilitado / configuración incompleta ----------

    def test_disabled_flag_does_not_contact_aws(self):
        """ENABLE_S3_BACKUP=false (o ausente) no instancia boto3 ni falla."""
        result, mock_boto, _ = _upload({"ENABLE_S3_BACKUP": "false"})
        self.assertEqual(result, DISABLED_RESULT)
        mock_boto.assert_not_called()

        result, mock_boto, _ = _upload({}, remove=("ENABLE_S3_BACKUP",))
        self.assertEqual(result, DISABLED_RESULT)
        mock_boto.assert_not_called()

    def test_enable_flag_is_case_insensitive_and_strict(self):
        with patch.dict(os.environ, {"ENABLE_S3_BACKUP": "TRUE"}):
            self.assertTrue(is_s3_backup_enabled())
        for value in ("false", "yes", "1", ""):
            with patch.dict(os.environ, {"ENABLE_S3_BACKUP": value}):
                self.assertFalse(is_s3_backup_enabled())

    def test_missing_bucket_returns_controlled_error(self):
        result, mock_boto, _ = _upload({**S3_ENV, "AWS_S3_BUCKET": ""})
        self.assertFalse(result["synced"])
        self.assertIn("AWS_S3_BUCKET", result["error"])
        mock_boto.assert_not_called()

    # ---------- Subida exitosa ----------

    def test_successful_upload_is_encrypted_with_sse_and_captures_version_id(self):
        result, mock_boto, client = _upload(S3_ENV, backup_id=7)

        # Autenticación exclusivamente vía cadena IAM: solo servicio y región, sin credenciales
        mock_boto.assert_called_once_with("s3", region_name="us-east-1")

        self.assertTrue(result["synced"])
        self.assertIsNone(result["error"])
        self.assertEqual(result["version_id"], "ver-123")
        self.assertRegex(result["s3_key"], r"^backups/R1/\d{8}T\d{6}Z_backup_7\.enc$")

        kwargs = client.put_object.call_args.kwargs
        self.assertEqual(kwargs["Bucket"], "bucket-test")
        self.assertEqual(kwargs["Key"], result["s3_key"])
        self.assertEqual(kwargs["ServerSideEncryption"], "AES256")

        # El cuerpo es un token Fernet: sin texto plano y descifrable con la clave local
        body = kwargs["Body"]
        self.assertIsInstance(body, bytes)
        self.assertTrue(body.startswith(b"gAAAAA"))
        self.assertNotIn(b"hostname R1", body)
        self.assertNotIn(b"SecretoDeLaConfig", body)
        self.assertEqual(decrypt_password(body.decode("utf-8")), CONFIG)

    def test_upload_without_version_id_returns_none(self):
        result, _, _ = _upload(S3_ENV, backup_id=2, client=_mock_client(response={"ETag": "abc"}))
        self.assertTrue(result["synced"])
        self.assertIsNone(result["version_id"])

    def test_hostname_is_sanitized_for_s3_key(self):
        key = _upload(S3_ENV, hostname="R1 core/../x:y", backup_id=3)[0]["s3_key"]
        fallback = _upload(S3_ENV, hostname="", backup_id=4)[0]["s3_key"]
        self.assertRegex(key, r"^backups/[A-Za-z0-9._-]+/\d{8}T\d{6}Z_backup_3\.enc$")
        self.assertRegex(fallback, r"^backups/unknown-host/\d{8}T\d{6}Z_backup_4\.enc$")

    # ---------- Tolerancia a fallos (degradación elegante) ----------

    def test_client_error_is_captured_without_raising(self):
        error = ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "PutObject")
        result, _, _ = _upload(S3_ENV, backup_id=5, client=_mock_client(error=error))
        self.assertFalse(result["synced"])
        self.assertIsNone(result["s3_key"])
        self.assertIsNone(result["version_id"])
        self.assertIn("AccessDenied", result["error"])
        self.assertNotIn("SecretoDeLaConfig", result["error"])

    def test_no_credentials_error_is_captured(self):
        result, _, _ = _upload(S3_ENV, backup_id=5, client=_mock_client(error=NoCredentialsError()))
        self.assertFalse(result["synced"])
        self.assertIn("credenciales", result["error"])

    def test_network_and_unexpected_errors_are_captured(self):
        errors = (EndpointConnectionError(endpoint_url="https://s3.amazonaws.com"), RuntimeError("boom"))
        for error in errors:
            result, _, _ = _upload(S3_ENV, backup_id=6, client=_mock_client(error=error))
            self.assertFalse(result["synced"])
            self.assertTrue(result["error"])

    def test_encryption_failure_never_uploads(self):
        with patch("cloud_storage.encrypt_password", side_effect=RuntimeError("sin clave")):
            result, mock_boto, _ = _upload(S3_ENV, backup_id=8)
        self.assertFalse(result["synced"])
        self.assertIn("cifrar", result["error"])
        mock_boto.assert_not_called()

    # ---------- Cero credenciales estáticas ----------

    def test_source_has_no_static_aws_credentials(self):
        root = Path(__file__).resolve().parent
        for name in ("cloud_storage.py", "app.py", "database.py"):
            source = (root / name).read_text(encoding="utf-8").lower()
            self.assertNotIn("aws_access_key_id", source, name)
            self.assertNotIn("aws_secret_access_key", source, name)


if __name__ == "__main__":
    unittest.main()
