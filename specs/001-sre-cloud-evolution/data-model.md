# Data Model: Evolución SRE/Cloud (SQLite + Cloud Sync)

**Feature**: `001-sre-cloud-evolution` | **Date**: 2026-11-25

## 1. Esquema Actualizado de Base de Datos (`database.py`)

### Tabla `devices` (Sin cambios)
Mantiene `id`, `hostname`, `ip_address`, `device_type`, `port`, `username`, `encrypted_password`, `encrypted_secret`, `created_at`.

### Tabla `backups` (Extendida con Trazabilidad S3)

| Columna | Tipo SQLite | Restricciones / Default | Descripción |
| :--- | :--- | :--- | :--- |
| `id` | `INTEGER` | `PRIMARY KEY AUTOINCREMENT` | Identificador único del respaldo. |
| `device_id` | `INTEGER` | `NOT NULL, FOREIGN KEY -> devices(id) ON DELETE CASCADE` | Dispositivo propietario del respaldo. |
| `raw_config` | `TEXT` | `NOT NULL` | Configuración extraída (`show running-config`). |
| `timestamp` | `DATETIME` | `DEFAULT CURRENT_TIMESTAMP` | Fecha/hora UTC de creación local. |
| `s3_key` | `TEXT` | `DEFAULT NULL` | Llave del objeto cifrado en S3 (`backups/{hostname}/{timestamp}_backup_{backup_id}.enc`). |
| `s3_version_id` | `TEXT` | `DEFAULT NULL` | `VersionId` retornado por S3 si el bucket tiene versionado habilitado. |

### DDL y Migración Idempotente (`init_db`)

```sql
CREATE TABLE IF NOT EXISTS backups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    raw_config TEXT NOT NULL,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    s3_key TEXT DEFAULT NULL,
    s3_version_id TEXT DEFAULT NULL,
    FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
);
```

En bases de datos existentes, `init_db()` inspecciona `PRAGMA table_info(backups);` y ejecuta de forma condicional:

```sql
ALTER TABLE backups ADD COLUMN s3_key TEXT DEFAULT NULL;
ALTER TABLE backups ADD COLUMN s3_version_id TEXT DEFAULT NULL;
```

### Nueva Función DAL (`database.py`)

```python
def update_backup_s3_metadata(
    backup_id: int,
    s3_key: str,
    s3_version_id: Optional[str] = None,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """
    Actualiza s3_key y s3_version_id para un backup existente mediante consulta parametrizada (?).
    Retorna True si se actualizó la fila, False si backup_id no existe.
    """
```

Query SQL parametrizado:
```sql
UPDATE backups
SET s3_key = ?, s3_version_id = ?
WHERE id = ?;
```

Las consultas de lectura (`get_devices`, `get_device_backups`, `get_backup_by_id`) pueden exponer `s3_key` y `s3_version_id` sin afectar la compatibilidad de los campos existentes.

## 2. Entidad de Respuesta Cloud (`cloud_sync`)

Retornada por `upload_encrypted_backup()` y embebida en la respuesta de `POST /api/devices/<device_id>/backup`:

| Campo | Tipo | Valores / Reglas |
| :--- | :--- | :--- |
| `synced` | `bool` | `True` si el objeto se subió a S3; `False` si S3 está deshabilitado o falló. |
| `s3_key` | `str \| None` | Ruta S3 generada (`backups/{hostname}/{timestamp}_backup_{backup_id}.enc`) si tuvo éxito; `None` si no se subió. |
| `version_id` | `str \| None` | `VersionId` devuelto por S3 (o `None` si el bucket no tiene versionado o falló). |
| `error` | `str \| None` | `None` en éxito o cuando `ENABLE_S3_BACKUP=false` (o mensaje descriptivo seguro si ocurrió un fallo controlado en S3). |
