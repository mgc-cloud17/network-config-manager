"""
app.py - Servidor web Flask y API REST para Network Config Manager.
Conecta el frontend HTML5/Tailwind/JS con la capa SQLite/Fernet (database.py)
y el motor de automatización SSH/Diff (net_automation.py).
Gobernado por RULES.md y ARCHITECTURE.md.
"""

import ipaddress
import sqlite3

from flask import Flask, jsonify, render_template, request
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from cloud_storage import upload_encrypted_backup
from database import (
    DEFAULT_DB_PATH,
    add_backup,
    add_device,
    delete_device,
    get_backup_by_id,
    get_device_backups,
    get_device_by_id,
    get_devices,
    init_db,
    update_backup_s3_metadata,
)
from net_automation import (
    apply_configuration,
    calculate_diff,
    fetch_running_config,
    test_connection,
)

app = Flask(__name__)
app.config["DB_PATH"] = DEFAULT_DB_PATH

# Garantizar esquema SQLite inicializado al cargar el servidor
init_db(app.config["DB_PATH"])


def _get_db_path() -> str:
    """Obtiene la ruta de base de datos configurada en la instancia Flask."""
    return app.config.get("DB_PATH", DEFAULT_DB_PATH)


@app.route("/", methods=["GET"])
def index():
    """Renderiza la interfaz principal del gestor de configuraciones."""
    return render_template("index.html")


@app.route("/api/devices", methods=["GET"])
def list_devices():
    """Lista todos los dispositivos registrados sin exponer contraseñas ni secrets."""
    db_path = _get_db_path()
    init_db(db_path)
    devices = get_devices(db_path=db_path)
    return jsonify({"status": "success", "data": devices}), 200


@app.route("/api/devices", methods=["POST"])
def create_device():
    """Valida, cifra credenciales con Fernet y registra un nuevo dispositivo."""
    db_path = _get_db_path()
    init_db(db_path)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "Se requiere un cuerpo JSON válido."}), 400

    hostname = payload.get("hostname") or payload.get("name")
    ip_address = payload.get("ip_address")
    device_type = payload.get("device_type") or "cisco_ios"
    port = payload.get("port", 22)
    username = payload.get("username")
    password = payload.get("password")
    secret = payload.get("secret")

    if not hostname or not ip_address or not username or not password:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Los campos 'hostname', 'ip_address', 'username' y 'password' son obligatorios.",
                }
            ),
            400,
        )

    try:
        ipaddress.ip_address(str(ip_address).strip())
    except ValueError:
        return jsonify({"status": "error", "message": "El formato de 'ip_address' no es una IP válida."}), 400

    try:
        device = add_device(
            hostname=str(hostname),
            ip_address=str(ip_address),
            username=str(username),
            plain_password=str(password),
            device_type=str(device_type),
            plain_secret=str(secret) if secret is not None else None,
            port=port,
            db_path=db_path,
        )
        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Dispositivo registrado correctamente.",
                    "data": device,
                }
            ),
            201,
        )
    except ValueError as val_err:
        return jsonify({"status": "error", "message": str(val_err)}), 400
    except sqlite3.IntegrityError:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Ya existe un dispositivo registrado con la IP {str(ip_address).strip()}.",
                }
            ),
            409,
        )


@app.route("/api/devices/<int:device_id>", methods=["DELETE"])
def remove_device(device_id: int):
    """Elimina un dispositivo y sus respaldos en cascada."""
    db_path = _get_db_path()
    deleted = delete_device(device_id, db_path=db_path)
    if not deleted:
        return jsonify({"status": "error", "message": f"Dispositivo {device_id} no encontrado."}), 404
    return (
        jsonify(
            {
                "status": "success",
                "message": f"Dispositivo {device_id} y sus respaldos fueron eliminados.",
            }
        ),
        200,
    )


@app.route("/api/devices/<int:device_id>/test", methods=["POST"])
def test_device_ssh(device_id: int):
    """Prueba conectividad SSH y elevación enable contra un dispositivo."""
    db_path = _get_db_path()
    device = get_device_by_id(device_id, include_encrypted_password=True, db_path=db_path)
    if not device:
        return jsonify({"status": "error", "message": f"Dispositivo {device_id} no encontrado."}), 404

    connected = test_connection(device)
    if connected:
        return (
            jsonify(
                {
                    "status": "success",
                    "connected": True,
                    "message": f"Conexión SSH exitosa con {device['hostname']} ({device['ip_address']}).",
                }
            ),
            200,
        )
    return (
        jsonify(
            {
                "status": "error",
                "connected": False,
                "message": f"Fallo al conectar por SSH con {device['hostname']} ({device['ip_address']}). Verifica IP, puerto, usuario, contraseña o enable secret.",
            }
        ),
        502,
    )


@app.route("/api/devices/<int:device_id>/backup", methods=["POST"])
def create_device_backup(device_id: int):
    """Extrae el running-config vía Netmiko y guarda un nuevo respaldo en SQLite."""
    db_path = _get_db_path()
    device = get_device_by_id(device_id, include_encrypted_password=True, db_path=db_path)
    if not device:
        return jsonify({"status": "error", "message": f"Dispositivo {device_id} no encontrado."}), 404

    try:
        raw_config = fetch_running_config(device)
        backup = add_backup(device_id, raw_config, db_path=db_path)

        # Sincronización cloud opcional: el respaldo local ya está persistido y
        # ninguna falla de S3 debe convertirse en un error de la API.
        cloud_sync = upload_encrypted_backup(device["hostname"], backup["id"], raw_config)
        if cloud_sync["synced"]:
            try:
                update_backup_s3_metadata(
                    backup["id"], cloud_sync["s3_key"], cloud_sync["version_id"], db_path=db_path
                )
            except sqlite3.Error:
                cloud_sync["error"] = (
                    "La copia se subió a S3 pero no se pudieron registrar sus metadatos en SQLite."
                )

        message = f"Backup capturado exitosamente desde {device['hostname']}."
        if cloud_sync["synced"]:
            message += " Copia cifrada sincronizada en S3."
        elif cloud_sync["error"]:
            message += f" Advertencia: la sincronización con S3 falló ({cloud_sync['error']}); el respaldo local se conservó."

        return (
            jsonify(
                {
                    "status": "success",
                    "message": message,
                    "data": {
                        "id": backup["id"],
                        "backup_id": backup["id"],
                        "device_id": device_id,
                        "hostname": device["hostname"],
                        "timestamp": backup["timestamp"],
                        "size_bytes": len(backup["raw_config"].encode("utf-8")),
                        "config_text": backup["raw_config"],
                        "raw_config": backup["raw_config"],
                        "s3_key": cloud_sync["s3_key"],
                        "s3_version_id": cloud_sync["version_id"],
                        "cloud_sync": cloud_sync,
                    },
                }
            ),
            201,
        )
    except NetmikoAuthenticationException as auth_err:
        return jsonify({"status": "error", "message": str(auth_err)}), 401
    except NetmikoTimeoutException as timeout_err:
        return jsonify({"status": "error", "message": str(timeout_err)}), 504
    except (ConnectionError, ValueError) as net_err:
        return jsonify({"status": "error", "message": str(net_err)}), 502


@app.route("/api/devices/<int:device_id>/backups", methods=["GET"])
def list_device_backups(device_id: int):
    """Lista el historial de respaldos de un dispositivo."""
    db_path = _get_db_path()
    device = get_device_by_id(device_id, db_path=db_path)
    if not device:
        return jsonify({"status": "error", "message": f"Dispositivo {device_id} no encontrado."}), 404

    backups = get_device_backups(device_id, db_path=db_path)
    return jsonify({"status": "success", "data": backups}), 200


@app.route("/api/backups/<int:backup_id>", methods=["GET"])
def get_backup_detail(backup_id: int):
    """Retorna el contenido completo de un respaldo específico."""
    db_path = _get_db_path()
    backup = get_backup_by_id(backup_id, db_path=db_path)
    if not backup:
        return jsonify({"status": "error", "message": f"Respaldo {backup_id} no encontrado."}), 404

    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "id": backup["id"],
                    "device_id": backup["device_id"],
                    "timestamp": backup["timestamp"],
                    "size_bytes": len(backup["raw_config"].encode("utf-8")),
                    "config_text": backup["raw_config"],
                    "raw_config": backup["raw_config"],
                    "s3_key": backup.get("s3_key"),
                    "s3_version_id": backup.get("s3_version_id"),
                },
            }
        ),
        200,
    )


@app.route("/api/devices/<int:device_id>/diff", methods=["GET"])
def diff_device_backups(device_id: int):
    """Calcula el diff unificado entre dos versiones de respaldo (?v1=<id>&v2=<id>)."""
    db_path = _get_db_path()
    v1_raw = request.args.get("v1")
    v2_raw = request.args.get("v2")

    if not v1_raw or not v2_raw:
        return jsonify({"status": "error", "message": "Los parámetros de consulta 'v1' y 'v2' son obligatorios."}), 400

    try:
        v1_id = int(v1_raw)
        v2_id = int(v2_raw)
    except ValueError:
        return jsonify({"status": "error", "message": "Los parámetros 'v1' y 'v2' deben ser enteros válidos."}), 400

    device = get_device_by_id(device_id, db_path=db_path)
    if not device:
        return jsonify({"status": "error", "message": f"Dispositivo {device_id} no encontrado."}), 404

    backup_v1 = get_backup_by_id(v1_id, device_id=device_id, db_path=db_path)
    backup_v2 = get_backup_by_id(v2_id, device_id=device_id, db_path=db_path)
    if not backup_v1 or not backup_v2:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Uno o ambos respaldos no existen o no pertenecen a este dispositivo.",
                }
            ),
            404,
        )

    diff_lines = calculate_diff(
        backup_v1["raw_config"],
        backup_v2["raw_config"],
        fromfile=f"backup_{backup_v1['id']} ({backup_v1['timestamp']})",
        tofile=f"backup_{backup_v2['id']} ({backup_v2['timestamp']})",
    )

    return (
        jsonify(
            {
                "status": "success",
                "data": {
                    "device_id": device_id,
                    "hostname": device["hostname"],
                    "v1": {"id": backup_v1["id"], "timestamp": backup_v1["timestamp"]},
                    "v2": {"id": backup_v2["id"], "timestamp": backup_v2["timestamp"]},
                    "has_changes": len(diff_lines) > 0,
                    "diff_lines": diff_lines,
                    "unified_diff": "\n".join(diff_lines),
                },
            }
        ),
        200,
    )


@app.route("/api/devices/<int:device_id>/rollback/<int:backup_id>", methods=["POST"])
def rollback_device_backup(device_id: int, backup_id: int):
    """Aplica la configuración de un respaldo previo al dispositivo en vivo."""
    db_path = _get_db_path()
    device = get_device_by_id(device_id, include_encrypted_password=True, db_path=db_path)
    if not device:
        return jsonify({"status": "error", "message": f"Dispositivo {device_id} no encontrado."}), 404

    backup = get_backup_by_id(backup_id, device_id=device_id, db_path=db_path)
    if not backup:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": f"Respaldo {backup_id} no encontrado para el dispositivo {device_id}.",
                }
            ),
            404,
        )

    try:
        result = apply_configuration(device, backup["raw_config"])
        return (
            jsonify(
                {
                    "status": "success",
                    "message": f"Configuración #{backup_id} aplicada exitosamente en {device['hostname']}.",
                    "data": {
                        "device_id": device_id,
                        "backup_id": backup_id,
                        "hostname": device["hostname"],
                        "commands_applied": result["commands_applied"],
                        "output": result["output"],
                    },
                }
            ),
            200,
        )
    except NetmikoAuthenticationException as auth_err:
        return jsonify({"status": "error", "message": str(auth_err)}), 401
    except NetmikoTimeoutException as timeout_err:
        return jsonify({"status": "error", "message": str(timeout_err)}), 504
    except (ConnectionError, ValueError) as net_err:
        return jsonify({"status": "error", "message": str(net_err)}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
