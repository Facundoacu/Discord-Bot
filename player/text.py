"""Formateo de texto para mensajes (puro, sin Discord)."""
from __future__ import annotations

import math
import re

from .queue import LoopMode, TrackQueue

_MD_SPECIALS = re.compile(r"([\\*_~`|>\[\]])")

LOOP_LABELS = {
    LoopMode.OFF: "desactivado",
    LoopMode.TRACK: "canción",
    LoopMode.QUEUE: "cola",
}


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "?:??"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def truncate(text: str, limit: int = 80) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def escape_md(text: str) -> str:
    """Los títulos vienen de internet: neutralizamos el markdown."""
    return _MD_SPECIALS.sub(r"\\\1", text)


def _label(title: str, limit: int = 80) -> str:
    return escape_md(truncate(title, limit))


def format_queue(queue: TrackQueue, *, page: int = 1, per_page: int = 10) -> tuple[str, int]:
    """Devuelve (texto, total_de_páginas) de la cola."""
    upcoming = queue.upcoming()
    pages = max(1, math.ceil(len(upcoming) / per_page))
    page = min(max(page, 1), pages)

    lines: list[str] = []
    if queue.current:
        cur = queue.current
        lines.append(f"**Sonando:** {_label(cur.title)} `{format_duration(cur.duration)}`")
    if not upcoming:
        lines.append("No hay más temas en la cola.")
    else:
        start = (page - 1) * per_page
        for i, track in enumerate(upcoming[start : start + per_page], start=start + 1):
            lines.append(
                f"`{i}.` {_label(track.title)} `{format_duration(track.duration)}`"
                f" · {escape_md(truncate(track.requester, 24))}"
            )

    known = [t.duration for t in upcoming if t.duration is not None]
    footer = [f"Página {page}/{pages}", f"{len(upcoming)} en cola"]
    if known:
        suffix = "+" if len(known) < len(upcoming) else ""
        footer.append(f"{format_duration(sum(known))}{suffix} en total")
    footer.append(f"loop: {LOOP_LABELS[queue.loop]}")
    lines.append("-# " + " · ".join(footer))
    return "\n".join(lines), pages
