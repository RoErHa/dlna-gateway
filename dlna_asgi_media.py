#!/usr/bin/env python3
"""
dlna_asgi_media.py — the byte relays: cover art and the audio proxies.

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

`/art` is same-origin because iOS MediaSession REFUSES cross-origin
lock-screen artwork; the PWA rewrites every art URL through here. It honours
a `size` bucket (96/256/512/1024) — a 772 KB cover is 12 KB at size=256,
which is the difference between a snappy album grid and a slow one.

`/stream` is a byte-perfect Range pass-through; the only mutation is
normalising `audio/x-flac` → `audio/flac` for Safari. `/radio_stream`
de-interleaves ICY metadata out of the audio so `<audio>` can play it.

The relay threadpool ceiling was raised 40 → 256 in the lifespan: audio
relays were starving behind browse/art traffic, which is the origin of the
2.0 "stops after one track" bug.
"""
import asyncio
import logging
import time
import urllib.parse

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response, StreamingResponse

import api_playback
import dlna_stream_proxy
from dlna_asgi_state import _peer
from dlna_config import close_quietly

router = APIRouter()

log = logging.getLogger("dlna.asgi")


# ── Binary proxies ────────────────────────────────────────────────────
# /art is a one-shot image proxy (lock-screen artwork must be same-origin).
# It shares api_playback.art_fetch with the legacy handler; the blocking
# fetch runs in a threadpool. /stream (Range) and /radio_stream (ICY) are the
# byte relays, served as StreamingResponse over a threadpool-driven upstream.

@router.get("/art", include_in_schema=False)
async def art(url: str = "", size: int = 0):
    """Same-origin artwork proxy for the PWA (iOS won't load cross-origin art
    on the lock screen), now with the same `size` downscaling the Subsonic
    getCoverArt route uses.

    Why it matters here too (2026-08-07): the PWA asked for the FULL-resolution
    cover everywhere — a 36px list thumbnail and a 130px grid card both pulled
    the multi-MB embedded original. The album cover grid made that worse by
    putting a dozen of them on screen at once. `size` snaps to the shared
    96/256/512/1024 bucket ladder and each bucket is scaled once and cached on
    disk, so the original is fetched at most once no matter how many sizes are
    asked for.

    `size=0` (or absent) is exactly the old behaviour: the unmodified original.
    Pillow is optional — without it every size serves the original, so the PWA
    degrades to the old bandwidth, never to a broken image."""
    code, ctype, body = await run_in_threadpool(
        api_playback.art_fetch_scaled, url, size)
    if code != 200:
        return JSONResponse({"error": ctype}, status_code=code)
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


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
    except ValueError:
        _tag = url[:80]        # log-tag only; a bad url must never break the relay
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
            close_quietly(conn)

    return StreamingResponse(_relay(), status_code=status,
                             media_type=ctype, headers=out)


@router.get("/stream", include_in_schema=False)
async def stream(request: Request, url: str = ""):
    """Browser-audio Range relay. Forwards the client's Range to the upstream
    and streams the (200 or 206) response back same-origin."""
    if not url:
        return JSONResponse({"error": "Missing url"}, status_code=400)
    return await _audio_relay_response(url, request.headers.get("range", ""),
                                       client=_peer(request))


@router.get("/radio_stream", include_in_schema=False)
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
            close_quietly(conn)

    return StreamingResponse(
        _relay(), media_type=media_type,
        headers={"Access-Control-Allow-Origin": "*",
                 "Cache-Control": "no-store"})
