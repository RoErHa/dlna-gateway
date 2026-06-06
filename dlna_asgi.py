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
import asyncio
import contextlib
import functools
import json
import logging
import os
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response, StreamingResponse

import api_browse
import api_playback
import api_radio
import api_subsonic
import dlna_routes
import dlna_server
import dlna_stream_proxy
from dlna_asgi_bridge import _LegacyH, make_bridged_route, run_subsonic_sync
from dlna_config import VERSION, raise_fd_limit
from dlna_events import EVENTS, sse_format
from dlna_discovery import SERVERS
from dlna_library import DB, INDEXER

log = logging.getLogger("dlna.asgi")


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _loop_exception_handler(loop, context):
    """Downgrade benign client-transport teardown errors to debug. Hypercorn's
    TCPServer._close() doesn't catch TimeoutError/ETIMEDOUT on Python 3.14, so a
    client connection that times out or resets uncleanly escapes as 'Unhandled
    exception in client_connected_cb' — a scary-looking but harmless traceback
    (no request impact, no FD leak). Everything else goes to the default
    handler so real bugs are still surfaced."""
    exc = context.get("exception")
    msg = context.get("message", "")
    if "client_connected_cb" in msg and isinstance(exc, OSError):
        log.debug(f"asyncio: benign client transport teardown — {exc!r}")
        return
    loop.default_exception_handler(context)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """Boot the gateway's background services so `hypercorn dlna_asgi:app` runs
    the gateway standalone: discovery (DB pre-probe / SSDP / subnet-scan /
    heartbeat), the gateway-as-MediaServer SSDP announcer, the album-art +
    AcoustID startup scans, and LocalFs wiring — the SAME set
    dlna_gateway.main() starts, via the SAME function
    (dlna_gateway.start_background_services). It ALSO starts the device-tier
    server (dlna_server.start_device_server) on GATEWAY_PORT, serving the
    Naim-facing /gw/* UPnP surface over plain LAN HTTP — Hypercorn owns the
    main port (and, later, TLS) but the Naim can't do HTTPS. The SSDP advert
    points at GATEWAY_PORT, so it correctly lands on the device server. Run
    exactly ONE of the two entrypoints (stdlib OR Hypercorn), never both —
    they'd double-announce and clash on ports.

    Env knobs:
      • GATEWAY_NO_SERVICES=1 — web tier only; skip ALL background startup +
        the device server (e.g. running purely as a web tier, or experiments).
      • GATEWAY_PORT (default 8770) — the plain-HTTP device-server port, also
        the port advertised in the gateway-as-MediaServer SSDP record.
      • GATEWAY_DEBUG=1 — verbose logging.

    Cleanup C (AFTER cutover): fold /gw/* into the ASGI app on a Hypercorn
    `--insecure-bind` plain port and retire dlna_server + the device server, so
    the whole gateway is one framework. See docs/BUILDING_2.0.md."""
    import dlna_gateway
    # Raise the open-file limit BEFORE serving — Hypercorn's threadpool + the
    # LocalFs scan open enough concurrent FDs to hit macOS's default 256-soft
    # limit → EMFILE → sqlite 'unable to open database file'. Unconditional
    # (even GATEWAY_NO_SERVICES still serves /api/* DB reads).
    raise_fd_limit()
    # Bind the SSE event bus to this loop so worker threads can publish() live
    # updates to /api/events subscribers (R2). Unconditional — the web tier
    # serves SSE regardless of GATEWAY_NO_SERVICES.
    loop = asyncio.get_running_loop()
    EVENTS.bind_loop(loop)
    loop.set_exception_handler(_loop_exception_handler)
    started = False
    device_server = None
    if not _truthy("GATEWAY_NO_SERVICES"):
        port = int(os.environ.get("GATEWAY_PORT", "8770"))
        dlna_gateway.setup_logging(debug=_truthy("GATEWAY_DEBUG"))
        lan_ip = dlna_gateway.get_lan_ip()
        dlna_gateway.start_background_services(lan_ip, port)
        device_server = dlna_server.start_device_server("0.0.0.0", port)
        started = True
        log.info("ASGI lifespan: gateway background services + device server "
                 f"started (/gw/* + SSDP advert on port {port})")
    try:
        yield
    finally:
        if started:
            if device_server is not None:
                try:
                    device_server.shutdown()
                except Exception:                   # noqa: BLE001
                    pass
            try:
                dlna_gateway.gw_ssdp_byebye(
                    dlna_gateway.get_lan_ip(),
                    int(os.environ.get("GATEWAY_PORT", "8770")))
            except Exception:                       # noqa: BLE001
                pass
        EVENTS.bind_loop(None)      # drop the (now closing) loop reference


app = FastAPI(title="DLNA Gateway", version=VERSION, docs_url="/api/docs",
              redoc_url=None, lifespan=_lifespan)


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


# ── Last bridged reads → native (the *_payload extraction pattern) ────
# Each calls a shared core in its api_* module that returns (status, body);
# the legacy (h, params) handler calls the SAME core, so there's no behaviour
# divergence (incl. browse's SERVERS.touch/re-probe side effects and the
# radio/search + lyrics network calls — all run in the threadpool here).

@app.get("/api/browse", include_in_schema=False)
async def browse_route(request: Request):
    code, body = await run_in_threadpool(
        api_browse.browse_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@app.get("/api/radio", include_in_schema=False)
async def radio_route(request: Request):
    code, body = await run_in_threadpool(
        api_browse.radio_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@app.get("/api/radio/search", include_in_schema=False)
async def radio_search_route(request: Request):
    code, body = await run_in_threadpool(
        api_radio.search_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@app.get("/api/radio/nowplaying", include_in_schema=False)
async def radio_nowplaying_route(request: Request):
    code, body = await run_in_threadpool(
        api_radio.nowplaying_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


@app.get("/api/lyrics", include_in_schema=False)
async def lyrics_route(request: Request):
    code, body = await run_in_threadpool(
        api_playback.lyrics_payload, dict(request.query_params))
    return JSONResponse(body, status_code=code)


# ── Binary proxies ────────────────────────────────────────────────────
# /art is a one-shot image proxy (lock-screen artwork must be same-origin).
# It shares api_playback.art_fetch with the legacy handler; the blocking
# fetch runs in a threadpool. /stream (Range) and /radio_stream (ICY) are the
# byte relays, served as StreamingResponse over a threadpool-driven upstream.

@app.get("/art", include_in_schema=False)
async def art(url: str = ""):
    code, ctype, body = await run_in_threadpool(api_playback.art_fetch, url)
    if code != 200:
        return JSONResponse({"error": ctype}, status_code=code)
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


async def _audio_relay_response(url: str, range_hdr: str):
    """Open `url` (forwarding `range_hdr`) and return a StreamingResponse that
    relays the (200/206) body same-origin, or a 502 JSONResponse if the
    upstream is unreachable. Shared by /stream and Subsonic /rest/stream. Each
    blocking upstream read runs in a threadpool; the generator closes the
    upstream when the client disconnects (Hypercorn closes it)."""
    conn, resp = await run_in_threadpool(
        dlna_stream_proxy.open_stream_upstream, url, range_hdr)
    if resp is None:
        return JSONResponse({"error": "upstream unreachable"}, status_code=502)

    out = {"Access-Control-Allow-Origin": "*"}
    for hname in ("Content-Range", "Accept-Ranges", "Content-Length",
                  "Last-Modified", "ETag"):
        v = resp.getheader(hname)
        if v:
            out[hname] = v
    ctype = dlna_stream_proxy.normalize_audio_ctype(
        resp.getheader("Content-Type") or "")
    status = resp.status

    async def _relay():
        try:
            while True:
                chunk = await run_in_threadpool(resp.read, 65_536)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return StreamingResponse(_relay(), status_code=status,
                             media_type=ctype, headers=out)


@app.get("/stream", include_in_schema=False)
async def stream(request: Request, url: str = ""):
    """Browser-audio Range relay. Forwards the client's Range to the upstream
    and streams the (200 or 206) response back same-origin."""
    if not url:
        return JSONResponse({"error": "Missing url"}, status_code=400)
    return await _audio_relay_response(url, request.headers.get("range", ""))


@app.get("/radio_stream", include_in_schema=False)
async def radio_stream(request: Request, url: str = ""):
    """Internet-radio browser relay. De-interleaves ICY metadata (parking
    each StreamTitle for /api/radio/nowplaying) and streams clean audio to
    <audio>. Endless stream — the generator stops when Hypercorn reports the
    client gone, and closes the upstream in its finally."""
    if not url:
        return JSONResponse({"error": "Missing url"}, status_code=400)
    conn, resp, metaint, ctype = await run_in_threadpool(
        dlna_stream_proxy.open_radio_upstream, url)
    if resp is None:
        return JSONResponse({"error": "radio upstream unreachable"},
                            status_code=502)
    media_type = dlna_stream_proxy.normalize_audio_ctype(ctype) or "audio/mpeg"

    async def _relay():
        gen = dlna_stream_proxy.iter_radio_audio(resp, metaint, url)
        try:
            while True:
                chunk = await run_in_threadpool(next, gen, b"")
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return StreamingResponse(
        _relay(), media_type=media_type,
        headers={"Access-Control-Allow-Origin": "*",
                 "Cache-Control": "no-store"})


# ── Server-Sent Events (R2) ───────────────────────────────────────────
# Long-lived text/event-stream the PWA subscribes to (EventSource) for live
# pushes — now-playing, index progress, device changes — instead of polling.
# Worker threads call dlna_events.EVENTS.publish({...}); the bus (bound to this
# loop in _lifespan) fans each event to every connected subscriber's queue.
# A 15 s comment heartbeat keeps the connection alive through proxies/idle.
_SSE_HEARTBEAT_SEC = 15.0


@app.get("/api/events", include_in_schema=False)
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
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"      # SSE comment frame
        finally:
            EVENTS.unsubscribe(q)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store",
                 "Access-Control-Allow-Origin": "*",
                 "X-Accel-Buffering": "no"})      # disable proxy buffering


# ── Subsonic API (/rest/*) ────────────────────────────────────────────
# CarPlay/Amperfy surface. The ~25 JSON/XML methods are bridged through the
# capture handler (run_subsonic_sync), so api_subsonic.handle() — auth, the
# xml/json wrapper, every method — runs UNCHANGED under Hypercorn. The 3 byte
# methods (stream / download / getCoverArt) are served natively here, reusing
# the same upstream-open + art_fetch the /stream and /art routes use; a shared
# auth gate enforces the same SUBSONIC_PASSWORD check as the bridged methods.
_SUBSONIC_BYTE_METHODS = {"stream", "download", "getcoverart"}


def _subsonic_fail_response(query, body, code: int, message: str,
                            http_code: int = 200) -> Response:
    """Build a format-correct Subsonic error Response via api_subsonic's own
    `_fail`, so the xml/json choice + `<subsonic-response>` wrapper match the
    bridged JSON methods exactly."""
    params = api_subsonic._parse_params(query, body)
    fmt = (params.get("f", "") or "").lower()
    h = _LegacyH({}, "", "GET")
    h._subsonic_format = "json" if fmt in ("json", "jsonp") else "xml"
    api_subsonic._fail(h, code, message, http_code=http_code)
    return Response(content=h._cap.body, status_code=h._cap.code,
                    media_type=h._cap.ctype)


def _subsonic_auth_gate(query, body) -> Optional[Response]:
    """Return a refusal Response (password unset → 503; bad credentials →
    wrong-auth) or None when the call is authorised. Mirrors handle()'s gate
    so the native byte routes enforce the same auth as the bridged methods."""
    params = api_subsonic._parse_params(query, body)
    if not api_subsonic._subsonic_password():
        return _subsonic_fail_response(
            query, body, api_subsonic.ERR_NOT_AUTHORIZED,
            "Server not configured (SUBSONIC_PASSWORD unset)", http_code=503)
    if not api_subsonic._check_auth(params):
        return _subsonic_fail_response(
            query, body, api_subsonic.ERR_WRONG_AUTH,
            "Wrong username or password")
    return None


@app.api_route("/rest/{rest_path:path}", methods=["GET", "POST"],
               include_in_schema=False)
async def subsonic(request: Request, rest_path: str):
    query = request.url.query
    body = await request.body() if request.method == "POST" else b""
    method = api_subsonic._method_from_path("/rest/" + rest_path).lower()

    if method in _SUBSONIC_BYTE_METHODS:
        gate = _subsonic_auth_gate(query, body)
        if gate is not None:
            return gate
        params = api_subsonic._parse_params(query, body)
        sid = params.get("id", "")
        if method == "getcoverart":
            art_url = await run_in_threadpool(api_subsonic._cover_art_url, sid)
            if not art_url:
                return Response(content=b"no art", status_code=404)
            code, ctype, art_body = await run_in_threadpool(
                api_playback.art_fetch, art_url)
            if code != 200:
                return JSONResponse({"error": ctype}, status_code=code)
            return Response(content=art_body, media_type=ctype,
                            headers={"Cache-Control": "public, max-age=86400",
                                     "Access-Control-Allow-Origin": "*"})
        # stream / download
        url = api_subsonic._track_id_decode(sid)
        if not url:
            return _subsonic_fail_response(
                query, body, api_subsonic.ERR_NOT_FOUND,
                f"Unknown track id: {sid}")
        return await _audio_relay_response(url, request.headers.get("range", ""))

    # JSON/XML method → bridge api_subsonic.handle() over the capture handler.
    code, resp_body, ctype = await run_in_threadpool(
        run_subsonic_sync, request.method, "/rest/" + rest_path, query, body,
        headers=request.headers)
    return Response(content=resp_body, status_code=code, media_type=ctype)


# Paths served by a native route above — must NOT also be bridged.
_NATIVE = {"/api/version", "/api/servers", "/api/renderers",
           "/api/artists", "/api/albums", "/api/genres",
           "/api/artist_albums", "/api/artist_tracks",
           "/api/genre_albums", "/api/genre_tracks", "/api/album_tracks",
           "/api/decades", "/api/decade_albums", "/api/decade_tracks",
           "/api/search", "/api/browse_letter",
           "/api/index/status", "/api/track_meta", "/api/playlists",
           "/api/playlist", "/api/album_favourites",
           "/api/album_favourites/check", "/api/radio/favourites",
           "/api/browse", "/api/radio", "/api/radio/search",
           "/api/radio/nowplaying", "/api/lyrics"}


# ── Bridged legacy read routes ────────────────────────────────────────
# Register the remaining JSON read API through the compatibility shim so
# the whole read API runs under Hypercorn TODAY, while handlers are
# migrated to native routes (the _NATIVE set above) one batch at a time.
# Excluded from the bridge:
#   • _NATIVE                — already ported to native FastAPI routes
#   • /stream /art /radio_stream — native StreamingResponse/Response byte
#       relays (in _STREAMING); never bridged
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
