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
import threading
import time

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


# ── Concurrency caps (audit Track B2, 2026-08-21) ─────────────────────
# Two endpoints hold a connection open for as long as the client wants it —
# the SSE stream and the audio relays — and neither had any ceiling. Measured
# on the running gateway: 40 stalled /stream requests cost 120 file
# descriptors (client socket + upstream socket + the LocalFs server's own
# accepted socket), and 80 of those survived the client disconnecting, taking
# up to a minute to drain. Against the 8192 limit that is a few thousand
# connections to exhaustion — and the symptom of running out is SQLite's
# "unable to open database file", which reads like corruption rather than
# like an attack.
#
# By contrast a request that never COMPLETES is already handled: hypercorn's
# keep_alive_timeout reaps half-open requests in about five seconds (also
# measured). It is the completed-but-long-lived ones that needed a bound.
#
# The limits are far above any real use — a household runs a handful of
# streams and a few browser tabs — so hitting one means something is wrong,
# and 503 with Retry-After is the honest answer.
class ConcurrencyCap:
    """Counts in-flight holders of a long-lived resource.

    Not a semaphore: acquire() must never block, because it is called from
    request handlers running on the event loop. It takes a plain lock instead,
    held only across a compare-and-increment.

    Today's callers are all async, so the loop already serialises them and the
    lock is redundant — but `acquire`/`release` are an obvious thing for a
    later caller to reach for from inside `run_in_threadpool`, which this
    codebase uses everywhere. Unlocked, a racing pair can both pass the limit
    check, and worse, a lost decrement ratchets the ceiling down permanently:
    the gateway would 503 every stream until it was restarted, with nothing in
    the log to explain why. Three lines to make that impossible.
    """

    def __init__(self, limit: int, what: str):
        self.limit = limit
        self.what = what
        self._n = 0
        self._warned = 0.0
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._n

    def acquire(self) -> bool:
        with self._lock:
            if self._n < self.limit:
                self._n += 1
                return True
        now = time.monotonic()
        if now - self._warned >= 60.0:          # one line a minute, not a flood
            self._warned = now
            log.warning(f"{self.what}: at the concurrency cap "
                        f"({self.limit}) — refusing new requests with 503")
        return False

    def release(self) -> None:
        with self._lock:
            # Never below zero: a double release must not create capacity that
            # does not exist.
            self._n = max(0, self._n - 1)


# A household streams to a few devices at once; 64 is generous even counting
# the fresh connection every Range seek opens (no keep-alive on this path).
# Shared by /stream, /radio_stream and Subsonic /rest/stream.
AUDIO_RELAYS = ConcurrencyCap(64, "audio relay")
# One per open PWA tab or device. 64 is many households.
SSE_STREAMS = ConcurrencyCap(64, "SSE")
