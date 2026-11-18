"""Carga de configuración que mantiene los secretos fuera del entorno.

Los secretos (token de Discord, credenciales de Spotify) quedan solo en la
memoria de este proceso:

  * no van a `os.environ`, así que no los heredan los procesos hijos
    (yt-dlp, ffmpeg) ni se ven con `docker inspect`;
  * `RedactSecrets` los tapa en cualquier línea de log, por si una librería
    llegara a imprimirlos.

En Docker el archivo llega como secreto de solo lectura en
/run/secrets/bot_env (ver docker-compose.yml); fuera de Docker se lee el
.env de la carpeta del bot.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import dotenv_values

SECRET_KEYS = ("DISCORD_TOKEN", "SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET")
DOCKER_SECRET_PATH = Path("/run/secrets/bot_env")

_secrets: dict[str, str] = {}


def _config_path() -> Path | None:
    explicit = os.getenv("BOT_ENV_FILE")
    for candidate in (explicit, DOCKER_SECRET_PATH, ".env"):
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def load() -> Path | None:
    """Lee la configuración. Los ajustes comunes van a `os.environ` (sin pisar
    lo que ya esté definido); los secretos, solo a memoria."""
    path = _config_path()
    values = dotenv_values(path) if path else {}
    for key, value in values.items():
        if value is None:
            continue
        if key in SECRET_KEYS:
            _secrets[key] = value
        else:
            os.environ.setdefault(key, value)
    # Si algún secreto vino como variable de entorno (p. ej. otro tipo de
    # deploy), se toma y se borra del entorno para que no se herede.
    for key in SECRET_KEYS:
        if key in os.environ:
            value = os.environ.pop(key)
            _secrets.setdefault(key, value)
    return path


def get(key: str) -> str | None:
    """El secreto `key`, o None si no está configurado."""
    value = _secrets.get(key, "").strip()
    return value or None


class RedactSecrets(logging.Filter):
    """Reemplaza cualquier secreto cargado por *** en los mensajes de log."""

    def filter(self, record: logging.LogRecord) -> bool:
        values = [v for v in (get(k) for k in SECRET_KEYS) if v and len(v) >= 6]
        if not values:
            return True
        message = record.getMessage()
        redacted = message
        for value in values:
            redacted = redacted.replace(value, "***")
        if redacted != message:
            record.msg, record.args = redacted, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            for value in values:
                record.exc_text = record.exc_text.replace(value, "***")
        return True


def install_log_redaction() -> None:
    """Agrega el filtro a todos los handlers del logger raíz."""
    redactor = RedactSecrets()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
