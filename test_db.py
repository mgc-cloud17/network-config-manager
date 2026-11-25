"""
test_db.py - Script de prueba rápida para el módulo de persistencia y criptografía.
Verifica:
1. Inicialización de base de datos SQLite con PRAGMA foreign_keys = ON.
2. Inserción de un router c7200 con contraseña cifrada vía Fernet.
3. Confirmación de que la columna encrypted_password contiene un token cifrado.
4. Descifrado en memoria y validación con la contraseña original.
5. Eliminación del registro para dejar la base de datos limpia.
"""

from crypto import decrypt_password
from database import (
    add_device,
    delete_device,
    get_device_by_id,
    init_db,
)


def run_quick_test():
    print("=" * 60)
    print(">>> Iniciando prueba rápida de persistencia y criptografía")
    print("=" * 60)

    # 1. Inicializar base de datos
    print("[1/5] Ejecutando init_db()...")
    init_db()
    print("      -> Base de datos y tablas inicializadas con éxito.")

    # Datos de prueba del router c7200
    test_device = {
        "hostname": "R1-c7200",
        "ip_address": "192.168.56.101",
        "username": "cisco_admin",
        "plain_password": "Cisco7200_Sup3rS3cr3t_2026!",
        "plain_secret": "Cisco123!",
        "device_type": "cisco_ios",
    }

    # 2. Insertar router c7200 de prueba
    print(f"[2/5] Insertando router {test_device['hostname']} ({test_device['ip_address']})...")
    created = add_device(
        hostname=test_device["hostname"],
        ip_address=test_device["ip_address"],
        username=test_device["username"],
        plain_password=test_device["plain_password"],
        device_type=test_device["device_type"],
        plain_secret=test_device["plain_secret"],
    )
    device_id = created["id"]
    print(f"      -> Dispositivo creado con ID: {device_id}")
    print(f"      -> Datos devueltos (sin contraseña/secret): {created}")

    # 3. Consultar base de datos confirmando que esté cifrado
    print("[3/5] Consultando valores almacenados en 'encrypted_password' y 'encrypted_secret'...")
    record = get_device_by_id(device_id, include_encrypted_password=True)
    assert record is not None, "Error: Dispositivo no encontrado tras inserción."
    
    stored_token = record["encrypted_password"]
    stored_secret_token = record["encrypted_secret"]
    print(f"      -> Token password en disco: {stored_token[:30]}... (longitud: {len(stored_token)})")
    print(f"      -> Token secret en disco:   {stored_secret_token[:30]}... (longitud: {len(stored_secret_token)})")
    
    # Verificaciones de no texto plano
    assert stored_token != test_device["plain_password"], "FALLO: La contraseña está en texto plano."
    assert test_device["plain_password"] not in stored_token, "FALLO: Contraseña legible en token."
    assert stored_token.startswith("gAAAAA"), "FALLO: No es un token Fernet válido."
    assert stored_secret_token != test_device["plain_secret"], "FALLO: El secret está en texto plano."
    assert stored_secret_token.startswith("gAAAAA"), "FALLO: Secret no es un token Fernet válido."
    print("      -> Confirmado: Password y enable secret cifrados simétricamente.")

    # 4. Descifrar y verificar coincidencia exacta
    print("[4/5] Descifrando credenciales en memoria volátil...")
    decrypted = decrypt_password(stored_token)
    decrypted_secret = decrypt_password(stored_secret_token)
    assert decrypted == test_device["plain_password"], "FALLO: La contraseña descifrada no coincide."
    assert decrypted_secret == test_device["plain_secret"], "FALLO: El secret descifrado no coincide."
    print("      -> Confirmado: Password y enable secret descifrados coinciden al 100% con los originales.")

    # 5. Borrar el registro para dejar la base de datos limpia
    print(f"[5/5] Eliminando registro ID {device_id} para dejar la base de datos limpia...")
    deleted = delete_device(device_id)
    assert deleted is True, "FALLO: No se pudo eliminar el registro."
    
    # Confirmar que no existe
    assert get_device_by_id(device_id) is None, "FALLO: El registro aún existe."
    print("      -> Confirmado: Registro eliminado. Base de datos limpia.")

    print("=" * 60)
    print(">>> TODAS LAS PRUEBAS RÁPIDAS PASARON EXITOSAMENTE (OK)")
    print("=" * 60)


if __name__ == "__main__":
    run_quick_test()

