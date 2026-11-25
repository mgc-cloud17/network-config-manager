# Quickstart Validation Guide: Evolución SRE/Cloud

**Feature**: `001-sre-cloud-evolution` | **Date**: 2026-11-25

## 1. Prerrequisitos
- Python 3.12+ y entorno virtual activo (`.\venv\Scripts\Activate.ps1` en Windows o `source venv/bin/activate` en Linux/macOS).
- Archivo `.env` configurado con `ENCRYPTION_KEY` válida (ver [`.env.example`](../../.env.example)).
- Docker Desktop / Docker Engine (para validar el contenedor endurecido).

## 2. Validación de Linting y Suite de Pruebas
```powershell
# Instalar dependencias actualizadas (incluye boto3, gunicorn y ruff)
pip install -r requirements.txt

# 1. Análisis estático con Ruff
ruff check .

# 2. Suite completa de pruebas unitarias e integración (incluye mocks de S3 y degradación elegante)
python -m unittest discover -s . -p "test_*.py" -v
```
**Resultado esperado:** `ruff check .` finaliza con código `0` y todas las pruebas `unittest` reportan `OK`.

## 3. Validación de Respaldo Híbrido S3 y Degradación Elegante
1. **Modo local por defecto (`ENABLE_S3_BACKUP=false`):**
   - Ejecutar un respaldo desde `http://127.0.0.1:5000` o `POST /api/devices/<id>/backup`.
   - Verificar respuesta `HTTP 201` con `data.cloud_sync.synced == false` y `data.cloud_sync.error == null`.
2. **Modo S3 con credenciales IAM activas (`ENABLE_S3_BACKUP=true`, `AWS_S3_BUCKET=<bucket>`, `AWS_REGION=us-east-1`):**
   - Ejecutar `POST /api/devices/<id>/backup`.
   - Verificar `HTTP 201`, `data.cloud_sync.synced == true`, `s3_key` con formato `backups/{hostname}/{timestamp}_backup_{id}.enc` y `version_id` registrado en SQLite.
3. **Degradación elegante ante fallo AWS:**
   - Con `ENABLE_S3_BACKUP=true` y un bucket inexistente o sin permisos IAM, ejecutar `POST /api/devices/<id>/backup`.
   - Verificar que el respaldo se guarda en SQLite y la API retorna `HTTP 201` (sin error `500`) con `data.cloud_sync.synced == false` y `data.cloud_sync.error` descriptivo.

## 4. Validación de Contenedor Endurecido
```powershell
# Construir imagen desde net-conf-manag/
docker build -t network-config-manager:local .

# Verificar usuario no-root dentro de la imagen
docker run --rm network-config-manager:local id
# Esperado: uid=1000(appuser) gid=1000(appuser)

# Ejecutar contenedor inyectando ENCRYPTION_KEY en runtime
docker run --rm -p 5000:5000 -e ENCRYPTION_KEY="<tu-clave-fernet>" network-config-manager:local
```
**Resultado esperado:** Gunicorn arranca en `0.0.0.0:5000`, `http://127.0.0.1:5000` responde `HTTP 200`, y `.env` / `*.db` / `venv/` no existen dentro de la imagen.
