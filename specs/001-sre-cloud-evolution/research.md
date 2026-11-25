# Research & Technical Decisions: Evolución SRE/Cloud

**Feature**: `001-sre-cloud-evolution` | **Date**: 2026-11-25

## 1. Cliente S3 y Autenticación IAM (`boto3`)

- **Decision**: Instanciar `boto3.client("s3", region_name=aws_region)` dentro de `cloud_storage.py` sin pasar `aws_access_key_id` ni `aws_secret_access_key`, capturando `(ClientError, BotoCoreError, Exception)` en la subida.
- **Rationale**: Garantiza cumplimiento estricto de IAM Roles (EC2 Instance Profile, ECS Task Role, EKS IRSA o credenciales estándar del entorno AWS) y asegura degradación elegante sin propagar excepciones no controladas a Flask (`HTTP 500`).
- **Alternatives considered**: Pasar llaves explícitas desde `.env`; descartado por riesgo de fuga de credenciales estáticas y violación de buenas prácticas IAM.

## 2. Doble Cifrado y Formato de Clave S3

- **Decision**: Reutilizar `crypto.encrypt_password(config_text)` (Fernet AES-128-CBC + HMAC-SHA256) antes de `put_object(..., Body=encrypted_payload.encode("utf-8"), ServerSideEncryption="AES256")` en la ruta `backups/{safe_hostname}/{timestamp}_backup_{backup_id}.enc`.
- **Rationale**: Combina cifrado controlado por el cliente (zero-trust frente al proveedor cloud) con cifrado en reposo gestionado por AWS S3 (`AES256`). Sanitizar `hostname` y usar timestamp compacto UTC (`YYYYMMDDTHHMMSSZ`) evita caracteres problemáticos en URLs/claves S3.
- **Alternatives considered**: Subir texto plano solo con SSE-S3; descartado porque dejaría las configuraciones legibles para cualquier rol con `s3:GetObject`.

## 3. Fijación de Dependencias y Compatibilidad IOSv en Contenedor

- **Decision**: Crear `requirements.txt` fijando `paramiko==3.5.1` junto con `Flask==3.1.3`, `netmiko==4.8.0`, `cryptography==50.0.2`, `python-dotenv==1.2.4`, `boto3`, `gunicorn` y `ruff`.
- **Rationale**: Durante la validación previa contra Cisco IOSv 15.9(3)M6 se comprobó que Paramiko 5.1.0 eliminó `diffie-hellman-group14-sha1` y `ssh-rsa`. Fijar `paramiko==3.5.1` asegura que tanto el contenedor Docker como el runner de CI mantengan compatibilidad real con IOSv.
- **Alternatives considered**: Usar rangos abiertos (`paramiko>=3.0`); descartado porque `pip install` resolvería Paramiko 5.x y rompería la negociación SSH con IOSv.

## 4. Ruta de Base de Datos SQLite en Contenedor No-Root

- **Decision**: Configurar en `Dockerfile` un directorio escribible propiedad de `appuser` (ej. `/app/data` o `/app` con `chown -R appuser:appuser /app` y soporte opcional de `DB_PATH` o ruta por defecto en `/app`) para que Gunicorn ejecutado como `appuser` pueda crear y escribir `network_manager.db` sin errores de permisos.
- **Rationale**: En contenedores `non-root`, escribir en `/app` falla si pertenece a `root`. Asignar propiedad explícita a `appuser` y permitir montar un volumen en `/app/data` resuelve la persistencia y seguridad simultáneamente.
- **Alternatives considered**: Ejecutar el contenedor como `root`; descartado por incumplir principios de mínimo privilegio.

## 5. Flujo CI en GitHub Actions

- **Decision**: Workflow `.github/workflows/ci.yml` con tres jobs secuenciales (`lint` $\rightarrow$ `test` $\rightarrow$ `docker-build`) en `ubuntu-latest`, generando una `ENCRYPTION_KEY` efímera en el paso de `test` mediante `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- **Rationale**: Evita depender de secrets externos en PRs de colaboradores, garantiza código limpio con `ruff check .`, valida la suite completa `unittest` y comprueba que el `Dockerfile` construye sin errores antes de fusionar a `main`.
- **Alternatives considered**: Un único job monolítico sin `needs`; descartado porque las etapas encadenadas dan retroalimentación más clara y rápida.
