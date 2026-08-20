#!/usr/bin/env python3
"""
dlna_asgi_bridge.py — run legacy `(h, params)` handlers inside the ASGI app.

Phase 2 migration aid (docs/BUILDING_2.0.md). The 1.x handlers
(api_browse / api_playback / api_playlists / api_radio) take a
BaseHTTPRequestHandler-like `h` plus `params` (GET: query dict, POST: body
string) and respond via `h._json` / `h._html` / `h._xml_response` /
`h.send_error`. This shim lets those handlers run UNCHANGED under
FastAPI/Hypercorn: a fake `h` captures those high-level helpers (no socket),
and the captured `(code, body, content-type)` becomes a Starlette `Response`.
The blocking handler runs in a threadpool so its DB/SOAP I/O doesn't stall
the event loop.

Only JSON/XML/HTML handlers are bridgeable. Handlers that stream bytes
straight to `h.wfile` (`/stream`, `/art`, `/radio_stream`) are NOT — they're
ported natively as `StreamingResponse` later. As each route is rewritten as
a native FastAPI route it's dropped from the bridge; eventually the bridge is
empty and the stdlib server (dlna_server.py) retires.
"""
import json
from collections.abc import Callable

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response


class _Capture:
    __slots__ = ("code", "body", "ctype")

    def __init__(self) -> None:
        self.code: int = 200
        self.body: bytes = b""
        self.ctype: str = "application/json"


class _LegacyH:
    """Minimal stand-in for GatewayHandler exposing only what bridgeable
    handlers touch: the response helpers (captured) + request metadata."""

    def __init__(self, headers, path: str, command: str) -> None:
        self.headers = headers      # Starlette Headers — supports .get(name, default)
        self.path = path
        self.command = command
        self._cap = _Capture()

    # ── response helpers — captured, never written to a socket ────────
    def _json(self, code: int, data) -> None:
        self._cap.code = code
        self._cap.body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._cap.ctype = "application/json"

    def _html(self, code: int, body: str) -> None:
        self._cap.code = code
        self._cap.body = body.encode("utf-8")
        self._cap.ctype = "text/html"

    def _xml_response(self, code: int, body) -> None:
        self._cap.code = code
        self._cap.body = body if isinstance(body, (bytes, bytearray)) \
            else str(body).encode("utf-8")
        self._cap.ctype = "text/xml"

    def send_error(self, code: int, message: str = "") -> None:
        self._cap.code = code
        self._cap.body = json.dumps({"error": message or ""}).encode("utf-8")
        self._cap.ctype = "application/json"


def run_legacy_sync(handler: Callable, arg, *, headers=None, path: str = "",
                    command: str = "GET") -> tuple[int, bytes, str]:
    """Run a legacy `(h, params|body)` handler against a capturing fake `h`
    and return `(status, body-bytes, content-type)`. Pure + sync → directly
    unit-testable without an HTTP client."""
    h = _LegacyH(headers if headers is not None else {}, path, command)
    handler(h, arg)
    c = h._cap
    return c.code, c.body, c.ctype


def run_subsonic_sync(http_method: str, path: str, query, body: bytes = b"",
                      *, headers=None) -> tuple[int, bytes, str]:
    """Run `api_subsonic.handle()` against the capturing fake `h` and return
    `(status, body-bytes, content-type)`. Subsonic's JSON/XML methods respond
    via `h._json` / `h._xml_response` (both captured) and set
    `h._subsonic_format` (a plain attr the fake `h` accepts). The byte methods
    (stream / download / getCoverArt) are served natively in dlna_asgi and
    never routed here. Lazy import keeps the bridge free of a hard
    api_subsonic dependency at import time."""
    import api_subsonic
    h = _LegacyH(headers if headers is not None else {}, path, http_method)
    api_subsonic.handle(h, http_method, path, query, body)
    c = h._cap
    return c.code, c.body, c.ctype


def make_bridged_route(handler: Callable, *, is_post: bool):
    """Build an async FastAPI endpoint that runs `handler` through the shim.
    The blocking handler is dispatched to a threadpool."""
    async def endpoint(request: Request) -> Response:
        if is_post:
            arg: object = (await request.body()).decode("utf-8", "replace")
        else:
            arg = dict(request.query_params)
        code, body, ctype = await run_in_threadpool(
            run_legacy_sync, handler, arg,
            headers=request.headers, path=request.url.path,
            command=request.method)
        return Response(content=body, status_code=code, media_type=ctype)

    return endpoint
