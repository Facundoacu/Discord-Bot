"""Configuración por variables de entorno (con defaults razonables)."""
import os


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} tiene que ser un número entero, no {raw!r}") from None


# Tope de temas pendientes por servidor.
MAX_QUEUE_SIZE = _int("MAX_QUEUE_SIZE", 200)
# Máximo de temas que se importan de una playlist/álbum de una vez.
MAX_PLAYLIST_TRACKS = _int("MAX_PLAYLIST_TRACKS", 100)
# Segundos sin reproducir nada antes de salir del canal de voz.
IDLE_TIMEOUT_SECONDS = _int("IDLE_TIMEOUT_SECONDS", 300)
# Segundos que espera el bot antes de irse si se queda solo en el canal.
ALONE_TIMEOUT_SECONDS = _int("ALONE_TIMEOUT_SECONDS", 60)
# Volumen inicial (0-100).
DEFAULT_VOLUME = min(100, max(0, _int("DEFAULT_VOLUME", 50))) / 100
# Autoplay: al terminarse la cola, seguir con temas parecidos al último.
AUTOPLAY = os.getenv("AUTOPLAY", "true").strip().lower() not in ("0", "false", "no", "off")
