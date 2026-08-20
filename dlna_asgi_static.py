#!/usr/bin/env python3
"""
dlna_asgi_static.py — PWA shell serving and the generated manifest/icons.

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

`/sw.js` is served no-store at ROOT scope: a cached service worker cannot
update itself, and a stale one is what pinned the app blank in the
2026-06-27 outage.

The manifest says `"orientation": "any"` deliberately — an INSTALLED PWA
obeys this field, so the previous "portrait" locked the home-screen app
upright and made the landscape layout unreachable from the one place it
mattered.

Icons are generated in-process (`_make_icon_png`) rather than shipped as
files, so the navy palette change repainted them with no binary assets to
keep in sync.
"""
import json
import logging
import os
import struct
import zlib

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.responses import Response

router = APIRouter()

log = logging.getLogger("dlna.asgi")


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
    # "any", not "portrait": an installed PWA obeys this, so the old value
    # LOCKED the home-screen app upright — the landscape-phone layout was
    # unreachable from the very place it matters most (2026-08-07).
    "orientation": "any",
    "background_color": "#0A1526",
    "theme_color": "#0A1526",
    "icons": [
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
         "purpose": "any maskable"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
    "categories": ["music", "entertainment"],
}, indent=2)


@router.get("/", include_in_schema=False)
@router.get("/index.html", include_in_schema=False)
async def _index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"),
                        media_type="text/html")


@router.get("/sw.js", include_in_schema=False)
async def _service_worker():
    # Root scope + no-store so an updated worker is always picked up.
    return FileResponse(
        os.path.join(_STATIC_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate",
                 "Service-Worker-Allowed": "/"})


@router.get("/manifest.json", include_in_schema=False)
async def _manifest():
    return Response(content=_MANIFEST, media_type="application/manifest+json")


def _make_icon_png(size: int) -> bytes:
    """Generate a simple PNG icon: navy square with amber ♪ symbol.
    Relocated from dlna_server.py (retired in Cleanup C) — this app is now the
    sole server, so the only caller (the /icon routes) keeps it local."""
    bg    = (10, 21, 38)      # --bg  #0A1526
    amber = (255, 194, 74)    # --amber #FFC24A

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


@router.get("/icon-192.png", include_in_schema=False)
async def _icon_192():
    return _icon(192)


@router.get("/icon-512.png", include_in_schema=False)
async def _icon_512():
    return _icon(512)
