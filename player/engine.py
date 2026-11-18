"""Motor de reproducción: orquesta cola, resolución, prefetch y avance.

No conoce Discord: habla con un `PlayerBackend` (en producción, el cliente de
voz de discord.py; en los tests, un fake). Eso permite testear toda la
lógica de concurrencia (skip, loops, carreras entre `/play` y el fin de un
tema) sin conectarse a nada.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Protocol

from .errors import SourceError
from .queue import LoopMode, TrackQueue
from .text import escape_md, truncate
from .track import Track

log = logging.getLogger("engine")

AfterCallback = Callable[[Exception | None], None]
# Dado el último tema y las claves de lo que ya sonó, devuelve temas parecidos.
Recommender = Callable[[Track, set[str]], Awaitable[list[Track]]]
# Cuántos temas recuerda el autoplay para no repetirlos.
HISTORY_SIZE = 100


def _default_key(track: Track) -> str:
    return track.webpage_url or track.query


class PlayerBackend(Protocol):
    def is_connected(self) -> bool: ...
    def is_playing(self) -> bool: ...
    def is_paused(self) -> bool: ...
    def play(self, track: Track, volume: float, after: AfterCallback) -> None: ...
    def stop(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def set_volume(self, volume: float) -> None: ...


Resolver = Callable[[Track], Awaitable[None]]


class PlayerEngine:
    def __init__(
        self,
        *,
        backend: PlayerBackend,
        resolver: Resolver,
        notify: Callable[[str], Awaitable[None]] | None = None,
        on_track_start: Callable[[Track], Awaitable[None]] | None = None,
        on_idle: Callable[[], Awaitable[None]] | None = None,
        max_queue: int = 200,
        volume: float = 0.5,
        recommender: Recommender | None = None,
        autoplay: bool = False,
        track_key: Callable[[Track], str] = _default_key,
    ):
        self.backend = backend
        self.queue = TrackQueue(max_size=max_queue)
        self.volume = volume
        # Autoplay: al quedarse sin cola, sigue con temas parecidos al último.
        self.autoplay = autoplay
        self._recommender = recommender
        self._track_key = track_key
        self._history: deque[str] = deque(maxlen=HISTORY_SIZE)
        self._last_played: Track | None = None
        self._autofill_task: asyncio.Task | None = None
        self._resolver = resolver
        self._notify = notify
        self._on_track_start = on_track_start
        self._on_idle = on_idle
        self._loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._skip_requested = False
        self._background: set[asyncio.Task] = set()

    # --- estado ---------------------------------------------------------

    @property
    def busy(self) -> bool:
        """Hay algo sonando o en pausa."""
        return self.backend.is_playing() or self.backend.is_paused()

    # --- comandos -------------------------------------------------------

    def add(self, tracks: list[Track]) -> int:
        # Lo que pide un usuario pasa adelante de lo que puso el autoplay.
        self._cancel_autofill()
        self.queue.drop_autoplay()
        added = self.queue.add_many(tracks)
        # Si ya suena algo y lo agregado quedó como próximo, resolverlo ya:
        # así un skip inmediato no espera la extracción.
        if added and self.busy:
            self._prefetch_next()
        return added

    async def start_if_idle(self) -> None:
        """Arranca el próximo tema si no está sonando nada."""
        await self._advance()

    def skip(self) -> Track | None:
        """Saltea el tema actual. Devuelve el salteado (None si no sonaba nada)."""
        if not self.busy:
            return None
        skipped = self.queue.current
        self._skip_requested = True
        self.backend.stop()  # dispara `after` -> _track_finished
        return skipped

    def stop(self) -> None:
        """Corta todo y vacía la cola."""
        # Sin semilla el autoplay no rellena: /stop es "basta de música".
        self._last_played = None
        self._cancel_autofill()
        self.queue.clear(include_current=True)
        self._skip_requested = False
        if self.busy:
            self.backend.stop()

    def pause(self) -> bool:
        if self.backend.is_playing():
            self.backend.pause()
            return True
        return False

    def resume(self) -> bool:
        if self.backend.is_paused():
            self.backend.resume()
            return True
        return False

    def toggle_pause(self) -> bool | None:
        """True = quedó en pausa, False = se reanudó, None = no había nada."""
        if self.backend.is_paused():
            self.backend.resume()
            return False
        if self.backend.is_playing():
            self.backend.pause()
            return True
        return None

    def set_volume(self, percent: int) -> None:
        self.volume = min(100, max(0, percent)) / 100
        self.backend.set_volume(self.volume)

    def shutdown(self) -> None:
        """Libera tareas y vacía la cola (el motor puede reutilizarse)."""
        for task in list(self._background):
            task.cancel()
        self._background.clear()
        self._autofill_task = None
        self._last_played = None
        self._skip_requested = False
        self.queue.clear(include_current=True)

    # --- avance ---------------------------------------------------------

    def _after(self, error: Exception | None) -> None:
        # Corre en el thread de audio de discord.py, no en el event loop.
        if error:
            log.error("Error durante la reproducción: %s", error)
        try:
            fut = asyncio.run_coroutine_threadsafe(self._track_finished(), self._loop)
        except RuntimeError:  # el loop ya se cerró
            return
        fut.add_done_callback(self._log_future_error)

    @staticmethod
    def _log_future_error(fut) -> None:
        if not fut.cancelled() and fut.exception() is not None:
            log.error("Falló el avance a la siguiente canción", exc_info=fut.exception())

    async def _track_finished(self) -> None:
        skipped, self._skip_requested = self._skip_requested, False
        await self._advance(skipped=skipped)

    async def _advance(self, *, skipped: bool = False) -> None:
        async with self._lock:
            # Si otro camino ya arrancó un tema (p. ej. /play justo cuando
            # terminó el anterior), no avanzamos de nuevo: se saltearía uno.
            if self.busy:
                return
            while True:
                if not self.backend.is_connected():
                    self.queue.clear(include_current=True)
                    return

                track = self.queue.advance(skipped=skipped)
                skipped = False
                if track is None:
                    # Cola vacía: con autoplay, rellenar con temas parecidos
                    # (normalmente ya se buscaron mientras sonaba el último).
                    if await self._autofill_now():
                        continue
                    await self._safe(self._on_idle)
                    return

                try:
                    await self._resolver(track)
                    if not self.backend.is_connected():
                        continue  # se desconectó mientras resolvíamos; el chequeo de arriba limpia
                    self.backend.play(track, self.volume, self._after)
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, SourceError) else "error inesperado"
                    if not isinstance(exc, SourceError):
                        log.exception("No se pudo reproducir %r", track.title)
                    await self._safe(
                        self._notify,
                        f"No pude reproducir **{escape_md(truncate(track.title))}** "
                        f"({reason}). Sigo con el siguiente.",
                    )
                    self.queue.drop_current()
                    continue

                self._last_played = track
                self._history.append(self._track_key(track))
                if self.autoplay and self.queue.peek_next() is None:
                    self._start_autofill()  # buscar parecidos mientras suena
                self._prefetch_next()
                await self._safe(self._on_track_start, track)
                return

    # --- autoplay -------------------------------------------------------

    def set_autoplay(self, enabled: bool) -> None:
        self.autoplay = enabled
        if not enabled:
            self._cancel_autofill()
            self.queue.drop_autoplay()
        elif self.busy and self.queue.peek_next() is None:
            self._start_autofill()

    def _start_autofill(self) -> asyncio.Task | None:
        """Busca temas parecidos al último en segundo plano (una sola vez)."""
        if self._recommender is None or self._last_played is None:
            return None
        task = self._autofill_task
        if task is None or task.done():
            task = self._loop.create_task(self._autofill(self._last_played))
            self._autofill_task = task
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        return task

    async def _autofill_now(self) -> bool:
        """Espera el relleno del autoplay. True si dejó algo en la cola."""
        if not self.autoplay:
            return False
        task = self._start_autofill()
        if task is None:
            return False
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():  # lo canceló un /play o /stop, no a nosotros
                return len(self.queue) > 0
            raise
        return len(self.queue) > 0

    async def _autofill(self, seed: Track) -> None:
        try:
            tracks = await self._recommender(seed, set(self._history))
        except Exception:
            log.warning("Autoplay: no pude buscar temas parecidos a %r", seed.title, exc_info=True)
            return
        # Si mientras tanto hubo /stop o se apagó el autoplay, no agregar.
        if not self.autoplay or self._last_played is not seed or not tracks:
            return
        added = self.queue.add_many(tracks)
        log.info("Autoplay: agregué %d temas parecidos a %r", added, seed.title)
        if added and self.busy:
            self._prefetch_next()

    def _cancel_autofill(self) -> None:
        task, self._autofill_task = self._autofill_task, None
        if task is not None and not task.done():
            task.cancel()

    # --- prefetch -------------------------------------------------------

    def _prefetch_next(self) -> None:
        """Resuelve en segundo plano el próximo tema mientras suena el actual,
        para que el cambio de canción no tenga silencio."""
        nxt = self.queue.peek_next()
        if nxt is None or not nxt.needs_resolve():
            return
        task = self._loop.create_task(self._prefetch(nxt))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _prefetch(self, track: Track) -> None:
        try:
            await self._resolver(track)
        except Exception:
            # Se reintenta al llegar su turno y ahí sí se avisa al usuario.
            log.debug("Prefetch falló para %r", track.title, exc_info=True)

    # --- util -----------------------------------------------------------

    async def _safe(self, fn, *args) -> None:
        """Llama un callback opcional sin dejar que una falla corte la reproducción."""
        if fn is None:
            return
        try:
            await fn(*args)
        except Exception:
            log.exception("Falló un callback del reproductor")


__all__ = ["PlayerEngine", "PlayerBackend", "LoopMode"]
