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
import struct
import threading
import time
import urllib.parse
import zlib
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
import api_upnp
import dlna_routes
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
    AcoustID startup scans, and LocalFs wiring, via
    dlna_gateway.start_background_services. The Naim-facing /gw/* UPnP surface
    is served NATIVELY by THIS app on the plain port (PLAIN_PORT) — Hypercorn
    owns TLS on :8443 but the Naim can't do HTTPS, so the SSDP advert +
    device.xml URLBase point at PLAIN_PORT. Hypercorn is the only server
    (Cleanup C retired the separate stdlib device tier).

    Env knobs:
      • GATEWAY_NO_SERVICES=1 — web tier only; skip ALL background startup.
      • GATEWAY_PLAIN_PORT (default 8765) — the plain-HTTP port the Naim reaches
        /gw/* on, advertised in the gateway-as-MediaServer SSDP record.
      • GATEWAY_DEBUG=1 — verbose logging."""
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
    # Raise the shared threadpool ceiling. Starlette's run_in_threadpool draws
    # from anyio's default limiter (40 tokens) — and EVERY blocking op shares it:
    # browse/DB queries, /art fetches, the legacy bridge, AND each byte-relay
    # read of every audio stream. Under a few concurrent iOS streams + browsing
    # + art loads the 40 slots saturate, new requests queue, and the audio
    # element's next Range request stalls long enough that iOS aborts the load
    # (the "stops after one track" / code-4 NETWORK_NO_SOURCE regression). 256
    # tokens is well under the raised FD limit and removes the serialization.
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = 256
    except Exception:                                   # noqa: BLE001
        log.warning("could not raise threadpool limit; staying at anyio default")
    EVENTS.bind_loop(loop)
    loop.set_exception_handler(_loop_exception_handler)
    started = False
    if not _truthy("GATEWAY_NO_SERVICES"):
        dlna_gateway.setup_logging(debug=_truthy("GATEWAY_DEBUG"))
        lan_ip = dlna_gateway.get_lan_ip()
        # Cleanup C: /gw/* is served by THIS app on the plain port (PLAIN_PORT,
        # a module global defined below); the SSDP advert + device.xml URLBase
        # point there — no separate device server any more.
        dlna_gateway.start_background_services(lan_ip, PLAIN_PORT)
        started = True
        log.info("ASGI lifespan: gateway background services started "
                 f"(/gw/* native + SSDP advert on :{PLAIN_PORT})")
    try:
        yield
    finally:
        if started:
            try:
                dlna_gateway.gw_ssdp_byebye(dlna_gateway.get_lan_ip(), PLAIN_PORT)
            except Exception:                       # noqa: BLE001
                pass
        EVENTS.bind_loop(None)      # drop the (now closing) loop reference


# docs_url=None: disable the Swagger UI page — it pulls swagger-ui assets from a
# CDN (jsdelivr) on load, an outbound call we don't want from a LAN/tailnet-only
# gateway. redoc_url already off. (Cutover runbook step 1, privacy.)
app = FastAPI(title="DLNA Gateway", version=VERSION, docs_url=None,
              redoc_url=None, lifespan=_lifespan)

# The plain-HTTP port the Naim reaches /gw/* on (Hypercorn's --insecure-bind).
# device.xml's URLBase + the SSDP advert point here (NEVER the TLS :8443 — the
# Naim can't do HTTPS). Cleanup C: /gw/* is served by this app on this port,
# replacing the old separate device server on :8770.
PLAIN_PORT = int(os.environ.get("GATEWAY_PLAIN_PORT", "8765"))


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
    code, ctype, body = await run_in_threadpool(api_playback.art_fetch_cached, url)
    if code != 200:
        return JSONResponse({"error": ctype}, status_code=code)
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


# ── Video (PWA, SAME-ORIGIN) ───────────────────────────────────────────
# The PWA <video> can't use the :8200 /localfs/video URL — over HTTPS that's
# mixed content (blocked) + cross-origin. Serve video + posters from THIS app
# (same origin). FileResponse handles Range automatically (seek/scrub). The LG
# TV keeps using :8200 /localfs/video directly (not a browser).
_VIDEO_UDN = "uuid:localfs-movies"


def _video_payload(v: dict) -> dict:
    return {
        "id": v["id"], "title": v.get("title"), "folder": v.get("folder"),
        "duration": v.get("duration"), "width": v.get("width"),
        "height": v.get("height"), "vcodec": v.get("vcodec"),
        "acodec": v.get("acodec"), "container": v.get("container"),
        "mime": v.get("mime"), "created": v.get("created"),
        "location_name": v.get("location_name"),
        "country": v.get("country"),
        "playUrl": f"/video/{v['id']}",
        "transcodeUrl": f"/video_transcode/{v['id']}",
        "hlsUrl": f"/video_hls/{v['id']}/index.m3u8",
        "posterUrl": (f"/video_poster?id={v['id']}" if v.get("poster") else ""),
    }


@app.get("/api/videos")
async def videos() -> list:
    rows = await run_in_threadpool(DB.all_videos, _VIDEO_UDN)
    return [_video_payload(v) for v in rows]


@app.get("/api/video_meta")
async def video_meta(id: str = ""):
    v = await run_in_threadpool(DB.video_by_id, id) if id else None
    if not v:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _video_payload(v)


@app.get("/video/{vid}", include_in_schema=False)
async def video_file(vid: str):
    v = await run_in_threadpool(DB.video_by_id, vid)
    if not v or not v.get("file_path") or not os.path.isfile(v["file_path"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(v["file_path"], media_type=(v.get("mime") or "video/mp4"))


@app.get("/video_poster", include_in_schema=False)
async def video_poster(id: str = ""):
    import dlna_ffmpeg
    p = os.path.join(dlna_ffmpeg.POSTER_DIR, f"{os.path.basename(id)}.jpg")
    if not id or not os.path.isfile(p):
        return JSONResponse({"error": "no poster"}, status_code=404)
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/video_transcode/{vid}", include_in_schema=False)
async def video_transcode(vid: str):
    """On-demand transcode → H.264/AAC fragmented MP4, streamed (V3). The PWA
    falls back here for clips the browser can't decode natively (HEVC / MKV /
    E-AC3). ffmpeg absent → 503 (native-only still works). Progressive (no
    Range/seek yet — HLS is the future upgrade)."""
    import dlna_ffmpeg
    v = await run_in_threadpool(DB.video_by_id, vid)
    if not v or not v.get("file_path") or not os.path.isfile(v["file_path"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not dlna_ffmpeg.find_ffmpeg():
        return JSONResponse({"error": "ffmpeg not available"}, status_code=503)
    cmd = dlna_ffmpeg.transcode_cmd(v["file_path"])
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def _pump():
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:        # client disconnected / done
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

    return StreamingResponse(_pump(), media_type="video/mp4",
                             headers={"Cache-Control": "no-store",
                                      "Connection": "close"})


@app.get("/video_hls/{vid}/{seg}", include_in_schema=False)
async def video_hls(vid: str, seg: str):
    """SEEKABLE transcode via on-demand HLS (V3+). `index.m3u8` = a VOD playlist
    computed from the duration (instant, no transcode); `segN.ts` = that ~6s
    segment transcoded to H.264/AAC MPEG-TS on demand → the player fetches only
    the segment for the seek target. ffmpeg absent → 503."""
    import dlna_ffmpeg
    import re as _re
    v = await run_in_threadpool(DB.video_by_id, vid)
    if not v or not v.get("file_path") or not os.path.isfile(v["file_path"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not dlna_ffmpeg.find_ffmpeg():
        return JSONResponse({"error": "ffmpeg not available"}, status_code=503)

    if seg == "index.m3u8":
        pl = dlna_ffmpeg.hls_playlist(v.get("duration") or 0)
        return Response(pl, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})

    m = _re.fullmatch(r"seg(\d+)\.ts", seg)
    if not m:
        return JSONResponse({"error": "bad segment"}, status_code=404)
    start = int(m.group(1)) * dlna_ffmpeg.HLS_SEG
    cmd = dlna_ffmpeg.hls_segment_cmd(v["file_path"], start)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def _pump():
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

    return StreamingResponse(_pump(), media_type="video/mp2t",
                             headers={"Cache-Control": "no-store"})


# ── Gateway-as-MediaServer UPnP surface (/gw/*) ────────────────────────
# Cleanup C: the Naim browses these over plain HTTP on PLAIN_PORT (:8765). They
# reuse api_upnp's pure helpers, so the SOAP/descriptors are byte-identical to
# the retired dlna_server device tier. Served on both binds; the Naim uses the
# plain one (device.xml's URLBase = http://<lan-ip>:PLAIN_PORT).
_GW_XML = 'text/xml; charset="utf-8"'


def _peer(request: Request) -> str:
    client = getattr(request, "client", None)
    return client.host if client else "?"


@app.get("/gw/device.xml", include_in_schema=False)
async def gw_device_xml(request: Request):
    import dlna_gateway
    log.debug("GW /gw/device.xml fetched by %s (ua=%s)", _peer(request),
              request.headers.get("user-agent", "")[:80])
    lan_ip = await run_in_threadpool(dlna_gateway.get_lan_ip)
    return Response(api_upnp._gw_device_xml(lan_ip, PLAIN_PORT).encode(),
                    media_type=_GW_XML)


@app.get("/gw/cd/desc.xml", include_in_schema=False)
async def gw_cd_desc(request: Request):
    log.debug("GW /gw/cd/desc.xml fetched by %s", _peer(request))
    return Response(api_upnp._gw_cd_desc_xml().encode(), media_type=_GW_XML)


async def _gw_event_route(request: Request, label: str, props: dict):
    """Shared GENA handler for /gw/cd/events + /gw/cm/events: a valid SUBSCRIBE
    (SID + TIMEOUT) then the initial NOTIFY — strict GUPnP/dLeyna needs both."""
    log.debug("GW %s %s by %s", label, request.method, _peer(request))
    if request.method == "SUBSCRIBE":
        hdrs, callback, sid = await run_in_threadpool(
            api_upnp.gw_event_subscribe, dict(request.headers))
        if callback:
            # Fire the initial NOTIFY in a daemon thread — NOT
            # asyncio.create_task (an un-referenced task can be GC'd before it
            # runs, so the NOTIFY would never be sent and a GUPnP/dLeyna client
            # would keep re-subscribing and never browse).
            threading.Thread(
                target=api_upnp.gw_event_initial_notify,
                args=(callback, sid, props), daemon=True).start()
        return Response(status_code=200, headers=hdrs)
    return Response(status_code=200)            # GET / UNSUBSCRIBE


@app.api_route("/gw/cd/events", methods=["GET", "SUBSCRIBE", "UNSUBSCRIBE"],
               include_in_schema=False)
async def gw_cd_events(request: Request):
    return await _gw_event_route(request, "/gw/cd/events", {"SystemUpdateID": "1"})


@app.get("/gw/cm/desc.xml", include_in_schema=False)
async def gw_cm_desc(request: Request):
    log.debug("GW /gw/cm/desc.xml fetched by %s", _peer(request))
    return Response(api_upnp._gw_cm_desc_xml().encode(), media_type=_GW_XML)


@app.post("/gw/cm/control", include_in_schema=False)
async def gw_cm_control(request: Request):
    body = await request.body()
    status, ctype, payload = await run_in_threadpool(api_upnp.cm_control_soap, body)
    if status != 200:
        action = (request.headers.get("soapaction", "").rsplit("#", 1)[-1]
                  .strip('"') or "?")
        log.warning("GW /gw/cm/control → %s for action=%s", status, action)
    return Response(payload, status_code=status, media_type=ctype)


@app.api_route("/gw/cm/events", methods=["GET", "SUBSCRIBE", "UNSUBSCRIBE"],
               include_in_schema=False)
async def gw_cm_events(request: Request):
    return await _gw_event_route(request, "/gw/cm/events", {
        "SourceProtocolInfo": api_upnp._GW_SOURCE_PROTOCOLS,
        "SinkProtocolInfo": "", "CurrentConnectionIDs": "0"})


@app.post("/gw/cd/control", include_in_schema=False)
async def gw_cd_control(request: Request):
    body = await request.body()
    status, ctype, payload = await run_in_threadpool(
        api_upnp.cd_control_soap, body)
    if status != 200:
        action = (request.headers.get("soapaction", "").rsplit("#", 1)[-1]
                  .strip('"') or "?")
        log.warning("GW /gw/cd/control → %s for action=%s", status, action)
    return Response(payload, status_code=status, media_type=ctype)


async def _audio_relay_response(url: str, range_hdr: str, client: str = "?"):
    """Open `url` (forwarding `range_hdr`) and return a StreamingResponse that
    relays the (200/206) body same-origin, or a 502 JSONResponse if the
    upstream is unreachable. Shared by /stream and Subsonic /rest/stream. Each
    blocking upstream read runs in a threadpool; the generator closes the
    upstream when the client disconnects (Hypercorn closes it). `client` is
    the requesting peer's IP — a 100.x address means tailnet (CarPlay/Amperfy
    or remote PWA), a 192.168.x one means LAN."""
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

    # Observability lost in the 2.0 native rewrite: log a START/END pair per
    # relay (parity with dlna_stream_proxy.proxy_stream) so stream failures are
    # greppable in gateway.log again. host/path keeps it terse; reason on END is
    # eof (upstream done), client_closed (browser/Hypercorn dropped), or error:T.
    try:
        _pr = urllib.parse.urlsplit(url)
        _tag = f"{_pr.hostname}{_pr.path}"
    except Exception:                                   # noqa: BLE001
        _tag = url[:80]
    log.info(f"stream ▶ START {_tag} ({status}) client={client}")
    _t0 = time.monotonic()

    async def _relay():
        # 256 KB reads (vs 64 KB) quarter the threadpool round-trips per stream,
        # so a long audio relay acquires/releases the shared limiter far less and
        # competes less with browse/art for it.
        sent = 0
        reason = "eof"
        try:
            while True:
                chunk = await run_in_threadpool(resp.read, 262_144)
                if not chunk:
                    break
                sent += len(chunk)
                yield chunk
        except asyncio.CancelledError:
            reason = "client_closed"
            raise
        except Exception as e:                          # noqa: BLE001
            reason = f"error:{type(e).__name__}"
            raise
        finally:
            log.info(f"stream ■ END   {_tag} sent={sent} "
                     f"in {time.monotonic()-_t0:.1f}s reason={reason} "
                     f"client={client}")
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
    return await _audio_relay_response(url, request.headers.get("range", ""),
                                       client=_peer(request))


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
    # One INFO line per /rest request (method, client app, peer IP, and for
    # the bridged methods status + duration) — Amperfy/CarPlay traffic is
    # otherwise invisible at the default log level, which made the 2026-07-02
    # "flaky in the car" afternoon undiagnosable from gateway.log.
    _params = api_subsonic._parse_params(query, body)
    _client = _params.get("c", "?")
    _ip = _peer(request)

    if method in _SUBSONIC_BYTE_METHODS:
        gate = _subsonic_auth_gate(query, body)
        if gate is not None:
            log.info(f"Subsonic {method} client={_client!r} ip={_ip} → refused")
            return gate
        params = _params
        sid = params.get("id", "")
        log.info(f"Subsonic {method} id={sid[:48]} client={_client!r} ip={_ip}")
        if method == "getcoverart":
            # Try every candidate art URL for the id (folder albums have one
            # per track; some files lack embedded art) and serve the first that
            # actually fetches 200 — not an arbitrary one that may 404.
            code, ctype, art_body = await run_in_threadpool(
                api_subsonic._resolve_cover, sid, api_playback.art_fetch_cached)
            if code != 200:
                return Response(content=b"no art", status_code=404)
            return Response(content=art_body, media_type=ctype,
                            headers={"Cache-Control": "public, max-age=86400",
                                     "Access-Control-Allow-Origin": "*"})
        # stream / download
        url = api_subsonic._track_id_decode(sid)
        if not url:
            return _subsonic_fail_response(
                query, body, api_subsonic.ERR_NOT_FOUND,
                f"Unknown track id: {sid}")
        return await _audio_relay_response(url, request.headers.get("range", ""),
                                           client=_ip)

    # JSON/XML method → bridge api_subsonic.handle() over the capture handler.
    _t0 = time.monotonic()
    code, resp_body, ctype = await run_in_threadpool(
        run_subsonic_sync, request.method, "/rest/" + rest_path, query, body,
        headers=request.headers)
    log.info(f"Subsonic {method} client={_client!r} ip={_ip} "
             f"→ {code} in {(time.monotonic()-_t0)*1000:.0f}ms")
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


def _make_icon_png(size: int) -> bytes:
    """Generate a simple PNG icon: dark square with amber ♪ symbol.
    Relocated from dlna_server.py (retired in Cleanup C) — this app is now the
    sole server, so the only caller (the /icon routes) keeps it local."""
    bg    = (14, 13, 11)
    amber = (212, 168, 67)

    img = [list(bg + (255,)) for _ in range(size * size)]

    s = size / 192.0

    def filled_circle(cx, cy, rad, color):
        for dy in range(-rad - 1, rad + 2):
            for dx in range(-rad - 1, rad + 2):
                if dx * dx + dy * dy <= rad * rad:
                    px, py = int(cx + dx), int(cy + dy)
                    if 0 <= px < size and 0 <= py < size:
                        img[py * size + px] = list(color + (255,))

    def filled_rect(x1, y1, x2, y2, color):
        for py in range(max(0, y1), min(size, y2)):
            for px in range(max(0, x1), min(size, x2)):
                img[py * size + px] = list(color + (255,))

    sx, sy = int(110 * s), int(60 * s)
    sw, sh = max(6, int(12 * s)), int(90 * s)
    filled_rect(sx, sy, sx + sw, sy + sh, amber)
    nx, ny = int(88 * s), int(135 * s)
    nr = max(8, int(20 * s))
    filled_circle(nx, ny, nr, amber)
    fx, fy = sx + sw, sy
    filled_rect(fx, fy, fx + int(35 * s), fy + int(10 * s), amber)
    filled_rect(fx, fy + int(20 * s), fx + int(28 * s), fy + int(30 * s), amber)

    def png_chunk(tag, data):
        c = zlib.crc32(tag + data) & 0xffffffff
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', c)

    raw = b''
    for row in range(size):
        raw += bytes([0])
        for col in range(size):
            raw += bytes(img[row * size + col])

    compressed = zlib.compress(raw, 6)
    png  = b'\x89PNG\r\n\x1a\n'
    png += png_chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
    png += png_chunk(b'IDAT', compressed)
    png += png_chunk(b'IEND', b'')
    return png


def _icon(size: int) -> Response:
    return Response(content=_make_icon_png(size),
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
