#!/usr/bin/env python3
"""
dlna_asgi.py — THE 2.0 server: a FastAPI application served by Hypercorn,
which terminates TLS + HTTP/2 (ALPN) on :8443 and plain HTTP on :8765.

The `lifespan` here boots the whole gateway (`start_background_services`),
so `hypercorn dlna_asgi:app` is the entire process — there is no separate
stdlib HTTP edge any more.

Run it:
    ./run-2.0-asgi.sh
    .venv/bin/hypercorn dlna_asgi:app --bind 127.0.0.1:8768

TLS is APP-OWNED, using a `tailscale cert`-issued cert. (`tailscale serve`
was tried and dropped — broken on this tailnet; see docs/BUILDING_2.0.md.)

── Module family ────────────────────────────────────────────────────
This file was 1,156 lines holding every route in the gateway until
2026-08-20. Routes now live in per-surface APIRouter modules that this one
includes:

    dlna_asgi_state.py     shared runtime handles (DB / SERVERS / INDEXER)
    dlna_asgi_browse.py    the JSON read API + SSE
    dlna_asgi_video.py     /video/* (PWA, same-origin)
    dlna_asgi_media.py     /art, /stream, /radio_stream byte relays
    dlna_asgi_upnp.py      /gw/* — the Naim-facing UPnP surface
    dlna_asgi_subsonic.py  /rest/* — the CarPlay surface
    dlna_asgi_static.py    /, /sw.js, /manifest.json, generated icons

What remains here is the app itself: the lifespan, the legacy-bridge wiring,
the router includes, and the dev entry point. Every route handler is
re-exported below, so the ~58 tests that call `dlna_asgi.<route>()` directly
are unaffected.

Router include ORDER is not load-bearing — no two routes in this app can
match the same request, which tests/test_asgi.py asserts explicitly so the
property cannot quietly stop being true.

⚠ `DB`, `SERVERS` and `INDEXER` are bound once, in dlna_asgi_state. Patching
a METHOD (`patch.object(dlna_asgi.DB, "album_tracks", ...)`) works from
anywhere — it is the same object. WHOLESALE rebinding must target the owner:
`patch.object(dlna_asgi_state, "DB", tmp_db)`.
"""
import asyncio
import contextlib
import logging

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import dlna_asgi_browse
import dlna_asgi_media
import dlna_asgi_static
import dlna_asgi_subsonic
import dlna_asgi_upnp
import dlna_asgi_video
import dlna_routes
from dlna_asgi_bridge import make_bridged_route
from dlna_asgi_state import DB, INDEXER, PLAIN_PORT, SERVERS  # noqa: F401
from dlna_config import VERSION, raise_fd_limit  # noqa: F401
from dlna_events import EVENTS

# ── Re-exports: route handlers + helpers the tests reach for ─────────
from dlna_asgi_browse import (  # noqa: F401
    _SSE_HEARTBEAT_SEC, album_favourite_check, album_favourites, album_tracks,
    artist_albums, artist_tracks, artists, albums, book_meta_all_route,
    browse_letter, browse_route, chapters_route, decade_albums, decade_tracks,
    decades, events, genre_albums, genre_tracks, genres, index_status,
    lyrics_route, playlist, playlists, position_get_route, position_save_route,
    positions_list_route, radio_favourites, radio_nowplaying_route,
    radio_route, radio_search_route, renderers, search, servers, track_meta,
    version,
)
from dlna_asgi_media import (  # noqa: F401
    _audio_relay_response, art, radio_stream, stream,
)
from dlna_asgi_state import _missing, _peer, _truthy, _VIDEO_UDN  # noqa: F401
from dlna_asgi_static import (  # noqa: F401
    _icon, _icon_192, _icon_512, _index, _make_icon_png, _manifest,
    _MANIFEST, _service_worker, _STATIC_DIR,
)
from dlna_asgi_subsonic import (  # noqa: F401
    _SUBSONIC_BYTE_METHODS, _subsonic_auth_gate, _subsonic_fail_response,
    subsonic,
)
from dlna_asgi_upnp import (  # noqa: F401
    _GW_XML, _gw_event_route, gw_cd_control, gw_cd_desc, gw_cd_events,
    gw_cm_control, gw_cm_desc, gw_cm_events, gw_device_xml,
)
from dlna_asgi_video import (  # noqa: F401
    _isfile, _video_payload, video_file, video_hls, video_meta, video_poster,
    video_transcode, videos, video_meta as _video_meta,
)

# Imported for the tests that patch through this module's namespace
# (`patch.object(dlna_asgi.api_playback, "art_fetch")`).
import api_browse  # noqa: F401,E402
import api_playback  # noqa: F401,E402
import api_radio  # noqa: F401,E402
import api_subsonic  # noqa: F401,E402
import api_upnp  # noqa: F401,E402
import dlna_stream_proxy  # noqa: F401,E402

log = logging.getLogger("dlna.asgi")


def _loop_exception_handler(loop, context):
    """Downgrade benign client-transport teardown errors to debug. Hypercorn's
    TCPServer._close() doesn't catch TimeoutError/ETIMEDOUT on Python 3.14, so a
    client connection that times out or resets uncleanly escapes as 'Unhandled
    exception in client_connected_cb' — a scary-looking but harmless traceback
    (no request impact, no FD leak). Everything else goes to the default
    handler so real bugs are still surfaced."""
    exc = context.get("exception")
    msg = context.get("message", "")
    if "client_connected_cb" in msg and isinstance(exc, OSError):
        log.debug(f"asyncio: benign client transport teardown — {exc!r}")
        return
    loop.default_exception_handler(context)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    """Boot the gateway's background services so `hypercorn dlna_asgi:app` runs
    the gateway standalone: discovery (DB pre-probe / SSDP / subnet-scan /
    heartbeat), the gateway-as-MediaServer SSDP announcer, the album-art +
    AcoustID startup scans, and LocalFs wiring, via
    dlna_gateway.start_background_services. The Naim-facing /gw/* UPnP surface
    is served NATIVELY by THIS app on the plain port (PLAIN_PORT) — Hypercorn
    owns TLS on :8443 but the Naim can't do HTTPS, so the SSDP advert +
    device.xml URLBase point at PLAIN_PORT. Hypercorn is the only server
    (Cleanup C retired the separate stdlib device tier).

    Env knobs:
      • GATEWAY_NO_SERVICES=1 — web tier only; skip ALL background startup.
      • GATEWAY_PLAIN_PORT (default 8765) — the plain-HTTP port the Naim reaches
        /gw/* on, advertised in the gateway-as-MediaServer SSDP record.
      • GATEWAY_DEBUG=1 — verbose logging."""
    import dlna_gateway
    # Raise the open-file limit BEFORE serving — Hypercorn's threadpool + the
    # LocalFs scan open enough concurrent FDs to hit macOS's default 256-soft
    # limit → EMFILE → sqlite 'unable to open database file'. Unconditional
    # (even GATEWAY_NO_SERVICES still serves /api/* DB reads).
    raise_fd_limit()
    # Bind the SSE event bus to this loop so worker threads can publish() live
    # updates to /api/events subscribers (R2). Unconditional — the web tier
    # serves SSE regardless of GATEWAY_NO_SERVICES.
    loop = asyncio.get_running_loop()
    # Raise the shared threadpool ceiling. Starlette's run_in_threadpool draws
    # from anyio's default limiter (40 tokens) — and EVERY blocking op shares it:
    # browse/DB queries, /art fetches, the legacy bridge, AND each byte-relay
    # read of every audio stream. Under a few concurrent iOS streams + browsing
    # + art loads the 40 slots saturate, new requests queue, and the audio
    # element's next Range request stalls long enough that iOS aborts the load
    # (the "stops after one track" / code-4 NETWORK_NO_SOURCE regression). 256
    # tokens is well under the raised FD limit and removes the serialization.
    try:
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = 256
    except Exception:                                   # noqa: BLE001
        log.warning("could not raise threadpool limit; staying at anyio default")
    EVENTS.bind_loop(loop)
    loop.set_exception_handler(_loop_exception_handler)
    started = False
    if not _truthy("GATEWAY_NO_SERVICES"):
        dlna_gateway.setup_logging(debug=_truthy("GATEWAY_DEBUG"))
        lan_ip = dlna_gateway.get_lan_ip()
        # Cleanup C: /gw/* is served by THIS app on the plain port (PLAIN_PORT,
        # a module global defined below); the SSDP advert + device.xml URLBase
        # point there — no separate device server any more.
        dlna_gateway.start_background_services(lan_ip, PLAIN_PORT)
        started = True
        log.info("ASGI lifespan: gateway background services started "
                 f"(/gw/* native + SSDP advert on :{PLAIN_PORT})")
    try:
        yield
    finally:
        if started:
            try:
                dlna_gateway.gw_ssdp_byebye(dlna_gateway.get_lan_ip(), PLAIN_PORT)
            except Exception as e:                  # shutdown must not hang
                log.debug(f"SSDP byebye on shutdown failed ({e}) — ignored")
        EVENTS.bind_loop(None)      # drop the (now closing) loop reference


# docs_url=None: disable the Swagger UI page — it pulls swagger-ui assets from a
# CDN (jsdelivr) on load, an outbound call we don't want from a LAN/tailnet-only
# gateway. redoc_url already off. (Cutover runbook step 1, privacy.)
app = FastAPI(title="DLNA Gateway", version=VERSION, docs_url=None,
              redoc_url=None, lifespan=_lifespan)

# ── Router includes ──────────────────────────────────────────────────
# Grouped by surface; see the module docstring for why order is free.
app.include_router(dlna_asgi_browse.router)
app.include_router(dlna_asgi_media.router)
app.include_router(dlna_asgi_video.router)
app.include_router(dlna_asgi_upnp.router)
app.include_router(dlna_asgi_subsonic.router)
app.include_router(dlna_asgi_static.router)

# Paths served by a native route above — must NOT also be bridged.
_NATIVE = {"/api/version", "/api/servers", "/api/renderers",
           "/api/artists", "/api/albums", "/api/genres",
           "/api/artist_albums", "/api/artist_tracks",
           "/api/genre_albums", "/api/genre_tracks", "/api/album_tracks",
           "/api/decades", "/api/decade_albums", "/api/decade_tracks",
           "/api/search", "/api/browse_letter",
           "/api/index/status", "/api/track_meta", "/api/playlists",
           "/api/playlist", "/api/album_favourites",
           "/api/album_favourites/check", "/api/radio/favourites",
           "/api/browse", "/api/radio", "/api/radio/search",
           "/api/radio/nowplaying", "/api/lyrics"}


# ── Bridged legacy read routes ────────────────────────────────────────
# Register the remaining JSON read API through the compatibility shim so
# the whole read API runs under Hypercorn TODAY, while handlers are
# migrated to native routes (the _NATIVE set above) one batch at a time.
# Excluded from the bridge:
#   • _NATIVE                — already ported to native FastAPI routes
#   • /stream /art /radio_stream — native StreamingResponse/Response byte
#       relays (in _STREAMING); never bridged
#   • /gw/*                 — UPnP device endpoints; stay on the legacy LAN
#       server (the Naim talks to it directly, never through this proxy)
_STREAMING = {"/stream", "/art", "/radio_stream"}


def _bridgeable(path: str) -> bool:
    return (path not in _NATIVE and path not in _STREAMING
            and not path.startswith("/gw/"))

for _path, _handler in dlna_routes.GET_ROUTES.items():
    if _bridgeable(_path):
        app.add_api_route(_path, make_bridged_route(_handler, is_post=False),
                          methods=["GET"], name=f"bridged_get:{_path}")


# ── Bridged legacy POST routes ────────────────────────────────────────
# The whole write API now runs under Hypercorn via the same shim (the legacy
# handler gets the raw request body as its second arg). Excluded: native POST
# ports (none yet) and `/gw/*` device endpoints (legacy LAN server only). The
# simple ones get rewritten native in a later batch.
_NATIVE_POST: set = set()

for _path, _handler in dlna_routes.POST_ROUTES.items():
    if _path not in _NATIVE_POST and not _path.startswith("/gw/"):
        app.add_api_route(_path, make_bridged_route(_handler, is_post=True),
                          methods=["POST"], name=f"bridged_post:{_path}")


# ── Static mount ─────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _run(host: str = "127.0.0.1", port: int = 8768) -> None:
    """Boot Hypercorn on the ASGI app — the module self-test / dev runner."""
    import asyncio
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    cfg = Config()
    cfg.bind = [f"{host}:{port}"]
    print(f"Hypercorn serving dlna_asgi:app → http://{host}:{port}/  (Ctrl-C to stop)")
    asyncio.run(serve(app, cfg))
