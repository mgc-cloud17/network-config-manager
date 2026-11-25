"""
cloud_storage.py - Respaldo híbrido opcional en AWS S3 para Network Config Manager.

Sube una copia doblemente cifrada del running-config:
  1. Cifrado en memoria con Fernet (crypto.encrypt_password) ANTES de salir del proceso.
  2. Cifrado en servidor de S3 (ServerSideEncryption="AES256").

Autenticación: exclusivamente la cadena estándar de credenciales de AWS (IAM Role de
instancia/tarea, perfil o variables de entorno estándar). Este módulo nunca recibe,
lee ni almacena credenciales de AWS.

Degradación elegante: ninguna falla de S3 se propaga; siempre se devuelve un diccionario
y el respaldo local en SQLite queda intacto. Gobernado por RULES.md y ARCHITECTURE.md.
"""

import os
import re
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from crypto import encrypt_password

S3_KEY_PREFIX = "backups"


def _result(
    synced: bool = False,
    s3_key: str | None = None,
    version_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Construye el contrato de retorno estandarizado de la sincronización cloud."""
    return {"synced": synced, "s3_key": s3_key, "version_id": version_id, "error": error}


def is_s3_backup_enabled() -> bool:
    """True solo si ENABLE_S3_BACKUP vale 'true' (sin distinguir mayúsculas). Por defecto: False."""
    return os.environ.get("ENABLE_S3_BACKUP", "false").strip().lower() == "true"


def _sanitize_hostname(hostname: str) -> str:
    """Normaliza el hostname para usarlo como segmento seguro de una clave S3."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(hostname or "").strip()).strip("._")
    return safe or "unknown-host"


def build_s3_key(hostname: str, backup_id: int) -> str:
    """Genera backups/{hostname}/{timestamp}_backup_{backup_id}.enc con timestamp UTC compacto."""
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{S3_KEY_PREFIX}/{_sanitize_hostname(hostname)}/{timestamp}_backup_{backup_id}.enc"


def upload_encrypted_backup(hostname: str, backup_id: int, config_text: str) -> dict:
    """
    Sube una copia cifrada del respaldo a S3 si ENABLE_S3_BACKUP=true.

    Returns:
        {"synced": bool, "s3_key": str | None, "version_id": str | None, "error": str | None}

        - S3 deshabilitado: synced=False y error=None (no se contacta a AWS).
        - Éxito: synced=True con s3_key y version_id (None si el bucket no tiene versionado).
        - Cualquier falla controlada: synced=False y error con un mensaje seguro
          (nunca incluye el contenido de la configuración).
    """
    if not is_s3_backup_enabled():
        return _result()

    bucket = (os.environ.get("AWS_S3_BUCKET") or "").strip()
    if not bucket:
        return _result(error="AWS_S3_BUCKET no está configurado.")
    region = (os.environ.get("AWS_REGION") or "").strip() or None

    try:
        encrypted_token = encrypt_password(config_text)
    except (ValueError, RuntimeError) as exc:
        return _result(error=f"No se pudo cifrar el respaldo antes de subirlo a S3: {exc}")

    s3_key = build_s3_key(hostname, backup_id)

    try:
        client = boto3.client("s3", region_name=region)
        response = client.put_object(
            Bucket=bucket,
            Key=s3_key,
            Body=encrypted_token.encode("utf-8"),
            ServerSideEncryption="AES256",
            ContentType="text/plain; charset=utf-8",
        )
    except NoCredentialsError:
        return _result(
            error="No se encontraron credenciales de AWS (se requiere un IAM Role o la cadena estándar de AWS)."
        )
    except ClientError as exc:
        details = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
        code = details.get("Code", "ClientError")
        message = details.get("Message", "sin detalle")
        return _result(error=f"AWS S3 rechazó la subida ({code}): {message}")
    except BotoCoreError as exc:
        return _result(error=f"Error de conexión o configuración del SDK de AWS: {exc}")
    except Exception as exc:  # noqa: BLE001 - degradación elegante: S3 nunca debe romper el respaldo local
        return _result(error=f"Error inesperado al subir a S3 ({type(exc).__name__}).")

    version_id = response.get("VersionId") if isinstance(response, dict) else None
    return _result(synced=True, s3_key=s3_key, version_id=version_id)
