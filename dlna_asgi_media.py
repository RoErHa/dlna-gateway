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
import os
import time
import urllib.parse

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response, StreamingResponse

import api_playback
import dlna_stream_proxy
import dlna_asgi_state as _st
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
        # Deliberately opaque. This used to return the upstream status and the
        # raw exception text, which made /art a clean open/closed/filtered
        # oracle for any address ("Upstream 404" vs "Connection refused" vs
        # "timed out"). The detail is logged, not served; every failure looks
        # identical to the caller.
        log.info(f"/art refused or failed ({code}) for {url[:120]!r}: {ctype}")
        return JSONResponse({"error": "art unavailable"}, status_code=502)
    return Response(content=body, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400",
                             "Access-Control-Allow-Origin": "*"})


# How much of a ranged response one /stream request may deliver. See the long
# note in _audio_relay_response for why a cap has to exist at all. 8 MB is
# roughly 70 s of 16/44 FLAC — comfortably more than any player buffers ahead,
# small enough that Safari's habit of opening many concurrent Range requests
# for one track costs tens of megabytes instead of hundreds. 0 disables the
# cap (env STREAM_SLICE_BYTES) for anyone who wants the old behaviour back.
_MAX_SLICE = max(0, int(os.environ.get("STREAM_SLICE_BYTES", 8 * 1024 * 1024)))


def _clamp_content_range(content_range: str, limit: int):
    """Shrink an upstream `Content-Range: bytes S-E/T` to at most `limit`
    bytes, returning `(content_range, content_length, limit_bytes)` — or None
    when it is absent, unparseable, or already within the cap (nothing to do).

    Pure so the arithmetic is directly testable: an off-by-one here is a
    corrupt audio stream, and the browser reports that as an unplayable file
    rather than as a bad byte count."""
    try:
        units, _, rng = content_range.strip().partition(" ")
        if units != "bytes":
            return None
        span, _, total = rng.partition("/")
        start_s, _, end_s = span.partition("-")
        start, end = int(start_s), int(end_s)
    except (ValueError, AttributeError):
        return None
    if end < start or (end - start + 1) <= limit:
        return None
    end = start + limit - 1
    return f"bytes {start}-{end}/{total}", str(limit), limit


async def _audio_relay_response(url: str, range_hdr: str, client: str = "?"):
    """Open `url` (forwarding `range_hdr`) and return a StreamingResponse that
    relays the (200/206) body same-origin, or a 502 JSONResponse if the
    upstream is unreachable. Shared by /stream and Subsonic /rest/stream. Each
    blocking upstream read runs in a threadpool; the generator closes the
    upstream when the client disconnects (Hypercorn closes it). `client` is
    the requesting peer's IP — a 100.x address means tailnet (CarPlay/Amperfy
    or remote PWA), a 192.168.x one means LAN."""
    # Capped: a relay holds a client socket AND an upstream socket for as long
    # as the client keeps it, so an uncapped one is a file-descriptor
    # exhaustion primitive. See ConcurrencyCap in dlna_asgi_state.
    if not _st.AUDIO_RELAYS.acquire():
        return JSONResponse({"error": "too many concurrent streams"},
                            status_code=503, headers={"Retry-After": "5"})
    try:
        conn, resp = await run_in_threadpool(
            dlna_stream_proxy.open_stream_upstream, url, range_hdr)
    except BaseException:
        _st.AUDIO_RELAYS.release()
        raise
    if resp is None:
        _st.AUDIO_RELAYS.release()
        return JSONResponse({"error": "stream unavailable"}, status_code=502)

    status = resp.status

    # An upstream error body is NOT media. Relaying it verbatim (which this
    # did until 2026-08-25) hands `<audio>` a 404 page typed as audio/flac;
    # the element reports MediaError.code 4 "unsupported format" and the PWA
    # skips the track. That is what a playlist row pointing at a track the
    # index no longer has looks like from the sofa: songs silently skipped,
    # nothing in the log that says why. Fail the request instead, and say so
    # once in the log with the upstream status the caller never sees.
    # (416 is passed through: it is a real, bodyless Range answer.)
    if status == 416:
        cr = resp.getheader("Content-Range") or ""
        close_quietly(conn)
        _st.AUDIO_RELAYS.release()
        return Response(status_code=416,
                        headers={"Content-Range": cr} if cr else None)
    if status not in (200, 206):
        log.warning(f"stream ✗ upstream {status} for {url[:160]} "
                    f"client={client} — refusing to relay a non-media body")
        close_quietly(conn)
        _st.AUDIO_RELAYS.release()
        # Opaque to the caller, same rule as /art: the status must not become
        # a probe oracle. The detail is in the line above.
        return JSONResponse({"error": "stream unavailable"}, status_code=502)

    out = {"Access-Control-Allow-Origin": "*"}
    for hname in ("Content-Range", "Accept-Ranges", "Content-Length",
                  "Last-Modified", "ETag"):
        v = resp.getheader(hname)
        if v:
            out[hname] = v
    ctype = dlna_stream_proxy.normalize_audio_ctype(
        resp.getheader("Content-Type") or "")

    # Bound how much of the file one request may pull ahead of the client.
    #
    # Hypercorn 0.18's HTTP/2 path has no effective backpressure: its
    # StreamBuffer.pop() unpauses the producer whenever the chunk it popped is
    # under the low-water mark, INCLUDING the empty chunk it pops when the
    # peer's flow-control window is shut. So a stalled reader does not stop
    # us; the generator below runs to EOF and the whole remainder of the file
    # lands in the worker's memory. Measured on this gateway: three 50 KB/s
    # clients pulling one 70 MB FLAC took RSS 195 MB → 383 MB, and every
    # `stream ■ END` was logged within 0.1 s while the clients still had
    # minutes of reading left. HTTP/1.1 backpressures properly; the PWA is on
    # h2, so the PWA is the case that breaks.
    #
    # We cannot see how much the client consumed, so we bound what we hand it:
    # a client that sent a Range gets at most _MAX_SLICE bytes of it, with a
    # truthful Content-Range/Content-Length, and asks for the next slice when
    # it wants more. Serving less of a range than was asked for is ordinary
    # HTTP and is exactly how `<audio>` already drives this endpoint — Safari
    # was observed issuing eleven concurrent Range requests for one track.
    # A request with NO Range header is left alone: it has not shown itself
    # Range-aware, and truncating it would corrupt a plain file download.
    if status == 206 and range_hdr and _MAX_SLICE > 0:
        sliced = _clamp_content_range(out.get("Content-Range", ""), _MAX_SLICE)
        if sliced is not None:
            out["Content-Range"], out["Content-Length"], limit = sliced
        else:
            limit = 0
    else:
        limit = 0

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
                want = 262_144
                if limit:
                    if sent >= limit:
                        reason = "slice_full"
                        break
                    want = min(want, limit - sent)
                chunk = await run_in_threadpool(resp.read, want)
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
            _st.AUDIO_RELAYS.release()

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
    # An endless stream by definition, so it holds its two sockets until the
    # client leaves — capped alongside the audio relays.
    if not _st.AUDIO_RELAYS.acquire():
        return JSONResponse({"error": "too many concurrent streams"},
                            status_code=503, headers={"Retry-After": "5"})
    try:
        conn, resp, metaint, ctype = await run_in_threadpool(
            dlna_stream_proxy.open_radio_upstream, url)
    except BaseException:
        _st.AUDIO_RELAYS.release()
        raise
    if resp is None:
        _st.AUDIO_RELAYS.release()
        return JSONResponse({"error": "stream unavailable"},
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
            _st.AUDIO_RELAYS.release()

    return StreamingResponse(
        _relay(), media_type=media_type,
        headers={"Access-Control-Allow-Origin": "*",
                 "Cache-Control": "no-store"})
