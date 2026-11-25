# Contracts: Cloud Storage, API REST, Contenedor y Pipeline CI

**Feature**: `001-sre-cloud-evolution` | **Date**: 2026-11-25

---

## 1. Contrato de `cloud_storage.py`

### Variables de Entorno (`.env.example`)
```ini
ENCRYPTION_KEY=genera_tu_propia_llave_fernet_aqui

# Respaldo híbrido opcional en AWS S3 (autenticación exclusiva vía IAM Role / cadena AWS)
ENABLE_S3_BACKUP=false
AWS_S3_BUCKET=tu-bucket-privado-network-backups
AWS_REGION=us-east-1
```

### Firma y Contrato de `upload_encrypted_backup`

```python
def upload_encrypted_backup(
    hostname: str,
    backup_id: int,
    config_text: str,
) -> dict:
    """
    Sube una copia doblemente cifrada del respaldo a AWS S3 cuando ENABLE_S3_BACKUP=true.

    Comportamiento:
    1. Si ENABLE_S3_BACKUP != 'true' (case-insensitive): no contacta a AWS y retorna
       {"synced": False, "s3_key": None, "version_id": None, "error": None}.
    2. Si falta AWS_S3_BUCKET: retorna
       {"synced": False, "s3_key": None, "version_id": None, "error": "AWS_S3_BUCKET no configurado."}.
    3. Cifra config_text en memoria con crypto.encrypt_password(config_text).
    4. Construye s3_key = f"backups/{safe_hostname}/{utc_timestamp}_backup_{backup_id}.enc".
    5. Instancia boto3.client("s3", region_name=aws_region) SIN aws_access_key_id ni aws_secret_access_key.
    6. Ejecuta client.put_object(
           Bucket=bucket,
           Key=s3_key,
           Body=encrypted_token.encode("utf-8"),
           ServerSideEncryption="AES256",
           ContentType="text/plain; charset=utf-8",
       ).
    7. En éxito retorna {"synced": True, "s3_key": s3_key, "version_id": response.get("VersionId"), "error": None}.
    8. Ante ClientError, BotoCoreError o cualquier error de entorno/cifrado:
       retorna {"synced": False, "s3_key": None, "version_id": None, "error": "<mensaje seguro>"}.
    """
```

---

## 2. Contrato Actualizado de API REST (`app.py`)

### `POST /api/devices/<int:device_id>/backup`

* **Flujo Interno:**
  1. `raw_config = fetch_running_config(device)`
  2. `backup = add_backup(device_id, raw_config, db_path=db_path)`
  3. `cloud_sync = upload_encrypted_backup(device["hostname"], backup["id"], raw_config)`
  4. Si `cloud_sync["synced"]` es `True`, invocar `update_backup_s3_metadata(backup["id"], cloud_sync["s3_key"], cloud_sync["version_id"], db_path=db_path)`.
* **Respuesta `201 Created` (compatibilidad hacia atrás garantizada):**
  ```json
  {
    "status": "success",
    "message": "Backup capturado exitosamente desde R1-IOSv.",
    "data": {
      "id": 12,
      "backup_id": 12,
      "device_id": 1,
      "hostname": "R1-IOSv",
      "timestamp": "2026-11-25 20:10:00",
      "size_bytes": 1842,
      "config_text": "...",
      "raw_config": "...",
      "s3_key": "backups/R1-IOSv/20261125T201000Z_backup_12.enc",
      "s3_version_id": "3/L4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY+MTRCxf3vjVBH40Nr8X8gdRQBpUMLUo",
      "cloud_sync": {
        "synced": true,
        "s3_key": "backups/R1-IOSv/20261125T201000Z_backup_12.enc",
        "version_id": "3/L4kqtJlcpXroDTDmJ+rmSpXd3dIbrHY+MTRCxf3vjVBH40Nr8X8gdRQBpUMLUo",
        "error": null
      }
    }
  }
  ```
* **Respuesta `201 Created` en Degradación Elegante (cuando S3 falla o está deshabilitado):**
  - El código HTTP sigue siendo `201 Created`, el respaldo local está persistido y `cloud_sync` indica `"synced": false` con `"error": "..."` (si falló) o `"error": null` (si está deshabilitado).

---

## 3. Contrato de Contenerización (`Dockerfile` y `.dockerignore`)

* **Base:** `FROM python:3.12-slim`
* **Variables Python:** `ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1`
* **Usuario sin privilegios:** `RUN useradd --create-home appuser` y `chown -R appuser:appuser /app` (incluyendo directorio escribible `/app/data` para SQLite)
* **Usuario activo:** `USER appuser`
* **Puerto:** `EXPOSE 5000`
* **Aranque WSGI:** `CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5000", "app:app"]`
* **Exclusiones obligatorias (`.dockerignore`):** `.env`, `*.db`, `*.sqlite*`, `venv/`, `__pycache__/`, `*.py[cod]`, `.git/`, `.specify/`, `specs/`.

---

## 4. Contrato de CI (`.github/workflows/ci.yml`)

* **Triggers:** `on: push` y `pull_request` hacia `branches: [main]`.
* **Jobs Encadenados:**
  1. `lint`: `runs-on: ubuntu-latest` -> `pip install ruff` -> `ruff check .`
  2. `test`: `needs: lint`, `runs-on: ubuntu-latest` -> `pip install -r requirements.txt` -> genera `ENCRYPTION_KEY` efímera con Fernet -> `python -m unittest discover -s . -p "test_*.py" -v`
  3. `docker-build`: `needs: test`, `runs-on: ubuntu-latest` -> `docker build -t network-config-manager:ci .`
