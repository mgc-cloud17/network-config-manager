"""
database.py - Capa de acceso a datos (DAL) para Network Config Manager.
Implementa persistencia en SQLite puro con consultas parametrizadas (?)
e integridad referencial. Gobernado por RULES.md y SPEC-01-DB-INVENTORY.
"""

import os
import sqlite3
from pathlib import Path
from typing import Any

from crypto import encrypt_password

# Ruta de base de datos por defecto: DB_PATH (p. ej. volumen en contenedor) o archivo junto al módulo
DEFAULT_DB_PATH = os.environ.get("DB_PATH") or str(Path(__file__).resolve().parent / "network_manager.db")


def get_connection(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """
    Retorna una conexión a SQLite con row_factory habilitado y
    verificación estricta de claves foráneas activada.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    """
    Inicializa el esquema de base de datos creando las tablas 'devices' y 'backups'
    junto con sus respectivos índices de optimización y claves foráneas en cascada.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # Tabla de Inventario de Dispositivos
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hostname TEXT NOT NULL,
                ip_address TEXT NOT NULL UNIQUE,
                device_type TEXT NOT NULL DEFAULT 'cisco_ios',
                port INTEGER NOT NULL DEFAULT 22,
                username TEXT NOT NULL,
                encrypted_password TEXT NOT NULL,
                encrypted_secret TEXT DEFAULT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Migración idempotente para bases de datos existentes
        cursor.execute("PRAGMA table_info(devices);")
        existing_columns = {row["name"] for row in cursor.fetchall()}
        if "encrypted_secret" not in existing_columns:
            cursor.execute("ALTER TABLE devices ADD COLUMN encrypted_secret TEXT DEFAULT NULL;")
        if "port" not in existing_columns:
            cursor.execute("ALTER TABLE devices ADD COLUMN port INTEGER NOT NULL DEFAULT 22;")

        # Tabla de Historial de Respaldos de Configuración
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id INTEGER NOT NULL,
                raw_config TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                s3_key TEXT DEFAULT NULL,
                s3_version_id TEXT DEFAULT NULL,
                FOREIGN KEY (device_id) REFERENCES devices(id) ON DELETE CASCADE
            );
        """)

        # Migración idempotente: metadatos del respaldo híbrido en S3
        cursor.execute("PRAGMA table_info(backups);")
        backup_columns = {row["name"] for row in cursor.fetchall()}
        if "s3_key" not in backup_columns:
            cursor.execute("ALTER TABLE backups ADD COLUMN s3_key TEXT DEFAULT NULL;")
        if "s3_version_id" not in backup_columns:
            cursor.execute("ALTER TABLE backups ADD COLUMN s3_version_id TEXT DEFAULT NULL;")

        # Índices requeridos
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_ip_address 
                ON devices(ip_address);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_backups_device_id 
                ON backups(device_id);
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_backups_device_timestamp 
                ON backups(device_id, timestamp DESC);
        """)
        conn.commit()


def add_device(
    hostname: str,
    ip_address: str,
    username: str,
    plain_password: str,
    device_type: str = "cisco_ios",
    plain_secret: str | None = None,
    port: int = 22,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """
    Valida y almacena un nuevo dispositivo en el inventario.
    Cifra la contraseña y el enable secret (opcional) con Fernet antes de persistirlos.
    Retorna los datos del dispositivo creado (excluyendo las credenciales).

    Raises:
        ValueError: Si algún campo requerido es nulo, vacío o el puerto es inválido.
        sqlite3.IntegrityError: Si ip_address ya está registrada en el sistema.
    """
    if not hostname or not isinstance(hostname, str) or not hostname.strip():
        raise ValueError("El campo 'hostname' es requerido y no puede estar vacío.")
    if not ip_address or not isinstance(ip_address, str) or not ip_address.strip():
        raise ValueError("El campo 'ip_address' es requerido y no puede estar vacío.")
    if not username or not isinstance(username, str) or not username.strip():
        raise ValueError("El campo 'username' es requerido y no puede estar vacío.")
    if not plain_password or not isinstance(plain_password, str):
        raise ValueError("El campo 'plain_password' es requerido y no puede estar vacío.")

    try:
        clean_port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("El campo 'port' debe ser un número entero válido.") from exc
    if not (1 <= clean_port <= 65535):
        raise ValueError("El campo 'port' debe estar entre 1 y 65535.")

    # Cifrado simétrico antes de tocar la base de datos
    encrypted_pw = encrypt_password(plain_password)
    encrypted_sec: str | None = None
    if plain_secret and isinstance(plain_secret, str) and plain_secret.strip():
        encrypted_sec = encrypt_password(plain_secret)

    clean_hostname = hostname.strip()
    clean_ip = ip_address.strip()
    clean_username = username.strip()
    clean_type = (device_type or "cisco_ios").strip()

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO devices (hostname, ip_address, device_type, port, username, encrypted_password, encrypted_secret)
            VALUES (?, ?, ?, ?, ?, ?, ?);
            """,
            (clean_hostname, clean_ip, clean_type, clean_port, clean_username, encrypted_pw, encrypted_sec),
        )
        new_id = cursor.lastrowid

        cursor.execute(
            """
            SELECT id, hostname, ip_address, device_type, port, username, created_at
            FROM devices
            WHERE id = ?;
            """,
            (new_id,),
        )
        row = cursor.fetchone()
        conn.commit()

        return dict(row)


def get_devices(db_path: str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """
    Retorna la lista completa de dispositivos registrados en el inventario
    ordenados ascendentemente por id, acompañados de su historial resumido de backups.
    Excluye estrictamente las contraseñas.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, hostname, ip_address, device_type, port, username, created_at
            FROM devices
            ORDER BY id ASC;
            """
        )
        device_rows = cursor.fetchall()
        devices = [dict(row) for row in device_rows]

        # Enriquecer con lista resumida de backups por dispositivo
        for device in devices:
            cursor.execute(
                """
                SELECT id, timestamp, LENGTH(raw_config) AS size_bytes, s3_key, s3_version_id
                FROM backups
                WHERE device_id = ?
                ORDER BY timestamp DESC, id DESC;
                """,
                (device["id"],),
            )
            backup_rows = cursor.fetchall()
            device["backups"] = [dict(b) for b in backup_rows]

        return devices


def get_device_by_id(
    device_id: int,
    include_encrypted_password: bool = False,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """
    Recupera un dispositivo por su ID primario.

    Args:
        device_id (int): ID del dispositivo en la tabla 'devices'.
        include_encrypted_password (bool): Solo True para el servicio interno de SSH/Netmiko.
        db_path (str): Ruta de la base de datos SQLite.

    Returns:
        dict con los datos del dispositivo o None si no existe.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        if include_encrypted_password:
            cursor.execute(
                """
                SELECT id, hostname, ip_address, device_type, port, username, encrypted_password, encrypted_secret, created_at
                FROM devices
                WHERE id = ?;
                """,
                (device_id,),
            )
        else:
            cursor.execute(
                """
                SELECT id, hostname, ip_address, device_type, port, username, created_at
                FROM devices
                WHERE id = ?;
                """,
                (device_id,),
            )

        row = cursor.fetchone()
        return dict(row) if row else None


def delete_device(device_id: int, db_path: str = DEFAULT_DB_PATH) -> bool:
    """
    Elimina un dispositivo del inventario. Con PRAGMA foreign_keys = ON,
    los backups asociados en la tabla 'backups' se eliminan automáticamente (ON DELETE CASCADE).

    Returns:
        bool: True si el registro existía y fue eliminado, False en caso contrario.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM devices
            WHERE id = ?;
            """,
            (device_id,),
        )
        deleted = cursor.rowcount > 0
        conn.commit()
        return deleted


def add_backup(
    device_id: int,
    raw_config: str,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    """
    Almacena una instantánea de configuración (running-config) asociada a un dispositivo.
    """
    if not raw_config or not isinstance(raw_config, str):
        raise ValueError("El contenido de configuración 'raw_config' no puede estar vacío.")

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO backups (device_id, raw_config)
            VALUES (?, ?);
            """,
            (device_id, raw_config),
        )
        backup_id = cursor.lastrowid
        cursor.execute(
            """
            SELECT id, device_id, raw_config, timestamp, s3_key, s3_version_id
            FROM backups
            WHERE id = ?;
            """,
            (backup_id,),
        )
        row = cursor.fetchone()
        conn.commit()
        return dict(row)


def get_backup_by_id(
    backup_id: int,
    device_id: int | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> dict[str, Any] | None:
    """
    Recupera un backup específico por su ID, verificando opcionalmente que pertenezca
    al dispositivo indicado.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        if device_id is None:
            cursor.execute(
                """
                SELECT id, device_id, raw_config, timestamp, s3_key, s3_version_id
                FROM backups
                WHERE id = ?;
                """,
                (backup_id,),
            )
        else:
            cursor.execute(
                """
                SELECT id, device_id, raw_config, timestamp, s3_key, s3_version_id
                FROM backups
                WHERE id = ? AND device_id = ?;
                """,
                (backup_id, device_id),
            )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_device_backups(
    device_id: int,
    db_path: str = DEFAULT_DB_PATH,
) -> list[dict[str, Any]]:
    """
    Retorna todos los backups de un dispositivo ordenados del más reciente al más antiguo.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, device_id, timestamp, LENGTH(raw_config) AS size_bytes, s3_key, s3_version_id
            FROM backups
            WHERE device_id = ?
            ORDER BY timestamp DESC, id DESC;
            """,
            (device_id,),
        )
        return [dict(row) for row in cursor.fetchall()]


def update_backup_s3_metadata(
    backup_id: int,
    s3_key: str,
    s3_version_id: str | None = None,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    """
    Registra en SQLite la llave y el VersionId del objeto cifrado subido a S3.

    Returns:
        bool: True si se actualizó el respaldo; False si backup_id no existe.
    """
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE backups
            SET s3_key = ?, s3_version_id = ?
            WHERE id = ?;
            """,
            (s3_key, s3_version_id, backup_id),
        )
        updated = cursor.rowcount > 0
        conn.commit()
        return updated
