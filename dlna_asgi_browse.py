#!/usr/bin/env python3
"""
dlna_asgi_browse.py — the JSON read API: library navigation, index
status, playlists, favourites, radio, lyrics, audiobook positions, and the
Server-Sent Events stream.

Split out of dlna_asgi.py on 2026-08-20, when that module reached 1,156
lines holding every route in the gateway. Each group is now an APIRouter that
dlna_asgi includes:

    dlna_asgi_state.py     the shared runtime handles every router binds against
    dlna_asgi_browse.py    the JSON read API + SSE
    dlna_asgi_video.py     /video/* (PWA, same-origin)
    dlna_asgi_media.py     /art, /stream, /radio_stream byte relays
    dlna_asgi_upnp.py      /gw/* — the Naim-facing UPnP surface
    dlna_asgi_subsonic.py  /rest/* — the CarPlay surface
    dlna_asgi_static.py    /, /sw.js, /manifest.json, generated icons
    dlna_asgi.py           lifespan, the app, legacy-bridge wiring, includes

Route ORDER across these routers is not load-bearing: no two routes in the
app can match the same request (asserted by tests/test_asgi.py), so grouping
is free. dlna_asgi re-exports every handler, so the ~58 tests that call
`dlna_asgi.<route>()` directly keep working.

Every handler here is `async def` but does its blocking SQLite work through
`run_in_threadpool`. That is not decoration: this is an ASGI app, and a
synchronous DB call inside an `async def` blocks the whole event loop —
every other request and the SSE stream with it. Ruff's ASYNC rules guard it.

The SSE heartbeat is 45s (was 15s): with the PWA's polls throttled it became
the most frequent thing on the wire, and 240 inbound frames/hour purely to
say "still here" measurably cost iPhone battery. Kept under 60s so a future
reverse proxy's idle timeout cannot close the stream.
"""
import asyncio
import functools
import json
import logging

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, StreamingResponse

import api_browse
import api_playback
import api_radio
import dlna_asgi_state as _st
from dlna_asgi_state import _missing
from dlna_config import VERSION
from dlna_events import EVENTS, sse_format

router = APIRouter()

log = logging.getLogger("dlna.asgi")


# ── Native routes ─────────────────────────────────────────────────────
# Handlers ported off the legacy (h, params) shape into native FastAPI
# routes. They call the SAME data functions the legacy handlers now use
# (api_browse.servers_payload, etc.) — single source of truth, zero
# divergence — and run the blocking DB/registry work in a threadpool.
# Each native path is excluded from the bridge below (_NATIVE).

@router.get("/api/version")
async def version() -> dict:
    """Release-line marker. Same payload as the legacy stdlib handler
    (api_playback.version) so the PWA version badge behaves identically."""
    return {"version": VERSION}


@router.get("/api/servers")
async def servers() -> list:
    return await run_in_threadpool(api_browse.servers_payload)


@router.get("/api/renderers")
async def renderers() -> list:
    return await run_in_threadpool(api_browse.renderers_payload)


@router.get("/api/artists")
async def artists(udn: str = ""):
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(_st.DB.all_artists, udn)


@router.get("/api/albums")
async def albums(udn: str = ""):
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(_st.DB.all_albums, udn)


@router.get("/api/genres")
async def genres(udn: str = ""):
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(_st.DB.all_genres, udn)


@router.get("/api/artist_albums")
async def artist_albums(udn: str = "", artist: str = ""):
    if not udn or not artist:
        return _missing("Missing udn or artist")
    return await run_in_threadpool(_st.DB.artist_albums, udn, artist)


@router.get("/api/artist_tracks")
async def artist_tracks(udn: str = "", artist: str = ""):
    if not udn or not artist:
        return _missing("Missing udn or artist")
    return {"tracks": await run_in_threadpool(_st.DB.artist_tracks, udn, artist)}


@router.get("/api/genre_albums")
async def genre_albums(udn: str = "", genre: str = ""):
    if not udn or not genre:
        return _missing("Missing udn or genre")
    return await run_in_threadpool(_st.DB.genre_albums, udn, genre)


@router.get("/api/genre_tracks")
async def genre_tracks(udn: str = "", genre: str = ""):
    if not udn or not genre:
        return _missing("Missing udn or genre")
    return {"tracks": await run_in_threadpool(_st.DB.genre_tracks, udn, genre)}


@router.get("/api/album_tracks")
async def album_tracks(udn: str = "", artist: str = "", album: str = "",
                       album_key: str = ""):
    # LocalFs opens by folder identity (album_key); UPnP/legacy by
    # (artist, album). Require at least one — same as the legacy handler.
    if not udn or not (album or album_key):
        return _missing("Missing udn or album/album_key")
    tracks = await run_in_threadpool(
        functools.partial(_st.DB.album_tracks, udn, artist, album,
                          album_key=album_key))
    _st.SERVERS.touch(udn)
    return {"tracks": tracks}


@router.get("/api/decades")
async def decades(udn: str = ""):
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(_st.DB.all_decades, udn)


@router.get("/api/decade_albums")
async def decade_albums(udn: str = "", decade: str = ""):
    if not udn or not decade:
        return _missing("Missing udn or decade")
    try:
        d = int(decade)
    except ValueError:
        return _missing("decade must be an integer")
    return await run_in_threadpool(_st.DB.decade_albums, udn, d)


@router.get("/api/decade_tracks")
async def decade_tracks(udn: str = "", decade: str = ""):
    if not udn or not decade:
        return _missing("Missing udn or decade")
    try:
        d = int(decade)
    except ValueError:
        return _missing("decade must be an integer")
    return {"tracks": await run_in_threadpool(_st.DB.decade_tracks, udn, d)}


@router.get("/api/search")
async def search(udn: str = "", q: str = ""):
    query = q.strip()
    if not query:
        return _missing("Missing q")
    if not udn:
        return _missing("Missing udn")
    # Don't search a half-built index — same guard as the legacy handler.
    if _st.INDEXER.state.status == "running" and _st.DB.track_count(udn) == 0:
        return {"tracks": [], "albums": [], "artists": [],
                "info": "Indexing — please wait"}
    result = await run_in_threadpool(_st.DB.search, udn, query)
    _st.SERVERS.touch(udn)
    return result


@router.get("/api/browse_letter")
async def browse_letter(udn: str = "", mode: str = "artists",
                        letter: str = "A", offset: int = 0, limit: int = 100):
    # offset/limit are typed ints (FastAPI 422 on garbage, vs the legacy
    # int()-raises path) — a strict-but-friendlier improvement.
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(
        _st.DB.browse_letter, udn, mode, letter.upper(), offset, limit)


# ── Status / playlists / favourites reads ─────────────────────────────

@router.get("/api/index/status")
async def index_status(udn: str = ""):
    count = await run_in_threadpool(_st.DB.track_count, udn) if udn else 0
    return {**_st.INDEXER.state.get(), "db_tracks": count}


@router.get("/api/track_meta")
async def track_meta(url: str = ""):
    if not url:
        return _missing("missing url")          # lowercase — matches legacy
    meta = await run_in_threadpool(_st.DB.track_meta_by_url, url)
    if not meta:
        return JSONResponse({"error": "track not in library"}, status_code=404)
    return meta


@router.get("/api/playlists")
async def playlists():
    return await run_in_threadpool(_st.DB.pl_list)


@router.get("/api/playlist")
async def playlist(id: str = ""):
    pl = await run_in_threadpool(_st.DB.pl_get, id)
    if pl is None:
        return JSONResponse({"error": "Playlist not found"}, status_code=404)
    return pl


@router.get("/api/album_favourites")
async def album_favourites():
    return await run_in_threadpool(_st.DB.album_fav_list)


@router.get("/api/album_favourites/check")
async def album_favourite_check(artist: str = "", album: str = "",
                                album_key: str = ""):
    if not (album or album_key):
        return _missing("Missing album/album_key")
    is_fav = await run_in_threadpool(_st.DB.album_fav_is, artist, album, album_key)
    return {"is_favourite": is_fav}


@router.get("/api/radio/favourites")
async def radio_favourites():
    stations = await run_in_threadpool(_st.DB.radio_fav_list)
    return {"stations": stations, "limit": _st.DB.RADIO_FAV_MAX}


# ── Last bridged reads → native (the *_payload extraction pattern) ────
# Each calls a shared core in its api_* module that returns (status, body);
# the legacy (h, params) handler calls the SAME core, so there's no behaviour
# divergence (incl. browse's _st.SERVERS.touch/re-probe side effects and the
# radio/search + lyrics network calls — all run in the threadpool here).

@router.get("/api/browse", include_in_schema=False)
async def browse_route(request: Request):
    code, body = await run_in_threadpool(
        api_browse.browse_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@router.get("/api/radio", include_in_schema=False)
async def radio_route(request: Request):
    code, body = await run_in_threadpool(
        api_browse.radio_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@router.get("/api/radio/search", include_in_schema=False)
async def radio_search_route(request: Request):
    code, body = await run_in_threadpool(
        api_radio.search_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@router.get("/api/radio/nowplaying", include_in_schema=False)
async def radio_nowplaying_route(request: Request):
    code, body = await run_in_threadpool(
        api_radio.nowplaying_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@router.get("/api/lyrics", include_in_schema=False)
async def lyrics_route(request: Request):
    code, body = await run_in_threadpool(
        api_playback.lyrics_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


# ── Audiobook resume positions (P2, 2026-07-13) ───────────────────────
# Native-only (no legacy handler — these endpoints post-date the bridge).
# NOT on the Service Worker's CACHEABLE_API allowlist: live state,
# network-only by default. POST accepts sendBeacon bodies (no reliable
# Content-Type there), so the body is parsed manually.

@router.post("/api/position", include_in_schema=False)
async def position_save_route(request: Request):
    try:
        payload = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid body"}, status_code=400)
    code, body = await run_in_threadpool(
        api_playback.position_save_payload, payload)
    return JSONResponse(body, status_code=code)


@router.get("/api/position", include_in_schema=False)
async def position_get_route(request: Request):
    code, body = await run_in_threadpool(
        api_playback.position_get_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@router.get("/api/positions", include_in_schema=False)
async def positions_list_route(request: Request):
    code, body = await run_in_threadpool(
        api_playback.positions_list_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@router.get("/api/book_meta_all", include_in_schema=False)
async def book_meta_all_route(request: Request):
    code, body = await run_in_threadpool(
        api_playback.book_meta_all_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@router.get("/api/chapters", include_in_schema=False)
async def chapters_route(request: Request):
    code, body = await run_in_threadpool(
        api_playback.chapters_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


# ── Server-Sent Events (R2) ───────────────────────────────────────────
# Long-lived text/event-stream the PWA subscribes to (EventSource) for live
# pushes — now-playing, index progress, device changes — instead of polling.
# Worker threads call dlna_events.EVENTS.publish({...}); the bus (bound to this
# loop in _lifespan) fans each event to every connected subscriber's queue.
# A comment heartbeat keeps the connection alive through proxies/idle.
#
# 45 s, raised from 15 s (2026-08-07, iOS battery). Now that the PWA throttles
# its polls when idle, this heartbeat is the most frequent thing left on the
# wire in a foreground-but-idle session — at 15 s it was 240 inbound frames an
# hour, each one a radio wake-up, purely to say "still here". Nothing needs
# that rate: it only has to beat whatever idle timeout sits in the middle, and
# on the tailnet there is no proxy at all. The cost of raising it is that a
# vanished client is reaped up to 45 s later (a bounded queue, freed in the
# `finally`) and that a dead connection is noticed one heartbeat later — both
# harmless. Keep it comfortably under 60 s so a future reverse proxy's default
# idle timeout can't close the stream.
_SSE_HEARTBEAT_SEC = 45.0


@router.get("/api/events", include_in_schema=False)
async def events(request: Request):
    q = EVENTS.subscribe()

    async def gen():
        try:
            yield sse_format({"type": "hello"})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(q.get(), _SSE_HEARTBEAT_SEC)
                    yield sse_format(ev)
                except TimeoutError:
                    yield ": keepalive\n\n"      # SSE comment frame
        finally:
            EVENTS.unsubscribe(q)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store",
                 "Access-Control-Allow-Origin": "*",
                 "X-Accel-Buffering": "no"})      # disable proxy buffering
