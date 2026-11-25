# Implementation Plan: Evolución SRE/Cloud (Respaldo Híbrido S3, Contenedor Endurecido y CI)

**Branch**: `001-sre-cloud-evolution` | **Date**: 2026-11-25 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-sre-cloud-evolution/spec.md`

## Summary

Extender **Network Config Manager** hacia una arquitectura SRE/Cloud lista para producción manteniendo compatibilidad total con [`RULES.md`](../../RULES.md), [`ARCHITECTURE.md`](../../ARCHITECTURE.md), el frontend *zero-build* (`templates/index.html`) y las 31 pruebas existentes:
1. **Respaldo Híbrido Versionado en AWS S3 (`cloud_storage.py`):** Cifrado doble (Fernet en memoria + `ServerSideEncryption="AES256"` en S3), autenticación exclusiva vía IAM Roles (`boto3.client("s3", region_name=...)`), trazabilidad en SQLite (`s3_key`, `s3_version_id`) y degradación elegante en `POST /api/devices/<device_id>/backup` devolviendo `cloud_sync` en HTTP `201`.
2. **Contenerización Endurecida:** `requirements.txt` con versiones fijadas (incluyendo `paramiko==3.5.1`), `Dockerfile` sobre `python:3.12-slim` con usuario no-root `appuser`, directorio de datos escribible para SQLite, Gunicorn (`-w 2 -b 0.0.0.0:5000 app:app`) y `.dockerignore`.
3. **Pipeline CI en GitHub Actions (`.github/workflows/ci.yml`):** Validación encadenada `lint` (`ruff check .`) $\rightarrow$ `test` (`unittest` con `ENCRYPTION_KEY` efímera) $\rightarrow$ `docker-build` (`docker build`).

## Technical Context

**Language/Version**: Python 3.12 (`python:3.12-slim` en contenedor)  
**Primary Dependencies**: Flask 3.1.3, Netmiko 4.8.0, Paramiko 3.5.1, cryptography 50.0.2, python-dotenv 1.2.4, boto3, gunicorn, ruff (linting/CI)  
**Storage**: SQLite (`sqlite3`, consultas parametrizadas `?`) + AWS S3 privado (opcional vía `ENABLE_S3_BACKUP=true`)  
**Testing**: `unittest` + `unittest.mock` (`python -m unittest discover -s . -p "test_*.py" -v`)  
**Target Platform**: Linux container (`python:3.12-slim`) / Host Windows & Linux local  
**Project Type**: Web service + Network automation backend (Flask + HTML5/Tailwind CDN/Fetch)  
**Performance Goals**: Respuesta inmediata de respaldo local sin bloqueo fatal por latencia o fallo de S3; 2 workers Gunicorn  
**Constraints**: Cero credenciales AWS en código; cero contraseñas en claro; frontend sin Node.js/npm/frameworks; compatibilidad SSHv2 con Cisco IOSv 15.9(3)M6 (`paramiko==3.5.1`)  
**Scale/Scope**: Múltiples dispositivos Cisco IOS/IOSv, historial versionado local y en S3, CI automatizado en cada `push`/`pull_request` a `main`

## Constitution Check

*GATE: Verificado antes de diseño y re-verificado tras Fase 1 (`RULES.md` y `ARCHITECTURE.md` actúan como gobernanza del proyecto).*

- **Frontend Zero-Build (`RULES.md` §1):** Se mantiene HTML5 semántico + Tailwind CSS CDN + JS nativo (`fetch`). Sin Node.js, npm ni bundlers. **PASS**
- **Seguridad y Cero Texto Plano (`RULES.md` §2):** `ENCRYPTION_KEY` exclusivamente en `.env` / variables de entorno de runtime; doble cifrado Fernet + SSE-AES256 para S3; cero `aws_access_key_id` / `aws_secret_access_key` en código. **PASS**
- **Automatización Netmiko (`RULES.md` §3):** Sin cambios regresivos en `net_automation.py`; `paramiko==3.5.1` fijado en `requirements.txt`. **PASS**
- **Persistencia SQLite Parametrizada (`RULES.md` §5):** Migración de `backups` y nueva función `update_backup_s3_metadata()` usando exclusivamente marcadores posicionales `?`. **PASS**

## Project Structure

### Documentation (this feature)

```text
specs/001-sre-cloud-evolution/
├── plan.md                                  # Plan técnico de implementación
├── research.md                              # Decisiones técnicas y alternativas
├── data-model.md                            # Migración SQLite y modelo Cloud Sync
├── quickstart.md                            # Guía de validación end-to-end
├── contracts/
│   └── api-and-cloud-contracts.md           # Contratos REST, cloud_storage.py, Docker y CI
└── checklists/
    └── requirements.md                      # Checklist de calidad de spec.md
```

### Source Code (repository root)

```text
net-conf-manag/
├── app.py                     # Actualización de POST /api/devices/<id>/backup con cloud_sync
├── cloud_storage.py           # Nuevo módulo AWS S3 (upload_encrypted_backup)
├── crypto.py                  # Cifrado Fernet reutilizado para doble cifrado S3
├── database.py                # Migración backups (s3_key, s3_version_id) + update_backup_s3_metadata
├── net_automation.py          # Sin cambios funcionales
├── templates/
│   └── index.html             # Indicador visual opcional de cloud_sync tras backup
├── test_api.py                # Pruebas ampliadas para cloud_sync en /backup
├── test_cloud_storage.py      # Nuevo archivo de pruebas unitarias (mock boto3, ClientError, Fernet)
├── test_db_crypto.py          # Pruebas de migración s3_key/s3_version_id y update_backup_s3_metadata
├── test_net_automation.py     # Sin regresiones
├── requirements.txt           # Dependencias fijadas (Flask, Netmiko, Paramiko 3.5.1, boto3, gunicorn, ruff)
├── Dockerfile                 # Imagen endurecida python:3.12-slim + appuser + Gunicorn :5000
├── .dockerignore              # Exclusión de .env, *.db, venv/, __pycache__/, .git/
├── .env.example               # Variables ENCRYPTION_KEY, ENABLE_S3_BACKUP, AWS_S3_BUCKET, AWS_REGION
└── .github/
    └── workflows/
        └── ci.yml             # Pipeline CI: lint (ruff) -> test (unittest) -> docker-build
```

**Structure Decision**: Se mantiene la arquitectura modular plana en la raíz de `net-conf-manag/` para preservar los imports directos existentes (`from database import ...`, `from crypto import ...`) sin introducir refactors disruptivos.

## Complexity Tracking

No existen violaciones a las reglas de gobernanza (`RULES.md` / `ARCHITECTURE.md`).
