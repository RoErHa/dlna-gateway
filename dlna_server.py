#!/usr/bin/env python3
"""
dlna_server.py — Threaded HTTP server: static serving + thin API router.

Routes GET/POST requests to the appropriate api_* module.
Domain logic lives in: api_browse, api_playback, api_playlists, api_upnp.

Standalone test (starts server on port 8766 for 30 s):
    python dlna_server.py
"""
import json
import logging
import os
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

import api_browse
import api_playback
import api_playlists
import api_upnp
from dlna_routes import GET_ROUTES, POST_ROUTES

# Re-export for dlna_gateway.py
from api_upnp import GW_UDN, gw_ssdp_announcer, gw_ssdp_byebye  # noqa: F401

log = logging.getLogger("dlna.server")


# ── Threaded HTTP server ──────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class TLSThreadedHTTPServer(ThreadedHTTPServer):
    """
    HTTPS variant hardened against accept-loop stalls.

    The default `SSLSocket.accept()` performs the TLS handshake inline on
    the accepting thread. A single client that opens a TCP connection and
    never sends a ClientHello (port scanner, sleeping phone, dropped peer)
    blocks the entire HTTPS server until that handshake completes — which
    is never. Has been observed to wedge the gateway for days.

    This subclass:
      • requires the listening socket to be wrapped with
        `do_handshake_on_connect=False`, so accept() returns immediately
        with an unhandshaked SSLSocket; the handshake then happens lazily
        on the per-request worker thread's first read;
      • sets a per-connection socket timeout, so a stalled handshake or
        slow client tears down its own thread instead of leaking forever;
      • downgrades the noisy stderr traceback for routine handshake/timeout
        errors to a single log.warning line.
    """

    REQUEST_TIMEOUT = 30.0  # per read/write op, not total connection

    def get_request(self):
        sock, addr = self.socket.accept()
        try:
            sock.settimeout(self.REQUEST_TIMEOUT)
        except OSError:
            pass
        return sock, addr

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, socket.timeout,
                            ConnectionResetError, BrokenPipeError, OSError)):
            log.warning(
                f"HTTPS: dropped {client_address}: "
                f"{type(exc).__name__}: {exc}")
            return
        super().handle_error(request, client_address)


# ── PWA icon generator ────────────────────────────────────────────

def _make_icon_png(size: int) -> bytes:
    """Generate a simple PNG icon: dark square with amber ♪ symbol."""
    bg    = (14, 13, 11)
    amber = (212, 168, 67)

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


# ── Request handler ───────────────────────────────────────────────

class GatewayHandler(BaseHTTPRequestHandler):

    # ── Logging / error suppression ───────────────────────────────

    def log_message(self, fmt, *args):
        log.debug(f"{self.address_string()} {fmt % args}")

    def handle_error(self):
        pass

    # ── Response helpers ──────────────────────────────────────────

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

    def _serve_static(self, filename: str, content_type: str):
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        fpath = os.path.join(static_dir, filename)
        try:
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            ct = content_type
            if content_type.startswith("text/") or "javascript" in content_type or "json" in content_type:
                ct += "; charset=utf-8"
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            if content_type in ("text/css", "application/javascript"):
                self.send_header("Cache-Control", "public, max-age=60")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._html(404, "<h1>404 Not Found</h1>")
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── HTTPS redirect ────────────────────────────────────────────

    # Paths that must stay on HTTP — UPnP renderers can't do HTTPS
    _HTTP_ONLY = ("/stream", "/gw/")

    def _redirect_https(self) -> bool:
        """
        If HTTPS is running and this request arrived on the plain HTTP server,
        send a 301 to the HTTPS equivalent — except for device-only endpoints.
        Returns True if a redirect was sent (caller should return immediately).
        """
        tls_port = getattr(self.server, "tls_port", None)
        if not tls_port:
            return False   # HTTPS not configured
        path = urllib.parse.urlparse(self.path).path
        if any(path.startswith(p) for p in self._HTTP_ONLY):
            return False   # device endpoint — keep on HTTP
        host = self.headers.get("Host", "").split(":")[0] or "localhost"
        self.send_response(301)
        self.send_header("Location", f"https://{host}:{tls_port}{self.path}")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        return True

    # ── OPTIONS (CORS pre-flight) ─────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ── GET ───────────────────────────────────────────────────────

    # Route tables live in dlna_routes — see that module to add endpoints.

    def do_GET(self):
        if self._redirect_https():
            return
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        params = dict(urllib.parse.parse_qsl(parsed.query))

        # ── Web UI ────────────────────────────────────────────────
        if path in ("/", "/index.html"):
            self._serve_static("index.html", "text/html")
            return

        # ── Static files ─────────────────────────────────────────
        if path.startswith("/static/"):
            fname = path[len("/static/"):]
            if ".." in fname or "/" in fname:
                self.send_error(403)
                return
            _MIME = {
                ".css":  "text/css",
                ".js":   "application/javascript",
                ".json": "application/json",
                ".png":  "image/png",
                ".svg":  "image/svg+xml",
            }
            ext = "." + fname.rsplit(".", 1)[-1] if "." in fname else ""
            self._serve_static(fname, _MIME.get(ext, "application/octet-stream"))
            return

        # ── Service Worker ────────────────────────────────────────
        if path == "/sw.js":
            static_dir = os.path.join(os.path.dirname(__file__), "static")
            try:
                with open(os.path.join(static_dir, "sw.js"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Service-Worker-Allowed", "/")
                self.end_headers()
                self.wfile.write(body)
            except FileNotFoundError:
                self._html(404, "<h1>sw.js not found</h1>")
            return

        # ── PWA manifest ──────────────────────────────────────────
        if path == "/manifest.json":
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
                    {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
                ],
                "categories": ["music", "entertainment"],
            }
            body = json.dumps(manifest, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── PWA icons ─────────────────────────────────────────────
        if path in ("/icon-192.png", "/icon-512.png"):
            size = 192 if "192" in path else 512
            png  = _make_icon_png(size)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(png)
            return

        # ── API / UPnP routes ─────────────────────────────────────
        fn = GET_ROUTES.get(path)
        if fn:
            fn(self, params)
            return

        self._html(404, "<h1>404 Not Found</h1>")

    # ── POST ──────────────────────────────────────────────────────

    def do_POST(self):
        if self._redirect_https():
            return
        parsed = urllib.parse.urlparse(self.path)
        path   = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = self.rfile.read(length)
        except Exception as e:
            log.warning(f"do_POST: cannot read body: {e}")
            body = b""

        fn = POST_ROUTES.get(path)
        if fn:
            fn(self, body)
            return

        self._html(404, "<h1>404 Not Found</h1>")


# ── Standalone test ───────────────────────────────────────────────

def _test():
    from dlna_config import setup_logging
    setup_logging(debug=True)
    log.info("=== dlna_server self-test (30 s on :8766) ===")

    server = ThreadedHTTPServer(("0.0.0.0", 8766), GatewayHandler)
    log.info("Server up at http://localhost:8766/")

    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(30)
    server.shutdown()
    log.info("PASS — dlna_server OK (server ran 30 s without crashing)")


if __name__ == "__main__":
    _test()
