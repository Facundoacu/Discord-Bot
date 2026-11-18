import asyncio

import pytest

from player import sources
from player.errors import SourceError
from player.track import Track


def run(coro):
    return asyncio.run(coro)


# --- parseo de entradas ---------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", ("track", "4uLU6hMCjMI75M1A2tKUQC")),
        ("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC?si=abc123", ("track", "4uLU6hMCjMI75M1A2tKUQC")),
        ("https://open.spotify.com/intl-es/album/1DFixLWuPkv3KT3TnV35m3", ("album", "1DFixLWuPkv3KT3TnV35m3")),
        ("  https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M  ", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
        ("spotify:track:4uLU6hMCjMI75M1A2tKUQC", ("track", "4uLU6hMCjMI75M1A2tKUQC")),
        ("https://open.spotify.com/artist/0TnOYISbd1XYRBk9myaseg", None),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("daft punk around the world", None),
    ],
)
def test_parse_spotify_url(text, expected):
    assert sources.parse_spotify_url(text) == expected


def test_spotify_short_links_are_detected():
    assert sources.is_spotify_short_link("https://spotify.link/AbCdEf")
    assert not sources.is_spotify_short_link("https://open.spotify.com/track/abc")


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.youtube.com/playlist?list=PL123", True),
        ("https://music.youtube.com/playlist?list=OLAK5uy", True),
        ("https://www.youtube.com/watch?v=abc&list=PL123", False),  # se trata como el video
        ("https://www.youtube.com/watch?v=abc&list=RDabc", False),  # mixes infinitos: no
        ("https://www.youtube.com/playlist", False),
        ("https://evil.example.com/playlist?list=PL123", False),
        ("not a url", False),
    ],
)
def test_is_youtube_playlist_url(url, expected):
    assert sources.is_youtube_playlist_url(url) is expected


def test_is_url():
    assert sources.is_url("https://soundcloud.com/a/b")
    assert sources.is_url("HTTP://example.com")
    assert not sources.is_url("soundcloud.com/a/b")
    assert not sources.is_url("bad bunny")


# --- Spotify: conversión y paginado con un cliente falso ---------------------

def sp_track(name, artist="Artista", ms=200_000):
    return {"name": name, "artists": [{"name": artist}], "duration_ms": ms}


class FakeSpotify:
    def __init__(self, pages=None, track=None):
        self.pages = pages or []
        self._track = track
        self.get_calls = []

    def track(self, _id): return self._track
    def album_tracks(self, _id, limit=50): return self.pages[0]
    def _get(self, path, **kw):
        self.get_calls.append(path)
        return self.pages[0]
    def next(self, page): return self.pages[page["_i"] + 1]


def make_pages(*chunks, key="item"):
    pages = []
    for i, chunk in enumerate(chunks):
        items = [{key: t} if key else t for t in chunk]
        pages.append({"items": items, "next": "more" if i < len(chunks) - 1 else None, "_i": i})
    return pages


def test_spotify_track_becomes_unresolved_track_with_duration():
    t = sources.spotify_data_to_track(sp_track("Around the World", "Daft Punk"), "ana")
    assert t.title == "Daft Punk - Around the World"
    assert t.query.startswith("ytsearch1:Daft Punk - Around the World")
    assert t.duration == 200 and t.stream_url is None and t.requester == "ana"


def test_spotify_data_without_name_is_skipped():
    assert sources.spotify_data_to_track(None, "x") is None
    assert sources.spotify_data_to_track({"artists": []}, "x") is None


def test_single_spotify_track():
    sp = FakeSpotify(track=sp_track("Song"))
    out = sources.collect_spotify_tracks(sp, "track", "id", "ana", 100)
    assert [t.title for t in out] == ["Artista - Song"]


def test_album_paginates_across_pages():
    pages = make_pages([sp_track("a"), sp_track("b")], [sp_track("c")], key=None)
    out = sources.collect_spotify_tracks(FakeSpotify(pages=pages), "album", "id", "ana", 100)
    assert [t.title for t in out] == ["Artista - a", "Artista - b", "Artista - c"]


@pytest.mark.parametrize("key", ["item", "track"])  # forma nueva (2026) y vieja
def test_playlist_reads_both_item_and_track_shapes_and_uses_new_endpoint(key):
    pages = make_pages([sp_track("a"), None, sp_track("b")], key=key)
    # una entrada con el objeto en None (tema borrado) no debe romper
    pages[0]["items"][1] = {key: None}
    sp = FakeSpotify(pages=pages)
    out = sources.collect_spotify_tracks(sp, "playlist", "pid", "ana", 100)
    assert [t.title for t in out] == ["Artista - a", "Artista - b"]
    assert sp.get_calls == ["playlists/pid/items"]


def test_playlist_respects_limit():
    pages = make_pages([sp_track(str(i)) for i in range(50)], [sp_track(str(i)) for i in range(50, 100)])
    out = sources.collect_spotify_tracks(FakeSpotify(pages=pages), "playlist", "pid", "ana", 60)
    assert len(out) == 60


def test_playlist_without_items_raises_friendly_error():
    class NoItems(FakeSpotify):
        def _get(self, path, **kw): return {"id": "x"}  # playlist ajena: 'items' ausente

    with pytest.raises(SourceError):
        sources.collect_spotify_tracks(NoItems(), "playlist", "pid", "ana", 10)


def test_spotify_without_credentials_gives_actionable_error(monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)
    with pytest.raises(SourceError, match="SPOTIFY_CLIENT_ID"):
        run(sources.build_tracks("https://open.spotify.com/track/abc123", "ana"))


def test_spotify_playlist_api_failure_explains_the_2026_limit(monkeypatch):
    class Boom:
        def _get(self, *a, **k): raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(sources, "_spotify_client", lambda: Boom())
    with pytest.raises(SourceError, match="playlists propias"):
        run(sources.build_tracks("https://open.spotify.com/playlist/abc123", "ana"))


def test_spotify_short_link_is_rejected_with_hint():
    with pytest.raises(SourceError, match="completo"):
        run(sources.build_tracks("https://spotify.link/AbCdEf", "ana"))


# --- resolución async ---------------------------------------------------------

def fake_extract(counter, delay=0.01):
    def _extract(query):
        import time
        counter.append(query)
        time.sleep(delay)
        return {"url": f"http://stream/{len(counter)}", "title": "Título real",
                "webpage_url": "http://page", "duration": 123}
    return _extract


def test_concurrent_resolutions_of_the_same_track_hit_yt_dlp_once(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "_extract_stream", fake_extract(calls))

    async def scenario():
        t = Track(title="x", requester="u", query="ytsearch1:x")
        await asyncio.gather(sources.ensure_resolved(t), sources.ensure_resolved(t), sources.ensure_resolved(t))
        await sources.ensure_resolved(t)  # ya vigente: no vuelve a llamar
        return t

    t = run(scenario())
    assert len(calls) == 1
    assert t.stream_url == "http://stream/1"


def test_resolution_keeps_known_title_but_fills_gaps(monkeypatch):
    monkeypatch.setattr(sources, "_extract_stream", fake_extract([]))

    async def scenario():
        t = Track(title="Daft Punk - Around the World", requester="u", query="ytsearch1:x")
        await sources.ensure_resolved(t)
        return t

    t = run(scenario())
    assert t.title == "Daft Punk - Around the World"
    assert t.webpage_url == "http://page" and t.duration == 123


def test_failed_resolution_raises_source_error_and_can_be_retried(monkeypatch):
    state = {"fail": True}

    def flaky(query):
        if state["fail"]:
            raise RuntimeError("HTTP 403")
        return {"url": "http://ok"}

    monkeypatch.setattr(sources, "_extract_stream", flaky)

    async def scenario():
        t = Track(title="x", requester="u", query="q")
        with pytest.raises(SourceError):
            await sources.ensure_resolved(t)
        state["fail"] = False
        await sources.ensure_resolved(t)
        return t

    assert run(scenario()).stream_url == "http://ok"


def test_plain_search_is_resolved_immediately_with_real_title(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "_extract_stream", fake_extract(calls))
    tracks = run(sources.build_tracks("  bad bunny  ", "ana"))
    assert calls == ["ytsearch1:bad bunny"]
    assert len(tracks) == 1 and tracks[0].title == "Título real" and tracks[0].stream_url


def test_direct_url_is_passed_through_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "_extract_stream", fake_extract(calls))
    run(sources.build_tracks("https://soundcloud.com/a/b", "ana"))
    assert calls == ["https://soundcloud.com/a/b"]


def test_youtube_playlist_tracks_are_lazy(monkeypatch):
    entries = [
        {"title": "Uno", "url": "https://www.youtube.com/watch?v=aaa", "duration": 100},
        {"title": "[Private video]", "url": "https://www.youtube.com/watch?v=priv"},
        {"title": "Dos", "id": "bbb"},          # sin url, solo id
        {"title": "Tres", "url": "ccc"},        # url "pelada" = id
    ]
    monkeypatch.setattr(sources, "_extract_youtube_playlist", lambda url, limit: ("Lista", entries))
    resolved = []
    monkeypatch.setattr(sources, "_extract_stream", fake_extract(resolved))

    tracks = run(sources.build_tracks("https://www.youtube.com/playlist?list=PL1", "ana"))
    assert [t.title for t in tracks] == ["Uno", "Dos", "Tres"]
    assert [t.query for t in tracks][1:] == [
        "https://www.youtube.com/watch?v=bbb",
        "https://www.youtube.com/watch?v=ccc",
    ]
    assert resolved == []  # nada se resolvió todavía: es lo que mantiene /play instantáneo
    assert all(t.stream_url is None for t in tracks)


def test_empty_query_is_rejected():
    with pytest.raises(SourceError):
        run(sources.build_tracks("   ", "ana"))
