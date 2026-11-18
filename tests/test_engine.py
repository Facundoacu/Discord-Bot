import asyncio

from player.engine import PlayerEngine
from player.errors import SourceError, StreamError
from player.queue import LoopMode
from player.track import Track


class FakeBackend:
    """Simula el cliente de voz: `play` guarda el callback `after` y
    `finish()` simula que el tema terminó (o que lo cortaron con stop)."""

    def __init__(self):
        self.connected = True
        self.playing: Track | None = None
        self.paused = False
        self.after = None
        self.volume = None
        self.played: list[str] = []

    def is_connected(self): return self.connected
    def is_playing(self): return self.playing is not None and not self.paused
    def is_paused(self): return self.playing is not None and self.paused

    def play(self, track, volume, after):
        assert self.playing is None, "Already playing audio"  # igual que discord.py
        self.playing, self.after, self.volume = track, after, volume
        self.paused = False
        self.played.append(track.title)

    def stop(self): self.finish()
    def pause(self): self.paused = True
    def resume(self): self.paused = False
    def set_volume(self, v): self.volume = v

    def finish(self, error=None):
        if self.playing is None:
            return
        after, self.playing, self.paused = self.after, None, False
        after(error)


def mk(n: int) -> Track:
    return Track(title=f"t{n}", requester="u", query=f"q{n}")


async def settle():
    for _ in range(10):
        await asyncio.sleep(0)


class Harness:
    def __init__(self, *, fail: set[str] | None = None, resolve_delay: float = 0):
        self.backend = FakeBackend()
        self.resolved: list[str] = []
        self.notes: list[str] = []
        self.started: list[str] = []
        self.idle_calls = 0
        self.fail = fail or set()
        self.resolve_delay = resolve_delay
        self.engine = PlayerEngine(
            backend=self.backend,
            resolver=self.resolve,
            notify=self.notify,
            on_track_start=self.on_start,
            on_idle=self.on_idle,
        )

    async def resolve(self, track: Track):
        if not track.needs_resolve():
            return
        if self.resolve_delay:
            await asyncio.sleep(self.resolve_delay)
        if track.title in self.fail:
            raise SourceError("no pude obtener el audio")
        self.resolved.append(track.title)
        track.mark_resolved(f"http://stream/{track.title}")

    async def notify(self, text): self.notes.append(text)
    async def on_start(self, track): self.started.append(track.title)

    async def on_idle(self):
        self.idle_calls += 1


def run(coro):
    return asyncio.run(coro)


def test_plays_in_order_then_reports_idle():
    async def scenario():
        h = Harness()
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        assert h.backend.playing.title == "t1"
        h.backend.finish(); await settle()
        assert h.backend.playing.title == "t2"
        h.backend.finish(); await settle()
        assert h.backend.playing is None
        assert h.idle_calls == 1
        assert h.started == ["t1", "t2"]
    run(scenario())


def test_start_if_idle_is_noop_while_busy():
    async def scenario():
        h = Harness()
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        await h.engine.start_if_idle()
        assert h.backend.played == ["t1"]
        assert len(h.engine.queue) == 1
    run(scenario())


def test_next_track_is_prefetched_while_current_plays():
    async def scenario():
        h = Harness()
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        await settle()
        assert h.resolved == ["t1", "t2"]  # t2 ya está lista antes de que termine t1
    run(scenario())


def test_track_added_while_playing_is_prefetched_right_away():
    async def scenario():
        h = Harness()
        h.engine.add([mk(1)])
        await h.engine.start_if_idle()
        await settle()
        h.engine.add([mk(2)])  # llega mientras suena t1
        await settle()
        assert h.resolved == ["t1", "t2"]  # un skip ya no espera la extracción
    run(scenario())


def test_skip_advances_even_with_loop_track():
    async def scenario():
        h = Harness()
        h.engine.queue.loop = LoopMode.TRACK
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        skipped = h.engine.skip()
        await settle()
        assert skipped.title == "t1"
        assert h.backend.playing.title == "t2"
    run(scenario())


def test_loop_track_replays_the_same_song():
    async def scenario():
        h = Harness()
        h.engine.queue.loop = LoopMode.TRACK
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        h.backend.finish(); await settle()
        assert h.backend.played == ["t1", "t1"]
    run(scenario())


def test_loop_queue_cycles():
    async def scenario():
        h = Harness()
        h.engine.queue.loop = LoopMode.QUEUE
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        for _ in range(3):
            h.backend.finish(); await settle()
        assert h.backend.played == ["t1", "t2", "t1", "t2"]
    run(scenario())


def test_unplayable_track_is_reported_dropped_and_next_one_plays():
    async def scenario():
        h = Harness(fail={"t1"})
        h.engine.queue.loop = LoopMode.TRACK  # el caso que podría colgarse en loop infinito
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        assert h.backend.playing.title == "t2"
        assert len(h.notes) == 1 and "t1" in h.notes[0]
    run(scenario())


def test_all_tracks_failing_ends_in_idle_without_hanging():
    async def scenario():
        h = Harness(fail={"t1", "t2", "t3"})
        h.engine.queue.loop = LoopMode.QUEUE
        h.engine.add([mk(1), mk(2), mk(3)])
        await asyncio.wait_for(h.engine.start_if_idle(), timeout=2)
        assert h.backend.playing is None
        assert len(h.notes) == 3
        assert h.idle_calls == 1
    run(scenario())


def test_play_racing_with_track_end_does_not_skip_a_song():
    """El tema termina (after ya agendó el avance) y justo antes de que corra
    llega un /play: no debe avanzarse dos veces."""
    async def scenario():
        h = Harness()
        h.engine.add([mk(1), mk(2), mk(3)])
        await h.engine.start_if_idle()
        h.backend.finish()                 # agenda _track_finished, todavía no corrió
        await h.engine.start_if_idle()     # /play llega primero y arranca t2
        await settle()                     # ahora corre _track_finished: debe ser no-op
        assert h.backend.playing.title == "t2"
        assert [t.title for t in h.engine.queue.upcoming()] == ["t3"]
    run(scenario())


def test_stop_clears_queue_and_goes_idle():
    async def scenario():
        h = Harness()
        h.engine.queue.loop = LoopMode.QUEUE
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        h.engine.stop()
        await settle()
        assert h.backend.playing is None
        assert len(h.engine.queue) == 0 and h.engine.queue.current is None
        assert h.idle_calls == 1
    run(scenario())


def test_disconnected_backend_clears_queue_and_plays_nothing():
    async def scenario():
        h = Harness()
        h.backend.connected = False
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        assert h.backend.played == []
        assert len(h.engine.queue) == 0
    run(scenario())


def test_pause_resume_toggle():
    async def scenario():
        h = Harness()
        assert h.engine.toggle_pause() is None
        h.engine.add([mk(1)])
        await h.engine.start_if_idle()
        assert h.engine.toggle_pause() is True and h.backend.paused
        assert h.engine.busy  # en pausa cuenta como ocupado
        await h.engine.start_if_idle()  # no debe avanzar estando en pausa
        assert h.backend.played == ["t1"]
        assert h.engine.toggle_pause() is False and not h.backend.paused
    run(scenario())


def test_skip_while_paused_moves_on():
    async def scenario():
        h = Harness()
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        h.engine.pause()
        assert h.engine.skip().title == "t1"
        await settle()
        assert h.backend.playing.title == "t2"
    run(scenario())


def test_callback_errors_do_not_break_playback():
    async def scenario():
        h = Harness()

        async def boom(track):
            raise RuntimeError("Discord se cayó")

        h.engine._on_track_start = boom
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        assert h.backend.playing.title == "t1"
        h.backend.finish(); await settle()
        assert h.backend.playing.title == "t2"
    run(scenario())


def test_set_volume_applies_to_backend_and_future_tracks():
    async def scenario():
        h = Harness()
        h.engine.set_volume(30)
        assert h.backend.volume == 0.3
        h.engine.add([mk(1)])
        await h.engine.start_if_idle()
        assert h.backend.volume == 0.3
    run(scenario())


# --- autoplay ---------------------------------------------------------------

def auto(n: int) -> Track:
    return Track(title=f"a{n}", requester="🎲", query=f"auto{n}", autoplay=True)


def with_autoplay(h: Harness, batches: list[list[Track]]) -> list[set[str]]:
    """Activa el autoplay con un recomendador falso; devuelve los `exclude` que recibió."""
    seen: list[set[str]] = []

    async def recommender(seed, exclude):
        seen.append(set(exclude))
        return batches.pop(0) if batches else []

    h.engine._recommender = recommender
    h.engine.autoplay = True
    return seen


def test_autoplay_searches_while_last_track_plays_and_continues():
    async def scenario():
        h = Harness()
        seen = with_autoplay(h, [[auto(1), auto(2)]])
        h.engine.add([mk(1)])
        await h.engine.start_if_idle()
        await settle()
        assert len(seen) == 1 and "q1" in seen[0]  # buscó mientras sonaba t1, sabiendo que t1 ya sonó
        assert h.resolved == ["t1", "a1"]          # y precargó el primero
        h.backend.finish()
        await settle()
        assert h.backend.playing.title == "a1"
        assert h.idle_calls == 0
    run(scenario())


def test_autoplay_waits_for_search_if_queue_ends_first():
    async def scenario():
        h = Harness()
        with_autoplay(h, [[], [auto(1)]])  # la búsqueda en segundo plano no trajo nada
        h.engine.add([mk(1)])
        await h.engine.start_if_idle()
        await settle()
        h.backend.finish()  # al terminarse la cola vuelve a buscar
        await settle()
        assert h.backend.playing.title == "a1"
    run(scenario())


def test_user_tracks_jump_ahead_of_autoplay_ones():
    async def scenario():
        h = Harness()
        with_autoplay(h, [[auto(1), auto(2)]])
        h.engine.add([mk(1)])
        await h.engine.start_if_idle()
        await settle()
        h.engine.add([mk(2)])
        assert [t.title for t in h.engine.queue.upcoming()] == ["t2"]
    run(scenario())


def test_stop_does_not_trigger_autoplay():
    async def scenario():
        h = Harness()
        seen = with_autoplay(h, [[auto(1)], [auto(2)]])
        h.engine.add([mk(1)])
        await h.engine.start_if_idle()
        await settle()
        h.engine.stop()
        await settle()
        assert h.backend.playing is None and len(h.engine.queue) == 0
        assert len(seen) == 1  # solo la búsqueda de cuando arrancó t1
    run(scenario())


def test_without_autoplay_queue_end_goes_idle():
    async def scenario():
        h = Harness()
        seen = with_autoplay(h, [[auto(1)]])
        h.engine.autoplay = False
        h.engine.add([mk(1)])
        await h.engine.start_if_idle()
        await settle()
        h.backend.finish()
        await settle()
        assert seen == [] and h.backend.playing is None and h.idle_calls == 1
    run(scenario())


def test_turning_autoplay_off_drops_its_tracks():
    async def scenario():
        h = Harness()
        with_autoplay(h, [[auto(1), auto(2)]])
        h.engine.add([mk(1)])
        await h.engine.start_if_idle()
        await settle()
        assert len(h.engine.queue) == 2
        h.engine.set_autoplay(False)
        assert len(h.engine.queue) == 0
    run(scenario())


# --- reintento si falla la descarga ------------------------------------------

def test_failed_download_is_retried_once_with_a_fresh_resolution():
    async def scenario():
        h = Harness()
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        await settle()
        h.backend.finish(StreamError("403"))  # YouTube rechazó la descarga
        await settle()
        assert h.backend.playing.title == "t1"           # el mismo tema, de nuevo
        assert h.backend.played == ["t1", "t1"]
        assert h.resolved.count("t1") == 2               # con una resolución nueva
        assert h.started == ["t1"] and h.notes == []     # sin re-anunciar ni avisar
    run(scenario())


def test_failed_download_twice_skips_and_tells_the_user():
    async def scenario():
        h = Harness()
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        await settle()
        h.backend.finish(StreamError("403"))
        await settle()
        h.backend.finish(StreamError("403"))  # el reintento también falló
        await settle()
        assert h.backend.playing.title == "t2"
        assert len(h.notes) == 1 and "t1" in h.notes[0] and "YouTube" in h.notes[0]
    run(scenario())


def test_skip_is_never_retried_even_if_download_was_failing():
    async def scenario():
        h = Harness()
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        await settle()
        h.engine._skip_requested = True
        h.backend.finish(StreamError("403"))
        await settle()
        assert h.backend.playing.title == "t2" and h.notes == []
    run(scenario())


def test_other_playback_errors_just_advance():
    async def scenario():
        h = Harness()
        h.engine.add([mk(1), mk(2)])
        await h.engine.start_if_idle()
        await settle()
        h.backend.finish(RuntimeError("ffmpeg murió"))
        await settle()
        assert h.backend.played == ["t1", "t2"]
    run(scenario())
