# Feature Specification: Evolución SRE/Cloud (Respaldo Híbrido S3, Contenedor Endurecido y CI)

**Feature Branch**: `001-sre-cloud-evolution`

**Created**: 2026-11-25

**Status**: Draft

**Input**: User description: "Definir la especificación funcional completa (spec.md) para la evolución SRE/Cloud de Network Config Manager: (1) Respaldo Híbrido Versionado en AWS S3 con Doble Cifrado e IAM, (2) Contenerización Endurecida (Docker python:3.12-slim, appuser no-root, Gunicorn :5000, .dockerignore) y (3) Pipeline CI en GitHub Actions (ruff -> unittest -> docker build)."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Respaldo Híbrido Versionado en AWS S3 con Doble Cifrado y Degradación Elegante (Priority: P1)

Como ingeniero de redes / SRE, al ejecutar un respaldo de configuración de un dispositivo (`POST /api/devices/<device_id>/backup`), necesito que el sistema persista primero la configuración en SQLite local y, si la sincronización en la nube está habilitada (`ENABLE_S3_BACKUP=true`), suba automáticamente una copia doblemente cifrada a un bucket privado de AWS S3 usando exclusivamente credenciales IAM del entorno, registrando la trazabilidad (`s3_key`, `s3_version_id`) en SQLite sin afectar la disponibilidad del respaldo local si S3 falla.

**Why this priority**: Protege los respaldos de configuración frente a fallos de disco locales (durabilidad off-site y versionado inmutable) manteniendo la seguridad criptográfica de extremo a extremo y la resiliencia operativa del servicio.

**Independent Test**: Se puede probar de forma independiente invocando `POST /api/devices/<device_id>/backup` con `ENABLE_S3_BACKUP=false` (solo local), con `ENABLE_S3_BACKUP=true` contra un cliente S3 simulado/mocked (verificando carga cifrada con Fernet + `AES256` y registro de `s3_key` y `VersionId` en SQLite), y simulando un `ClientError` de AWS para confirmar la degradación elegante con respuesta `201 Created` y conservación íntegra del respaldo local.

**Acceptance Scenarios**:

1. **Given** un dispositivo registrado y `ENABLE_S3_BACKUP=false` (valor por defecto), **When** se ejecuta `POST /api/devices/<device_id>/backup`, **Then** el sistema extrae el `running-config` vía `net_automation.py`, lo guarda en SQLite y retorna `201 Created` indicando estado de sincronización cloud deshabilitado/omitido sin invocar a `boto3`.
2. **Given** `ENABLE_S3_BACKUP=true` y un bucket S3 configurado (`S3_BACKUP_BUCKET`), **When** se ejecuta `POST /api/devices/<device_id>/backup`, **Then** el sistema guarda primero el respaldo en SQLite, cifra el `raw_config` en memoria con `crypto.encrypt_password()` (Fernet AES-128), y lo sube mediante `boto3.client("s3")` (sin `aws_access_key_id` ni `aws_secret_access_key` explícitos) con `ServerSideEncryption="AES256"` a la ruta `backups/{hostname}/{timestamp}_backup_{backup_id}.enc`, almacenando `s3_key` y el `VersionId` devuelto en el registro de `backups` en SQLite y devolviendo el estado cloud en la respuesta JSON.
3. **Given** `ENABLE_S3_BACKUP=true` pero AWS S3 responde con `botocore.exceptions.ClientError` (permisos IAM insuficientes, bucket inexistente o fallo de red), **When** se ejecuta `POST /api/devices/<device_id>/backup`, **Then** el respaldo local en SQLite permanece intacto, la API **no** retorna error `500`, retorna `201 Created` con el respaldo local y reporta explícitamente el fallo parcial de sincronización cloud (`s3_sync.status = "failed"` y detalle seguro sin exponer secretos).

---

### User Story 2 - Contenerización Endurecida para Despliegue Portátil (Priority: P2)

Como ingeniero SRE / DevOps, necesito empaquetar Network Config Manager en una imagen Docker ligera y endurecida para ejecutar la aplicación de forma reproducible y segura en cualquier entorno sin privilegios de `root` y sin filtrar secretos ni artefactos locales dentro de la imagen.

**Why this priority**: Elimina inconsistencias de entorno ("funciona en mi máquina", incluida la fijación de `paramiko==3.5.1` para Cisco IOSv), reduce la superficie de ataque mediante ejecución `non-root` y habilita un servidor WSGI de producción (Gunicorn) en el puerto `5000`.

**Independent Test**: Se puede probar construyendo la imagen (`docker build`) desde la raíz del proyecto, verificando que `.env`, `*.db` y `venv/` no formen parte del contexto/imagen, y arrancando el contenedor para confirmar que el proceso Gunicorn responde en el puerto `5000` bajo el usuario `appuser` (UID no-root).

**Acceptance Scenarios**:

1. **Given** el código fuente en el repositorio, **When** se construye la imagen con `Dockerfile`, **Then** se utiliza `python:3.12-slim` como imagen base, se instalan las dependencias fijadas (incluyendo `Flask`, `netmiko`, `paramiko==3.5.1`, `cryptography`, `python-dotenv`, `boto3` y `gunicorn`) sin residuos de caché apt/pip, y la aplicación corre bajo el usuario sin privilegios `appuser`.
2. **Given** el contenedor en ejecución, **When** inicia el punto de entrada, **Then** Gunicorn sirve `app:app` en `0.0.0.0:5000` y las peticiones `GET /` y `GET /api/devices` responden correctamente.
3. **Given** la presencia local de `.env`, `network_manager.db` y `venv/`, **When** se construye la imagen Docker, **Then** `.dockerignore` excluye `.env`, `*.db`, `venv/`, `__pycache__/` y `.git/` para evitar fuga de secretos o contaminación de estado en la imagen.

---

### User Story 3 - Pipeline de Integración Continua (CI) en GitHub Actions (Priority: P3)

Como mantenedor del proyecto, necesito un flujo de CI automatizado en GitHub Actions que valide automáticamente la calidad estática del código, la suite completa de pruebas unitarias/integración y la construcción de la imagen Docker en cada cambio hacia `main`.

**Why this priority**: Garantiza que ninguna regresión de código, fallo de estilo/linting o rotura del empaquetado Docker llegue a la rama principal `main`.

**Independent Test**: Se puede probar verificando la definición del workflow en `.github/workflows/ci.yml` y comprobando la ejecución secuencial exitosa de las tres etapas (`ruff`, `unittest` con `ENCRYPTION_KEY` efímera generada en el runner, y `docker build`) ante un `push` o `pull_request` a `main`.

**Acceptance Scenarios**:

1. **Given** un `push` o `pull_request` dirigido a la rama `main`, **When** se dispara el workflow de GitHub Actions, **Then** se ejecutan en orden encadenado las etapas: (1) análisis estático con `ruff`, (2) ejecución completa de `python -m unittest discover -s . -p "test_*.py" -v` inyectando una `ENCRYPTION_KEY` efímera válida en el entorno del job, y (3) construcción verificada de la imagen Docker (`docker build`).
2. **Given** un error de linting reportado por `ruff` o el fallo de al menos una prueba en `unittest`, **When** corre el pipeline, **Then** el workflow falla inmediatamente en esa etapa y bloquea la construcción de la imagen Docker posterior.

---

### Edge Cases

- ¿Qué ocurre si `ENABLE_S3_BACKUP=true` pero la variable `S3_BACKUP_BUCKET` está vacía o no definida? El sistema debe degradar de forma elegante: conservar el respaldo local en SQLite, no lanzar error `500` y reportar el estado de sincronización cloud como omitido/fallido por configuración incompleta.
- ¿Qué ocurre si el bucket S3 no tiene habilitado el versionado (S3 Bucket Versioning) y AWS retorna `ResponseMetadata` sin `VersionId`? El sistema debe almacenar `s3_key` en SQLite con `s3_version_id = NULL` y reportar la subida exitosa sin fallar por ausencia de `VersionId`.
- ¿Qué ocurre si el `hostname` del dispositivo contiene espacios o caracteres especiales al construir la ruta `backups/{hostname}/{timestamp}_backup_{backup_id}.enc`? El sistema debe sanitizar el segmento `{hostname}` y formatear `{timestamp}` de forma segura para claves S3 (sin espacios ni caracteres conflictivos como `:`).
- ¿Qué ocurre con bases de datos SQLite ya existentes (`network_manager.db`) que no tienen las columnas `s3_key` ni `s3_version_id` en `backups`? `database.init_db()` debe migrar de forma idempotente mediante `PRAGMA table_info(backups)` y `ALTER TABLE` sin pérdida de respaldos previos.
- ¿Qué ocurre en el flujo de CI si `ENCRYPTION_KEY` no está presente como archivo `.env` (porque `.env` está en `.gitignore`)? El paso de pruebas en GitHub Actions debe generar una clave Fernet efímera válida en memoria/entorno del job sin almacenar claves reales en el repositorio.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001 (Respaldo Local Primero):** Al recibir `POST /api/devices/<device_id>/backup`, el sistema DEBE extraer el `running-config` vía `net_automation.fetch_running_config()` y persistirlo en SQLite local (`backups`) antes de intentar cualquier operación hacia AWS S3.
- **FR-002 (Activación Explícita de S3):** La sincronización hacia AWS S3 DEBE estar controlada por la variable de entorno `ENABLE_S3_BACKUP`, cuyo valor por defecto será `"false"`. Solo cuando su valor sea verdadero (`"true"`, insensible a mayúsculas/minúsculas) se ejecutará la subida a S3.
- **FR-003 (Autenticación Exclusiva vía IAM):** El cliente de `boto3` para S3 DEBE instanciarse sin parámetros explícitos `aws_access_key_id` ni `aws_secret_access_key`, delegando la autenticación exclusivamente a la cadena estándar de credenciales de AWS (IAM Role de instancia/tarea o entorno estándar de AWS).
- **FR-004 (Doble Cifrado en S3):** Antes de invocar `put_object` en S3, el contenido `raw_config` DEBE cifrarse en memoria con Fernet (`crypto.encrypt_password()`). La llamada a S3 DEBE especificar `ServerSideEncryption="AES256"`.
- **FR-005 (Nomenclatura y Metadatos S3):** El objeto cifrado DEBE almacenarse bajo la clave `backups/{hostname}/{timestamp}_backup_{backup_id}.enc` en el bucket indicado por `S3_BACKUP_BUCKET`. El sistema DEBE registrar `s3_key` y `s3_version_id` (si está presente en la respuesta de S3) en la tabla `backups` mediante consultas SQL parametrizadas (`?`).
- **FR-006 (Degradación Elegante ante Fallos S3):** Si `ENABLE_S3_BACKUP=false`, falta `S3_BACKUP_BUCKET`, o S3 lanza `botocore.exceptions.ClientError` / error de red (`BotoCoreError`), el respaldo local DEBE permanecer intacto, la API NO DEBE devolver `HTTP 500`, y la respuesta `201 Created` DEBE incluir un objeto de estado cloud (`s3_sync`) indicando `enabled`, `status` (`"disabled"`, `"uploaded"`, `"failed"`), `s3_key`, `version_id` y mensaje descriptivo seguro.
- **FR-007 (Compatibilidad de Esquema y Pruebas Existentes):** Las migraciones en `database.py` (`s3_key`, `s3_version_id`), las actualizaciones en `app.py` y la interfaz en `templates/index.html` (indicador visual opcional de estado S3 si viene en la respuesta) DEBEN mantener compatibilidad total con las 31 pruebas existentes en `test_db_crypto.py`, `test_net_automation.py` y `test_api.py` (incluyendo las 28 pruebas originales y las 3 añadidas para IOSv).
- **FR-008 (Contenedor Endurecido):** El proyecto DEBE incluir un `Dockerfile` basado en `python:3.12-slim` que instale dependencias desde `requirements.txt` (fijando `paramiko==3.5.1` e incluyendo `boto3` y `gunicorn`), cree un usuario no-root `appuser`, cambie la propiedad del directorio de trabajo a `appuser`, exponga el puerto `5000` y ejecute `gunicorn --bind 0.0.0.0:5000 app:app`.
- **FR-009 (Exclusión de Secretos y Estado en Contenedor):** El proyecto DEBE incluir `.dockerignore` excluyendo como mínimo `.env`, `*.db`, `venv/`, `__pycache__/`, `*.pyc` y `.git/`.
- **FR-010 (Pipeline CI Encadenado):** El proyecto DEBE incluir `.github/workflows/ci.yml` activado en `push` y `pull_request` hacia `main` con 3 etapas encadenadas (`needs`):
  1. `lint`: ejecución de `ruff check .`.
  2. `test`: ejecución de `python -m unittest discover -s . -p "test_*.py" -v` con variable de entorno `ENCRYPTION_KEY` efímera generada en el runner.
  3. `docker-build`: construcción de la imagen Docker (`docker build .`) dependiente del éxito previo de `lint` y `test`.

### Key Entities

- **Backup (`backups`):** Instantánea de configuración almacenada localmente en SQLite (`id`, `device_id`, `raw_config`, `timestamp`) ampliada con trazabilidad opcional en la nube (`s3_key TEXT DEFAULT NULL`, `s3_version_id TEXT DEFAULT NULL`).
- **Cloud Sync Result (`s3_sync`):** Estructura de estado retornada por la API de respaldo (`enabled: bool`, `status: "disabled" | "uploaded" | "failed"`, `bucket: str | None`, `s3_key: str | None`, `version_id: str | None`, `message: str`).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001 (Resiliencia de Respaldos):** El 100% de los respaldos ejecutados cuando AWS S3 está deshabilitado (`ENABLE_S3_BACKUP=false`) o inalcanzable (`ClientError`) conservan el registro completo en SQLite y retornan respuesta exitosa `201 Created` sin errores `500`.
- **SC-002 (Seguridad de Doble Cifrado e IAM):** El 100% de los objetos subidos a S3 son ilegibles sin `ENCRYPTION_KEY` (Fernet), utilizan `ServerSideEncryption="AES256"`, y el código fuente contiene `0` ocurrencias de credenciales estáticas de AWS (`aws_access_key_id` / `aws_secret_access_key`).
- **SC-003 (Integridad de Regresión):** El 100% de la suite de pruebas (`unittest discover`, incluyendo las pruebas existentes y las nuevas pruebas unitarias con mock de S3/degradación elegante) pasa sin errores.
- **SC-004 (Endurecimiento del Contenedor):** La imagen Docker construida ejecuta el proceso WSGI como usuario no-root (`appuser`, UID != 0), escucha en el puerto `5000` y contiene `0` archivos `.env`, `*.db` o directorios `venv/`.
- **SC-005 (Automatización CI):** El pipeline de GitHub Actions valida de forma automática las 3 etapas (`ruff` → `unittest` → `docker build`) en cada `push` o `pull_request` a `main`, impidiendo la etapa de build si fallan el análisis estático o las pruebas.

## Assumptions

- El almacenamiento de dependencias en `requirements.txt` se formalizará para habilitar tanto la construcción reproducible en Docker como la instalación en GitHub Actions, manteniendo `paramiko==3.5.1` por compatibilidad con Cisco IOSv 15.9(3)M6.
- En despliegues en contenedor sobre AWS (ECS/EC2/EKS), `ENCRYPTION_KEY`, `ENABLE_S3_BACKUP`, `S3_BACKUP_BUCKET` y `AWS_DEFAULT_REGION` se inyectarán en tiempo de ejecución como variables de entorno, y la base de datos SQLite residirá en un volumen persistente si se requiere retención entre reinicios del contenedor.
- Las pruebas de integración S3 en la suite automatizada utilizarán mocks (`unittest.mock`) sobre `boto3.client` para ejecutarse de forma determinista y sin costos ni credenciales reales en CI.
