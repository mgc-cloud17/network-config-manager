"""
crypto.py - Módulo de seguridad criptográfica para Network Config Manager.
Implementa cifrado simétrico con Fernet para contraseñas de dispositivos.
Gobernado por RULES.md y SPEC-01-DB-INVENTORY.
"""

import os
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

# Cargar variables de entorno desde .env local
ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=ENV_PATH)


def get_fernet() -> Fernet:
    """
    Carga ENCRYPTION_KEY desde las variables de entorno y retorna una
    instancia configurada de Fernet.

    Raises:
        RuntimeError: Si ENCRYPTION_KEY no está definida o está vacía.
        ValueError: Si la clave no es válida según los estándares de Fernet.
    """
    # Re-cargar variables si no estuvieran presentes
    if not os.environ.get("ENCRYPTION_KEY"):
        load_dotenv(dotenv_path=ENV_PATH)

    key = os.environ.get("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "La variable de entorno ENCRYPTION_KEY no está configurada o está vacía en .env."
        )

    try:
        # Fernet requiere exactamente 32 bytes codificados en base64 url-safe
        return Fernet(key.encode("utf-8"))
    except Exception as exc:
        raise ValueError(
            f"ENCRYPTION_KEY no es una clave Fernet válida de 32 bytes en base64: {exc}"
        ) from exc


def encrypt_password(plain_password: str) -> str:
    """
    Cifra una contraseña en texto claro utilizando AES-128-CBC + HMAC-SHA256 (Fernet).

    Args:
        plain_password (str): Contraseña en texto plano a cifrar.

    Returns:
        str: Token Fernet en formato string UTF-8 listo para persistencia en SQLite.

    Raises:
        ValueError: Si la contraseña es nula, vacía o no es una cadena.
    """
    if not plain_password or not isinstance(plain_password, str):
        raise ValueError("La contraseña en texto plano debe ser una cadena no vacía.")

    fernet = get_fernet()
    token = fernet.encrypt(plain_password.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_password(encrypted_password: str) -> str:
    """
    Descifra un token Fernet en memoria volátil.

    Args:
        encrypted_password (str): Token Fernet almacenado.

    Returns:
        str: Contraseña original en texto plano.

    Raises:
        ValueError: Si el token es nulo o vacío.
        InvalidToken: Si el token está corrupto, fue alterado o no coincide con la clave.
    """
    if not encrypted_password or not isinstance(encrypted_password, str):
        raise ValueError("El token cifrado debe ser una cadena no vacía.")

    fernet = get_fernet()
    plain_bytes = fernet.decrypt(encrypted_password.encode("utf-8"))
    return plain_bytes.decode("utf-8")

