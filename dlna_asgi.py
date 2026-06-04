#!/usr/bin/env python3
"""
dlna_asgi.py — 2.0 ASGI application (FastAPI), served by Hypercorn.

Phase 2 of the 2.0 transport refresh (docs/BUILDING_2.0_SIDE_BY_SIDE.md):
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

Behind `tailscale serve` the backend is plain HTTP/1.1 on localhost; h2/h3
to clients comes from the front (or, later, from Hypercorn's own TLS).
"""
from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool

import api_browse
import dlna_routes
from dlna_asgi_bridge import make_bridged_route
from dlna_config import VERSION

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


# Paths served by a native route above — must NOT also be bridged.
_NATIVE = {"/api/version", "/api/servers", "/api/renderers"}


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
# POST routes are bridged in a later step (read-only first).
_STREAMING = {"/stream", "/art", "/radio_stream"}


def _bridgeable(path: str) -> bool:
    return (path not in _NATIVE and path not in _STREAMING
            and not path.startswith("/gw/"))


for _path, _handler in dlna_routes.GET_ROUTES.items():
    if _bridgeable(_path):
        app.add_api_route(_path, make_bridged_route(_handler, is_post=False),
                          methods=["GET"], name=f"bridged_get:{_path}")


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
