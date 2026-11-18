from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

# Las URLs directas de audio de YouTube vencen (en la práctica a las ~6 h).
# Si un track estuvo resuelto más tiempo que esto, se vuelve a resolver
# antes de reproducirlo.
STREAM_TTL_SECONDS = 3600


@dataclass(eq=False)  # identidad, no igualdad: dos pedidos del mismo tema son tracks distintos
class Track:
    title: str
    requester: str
    # Lo que se le pasa a yt-dlp: una URL o un "ytsearch1:..." para buscar.
    query: str
    webpage_url: str | None = None
    duration: int | None = None
    # Lo agregó el autoplay (no un usuario): cede su lugar a lo que pidan.
    autoplay: bool = False
    # Se completan al resolver (ver sources.ensure_resolved).
    stream_url: str | None = None
    resolved_at: float | None = None
    # Info completa de yt-dlp. YouTube rechaza (403) que ffmpeg abra la URL
    # directa, así que el audio se baja con yt-dlp reusando esta info.
    info: dict | None = field(default=None, repr=False)
    # Tarea de resolución en curso; evita resolver dos veces el mismo track
    # cuando coinciden el prefetch y la reproducción.
    resolving: asyncio.Task | None = field(default=None, repr=False)

    def needs_resolve(self, now: float | None = None) -> bool:
        if self.stream_url is None or self.resolved_at is None:
            return True
        now = time.monotonic() if now is None else now
        return (now - self.resolved_at) > STREAM_TTL_SECONDS

    def mark_resolved(self, stream_url: str, now: float | None = None, info: dict | None = None) -> None:
        self.stream_url = stream_url
        self.info = info
        self.resolved_at = time.monotonic() if now is None else now
