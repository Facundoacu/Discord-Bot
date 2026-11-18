"""Resolución de lo que pide el usuario a tracks reproducibles.

Principios para mantener la latencia baja:
  * Todo lo bloqueante (yt-dlp, spotipy) corre en el threadpool, nunca en el
    event loop.
  * Los tracks de playlists/álbumes se crean *sin resolver*: solo se
    resuelve el audio cuando están por sonar (o por prefetch). Así `/play`
    de una playlist de 100 temas responde al instante.
  * Una misma resolución nunca se hace dos veces en paralelo.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from urllib.parse import parse_qs, urlparse

from .errors import SourceError
from .track import Track

log = logging.getLogger("sources")

# Se agrega a la búsqueda de temas de Spotify para sesgar hacia el audio
# "limpio" y no hacia videoclips con intros o versiones en vivo.
SPOTIFY_SEARCH_SUFFIX = "audio"

_SPOTIFY_URL_RE = re.compile(
    r"https?://open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?"
    r"(?P<kind>track|album|playlist)/(?P<id>[A-Za-z0-9]+)"
)
_SPOTIFY_URI_RE = re.compile(r"^spotify:(?P<kind>track|album|playlist):(?P<id>[A-Za-z0-9]+)$")
_SPOTIFY_SHORT_RE = re.compile(r"https?://spotify\.link/", re.IGNORECASE)
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


# ---------------------------------------------------------------------------
# Parseo de entradas (puro, sin red)
# ---------------------------------------------------------------------------

def is_url(text: str) -> bool:
    return bool(re.match(r"https?://", text.strip(), re.IGNORECASE))


def parse_spotify_url(text: str) -> tuple[str, str] | None:
    """Devuelve (tipo, id) para links/URIs de Spotify; None si no es Spotify."""
    text = text.strip()
    m = _SPOTIFY_URL_RE.search(text) or _SPOTIFY_URI_RE.match(text)
    if not m:
        return None
    return m.group("kind"), m.group("id")


def is_spotify_short_link(text: str) -> bool:
    return bool(_SPOTIFY_SHORT_RE.match(text.strip()))


def is_youtube_playlist_url(text: str) -> bool:
    """True solo para links de playlist "puros" (/playlist?list=...).

    Un link `watch?v=X&list=Y` se trata como el video X: es lo que casi
    siempre se quiere, y evita tragarse mixes/radios infinitos.
    """
    try:
        parsed = urlparse(text.strip())
    except ValueError:
        return False
    if parsed.hostname not in _YOUTUBE_HOSTS:
        return False
    return parsed.path == "/playlist" and "list" in parse_qs(parsed.query)


# ---------------------------------------------------------------------------
# Spotify (solo metadata: el audio sale de YouTube)
# ---------------------------------------------------------------------------

def spotify_data_to_track(data: dict | None, requester: str) -> Track | None:
    """Convierte un objeto track de la API de Spotify en un Track sin resolver."""
    if not data:
        return None
    name = data.get("name")
    if not name:
        return None
    artists = [a.get("name") for a in (data.get("artists") or []) if a.get("name")]
    title = f"{artists[0]} - {name}" if artists else name
    duration_ms = data.get("duration_ms")
    return Track(
        title=title,
        requester=requester,
        query=f"ytsearch1:{title} {SPOTIFY_SEARCH_SUFFIX}",
        duration=duration_ms // 1000 if isinstance(duration_ms, int) else None,
    )


def _paginate(sp, page: dict | None, limit: int) -> list[dict]:
    out: list[dict] = []
    while page:
        out.extend(page.get("items") or [])
        if len(out) >= limit or not page.get("next"):
            break
        page = sp.next(page)
    return out[:limit]


def collect_spotify_tracks(sp, kind: str, spotify_id: str, requester: str, limit: int) -> list[Track]:
    """Bloqueante. `sp` es un cliente spotipy (o un fake en los tests)."""
    if kind == "track":
        raw = [sp.track(spotify_id)]
    elif kind == "album":
        raw = _paginate(sp, sp.album_tracks(spotify_id, limit=50), limit)
    elif kind == "playlist":
        # Desde febrero 2026 el endpoint pasó de /tracks a /items y cada
        # entrada de `track` a `item`. Se llama el endpoint nuevo
        # directamente (spotipy puede seguir apuntando al viejo) y se leen
        # ambas formas del campo.
        page = sp._get(f"playlists/{spotify_id}/items", limit=100)
        if not isinstance(page, dict) or "items" not in page:
            raise SourceError("Spotify no me devolvió los temas de esa playlist.")
        entries = _paginate(sp, page, limit)
        raw = [(e.get("item") or e.get("track")) for e in entries if isinstance(e, dict)]
    else:  # pragma: no cover - el regex ya filtra
        raise SourceError("Tipo de link de Spotify no soportado.")

    tracks = [t for t in (spotify_data_to_track(d, requester) for d in raw) if t]
    return tracks[:limit]


def _spotify_client():
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SourceError(
            "Para usar links de Spotify hay que configurar "
            "SPOTIFY_CLIENT_ID y SPOTIFY_CLIENT_SECRET en el .env del bot."
        )
    import spotipy
    from spotipy.cache_handler import MemoryCacheHandler
    from spotipy.oauth2 import SpotifyClientCredentials

    auth = SpotifyClientCredentials(
        client_id=client_id,
        client_secret=client_secret,
        cache_handler=MemoryCacheHandler(),  # sin archivos .cache en disco
    )
    return spotipy.Spotify(auth_manager=auth, requests_timeout=10, retries=2)


def _fetch_spotify(kind: str, spotify_id: str, requester: str, limit: int) -> list[Track]:
    sp = _spotify_client()
    try:
        return collect_spotify_tracks(sp, kind, spotify_id, requester, limit)
    except SourceError:
        raise
    except Exception as exc:
        log.warning("Spotify falló (%s %s): %s", kind, spotify_id, exc)
        if kind == "playlist":
            raise SourceError(
                "Spotify no me deja leer esa playlist. Desde 2026 las apps en "
                "modo desarrollo solo pueden leer playlists propias o "
                "colaborativas. Probá con un link de tema o álbum, o con una "
                "playlist de YouTube."
            ) from exc
        raise SourceError("No pude leer ese link de Spotify.") from exc


# ---------------------------------------------------------------------------
# yt-dlp (bloqueante; siempre se llama desde el threadpool)
# ---------------------------------------------------------------------------

def _ydl_opts(**extra) -> dict:
    opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 10,
        # Forzar IPv4: muchos VPS tienen IPv6 que YouTube bloquea.
        "source_address": "0.0.0.0",
    }
    opts.update(extra)
    return opts


def _extract_stream(query: str) -> dict:
    import yt_dlp

    with yt_dlp.YoutubeDL(_ydl_opts()) as ydl:
        info = ydl.extract_info(query, download=False)
        # Serializable a JSON: se le pasa a `yt-dlp --load-info-json` al reproducir.
        if info is not None:
            info = ydl.sanitize_info(info)
    if info is None:
        raise SourceError("No encontré resultados.")
    if "entries" in info:
        entries = [e for e in (info["entries"] or []) if e]
        if not entries:
            raise SourceError("No encontré resultados.")
        info = entries[0]
    if not info.get("url"):
        raise SourceError("No pude obtener el audio de ese video.")
    return info


def _extract_youtube_playlist(url: str, limit: int) -> tuple[str | None, list[dict]]:
    import yt_dlp

    opts = _ydl_opts(extract_flat="in_playlist", noplaylist=False, playlistend=limit)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise SourceError("No pude leer esa playlist.")
    return info.get("title"), [e for e in (info.get("entries") or []) if e]


# ---------------------------------------------------------------------------
# API async
# ---------------------------------------------------------------------------

async def _resolve_track(track: Track) -> None:
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, _extract_stream, track.query)
    except SourceError:
        raise
    except Exception as exc:
        log.warning("No se pudo resolver %r: %s", track.query, exc)
        raise SourceError("no pude obtener el audio") from exc

    track.mark_resolved(info["url"], info=info)
    # Completar lo que faltaba sin pisar lo que ya sabíamos (p. ej. el título
    # "Artista - Tema" que vino de Spotify).
    track.webpage_url = track.webpage_url or info.get("webpage_url")
    if track.duration is None:
        track.duration = info.get("duration")
    if not track.title:
        track.title = info.get("title") or track.query


async def ensure_resolved(track: Track) -> None:
    """Deja `track.stream_url` listo; no hace nada si ya está vigente.

    Si ya hay una resolución en curso (por ejemplo el prefetch), se espera a
    esa misma en vez de lanzar otra.
    """
    if not track.needs_resolve():
        return
    task = track.resolving
    if task is None or task.done():
        task = asyncio.get_running_loop().create_task(_resolve_track(track))
        track.resolving = task
    # shield: que cancelar a un espectador no cancele la resolución compartida
    await asyncio.shield(task)


async def build_tracks(query: str, requester: str, *, max_tracks: int = 100) -> list[Track]:
    """Convierte lo que escribió el usuario en una lista de tracks.

    Un tema suelto (búsqueda o link) se resuelve ya, para mostrar el título
    real. Playlists y álbumes quedan sin resolver (lazy).
    """
    query = query.strip()
    if not query:
        raise SourceError("Decime qué querés escuchar.")
    loop = asyncio.get_running_loop()

    if is_spotify_short_link(query):
        raise SourceError(
            "Los links cortos spotify.link no los puedo leer: pegá el link "
            "completo de open.spotify.com."
        )

    spotify = parse_spotify_url(query)
    if spotify:
        kind, spotify_id = spotify
        tracks = await loop.run_in_executor(
            None, _fetch_spotify, kind, spotify_id, requester, max_tracks
        )
        if not tracks:
            raise SourceError("No encontré temas en ese link de Spotify.")
        return tracks

    if is_youtube_playlist_url(query):
        try:
            _, entries = await loop.run_in_executor(
                None, _extract_youtube_playlist, query, max_tracks
            )
        except SourceError:
            raise
        except Exception as exc:
            log.warning("Playlist de YouTube falló (%s): %s", query, exc)
            raise SourceError("No pude leer esa playlist de YouTube.") from exc

        tracks = []
        for entry in entries:
            title = entry.get("title") or ""
            if title in ("[Private video]", "[Deleted video]"):
                continue
            url = entry.get("url") or entry.get("webpage_url")
            if url and not is_url(url):
                url = f"https://www.youtube.com/watch?v={url}"
            elif not url and entry.get("id"):
                url = f"https://www.youtube.com/watch?v={entry['id']}"
            if not url:
                continue
            tracks.append(
                Track(
                    title=title or url,
                    requester=requester,
                    query=url,
                    webpage_url=url,
                    duration=entry.get("duration"),
                )
            )
        if not tracks:
            raise SourceError("Esa playlist está vacía o es privada.")
        return tracks[:max_tracks]

    # Tema suelto: link directo (YouTube, SoundCloud, Bandcamp...) o búsqueda.
    search = query if is_url(query) else f"ytsearch1:{query}"
    # Título vacío a propósito: se completa con el título real al resolver.
    track = Track(title="", requester=requester, query=search)
    await ensure_resolved(track)
    return [track]
