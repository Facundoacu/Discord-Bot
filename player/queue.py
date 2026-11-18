from __future__ import annotations

import random
from collections import deque
from enum import Enum

from .track import Track


class LoopMode(str, Enum):
    OFF = "off"
    TRACK = "track"
    QUEUE = "queue"


_NEXT_LOOP = {
    LoopMode.OFF: LoopMode.TRACK,
    LoopMode.TRACK: LoopMode.QUEUE,
    LoopMode.QUEUE: LoopMode.OFF,
}


class TrackQueue:
    """Cola de reproducción de un servidor.

    `current` es lo que está sonando; los pendientes viven aparte. Es lógica
    pura (sin Discord ni asyncio) para poder testearla fácil.
    """

    def __init__(self, max_size: int = 200):
        self.max_size = max_size
        self.current: Track | None = None
        self.loop: LoopMode = LoopMode.OFF
        self._items: deque[Track] = deque()

    def __len__(self) -> int:
        return len(self._items)

    def upcoming(self) -> list[Track]:
        return list(self._items)

    # --- agregar / sacar ------------------------------------------------

    def add_many(self, tracks: list[Track]) -> int:
        """Agrega hasta llenar la cola. Devuelve cuántos entraron."""
        added = 0
        for track in tracks:
            if len(self._items) >= self.max_size:
                break
            self._items.append(track)
            added += 1
        return added

    def remove(self, position: int) -> Track:
        """Saca el track en la posición `position` (1 = el próximo)."""
        if not 1 <= position <= len(self._items):
            raise IndexError(f"Posición inválida: {position}")
        items = list(self._items)
        track = items.pop(position - 1)
        self._items = deque(items)
        return track

    def clear(self, *, include_current: bool = False) -> int:
        """Vacía los pendientes (y opcionalmente el actual). Devuelve cuántos sacó."""
        removed = len(self._items)
        self._items.clear()
        if include_current:
            self.current = None
        return removed

    def drop_autoplay(self) -> int:
        """Saca los pendientes que puso el autoplay. Devuelve cuántos sacó."""
        kept = deque(t for t in self._items if not t.autoplay)
        removed = len(self._items) - len(kept)
        self._items = kept
        return removed

    def shuffle(self) -> None:
        items = list(self._items)
        random.shuffle(items)
        self._items = deque(items)

    # --- avance ---------------------------------------------------------

    def advance(self, *, skipped: bool = False) -> Track | None:
        """Pasa al siguiente track y lo devuelve (None si no queda nada).

        Se llama cuando el track actual terminó o fue salteado. `skipped`
        importa solo con loop de canción: un skip tiene que avanzar igual.
        """
        prev = self.current
        if prev is not None:
            if self.loop is LoopMode.TRACK and not skipped:
                return prev
            if self.loop is LoopMode.QUEUE:
                self._items.append(prev)
        self.current = self._items.popleft() if self._items else None
        return self.current

    def drop_current(self) -> None:
        """Descarta el actual sin reencolarlo (p. ej. si no se pudo reproducir)."""
        self.current = None

    def peek_next(self) -> Track | None:
        """El track que sonaría después del actual (para precargarlo)."""
        if self.loop is LoopMode.TRACK and self.current is not None:
            return self.current
        if self._items:
            return self._items[0]
        if self.loop is LoopMode.QUEUE:
            return self.current
        return None

    # --- loop -----------------------------------------------------------

    def cycle_loop(self) -> LoopMode:
        self.loop = _NEXT_LOOP[self.loop]
        return self.loop
