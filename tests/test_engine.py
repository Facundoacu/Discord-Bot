import asyncio

from player.engine import PlayerEngine
from player.errors import SourceError
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

    def finish(self):
        if self.playing is None:
            return
        after, self.playing, self.paused = self.after, None, False
        after(None)


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
