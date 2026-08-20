#!/usr/bin/env python3
"""
dlna_asgi_state.py — the shared runtime handles the ASGI route modules
bind against.

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

⚠ `DB`, `SERVERS` and `INDEXER` ARE BOUND HERE, ONCE. Route modules use
`_st.DB` — an attribute lookup resolved at CALL time — so a test patch
actually lands. Inject with `patch.object(dlna_asgi_state, "DB", tmp_db)`.

Patching a METHOD (`patch.object(dlna_asgi.DB, "album_tracks", ...)`, which
is what most of test_asgi.py does) works regardless, because every module
sees the SAME object. It is only WHOLESALE rebinding that needs the owner.
"""
import logging
import os

from fastapi import Request
from starlette.responses import JSONResponse

from dlna_discovery import SERVERS  # noqa: F401 — shared handle
from dlna_library import DB, INDEXER  # noqa: F401 — shared handle

log = logging.getLogger("dlna.asgi")


# The plain-HTTP port the Naim reaches /gw/* on (Hypercorn's --insecure-bind).
# device.xml's URLBase + the SSDP advert point here (NEVER the TLS :8443 — the
# Naim can't do HTTPS). Cleanup C: /gw/* is served by this app on this port,
# replacing the old separate device server on :8770.
PLAIN_PORT = int(os.environ.get("GATEWAY_PLAIN_PORT", "8765"))


# ── Video (PWA, SAME-ORIGIN) ───────────────────────────────────────────
# The PWA <video> can't use the :8200 /localfs/video URL — over HTTPS that's
# mixed content (blocked) + cross-origin. Serve video + posters from THIS app
# (same origin). FileResponse handles Range automatically (seek/scrub). The LG
# TV keeps using :8200 /localfs/video directly (not a browser).
_VIDEO_UDN = "uuid:localfs-movies"


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


# ── Browse-navigation reads ───────────────────────────────────────────
# Trivial `validate params → DB call` handlers ported native: FastAPI does
# the query-param binding, the shared DB methods are the source of truth,
# and the blocking query runs in a threadpool. 400 bodies match the legacy
# handlers exactly (`{"error": "..."}`).

def _missing(msg: str) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=400)


def _peer(request: Request) -> str:
    client = getattr(request, "client", None)
    return client.host if client else "?"
