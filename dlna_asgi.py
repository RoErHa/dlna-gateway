#!/usr/bin/env python3
"""
dlna_asgi.py — 2.0 ASGI application (FastAPI), served by Hypercorn.

Phase 2 of the 2.0 transport refresh (docs/BUILDING_2.0.md):
migrate the stdlib `BaseHTTPRequestHandler` gateway (dlna_server.py +
dlna_routes.py) onto an async ASGI app served by Hypercorn — HTTP/2-capable,
async I/O, and a path to WebSocket/SSE (R2). The migration is INCREMENTAL:
routes are ported batch-by-batch from the legacy handlers, and the app stays
runnable + test-green at every step. This module begins as a SKELETON with a
single native route to prove the stack; it grows as handlers move over.

Run it (from the 2.0 worktree):
    .venv/bin/hypercorn dlna_asgi:app --bind 127.0.0.1:8768
    # or, programmatically:  python dlna_asgi.py   (boots Hypercorn on :8768)
    # interactive API docs:  http://127.0.0.1:8768/api/docs

TLS is APP-OWNED: once this app is the gateway, Hypercorn terminates TLS +
HTTP/2/3 with a `tailscale cert`-issued cert. (`tailscale serve` was tried
and dropped — broken on this tailnet; see docs/BUILDING_2.0.md.)
"""
import functools
import json
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

import api_browse
import dlna_routes
import dlna_server
from dlna_asgi_bridge import make_bridged_route
from dlna_config import VERSION
from dlna_discovery import SERVERS
from dlna_library import DB, INDEXER

app = FastAPI(title="DLNA Gateway", version=VERSION, docs_url="/api/docs",
              redoc_url=None)


# ── Native routes ─────────────────────────────────────────────────────
# Handlers ported off the legacy (h, params) shape into native FastAPI
# routes. They call the SAME data functions the legacy handlers now use
# (api_browse.servers_payload, etc.) — single source of truth, zero
# divergence — and run the blocking DB/registry work in a threadpool.
# Each native path is excluded from the bridge below (_NATIVE).

@app.get("/api/version")
async def version() -> dict:
    """Release-line marker. Same payload as the legacy stdlib handler
    (api_playback.version) so the PWA version badge behaves identically."""
    return {"version": VERSION}


@app.get("/api/servers")
async def servers() -> list:
    return await run_in_threadpool(api_browse.servers_payload)


@app.get("/api/renderers")
async def renderers() -> list:
    return await run_in_threadpool(api_browse.renderers_payload)


# ── Browse-navigation reads ───────────────────────────────────────────
# Trivial `validate params → DB call` handlers ported native: FastAPI does
# the query-param binding, the shared DB methods are the source of truth,
# and the blocking query runs in a threadpool. 400 bodies match the legacy
# handlers exactly (`{"error": "..."}`).

def _missing(msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=400)


@app.get("/api/artists")
async def artists(udn: str = ""):
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(DB.all_artists, udn)


@app.get("/api/albums")
async def albums(udn: str = ""):
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(DB.all_albums, udn)


@app.get("/api/genres")
async def genres(udn: str = ""):
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(DB.all_genres, udn)


@app.get("/api/artist_albums")
async def artist_albums(udn: str = "", artist: str = ""):
    if not udn or not artist:
        return _missing("Missing udn or artist")
    return await run_in_threadpool(DB.artist_albums, udn, artist)


@app.get("/api/artist_tracks")
async def artist_tracks(udn: str = "", artist: str = ""):
    if not udn or not artist:
        return _missing("Missing udn or artist")
    return {"tracks": await run_in_threadpool(DB.artist_tracks, udn, artist)}


@app.get("/api/genre_albums")
async def genre_albums(udn: str = "", genre: str = ""):
    if not udn or not genre:
        return _missing("Missing udn or genre")
    return await run_in_threadpool(DB.genre_albums, udn, genre)


@app.get("/api/genre_tracks")
async def genre_tracks(udn: str = "", genre: str = ""):
    if not udn or not genre:
        return _missing("Missing udn or genre")
    return {"tracks": await run_in_threadpool(DB.genre_tracks, udn, genre)}


@app.get("/api/album_tracks")
async def album_tracks(udn: str = "", artist: str = "", album: str = "",
                       album_key: str = ""):
    # LocalFs opens by folder identity (album_key); UPnP/legacy by
    # (artist, album). Require at least one — same as the legacy handler.
    if not udn or not (album or album_key):
        return _missing("Missing udn or album/album_key")
    tracks = await run_in_threadpool(
        functools.partial(DB.album_tracks, udn, artist, album,
                          album_key=album_key))
    SERVERS.touch(udn)
    return {"tracks": tracks}


@app.get("/api/decades")
async def decades(udn: str = ""):
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(DB.all_decades, udn)


@app.get("/api/decade_albums")
async def decade_albums(udn: str = "", decade: str = ""):
    if not udn or not decade:
        return _missing("Missing udn or decade")
    try:
        d = int(decade)
    except ValueError:
        return _missing("decade must be an integer")
    return await run_in_threadpool(DB.decade_albums, udn, d)


@app.get("/api/decade_tracks")
async def decade_tracks(udn: str = "", decade: str = ""):
    if not udn or not decade:
        return _missing("Missing udn or decade")
    try:
        d = int(decade)
    except ValueError:
        return _missing("decade must be an integer")
    return {"tracks": await run_in_threadpool(DB.decade_tracks, udn, d)}


@app.get("/api/search")
async def search(udn: str = "", q: str = ""):
    query = q.strip()
    if not query:
        return _missing("Missing q")
    if not udn:
        return _missing("Missing udn")
    # Don't search a half-built index — same guard as the legacy handler.
    if INDEXER.state.status == "running" and DB.track_count(udn) == 0:
        return {"tracks": [], "albums": [], "artists": [],
                "info": "Indexing — please wait"}
    result = await run_in_threadpool(DB.search, udn, query)
    SERVERS.touch(udn)
    return result


@app.get("/api/browse_letter")
async def browse_letter(udn: str = "", mode: str = "artists",
                        letter: str = "A", offset: int = 0, limit: int = 100):
    # offset/limit are typed ints (FastAPI 422 on garbage, vs the legacy
    # int()-raises path) — a strict-but-friendlier improvement.
    if not udn:
        return _missing("Missing udn")
    return await run_in_threadpool(
        DB.browse_letter, udn, mode, letter.upper(), offset, limit)


# ── Status / playlists / favourites reads ─────────────────────────────

@app.get("/api/index/status")
async def index_status(udn: str = ""):
    count = await run_in_threadpool(DB.track_count, udn) if udn else 0
    return {**INDEXER.state.get(), "db_tracks": count}


@app.get("/api/track_meta")
async def track_meta(url: str = ""):
    if not url:
        return _missing("missing url")          # lowercase — matches legacy
    meta = await run_in_threadpool(DB.track_meta_by_url, url)
    if not meta:
        return JSONResponse({"error": "track not in library"}, status_code=404)
    return meta


@app.get("/api/playlists")
async def playlists():
    return await run_in_threadpool(DB.pl_list)


@app.get("/api/playlist")
async def playlist(id: str = ""):
    pl = await run_in_threadpool(DB.pl_get, id)
    if pl is None:
        return JSONResponse({"error": "Playlist not found"}, status_code=404)
    return pl


@app.get("/api/album_favourites")
async def album_favourites():
    return await run_in_threadpool(DB.album_fav_list)


@app.get("/api/album_favourites/check")
async def album_favourite_check(artist: str = "", album: str = "",
                                album_key: str = ""):
    if not (album or album_key):
        return _missing("Missing album/album_key")
    is_fav = await run_in_threadpool(DB.album_fav_is, artist, album, album_key)
    return {"is_favourite": is_fav}


@app.get("/api/radio/favourites")
async def radio_favourites():
    stations = await run_in_threadpool(DB.radio_fav_list)
    return {"stations": stations, "limit": DB.RADIO_FAV_MAX}


# Paths served by a native route above — must NOT also be bridged.
_NATIVE = {"/api/version", "/api/servers", "/api/renderers",
           "/api/artists", "/api/albums", "/api/genres",
           "/api/artist_albums", "/api/artist_tracks",
           "/api/genre_albums", "/api/genre_tracks", "/api/album_tracks",
           "/api/decades", "/api/decade_albums", "/api/decade_tracks",
           "/api/search", "/api/browse_letter",
           "/api/index/status", "/api/track_meta", "/api/playlists",
           "/api/playlist", "/api/album_favourites",
           "/api/album_favourites/check", "/api/radio/favourites"}


# ── Bridged legacy read routes ────────────────────────────────────────
# Register the remaining JSON read API through the compatibility shim so
# the whole read API runs under Hypercorn TODAY, while handlers are
# migrated to native routes (the _NATIVE set above) one batch at a time.
# Excluded from the bridge:
#   • _NATIVE                — already ported to native FastAPI routes
#   • /stream /art /radio_stream — stream bytes to the socket; not
#       bridgeable, ported as StreamingResponse later
#   • /gw/*                 — UPnP device endpoints; stay on the legacy LAN
#       server (the Naim talks to it directly, never through this proxy)
_STREAMING = {"/stream", "/art", "/radio_stream"}


def _bridgeable(path: str) -> bool:
    return (path not in _NATIVE and path not in _STREAMING
            and not path.startswith("/gw/"))


for _path, _handler in dlna_routes.GET_ROUTES.items():
    if _bridgeable(_path):
        app.add_api_route(_path, make_bridged_route(_handler, is_post=False),
                          methods=["GET"], name=f"bridged_get:{_path}")


# ── Bridged legacy POST routes ────────────────────────────────────────
# The whole write API now runs under Hypercorn via the same shim (the legacy
# handler gets the raw request body as its second arg). Excluded: native POST
# ports (none yet) and `/gw/*` device endpoints (legacy LAN server only). The
# simple ones get rewritten native in a later batch.
_NATIVE_POST: set = set()

for _path, _handler in dlna_routes.POST_ROUTES.items():
    if _path not in _NATIVE_POST and not _path.startswith("/gw/"):
        app.add_api_route(_path, make_bridged_route(_handler, is_post=True),
                          methods=["POST"], name=f"bridged_post:{_path}")


# ── Static / PWA serving ──────────────────────────────────────────────
# Replicates the legacy stdlib server's static routes so the PWA loads from
# the ASGI app: GET / (+ /index.html); /static/* (app.js/app.css); the
# service worker /sw.js (root scope, no-store); the generated /manifest.json;
# and the generated /icon-192.png / /icon-512.png. `include_in_schema=False`
# keeps them out of the OpenAPI docs.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_MANIFEST = json.dumps({
    "name": "DLNA Gateway",
    "short_name": "DLNA GW",
    "description": "Personal music gateway — browse, search and play your library",
    "start_url": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#0e0d0b",
    "theme_color": "#0e0d0b",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
    "categories": ["music", "entertainment"],
}, indent=2)


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
async def _index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"),
                        media_type="text/html")


@app.get("/sw.js", include_in_schema=False)
async def _service_worker():
    # Root scope + no-store so an updated worker is always picked up.
    return FileResponse(
        os.path.join(_STATIC_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                 "Service-Worker-Allowed": "/"})


@app.get("/manifest.json", include_in_schema=False)
async def _manifest():
    return Response(content=_MANIFEST, media_type="application/manifest+json")


def _icon(size: int) -> Response:
    return Response(content=dlna_server._make_icon_png(size),
                    media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/icon-192.png", include_in_schema=False)
async def _icon_192():
    return _icon(192)


@app.get("/icon-512.png", include_in_schema=False)
async def _icon_512():
    return _icon(512)


# /static/* — app.js, app.css (+ anything future). StaticFiles handles MIME
# and path-traversal defence. Mounted last; doesn't shadow the API routes.
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _run(host: str = "127.0.0.1", port: int = 8768) -> None:
    """Boot Hypercorn on the ASGI app — the module self-test / dev runner."""
    import asyncio
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    cfg = Config()
    cfg.bind = [f"{host}:{port}"]
    print(f"Hypercorn serving dlna_asgi:app → http://{host}:{port}/  (Ctrl-C to stop)")
    asyncio.run(serve(app, cfg))


if __name__ == "__main__":
    _run()
