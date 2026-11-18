import pytest

from player.queue import LoopMode, TrackQueue
from player.track import STREAM_TTL_SECONDS, Track


def mk(n: int) -> Track:
    return Track(title=f"t{n}", requester="u", query=f"q{n}")


def titles(q: TrackQueue) -> list[str]:
    return [t.title for t in q.upcoming()]


def test_advance_walks_the_queue_in_order():
    q = TrackQueue()
    q.add_many([mk(1), mk(2)])
    assert q.advance().title == "t1"
    assert q.advance().title == "t2"
    assert q.advance() is None
    assert q.current is None


def test_loop_off_does_not_requeue():
    q = TrackQueue()
    q.add_many([mk(1)])
    q.advance()
    assert q.advance() is None
    assert len(q) == 0


def test_loop_track_replays_on_natural_end():
    q = TrackQueue()
    q.loop = LoopMode.TRACK
    q.add_many([mk(1), mk(2)])
    first = q.advance()
    assert q.advance() is first
    assert q.advance() is first


def test_loop_track_still_advances_on_skip():
    q = TrackQueue()
    q.loop = LoopMode.TRACK
    q.add_many([mk(1), mk(2)])
    q.advance()
    assert q.advance(skipped=True).title == "t2"


def test_loop_queue_cycles_through_everything():
    q = TrackQueue()
    q.loop = LoopMode.QUEUE
    q.add_many([mk(1), mk(2)])
    seen = [q.advance().title for _ in range(5)]
    assert seen == ["t1", "t2", "t1", "t2", "t1"]


def test_loop_queue_with_single_track_repeats_it():
    q = TrackQueue()
    q.loop = LoopMode.QUEUE
    q.add_many([mk(1)])
    assert [q.advance().title for _ in range(3)] == ["t1", "t1", "t1"]


def test_drop_current_does_not_requeue_even_with_loop_queue():
    q = TrackQueue()
    q.loop = LoopMode.QUEUE
    q.add_many([mk(1), mk(2)])
    q.advance()
    q.drop_current()
    assert q.advance().title == "t2"
    assert titles(q) == []


def test_peek_next_for_each_mode():
    q = TrackQueue()
    q.add_many([mk(1), mk(2)])
    q.advance()
    assert q.peek_next().title == "t2"

    q.loop = LoopMode.TRACK
    assert q.peek_next().title == "t1"

    q.loop = LoopMode.QUEUE
    q.clear()
    assert q.peek_next().title == "t1"

    q.loop = LoopMode.OFF
    assert q.peek_next() is None


def test_add_many_respects_max_size():
    q = TrackQueue(max_size=3)
    assert q.add_many([mk(i) for i in range(5)]) == 3
    assert len(q) == 3
    assert q.add_many([mk(9)]) == 0


def test_remove_is_one_based_and_validates():
    q = TrackQueue()
    q.add_many([mk(1), mk(2), mk(3)])
    assert q.remove(2).title == "t2"
    assert titles(q) == ["t1", "t3"]
    for bad in (0, 3, -1):
        with pytest.raises(IndexError):
            q.remove(bad)


def test_clear_optionally_drops_current():
    q = TrackQueue()
    q.add_many([mk(1), mk(2), mk(3)])
    q.advance()
    assert q.clear() == 2
    assert q.current is not None
    q.clear(include_current=True)
    assert q.current is None


def test_shuffle_keeps_the_same_tracks():
    q = TrackQueue()
    tracks = [mk(i) for i in range(20)]
    q.add_many(tracks)
    q.shuffle()
    assert sorted(titles(q)) == sorted(t.title for t in tracks)
    assert len(q) == 20


def test_cycle_loop_goes_off_track_queue_off():
    q = TrackQueue()
    assert [q.cycle_loop() for _ in range(4)] == [
        LoopMode.TRACK, LoopMode.QUEUE, LoopMode.OFF, LoopMode.TRACK,
    ]


def test_same_song_requested_twice_stays_two_distinct_tracks():
    a, b = mk(1), mk(1)
    assert a is not b and a != b


def test_stream_url_expires_after_ttl():
    t = mk(1)
    assert t.needs_resolve()
    t.mark_resolved("http://x", now=1000.0)
    assert not t.needs_resolve(now=1000.0 + STREAM_TTL_SECONDS - 1)
    assert t.needs_resolve(now=1000.0 + STREAM_TTL_SECONDS + 1)


def test_invalidate_forces_a_fresh_resolution():
    t = Track(title="x", requester="u", query="q")
    t.mark_resolved("http://x", now=1000.0, info={"url": "http://x"})
    t.invalidate()
    assert t.needs_resolve() and t.info is None and t.stream_url is None
