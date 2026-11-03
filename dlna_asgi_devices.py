#!/usr/bin/env python3
"""
dlna_asgi_devices.py — forgetting a renderer.

  POST /api/renderers/forget {udn}  → 200 {forgotten: udn}
                                      400 missing udn · 403 not loopback
                                      404 not a known renderer

The gateway otherwise never forgets a renderer: `RENDERERS` only grows, and
SSDP `ssdp:byebye` is ignored on purpose (a Naim that announced byebye on
standby would drop out of the PWA's output picker). This exists for
`tests/chaos.py --fake-renderer`, which registers a fake renderer with the
live gateway through real discovery and must take it away again — and it
doubles as the manual way to drop a renderer that is gone for good.

**Loopback only.** Every other API route is open to the LAN and tailnet (that
is the access model — see docs/SECURITY_POSTURE.md); a WRITE that removes
devices is not something a LAN peer needs. Hypercorn terminates every
connection itself, so `request.client` is the real peer, not a proxy's.
Misuse would be cheap anyway: a forgotten live renderer is rediscovered on
the next M-SEARCH (≤ 30 s), because forgetting also clears its seen location.

Its own module because `dlna_asgi_browse.py` (which serves GET
/api/renderers) sits at its line budget.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import dlna_discovery
from dlna_asgi_state import _peer

log = logging.getLogger("dlna.asgi")

router = APIRouter()

_LOOPBACK = {"127.0.0.1", "::1"}


@router.post("/api/renderers/forget", include_in_schema=False)
async def forget_renderer_route(request: Request):
    peer = _peer(request)
    if peer not in _LOOPBACK:
        log.warning(f"renderers/forget refused for non-loopback peer {peer}")
        return JSONResponse({"error": "loopback only"}, status_code=403)
    try:
        body = json.loads(await request.body() or b"{}")
        udn = body.get("udn", "") if isinstance(body, dict) else ""
    except (ValueError, UnicodeDecodeError):
        udn = ""
    if not isinstance(udn, str) or not udn:
        return JSONResponse({"error": "missing udn"}, status_code=400)
    # Blocking: stopping a playing queue sends a Stop SOAP to the renderer.
    if not await run_in_threadpool(dlna_discovery.forget_renderer, udn):
        return JSONResponse({"error": "unknown renderer"}, status_code=404)
    log.info(f"renderers/forget: {udn} forgotten (peer {peer})")
    return JSONResponse({"forgotten": udn})
