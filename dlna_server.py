#!/usr/bin/env python3
"""
dlna_server.py — Threaded HTTP server: REST API + UPnP ContentDirectory gateway.

GatewayHandler.do_GET  — browse, search, state, playlists, stream proxy
GatewayHandler.do_POST — play, play_tracks, render, control, UPnP SOAP

UPnP ContentDirectory gateway: exposes playlists/favourites to Naim Uniti.

Standalone test (starts server on port 8766 for 30 s):
    python dlna_server.py
"""
import json
import logging
import os
import socket
import struct
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Optional

from dlna_config import M3U_TMP
from dlna_content import (avtransport_send, cd_browse)
from dlna_discovery import SERVERS, RENDERERS, _STALE_SEC
from dlna_library import DB, INDEXER, FAVOURITES_ID
from dlna_player import PLAYER, RENDERER_QUEUE, proxy_stream
from dlna_cast import CAST_DEVICES, CAST_QUEUE, start_discovery as cast_start_discovery

log = logging.getLogger("dlna.server")


# ── Threaded HTTP server ──────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Gateway UPnP identity ─────────────────────────────────────────

GW_UDN  = "uuid:dlna-gateway-iina-8765"
GW_NAME = "DLNA Gateway (IINA)"

# Throttle re-probe attempts: udn → last_reprobe_timestamp
_reprobe_times: dict = {}


# ── Service Worker JS ─────────────────────────────────────────────
# Served at /sw.js — makes the gateway installable as a PWA.
# Strategy:
#   App shell (/, manifest, icons, fonts) → stale-while-revalidate
#   API + stream requests → network-only (never cache dynamic data)
#   Album art from AssetUPnP → cache-first with long TTL

_SERVICE_WORKER_JS = r"""
const APP_CACHE = 'dlna-gw-app-v2';
const ART_CACHE = 'dlna-gw-art-v1';

const SHELL = [
  '/',
  '/manifest.json',
  '/icon-192.png',
  '/icon-512.png'
];

// ── Install: pre-cache app shell ────────────────────────────────
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(APP_CACHE)
      .then(cache => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
  );
});

// ── Activate: clean old caches ──────────────────────────────────
self.addEventListener('activate', event => {
  const keep = new Set([APP_CACHE, ART_CACHE]);
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => !keep.has(k)).map(k => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

// ── Fetch: route by request type ────────────────────────────────
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);

  // Never intercept API calls, streams, or POST requests
  if (url.pathname.startsWith('/api/') ||
      url.pathname.startsWith('/stream') ||
      url.pathname.startsWith('/cd/') ||
      event.request.method !== 'GET') {
    return;
  }

  // Album art images — cache-first (images rarely change)
  if (url.pathname === '/art' ||
      (event.request.destination === 'image' && url.origin !== self.location.origin)) {
    event.respondWith(
      caches.open(ART_CACHE).then(cache =>
        cache.match(event.request).then(cached => {
          if (cached) return cached;
          return fetch(event.request).then(resp => {
            if (resp.ok) cache.put(event.request, resp.clone());
            return resp;
          }).catch(() => cached || new Response('', { status: 404 }));
        })
      )
    );
    return;
  }

  // App shell & static assets — stale-while-revalidate
  event.respondWith(
    caches.open(APP_CACHE).then(cache =>
      cache.match(event.request).then(cached => {
        const network = fetch(event.request).then(resp => {
          if (resp.ok) cache.put(event.request, resp.clone());
          return resp;
        }).catch(() => cached);
        return cached || network;
      })
    )
  );
});
"""


# ── PWA icon generator ────────────────────────────────────────────
# Generates a PNG icon in pure Python (no Pillow/cairosvg needed).
# Dark background with amber music note — matches the app theme.

def _make_icon_png(size: int) -> bytes:
    """
    Generate a simple PNG icon: dark square with amber ♪ symbol.
    Uses only stdlib (zlib + struct) — no image library required.
    """
    import zlib, struct

    bg   = (14, 13, 11)      # --bg  #0e0d0b
    amber = (212, 168, 67)   # --amber #d4a843

    # Create RGBA pixel array
    img = [list(bg + (255,)) for _ in range(size * size)]

    # Draw a filled rounded rectangle as background (full canvas minus 8% margin)
    margin = size // 12
    r = size // 8  # corner radius
    for y in range(size):
        for x in range(size):
            # Round corners using distance
            cx = max(margin + r - x, 0, x - (size - margin - r - 1))
            cy = max(margin + r - y, 0, y - (size - margin - r - 1))
            if cx*cx + cy*cy <= r*r or (cx == 0 or cy == 0):
                pass  # inside rounded rect — keep bg

    # Draw a simple ♪ note shape using filled circles + rectangles
    # Scale relative to icon size
    s = size / 192.0
    def filled_circle(cx, cy, rad, color):
        for dy in range(-rad-1, rad+2):
            for dx in range(-rad-1, rad+2):
                if dx*dx + dy*dy <= rad*rad:
                    px, py = int(cx+dx), int(cy+dy)
                    if 0 <= px < size and 0 <= py < size:
                        img[py*size+px] = list(color+(255,))

    def filled_rect(x1, y1, x2, y2, color):
        for py in range(max(0,y1), min(size,y2)):
            for px in range(max(0,x1), min(size,x2)):
                img[py*size+px] = list(color+(255,))

    # Note stem
    sx, sy = int(110*s), int(60*s)
    sw, sh = max(6, int(12*s)), int(90*s)
    filled_rect(sx, sy, sx+sw, sy+sh, amber)
    # Note head (filled oval approximated by circle)
    nx, ny = int(88*s), int(135*s)
    nr = max(8, int(20*s))
    filled_circle(nx, ny, nr, amber)
    # Flag on stem
    fx, fy = sx+sw, sy
    filled_rect(fx, fy, fx+int(35*s), fy+int(10*s), amber)
    filled_rect(fx, fy+int(20*s), fx+int(28*s), fy+int(30*s), amber)

    # Pack pixels into PNG
    def png_chunk(tag, data):
        c = zlib.crc32(tag + data) & 0xffffffff
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', c)

    raw = b''
    for row in range(size):
        raw += bytes([0])  # filter type: None (PNG)
        for col in range(size):
            raw += bytes(img[row*size+col])

    compressed = zlib.compress(raw, 6)

    ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)  # RGB not RGBA
    # Actually use RGBA (colortype=6)
    ihdr = struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0)[:13]

    png  = b'\x89PNG\r\n\x1a\n'
    png += png_chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 6, 0, 0, 0))
    png += png_chunk(b'IDAT', compressed)
    png += png_chunk(b'IEND', b'')
    return png


# ── Request handler ───────────────────────────────────────────────

class GatewayHandler(BaseHTTPRequestHandler):
    """
    REST API + static web UI + UPnP ContentDirectory SOAP handler.

    GET  /              → Web UI (HTML)
    GET  /api/servers   → list MediaServers
    GET  /api/renderers → list MediaRenderers (for output selector)
    GET  /api/browse    → ContentDirectory Browse
    GET  /api/search    → FTS5 library search
    GET  /api/album_tracks → tracks for a given album
    GET  /api/play      → launch IINA with a URL (GET for easy linking)
    GET  /api/state     → player state (polled by UI)
    GET  /api/index/status → indexer state
    GET  /api/index/rebuild → trigger re-index
    GET  /api/playlists → list playlists
    GET  /api/playlist  → single playlist + tracks
    GET  /api/playlist/create
    GET  /api/playlist/delete
    GET  /api/playlist/add
    GET  /api/playlist/remove
    GET  /stream        → Range-aware stream proxy
    GET  /gw/device.xml → UPnP device description
    GET  /gw/cd/desc.xml
    GET  /gw/cd/events

    POST /api/play_tracks → write M3U + launch IINA (or send to renderer)
    POST /api/render      → AVTransport send to a UPnP renderer
    POST /api/control     → mpv IPC commands (pause/seek/volume/…)
    POST /gw/cd/control   → UPnP ContentDirectory SOAP Browse
    """

    # ── Response helpers ──────────────────────────────────────────

    def log_message(self, fmt, *args):
        log.debug(f"{self.address_string()} {fmt % args}")

    def handle_error(self):
        pass   # suppress BrokenPipeError spam in server log

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")

    def _json(self, code: int, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _html(self, code: int, body: str):
        enc = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(enc)))
        self.end_headers()
        try:
            self.wfile.write(enc)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _xml_response(self, code: int, body: bytes):
        self.send_response(code)
        self._send_cors()
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── OPTIONS (CORS pre-flight) ─────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        # ── Web UI ────────────────────────────────────────────────
        if path in ("/", "/index.html"):
            from dlna_gateway import WEB_UI
            self._html(200, WEB_UI)
            return

        # ── Servers / renderers ───────────────────────────────────
        if path == "/api/servers":
            # Combined devices (AVTransport) are excluded at registration time
            # in _fetch_device. The RENDERERS check below is belt-and-suspenders.
            now = time.time()
            result = []
            for s in SERVERS.all():
                if RENDERERS.get(s.udn):
                    continue   # safety: skip any that slipped through
                d = s.to_dict()
                d["online"] = (now - s.last_seen) < _STALE_SEC
                d["tracks"] = DB.track_count(s.udn)
                result.append(d)
            self._json(200, result)
            return

        if path == "/api/renderers":
            self._json(200, [r.to_dict() for r in RENDERERS.all()])
            return

        # ── Chromecast devices ────────────────────────────────────
        if path == "/api/cast_devices":
            self._json(200, [d.to_dict() for d in CAST_DEVICES.all()])
            return

        if path == "/api/cast_state":
            self._json(200, CAST_QUEUE.snapshot())
            return

        # ── Browse ────────────────────────────────────────────────
        if path == "/api/browse":
            udn = params.get("udn", "")
            oid = params.get("id", "0")
            srv = SERVERS.get(udn)
            if not srv:
                self._json(404, {"error": "Server not found — still discovering?"})
                return
            result = cd_browse(srv.control_url, oid)
            if "error" not in result:
                SERVERS.touch(udn)   # successful SOAP → server is alive
                _reprobe_times.pop(udn, None)  # clear backoff on success
            else:
                # Browse failed — trigger background re-probe, but throttle
                # to once per 60 s to avoid a tight re-probe/fail loop
                # (e.g. AssetUPnP ContentDirectory not ready on startup).
                now = time.time()
                last = _reprobe_times.get(udn, 0)
                if now - last > 60:
                    _reprobe_times[udn] = now
                    loc = srv.location
                    log.warning(f"Browse failed for {srv.name!r} — re-probing {loc}")
                    import dlna_discovery as _disc
                    threading.Thread(
                        target=_disc.probe_url,
                        args=(loc, GW_UDN), daemon=True).start()
                else:
                    log.debug(f"Browse failed for {srv.name!r} — re-probe throttled")
            self._json(200, result)
            return

        # ── Artists (SQLite — works offline) ─────────────────────
        if path == "/api/artists":
            udn = params.get("udn", "")
            if not udn:
                self._json(400, {"error": "Missing udn"})
                return
            self._json(200, DB.all_artists(udn))
            return

        # ── Search ────────────────────────────────────────────────
        if path == "/api/search":
            udn   = params.get("udn", "")
            query = params.get("q", "").strip()
            if not query:
                self._json(400, {"error": "Missing q"})
                return
            if not udn:
                self._json(400, {"error": "Missing udn"})
                return
            # Search is a pure SQLite operation — works even when AssetUPnP
            # is temporarily unreachable. No live-server check needed.
            if INDEXER.state.status == "running" and DB.track_count(udn) == 0:
                self._json(200, {"tracks": [], "albums": [], "artists": [],
                                 "info": "Indexing — please wait"})
                return
            result = DB.search(udn, query)
            # Touch server last_seen — the client is successfully using this UDN
            SERVERS.touch(udn)
            log.debug(f"Search {query!r}: {len(result['tracks'])} tracks, "
                      f"{len(result['albums'])} albums")
            self._json(200, result)
            return

        # ── Album tracks ──────────────────────────────────────────
        if path == "/api/album_tracks":
            udn    = params.get("udn", "")
            artist = params.get("artist", "")
            album  = params.get("album", "")
            if not udn or not album:
                self._json(400, {"error": "Missing udn or album"})
                return
            # Pure SQLite — works offline
            tracks = DB.album_tracks(udn, artist, album)
            SERVERS.touch(udn)
            self._json(200, {"tracks": tracks})
            return

        # ── Albums (SQLite — all albums A-Z) ─────────────────────
        if path == "/api/albums":
            udn = params.get("udn", "")
            if not udn:
                self._json(400, {"error": "Missing udn"})
                return
            self._json(200, DB.all_albums(udn))
            return

        # ── Genres (SQLite — all genres A-Z) ─────────────────────
        if path == "/api/genres":
            udn = params.get("udn", "")
            if not udn:
                self._json(400, {"error": "Missing udn"})
                return
            self._json(200, DB.all_genres(udn))
            return

        # ── Genre albums (SQLite) ────────────────────────────────
        if path == "/api/genre_albums":
            udn   = params.get("udn", "")
            genre = params.get("genre", "")
            if not udn or not genre:
                self._json(400, {"error": "Missing udn or genre"})
                return
            self._json(200, DB.genre_albums(udn, genre))
            return

        # ── Genre tracks (SQLite) ────────────────────────────────
        if path == "/api/genre_tracks":
            udn   = params.get("udn", "")
            genre = params.get("genre", "")
            if not udn or not genre:
                self._json(400, {"error": "Missing udn or genre"})
                return
            tracks = DB.genre_tracks(udn, genre)
            self._json(200, {"tracks": tracks})
            return

        # ── Play a single track (GET keeps it simple for the JS side) ──
        if path == "/api/play":
            url   = params.get("url", "")
            title = params.get("title", "")
            if not url:
                self._json(400, {"error": "Missing url"})
                return
            threading.Thread(target=PLAYER.play, args=(url, title),
                             daemon=True).start()
            log.info(f"GET /api/play  title={title!r}")
            self._json(200, {"ok": True})
            return

        # ── Renderer state (for UI polling when Uniti is active output) ──
        if path == "/api/renderer_state":
            self._json(200, RENDERER_QUEUE.snapshot())
            return

        # ── Capabilities — what outputs are available on this host ──
        if path == "/api/capabilities":
            import shutil
            from dlna_player import IINA_PATHS
            iina_ok = any(
                os.path.exists(p) or bool(shutil.which(p))
                for p in IINA_PATHS)
            self._json(200, {"iina": iina_ok})
            return

        # ── Player state ──────────────────────────────────────────
        if path == "/api/state":
            self._json(200, PLAYER.snapshot())
            return

        # ── Index ─────────────────────────────────────────────────
        if path == "/api/index/status":
            udn   = params.get("udn", "")
            count = DB.track_count(udn) if udn else 0
            self._json(200, {**INDEXER.state.get(), "db_tracks": count})
            return

        if path == "/api/index/rebuild":
            udn = params.get("udn", "")
            srv = SERVERS.get(udn)
            if not srv:
                self._json(404, {"error": "Server not found"})
                return
            INDEXER.start(srv, force=True)
            self._json(200, {"ok": True, "message": "Reindex started"})
            return

        # ── Playlists (all GET — no side effects except create/delete) ──
        if path == "/api/playlists":
            self._json(200, DB.pl_list())
            return

        if path == "/api/playlist":
            pl_id = params.get("id", "")
            pl = DB.pl_get(pl_id)
            if pl is None:
                self._json(404, {"error": "Playlist not found"})
                return
            self._json(200, pl)
            return

        if path == "/api/playlist/create":
            name  = params.get("name", "").strip() or "New Playlist"
            pl_id = DB.pl_create(name)
            self._json(200, {"id": pl_id, "name": name})
            return

        if path == "/api/playlist/delete":
            pl_id = params.get("id", "")
            ok = DB.pl_delete(pl_id)
            self._json(200 if ok else 400, {"ok": ok})
            return

        if path == "/api/playlist/add":
            pl_id = params.get("pl", "")
            track = {k: params.get(k, "") for k in
                     ("url", "title", "artist", "album", "duration", "art")}
            if not track["url"] or not pl_id:
                self._json(400, {"error": "Missing pl or url"})
                return
            result = DB.pl_add_track(pl_id, track)
            if result == "not_found":
                self._json(404, {"ok": False, "error": "Playlist not found"})
            elif result == "duplicate":
                self._json(200, {"ok": True, "duplicate": True})
            else:
                self._json(200, {"ok": True, "duplicate": False})
            return

        if path == "/api/playlist/remove":
            pl_id = params.get("pl", "")
            url   = params.get("url", "")
            ok    = DB.pl_remove_track(pl_id, url)
            self._json(200, {"ok": ok})
            return

        # ── Stream proxy ──────────────────────────────────────────
        if path == "/stream":
            url = params.get("url", "")
            if not url:
                self.send_error(400, "Missing url")
                return
            proxy_stream(url, self)
            return

        # ── UPnP gateway device description ───────────────────────
        if path == "/gw/device.xml":
            lan_ip = _get_lan_ip()
            port   = self.server.server_address[1]
            self._xml_response(200, _gw_device_xml(lan_ip, port).encode())
            return

        if path == "/gw/cd/desc.xml":
            self._xml_response(200, _gw_cd_desc_xml().encode())
            return

        if path == "/gw/cd/events":
            self.send_response(200)
            self.end_headers()
            return

        # ── PWA manifest ─────────────────────────────────────────
        if path == "/manifest.json":
            import json as _json
            manifest = {
                "name": "DLNA Gateway",
                "short_name": "DLNA GW",
                "description": "Personal music gateway — browse, search and play your library",
                "start_url": "/",
                "display": "standalone",
                "orientation": "portrait",
                "background_color": "#0e0d0b",
                "theme_color": "#0e0d0b",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
                ],
                "categories": ["music", "entertainment"]
            }
            body = _json.dumps(manifest, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── PWA icons (generated as PNG from SVG via cairosvg or fallback) ──
        if path in ("/icon-192.png", "/icon-512.png"):
            size = 192 if "192" in path else 512
            png = _make_icon_png(size)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(png)
            return

        # ── Service Worker ───────────────────────────────────────
        if path == "/sw.js":
            sw_js = _SERVICE_WORKER_JS
            body = sw_js.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            # Service workers must NOT be cached aggressively
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)
            return

        self._html(404, "<h1>404 Not Found</h1>")

    # ── POST ──────────────────────────────────────────────────────

    def do_POST(self):
        """
        ALL POST endpoints live here — separate from do_GET.
        This was the missing method that caused all playback to fail.
        """
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = self.rfile.read(length)
        except Exception as e:
            log.warning(f"do_POST: cannot read body: {e}")
            body = b""

        # ── Play a track list (M3U → IINA) ───────────────────────
        if path == "/api/play_tracks":
            try:
                data   = json.loads(body)
                tracks = data.get("tracks", [])
                title  = data.get("title", "Playlist")
                if not tracks:
                    self._json(400, {"error": "No tracks provided"})
                    return
                m3u = DB.tracks_to_m3u(tracks, M3U_TMP)
                threading.Thread(target=PLAYER.play, args=(m3u, title),
                                 daemon=True).start()
                log.info(f"POST /api/play_tracks  {len(tracks)} tracks → {m3u}")
                self._json(200, {"ok": True, "tracks": len(tracks)})
            except Exception as e:
                log.exception(f"play_tracks error: {e}")
                self._json(500, {"error": str(e)})
            return

        # ── Send full track queue to a UPnP renderer ──────────────
        # ── Chromecast queue ───────────────────────────────────────
        if path == "/api/cast_queue":
            try:
                data   = json.loads(body)
                uuid   = data.get("uuid", "")
                tracks = data.get("tracks", [])
                dev    = CAST_DEVICES.get(uuid)
                if not dev:
                    self._json(404, {"error": f"Cast device {uuid!r} not found"})
                    return
                if not tracks:
                    self._json(400, {"error": "No tracks"})
                    return
                # Build stream base URL from the request host
                host_hdr = self.headers.get("Host", "localhost:8765")
                scheme = "https" if self.server.socket.fileno() and hasattr(self.server.socket, 'getpeercert') else "http"
                # Use the LAN IP so the Chromecast can reach us
                lan_ip = socket.gethostbyname(socket.gethostname())
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    s.connect(("8.8.8.8", 80))
                    lan_ip = s.getsockname()[0]
                    s.close()
                except Exception:
                    pass
                port = self.server.server_address[1]
                stream_base = f"http://{lan_ip}:{port}"
                log.info(f"POST /api/cast_queue  {len(tracks)} tracks → {dev.name}  (base: {stream_base})")
                threading.Thread(
                    target=CAST_QUEUE.start,
                    args=(uuid, tracks, stream_base),
                    daemon=True).start()
                self._json(200, {"ok": True, "tracks": len(tracks), "device": dev.name})
            except Exception as e:
                log.exception("cast_queue error")
                self._json(500, {"error": str(e)})
            return

        # ── UPnP renderer ──────────────────────────────────────────
        if path == "/api/render_queue":
            try:
                data   = json.loads(body)
                udn    = data.get("udn", "")
                tracks = data.get("tracks", [])
                rnd    = RENDERERS.get(udn)
                if not rnd:
                    self._json(404, {"error": f"Renderer {udn!r} not found"})
                    return
                if not tracks:
                    self._json(400, {"error": "No tracks"})
                    return
                log.info(f"POST /api/render_queue  {len(tracks)} tracks → {rnd.name}")
                threading.Thread(
                    target=RENDERER_QUEUE.start,
                    args=(rnd.av_url, tracks, rnd.name),
                    daemon=True).start()
                self._json(200, {"ok": True, "tracks": len(tracks)})
            except Exception as e:
                log.exception(f"render_queue error: {e}")
                self._json(500, {"error": str(e)})
            return

        # ── Send a single track to a UPnP renderer ────────────────
        if path == "/api/render":
            try:
                data  = json.loads(body)
                udn   = data.get("udn", "")
                url   = data.get("url", "")
                title = data.get("title", "")
                mime  = data.get("mime", "")
                rnd   = RENDERERS.get(udn)
                if not rnd:
                    self._json(404, {"error": f"Renderer {udn!r} not found"})
                    return
                ok = avtransport_send(rnd.av_url, url, title, mime)
                log.info(f"POST /api/render  {title!r} → {rnd.name}  ok={ok}")
                self._json(200 if ok else 502, {"ok": ok})
            except Exception as e:
                log.exception(f"render error: {e}")
                self._json(500, {"error": str(e)})
            return

        # ── Player / renderer control ─────────────────────────────
        if path == "/api/control":
            try:
                cmd    = json.loads(body)
                action = cmd.get("action", "")
                device = cmd.get("device", "iina")   # "iina", "upnp:<udn>", or "cast:<uuid>"

                if device.startswith("cast:"):
                    # ── Chromecast control ────────────────────────
                    uuid = device.replace("cast:", "")
                    dev = CAST_DEVICES.get(uuid)
                    if not dev:
                        self._json(404, {"error": "Cast device not found"})
                        return
                    if action == "pause":
                        CAST_QUEUE.pause()
                    elif action == "stop":
                        CAST_QUEUE.stop()
                    elif action == "next":
                        # Build stream_base for next track
                        lan_ip = socket.gethostbyname(socket.gethostname())
                        try:
                            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            s.connect(("8.8.8.8", 80))
                            lan_ip = s.getsockname()[0]
                            s.close()
                        except Exception:
                            pass
                        port = self.server.server_address[1]
                        CAST_QUEUE.next_track(f"http://{lan_ip}:{port}")
                    elif action == "prev":
                        lan_ip = socket.gethostbyname(socket.gethostname())
                        try:
                            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                            s.connect(("8.8.8.8", 80))
                            lan_ip = s.getsockname()[0]
                            s.close()
                        except Exception:
                            pass
                        port = self.server.server_address[1]
                        CAST_QUEUE.prev_track(f"http://{lan_ip}:{port}")
                    else:
                        log.debug(f"Cast control: {action!r} not implemented")
                    self._json(200, {"ok": True})

                elif device != "iina" and device.startswith("upnp:"):
                    # ── Uniti / AVTransport control ───────────────
                    from dlna_content import (avtransport_stop,
                                              avtransport_pause)
                    udn = device.replace("upnp:", "")
                    rnd = RENDERERS.get(udn)
                    if not rnd:
                        self._json(404, {"error": "Renderer not found"})
                        return
                    if action == "pause":
                        RENDERER_QUEUE.pause()
                    elif action == "stop":
                        RENDERER_QUEUE.stop()
                    elif action == "next":
                        RENDERER_QUEUE.next_track()
                    elif action == "prev":
                        RENDERER_QUEUE.prev_track()
                    else:
                        # seek, volume etc. not supported on AVTransport simply
                        log.debug(f"Renderer control: {action!r} not implemented")
                    self._json(200, {"ok": True})

                else:
                    # ── IINA / mpv control ────────────────────────
                    if action == "pause":
                        PLAYER.ipc({"command": ["cycle", "pause"]})
                    elif action == "stop":
                        PLAYER.ipc({"command": ["stop"]})
                        with PLAYER._lock:
                            PLAYER.state = "stopped"
                    elif action == "next":
                        PLAYER.ipc({"command": ["playlist-next", "force"]})
                    elif action == "prev":
                        PLAYER.ipc({"command": ["playlist-prev", "force"]})
                    elif action == "shuffle":
                        PLAYER.ipc({"command": ["cycle", "shuffle"]})
                    elif action == "seek":
                        PLAYER.ipc({"command": ["seek",
                                    float(cmd.get("value", 0)), "relative"]})
                    elif action == "seek_abs":
                        PLAYER.ipc({"command": ["seek",
                                    float(cmd.get("value", 0)), "absolute"]})
                    elif action == "volume":
                        PLAYER.ipc({"command": ["set_property", "volume",
                                    int(cmd.get("value", 80))]})
                    self._json(200, {"ok": True})

            except Exception as e:
                log.warning(f"control error: {e}")
                self._json(400, {"error": str(e)})
            return

        # ── UPnP ContentDirectory Browse (for Naim Uniti) ─────────
        if path == "/gw/cd/control":
            try:
                root   = ET.fromstring(body.decode("utf-8"))
                ns     = {"s": "http://schemas.xmlsoap.org/soap/envelope/",
                          "u": "urn:schemas-upnp-org:service:ContentDirectory:1"}
                browse = root.find(".//u:Browse", ns)
                if browse is None:
                    # Try without namespace (some renderers omit it)
                    browse = root.find(".//Browse")
                if browse is None:
                    log.debug("GW Browse: no Browse element in SOAP body (ignored)")
                    self._html(400, "<h1>Missing Browse element</h1>")
                    return
                obj_id = browse.findtext("ObjectID") or "0"
                flag   = browse.findtext("BrowseFlag") or "BrowseDirectChildren"
                start  = int(browse.findtext("StartingIndex") or 0)
                count  = int(browse.findtext("RequestedCount") or 0)
                if count == 0:
                    count = 9999

                result_xml, n_ret, total = _gw_browse(obj_id, flag, start, count)
                resp = _gw_browse_response(result_xml, n_ret, total)
                log.debug(f"GW SOAP Browse {obj_id!r} → {n_ret}/{total}")
                self._xml_response(200, resp)
            except Exception as e:
                log.error(f"GW Browse error: {e}")
                self._html(500, f"<h1>Browse error: {e}</h1>")
            return

        self._html(404, "<h1>404 Not Found</h1>")


# ── Utilities ─────────────────────────────────────────────────────

def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── UPnP ContentDirectory gateway ────────────────────────────────

def _xml_esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;").replace('"', "&quot;")


def _gw_device_xml(lan_ip: str, port: int) -> str:
    base = f"http://{lan_ip}:{port}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">'
        '<specVersion><major>1</major><minor>0</minor></specVersion>'
        f'<URLBase>{base}</URLBase>'
        '<device>'
        '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>'
        f'<friendlyName>{GW_NAME}</friendlyName>'
        '<manufacturer>dlna-gateway</manufacturer>'
        '<modelName>dlna-gateway</modelName>'
        f'<UDN>{GW_UDN}</UDN>'
        '<serviceList><service>'
        '<serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>'
        '<serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>'
        '<SCPDURL>/gw/cd/desc.xml</SCPDURL>'
        '<controlURL>/gw/cd/control</controlURL>'
        '<eventSubURL>/gw/cd/events</eventSubURL>'
        '</service></serviceList>'
        '</device></root>'
    )


def _gw_cd_desc_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        '<specVersion><major>1</major><minor>0</minor></specVersion>'
        '<actionList><action><n>Browse</n><argumentList>'
        '<argument><n>ObjectID</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>'
        '<argument><n>BrowseFlag</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>'
        '<argument><n>Filter</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>'
        '<argument><n>StartingIndex</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>'
        '<argument><n>RequestedCount</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '<argument><n>SortCriteria</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>'
        '<argument><n>Result</n><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>'
        '<argument><n>NumberReturned</n><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '<argument><n>TotalMatches</n><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '<argument><n>UpdateID</n><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '</argumentList></action></actionList>'
        '<serviceStateTable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_ObjectID</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_BrowseFlag</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_Filter</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_Index</n>'
        '<dataType>ui4</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_Count</n>'
        '<dataType>ui4</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_SortCriteria</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_Result</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="yes"><n>SystemUpdateID</n>'
        '<dataType>ui4</dataType></stateVariable>'
        '</serviceStateTable></scpd>'
    )


def _gw_browse(obj_id: str, browse_flag: str,
               start: int, count: int) -> tuple:
    """
    Returns (DIDL-Lite XML, number_returned, total_matches).
    Browse tree: 0 → playlists → pl:XXXX → tracks
    """
    def container(cid, parent, title, child_count):
        return (f'<container id="{_xml_esc(cid)}" parentID="{_xml_esc(parent)}" '
                f'restricted="1" childCount="{child_count}">'
                f'<dc:title>{_xml_esc(title)}</dc:title>'
                f'<upnp:class>object.container.playlistContainer</upnp:class>'
                f'</container>')

    def track_item(t, parent_id):
        url   = _xml_esc(t.get("url",""))
        title = _xml_esc(t.get("title",""))
        art   = _xml_esc(t.get("art",""))
        dur   = t.get("duration","")
        art_tag = f'<upnp:albumArtURI>{art}</upnp:albumArtURI>' if art else ""
        return (
            f'<item id="tr:{_xml_esc(t.get("url",""))}" '
            f'parentID="{_xml_esc(parent_id)}" restricted="1">'
            f'<dc:title>{title}</dc:title>'
            f'<dc:creator>{_xml_esc(t.get("artist",""))}</dc:creator>'
            f'<upnp:artist>{_xml_esc(t.get("artist",""))}</upnp:artist>'
            f'<upnp:album>{_xml_esc(t.get("album",""))}</upnp:album>'
            f'{art_tag}'
            f'<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            f'<res protocolInfo="http-get:*:audio/x-flac:*" '
            f'duration="{dur}">{url}</res>'
            f'</item>')

    OPEN  = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">')
    CLOSE = '</DIDL-Lite>'

    if obj_id == "0":
        if browse_flag == "BrowseMetadata":
            return OPEN + container("0", "-1", GW_NAME, 1) + CLOSE, 1, 1
        n_pls = len(DB.pl_list())
        return OPEN + container("playlists", "0", "Playlists", n_pls) + CLOSE, 1, 1

    if obj_id == "playlists":
        pls   = DB.pl_list()
        total = len(pls)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container("playlists", "0", "Playlists", total) + CLOSE,
                    1, 1)
        page  = pls[start:start + count] if count else pls[start:]
        items = [container(f"pl:{p['id']}", "playlists", p["name"], p["count"])
                 for p in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id.startswith("pl:"):
        pl_id  = obj_id[3:]
        pl     = DB.pl_get(pl_id)
        if not pl:
            return OPEN + CLOSE, 0, 0
        tracks = pl["tracks"]
        total  = len(tracks)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, "playlists", pl["name"], total) + CLOSE,
                    1, 1)
        page  = tracks[start:start + count] if count else tracks[start:]
        items = [track_item(t, obj_id) for t in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    return OPEN + CLOSE, 0, 0


def _gw_browse_response(result_xml: str, n_returned: int,
                        total: int, update_id: int = 1) -> bytes:
    escaped = (result_xml.replace("&", "&amp;")
                         .replace("<", "&lt;")
                         .replace(">", "&gt;"))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        '<u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        f'<Result>{escaped}</Result>'
        f'<NumberReturned>{n_returned}</NumberReturned>'
        f'<TotalMatches>{total}</TotalMatches>'
        f'<UpdateID>{update_id}</UpdateID>'
        '</u:BrowseResponse>'
        '</s:Body></s:Envelope>'
    )
    return body.encode("utf-8")


# ── SSDP announce for gateway as MediaServer ──────────────────────

def _gw_ssdp_notify(lan_ip: str, port: int, alive: bool = True):
    location = f"http://{lan_ip}:{port}/gw/device.xml"
    entries = [
        ("upnp:rootdevice",
         f"{GW_UDN}::upnp:rootdevice"),
        (GW_UDN,
         GW_UDN),
        ("urn:schemas-upnp-org:device:MediaServer:1",
         f"{GW_UDN}::urn:schemas-upnp-org:device:MediaServer:1"),
        ("urn:schemas-upnp-org:service:ContentDirectory:1",
         f"{GW_UDN}::urn:schemas-upnp-org:service:ContentDirectory:1"),
    ]
    msgs = []
    for nt, usn in entries:
        if alive:
            m = (f"NOTIFY * HTTP/1.1\r\n"
                 f"HOST: 239.255.255.250:1900\r\n"
                 f"CACHE-CONTROL: max-age=1800\r\n"
                 f"LOCATION: {location}\r\n"
                 f"NT: {nt}\r\n"
                 f"NTS: ssdp:alive\r\n"
                 f"SERVER: Python/3 UPnP/1.0 dlna-gateway/1.0\r\n"
                 f"USN: {usn}\r\n\r\n")
        else:
            m = (f"NOTIFY * HTTP/1.1\r\n"
                 f"HOST: 239.255.255.250:1900\r\n"
                 f"NT: {nt}\r\n"
                 f"NTS: ssdp:byebye\r\n"
                 f"USN: {usn}\r\n\r\n")
        msgs.append(m.encode("utf-8"))
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                             socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(lan_ip))
        for m in msgs:
            sock.sendto(m, ("239.255.255.250", 1900))
            time.sleep(0.05)
        sock.close()
    except Exception as e:
        log.debug(f"GW SSDP notify: {e}")


def gw_ssdp_announcer(lan_ip: str, port: int):
    """Background thread: send SSDP alive every 60 s."""
    time.sleep(3)
    while True:
        _gw_ssdp_notify(lan_ip, port, alive=True)
        log.debug("GW SSDP alive sent")
        time.sleep(60)


def gw_ssdp_byebye(lan_ip: str, port: int):
    _gw_ssdp_notify(lan_ip, port, alive=False)


# ── Standalone test ───────────────────────────────────────────────

def _test():
    from dlna_config import setup_logging
    setup_logging(debug=True)
    log.info("=== dlna_server self-test (30 s on :8766) ===")

    server = ThreadedHTTPServer(("0.0.0.0", 8766), GatewayHandler)
    log.info("Server up at http://localhost:8766/")
    log.info("  GET  /api/servers   → should return []")
    log.info("  GET  /api/renderers → should return []")
    log.info("  GET  /api/state     → player state")
    log.info("  GET  /api/playlists → playlist list")

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(30)
    server.shutdown()
    log.info("PASS — dlna_server OK (server ran 30 s without crashing)")


if __name__ == "__main__":
    _test()
    