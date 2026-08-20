#!/usr/bin/env python3
"""
dlna_asgi_upnp.py — the `/gw/*` UPnP surface the Naim and the LG TV talk
to, on the PLAIN :8765 bind.

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

These stay plain HTTP deliberately: the renderers do not do TLS here, and
they fetch from this surface directly.

⚠ The initial GENA NOTIFY is fired on a daemon `threading.Thread`, NOT an
asyncio task — an un-referenced task is garbage-collected before it runs, the
NOTIFY never sends, and GUPnP/dLeyna then re-SUBSCRIBEs forever without ever
browsing. That was the last bug of the 2026-06-13 session; do not
"modernise" it.
"""
import logging
import threading

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

import api_upnp
from dlna_asgi_state import PLAIN_PORT, _peer

router = APIRouter()

log = logging.getLogger("dlna.asgi")


# ── Gateway-as-MediaServer UPnP surface (/gw/*) ────────────────────────
# Cleanup C: the Naim browses these over plain HTTP on PLAIN_PORT (:8765). They
# reuse api_upnp's pure helpers, so the SOAP/descriptors are byte-identical to
# the retired dlna_server device tier. Served on both binds; the Naim uses the
# plain one (device.xml's URLBase = http://<lan-ip>:PLAIN_PORT).
_GW_XML = 'text/xml; charset="utf-8"'


@router.get("/gw/device.xml", include_in_schema=False)
async def gw_device_xml(request: Request):
    import dlna_gateway
    log.debug("GW /gw/device.xml fetched by %s (ua=%s)", _peer(request),
              request.headers.get("user-agent", "")[:80])
    lan_ip = await run_in_threadpool(dlna_gateway.get_lan_ip)
    return Response(api_upnp._gw_device_xml(lan_ip, PLAIN_PORT).encode(),
                    media_type=_GW_XML)


@router.get("/gw/cd/desc.xml", include_in_schema=False)
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


@router.api_route("/gw/cd/events", methods=["GET", "SUBSCRIBE", "UNSUBSCRIBE"],
               include_in_schema=False)
async def gw_cd_events(request: Request):
    return await _gw_event_route(request, "/gw/cd/events", {"SystemUpdateID": "1"})


@router.get("/gw/cm/desc.xml", include_in_schema=False)
async def gw_cm_desc(request: Request):
    log.debug("GW /gw/cm/desc.xml fetched by %s", _peer(request))
    return Response(api_upnp._gw_cm_desc_xml().encode(), media_type=_GW_XML)


@router.post("/gw/cm/control", include_in_schema=False)
async def gw_cm_control(request: Request):
    body = await request.body()
    status, ctype, payload = await run_in_threadpool(api_upnp.cm_control_soap, body)
    if status != 200:
        action = (request.headers.get("soapaction", "").rsplit("#", 1)[-1]
                  .strip('"') or "?")
        log.warning("GW /gw/cm/control → %s for action=%s", status, action)
    return Response(payload, status_code=status, media_type=ctype)


@router.api_route("/gw/cm/events", methods=["GET", "SUBSCRIBE", "UNSUBSCRIBE"],
               include_in_schema=False)
async def gw_cm_events(request: Request):
    return await _gw_event_route(request, "/gw/cm/events", {
        "SourceProtocolInfo": api_upnp._GW_SOURCE_PROTOCOLS,
        "SinkProtocolInfo": "", "CurrentConnectionIDs": "0"})


@router.post("/gw/cd/control", include_in_schema=False)
async def gw_cd_control(request: Request):
    body = await request.body()
    status, ctype, payload = await run_in_threadpool(
        api_upnp.cd_control_soap, body)
    if status != 200:
        action = (request.headers.get("soapaction", "").rsplit("#", 1)[-1]
                  .strip('"') or "?")
        log.warning("GW /gw/cd/control → %s for action=%s", status, action)
    return Response(payload, status_code=status, media_type=ctype)
