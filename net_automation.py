"""
net_automation.py - Módulo de automatización de red y cálculo de diferencias (diffs)
para Network Config Manager.
Implementa interacción SSH determinista con Cisco IOS vía Netmiko
y motor de diffs con difflib.unified_diff.
Gobernado por RULES.md, ARCHITECTURE.md y SPEC-02-NET-AUTOMATION.
"""

import difflib
import socket
from typing import Any

from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoBaseException,
    NetmikoTimeoutException,
)
from paramiko.ssh_exception import SSHException

from crypto import decrypt_password

# Timeouts calibrados para Cisco IOSv real (QEMU): la lectura completa del
# running-config y los comandos de configuración pueden tardar bastante más
# de 10 s en un router virtualizado.
CONNECT_TIMEOUT = 30          # TCP / canal SSH / autenticación / banner (segundos)
READ_TIMEOUT = 30             # show running-config y lecturas de canal
CONFIG_READ_TIMEOUT = 60      # send_config_set (rollback de configuraciones extensas)
GLOBAL_DELAY_FACTOR = 2.0     # Cadencia de lectura más holgada para QEMU/IOSv

# Errores de transporte: socket/OS, Netmiko genérico y fallos de negociación SSH de Paramiko
# (p. ej. "Incompatible ssh peer") que no heredan de las excepciones anteriores.
_TRANSPORT_ERRORS = (socket.error, OSError, EOFError, NetmikoBaseException, SSHException)


def _resolve_credentials(device_dict: dict[str, Any]) -> str:
    """
    Resuelve la contraseña en memoria volátil sin persistirla ni exponerla.
    Si el diccionario contiene 'encrypted_password', la descifra con Fernet.
    Si contiene 'password' o 'plain_password', utiliza ese valor en memoria.

    Raises:
        ValueError: Si no se encuentra ninguna credencial válida en el diccionario.
    """
    if device_dict.get("encrypted_password"):
        return decrypt_password(device_dict["encrypted_password"])

    if device_dict.get("password"):
        return str(device_dict["password"])

    if device_dict.get("plain_password"):
        return str(device_dict["plain_password"])

    raise ValueError("No se proporcionó una credencial válida ('encrypted_password' o 'password').")


def _resolve_secret(device_dict: dict[str, Any]) -> str:
    """
    Resuelve la contraseña de enable secret en memoria volátil.
    Si existe 'encrypted_secret', la descifra con Fernet.
    Si no existe ningún secret configurado, retorna cadena vacía ''.
    """
    if device_dict.get("encrypted_secret"):
        return decrypt_password(device_dict["encrypted_secret"])

    if device_dict.get("secret"):
        return str(device_dict["secret"])

    if device_dict.get("plain_secret"):
        return str(device_dict["plain_secret"])

    return ""


def _build_netmiko_params(
    device_dict: dict[str, Any],
    plain_password: str,
    plain_secret: str | None = None,
) -> dict[str, Any]:
    """
    Construye el diccionario de parámetros con configuración defensiva estricta
    para routers Cisco IOS / IOSv 15.9(3)M6 (Dynamips/GNS3/Paramiko 5.1.0).
    """
    resolved_secret = plain_secret if plain_secret is not None else _resolve_secret(device_dict)

    return {
        "device_type": device_dict.get("device_type") or "cisco_ios",
        "host": device_dict["ip_address"],
        "username": device_dict["username"],
        "password": plain_password,
        "secret": resolved_secret,
        "port": int(device_dict.get("port", 22)),
        "fast_cli": False,            # Evita saturar el buffer VTY en Dynamips/GNS3/QEMU
        "timeout": CONNECT_TIMEOUT,   # Timeout TCP/SSH inicial (segundos)
        "conn_timeout": CONNECT_TIMEOUT,    # Timeout para establecer el canal SSH
        "auth_timeout": CONNECT_TIMEOUT,    # Timeout para autenticación
        "banner_timeout": CONNECT_TIMEOUT,  # Margen para banners extensos / IOSv lento
        "read_timeout_override": READ_TIMEOUT,  # Timeout de lectura por defecto de Netmiko
        "global_delay_factor": GLOBAL_DELAY_FACTOR,
        "disabled_algorithms": {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]},
    }


def test_connection(device_dict: dict[str, Any]) -> bool:
    """
    Verifica la conectividad SSH y validez de credenciales contra un dispositivo Cisco IOS.

    Args:
        device_dict (Dict[str, Any]): Datos del dispositivo (ip_address, username, credencial, etc.)

    Returns:
        bool: True si la conexión se establece y devuelve un prompt válido; False en caso contrario.
    """
    plain_password: str | None = None
    plain_secret: str | None = None
    try:
        plain_password = _resolve_credentials(device_dict)
        plain_secret = _resolve_secret(device_dict)
        params = _build_netmiko_params(device_dict, plain_password, plain_secret)

        with ConnectHandler(**params) as conn:
            conn.clear_buffer()
            if plain_secret and not conn.check_enable_mode():
                conn.enable()
            prompt = conn.find_prompt()
            return bool(prompt and len(prompt.strip()) > 0)

    except NetmikoAuthenticationException:
        # Fallo de credenciales en el equipo
        return False
    except NetmikoTimeoutException:
        # Host inalcanzable o timeout
        return False
    except (*_TRANSPORT_ERRORS, ValueError):
        # Fallos de transporte de red / socket / OS
        return False
    finally:
        if plain_password is not None:
            del plain_password
        if plain_secret is not None:
            del plain_secret


def fetch_running_config(device_dict: dict[str, Any]) -> str:
    """
    Conecta vía SSH al dispositivo Cisco IOS, eleva privilegios con enable() si es necesario,
    ejecuta 'show running-config' con read_timeout=30 y retorna la configuración limpia.

    Args:
        device_dict (Dict[str, Any]): Datos del dispositivo.

    Returns:
        str: Configuración en ejecución normalizada.

    Raises:
        NetmikoAuthenticationException: Si las credenciales SSH son rechazadas.
        NetmikoTimeoutException: Si el dispositivo no responde dentro del timeout.
        ConnectionError: Si ocurre un fallo de socket o red subyacente.
        ValueError: Si la salida está vacía o contiene errores de sintaxis IOS.
    """
    plain_password: str | None = None
    plain_secret: str | None = None
    host = device_dict.get("ip_address", "desconocido")

    try:
        plain_password = _resolve_credentials(device_dict)
        plain_secret = _resolve_secret(device_dict)
        params = _build_netmiko_params(device_dict, plain_password, plain_secret)

        with ConnectHandler(**params) as conn:
            conn.clear_buffer()
            if not conn.check_enable_mode():
                conn.enable()
            raw_output = conn.send_command(
                "show running-config",
                read_timeout=READ_TIMEOUT,
                strip_prompt=True,
                strip_command=True,
            )

        if not raw_output or not raw_output.strip():
            raise ValueError(f"Salida vacía recibida desde {host} al ejecutar 'show running-config'.")

        # Verificar errores comunes de ejecución en Cisco IOS
        error_indicators = [
            "% Invalid input detected",
            "% Incomplete command",
            "Command authorization failed",
            "% Ambiguous command",
        ]
        for indicator in error_indicators:
            if indicator in raw_output:
                raise ValueError(f"Error de comando Cisco IOS reportado por {host}: '{indicator}'")

        # Sanitizar encabezados volátiles típicos de Cisco IOS
        cleaned_lines: list[str] = []
        for line in raw_output.replace("\r\n", "\n").splitlines():
            stripped = line.strip()
            # Omitir cabeceras volátiles
            if stripped.startswith("Building configuration..."):
                continue
            if stripped.startswith("Current configuration :"):
                continue
            cleaned_lines.append(line)

        cleaned_config = "\n".join(cleaned_lines).strip()
        if not cleaned_config:
            raise ValueError(f"La configuración procesada de {host} resultó vacía tras la sanitización.")

        return cleaned_config

    except NetmikoAuthenticationException as auth_err:
        raise NetmikoAuthenticationException(
            f"Fallo de autenticación SSH al conectar con {host}: credenciales inválidas."
        ) from auth_err
    except NetmikoTimeoutException as timeout_err:
        raise NetmikoTimeoutException(
            f"Timeout al conectar o leer datos desde {host} "
            f"(conexión={CONNECT_TIMEOUT}s, lectura={READ_TIMEOUT}s). Detalle: {timeout_err}"
        ) from timeout_err
    except _TRANSPORT_ERRORS as sock_err:
        raise ConnectionError(
            f"Error de transporte/socket al comunicar con {host}: {sock_err}"
        ) from sock_err
    finally:
        if plain_password is not None:
            del plain_password
        if plain_secret is not None:
            del plain_secret


def apply_configuration(
    device_dict: dict[str, Any],
    config_text: str | list[str],
) -> dict[str, Any]:
    """
    Aplica comandos de configuración (cambios o rollback) a un dispositivo Cisco IOS,
    elevando a modo privilegiado con enable(), enviando el set de configuración y
    persistiendo los cambios en NVRAM con save_config().

    Args:
        device_dict (Dict[str, Any]): Datos del dispositivo.
        config_text (Union[str, List[str]]): Bloque de configuración o lista de comandos.

    Returns:
        Dict[str, Any]: Resultado estandarizado con status, host, commands_applied y output.

    Raises:
        ValueError: Si config_text está vacío o no contiene comandos válidos.
        NetmikoAuthenticationException: Si las credenciales SSH/enable son rechazadas.
        NetmikoTimeoutException: Si el dispositivo supera el timeout de lectura (read_timeout=60).
        ConnectionError: Si ocurre un fallo de transporte/socket/Netmiko.
    """
    if config_text is None:
        raise ValueError("La configuración a aplicar no puede ser None.")

    if isinstance(config_text, str):
        raw_lines = config_text.replace("\r\n", "\n").splitlines()
    elif isinstance(config_text, list):
        raw_lines = [str(line) for line in config_text]
    else:
        raise TypeError("config_text debe ser una cadena (str) o una lista de comandos (List[str]).")

    config_lines: list[str] = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("!"):
            continue
        if stripped.startswith("Building configuration..."):
            continue
        if stripped.startswith("Current configuration :"):
            continue
        if stripped.lower() == "end":
            continue
        config_lines.append(line.rstrip())

    if not config_lines:
        raise ValueError("No hay comandos de configuración válidos para aplicar.")

    plain_password: str | None = None
    plain_secret: str | None = None
    host = device_dict.get("ip_address", "desconocido")

    try:
        plain_password = _resolve_credentials(device_dict)
        plain_secret = _resolve_secret(device_dict)
        params = _build_netmiko_params(device_dict, plain_password, plain_secret)

        with ConnectHandler(**params) as conn:
            conn.clear_buffer()
            if not conn.check_enable_mode():
                conn.enable()
            config_output = conn.send_config_set(config_lines, read_timeout=CONFIG_READ_TIMEOUT)
            save_output = conn.save_config()

        combined_output = f"{config_output or ''}\n{save_output or ''}".strip()
        return {
            "status": "success",
            "host": host,
            "commands_applied": len(config_lines),
            "output": combined_output,
        }

    except NetmikoAuthenticationException as auth_err:
        raise NetmikoAuthenticationException(
            f"Fallo de autenticación SSH/enable al aplicar configuración en {host}."
        ) from auth_err
    except NetmikoTimeoutException as timeout_err:
        raise NetmikoTimeoutException(
            f"Timeout al aplicar configuración en {host} "
            f"(conexión={CONNECT_TIMEOUT}s, lectura={CONFIG_READ_TIMEOUT}s). Detalle: {timeout_err}"
        ) from timeout_err
    except _TRANSPORT_ERRORS as sock_err:
        raise ConnectionError(
            f"Error de transporte/socket al aplicar configuración en {host}: {sock_err}"
        ) from sock_err
    finally:
        if plain_password is not None:
            del plain_password
        if plain_secret is not None:
            del plain_secret


def calculate_diff(
    old_config: str,
    new_config: str,
    fromfile: str = "anterior",
    tofile: str = "actual",
) -> list[str]:
    """
    Calcula las diferencias unificadas entre dos configuraciones usando difflib.unified_diff.

    Args:
        old_config (str): Configuración base (versión anterior).
        new_config (str): Configuración comparada (versión actual).
        fromfile (str): Etiqueta origen (default: 'anterior').
        tofile (str): Etiqueta destino (default: 'actual').

    Returns:
        List[str]: Lista de líneas del diff con encabezados ('---', '+++'),
                   bloques ('@@') y marcadores ('+', '-', ' '). Retorna [] si son idénticas.
    """
    if not isinstance(old_config, str) or not isinstance(new_config, str):
        raise TypeError("Ambas configuraciones deben ser cadenas de texto (str).")

    # Normalizar saltos de línea y separar sin conservar caracteres de escape de salto
    old_lines = old_config.replace("\r\n", "\n").splitlines(keepends=False)
    new_lines = new_config.replace("\r\n", "\n").splitlines(keepends=False)

    diff_generator = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
    )

    return list(diff_generator)
