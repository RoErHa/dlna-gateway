#!/usr/bin/env python3
"""
dlna_asgi_subsonic.py — the `/rest/*` Subsonic surface (Amperfy/CarPlay).

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

Two shapes share one route. The BYTE methods (stream, download, getCoverArt)
are served natively here so they can stream and set their own headers; every
other method runs the shared `api_subsonic.handle()` through the bridge in a
threadpool.

Each request logs ONE line at INFO. That was added after an undiagnosable
"Amperfy is flaky in the car" afternoon — Subsonic traffic had been visible
only at debug. The diagnostic shortcut it buys: if `grep Subsonic
gateway.log` is EMPTY during a flaky window, the requests never reached the
gateway at all (phone-side Tailscale/cellular), and it is not a gateway
problem.
"""
import functools
import logging
import time

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

import api_playback
import api_subsonic
from dlna_asgi_media import _audio_relay_response
from dlna_asgi_state import _peer
from dlna_asgi_bridge import _LegacyH, run_subsonic_sync

router = APIRouter()

log = logging.getLogger("dlna.asgi")


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


def _subsonic_auth_gate(query, body) -> Response | None:
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


@router.api_route("/rest/{rest_path:path}", methods=["GET", "POST"],
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
            # Honour the Subsonic `size` box: Amperfy asks for a thumbnail
            # (~100–600 px) per list row, so serve a downscaled JPEG instead of
            # the full multi-MB embedded original — the dominant cost of a
            # library art-sync over the tailnet. Malformed size → 0 (original).
            try:
                _size = int(params.get("size", "0") or 0)
            except (TypeError, ValueError):
                _size = 0
            _fetch = functools.partial(api_playback.art_fetch_scaled, size=_size)
            # Try every candidate art URL for the id (folder albums have one
            # per track; some files lack embedded art) and serve the first that
            # actually fetches 200 — not an arbitrary one that may 404.
            code, ctype, art_body = await run_in_threadpool(
                api_subsonic._resolve_cover, sid, _fetch)
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
