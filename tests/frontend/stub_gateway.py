"""
Stub HTTP gateway for frontend Playwright tests.

Serves the real `static/` files (so we test the real index.html / app.js)
and mocks every `/api/*` endpoint app.js calls. State is settable per-test
via attributes on the StubGateway instance; every received request is
captured into `gateway.requests` so tests can assert what the frontend
sent.

Why a stub instead of the real gateway: the real gateway needs UPnP
devices on the network and a SQLite library. Tests must be deterministic
and runnable on CI without an AssetUPnP server.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"


class StubGateway:
    """In-process state for a single test. Created by the `gateway` fixture."""

    def __init__(self) -> None:
        # Default state — overridden by tests as needed
        self.servers: list[dict] = [
            {"udn": "uuid:asset-1", "name": "AssetUPnP", "online": True, "tracks": 1234}
        ]
        self.renderers: list[dict] = []  # tests can populate
        # browse_letter responses keyed by (mode, letter) -> {items, total, offset, limit}
        self.browse_pages: dict[tuple, dict] = {}
        self.artists_default: list[dict] = []
        self.albums_default: list[dict] = []
        self.tracks_default: list[dict] = []
        self.genres: list[dict] = []
        # artist_albums keyed by artist name
        self.artist_albums: dict[str, list[dict]] = {}
        # album_tracks keyed by (artist, album)
        self.album_tracks: dict[tuple, list[dict]] = {}
        # album_tracks keyed by album_key (LocalFs folder identity)
        self.album_tracks_by_key: dict[str, list[dict]] = {}
        # search responses keyed by query string (lowercased)
        self.search_results: dict[str, dict] = {}
        # playlists
        self.playlists: list[dict] = [
            {"id": "__favourites__", "name": "Favourites", "count": 0, "tracks": []}
        ]
        # whole-album favourites — distinct from the track-level Favourites
        # playlist. Each entry: {artist, album, art, track_count, udn, added_at}
        self.album_favourites: list[dict] = []
        # radio tracks pool
        self.radio_tracks: list[dict] = []
        # renderer state
        self.renderer_state: dict = {
            "state": "stopped", "alive": False,
            "media_title": "", "title": "", "artist": "", "album": "",
            "duration": 0, "position": 0,
            "queue_pos": 0, "queue_len": 0, "paused": False,
        }
        # index status
        self.index_status: dict = {"status": "idle", "progress": 0, "total": 0,
                                   "tracks": 0, "db_tracks": 0}
        # /api/render_queue can be told to reject with 409 once
        self.render_queue_busy: dict | None = None
        # internet radio ("📡 Stations") — Phase 2 frontend
        self.radio_favourites: list[dict] = []
        self.radio_search_results: list[dict] = []
        self.radio_fav_full: bool = False      # force /add to 409
        self.icy_title: str = ""               # /api/radio/nowplaying
        # captured requests: list of {method, path, query, body, headers}
        self.requests: list[dict] = []
        self._req_lock = threading.Lock()

    def add_artist(self, name: str, album_count: int = 1, track_count: int = 10,
                   art: str = "") -> None:
        self.artists_default.append(
            {"artist": name, "album_count": album_count,
             "track_count": track_count, "art": art}
        )

    def add_album(self, artist: str, album: str, track_count: int = 10,
                  art: str = "") -> None:
        self.albums_default.append(
            {"artist": artist, "album": album,
             "track_count": track_count, "art": art}
        )

    def add_track(self, artist: str, album: str, title: str,
                  url: str | None = None, duration: str = "0:03:30",
                  art: str = "") -> dict:
        url = url or f"http://stub/{artist}/{album}/{title}.flac".replace(" ", "_")
        t = {
            "url": url, "title": title, "artist": artist, "album": album,
            "duration": duration, "art": art, "type": "audio",
            "id": f"track:{title}", "mime": "audio/flac",
        }
        self.tracks_default.append(t)
        self.album_tracks.setdefault((artist, album), []).append(t)
        return t

    def add_playlist(self, pl_id: str, name: str, tracks: list[dict] | None = None) -> dict:
        pl = {"id": pl_id, "name": name, "count": len(tracks or []),
              "tracks": tracks or []}
        # Replace existing if id matches
        self.playlists = [p for p in self.playlists if p["id"] != pl_id]
        self.playlists.append(pl)
        return pl

    def captured(self, method: str | None = None, path_contains: str | None = None) -> list[dict]:
        with self._req_lock:
            out = list(self.requests)
        if method:
            out = [r for r in out if r["method"] == method.upper()]
        if path_contains:
            out = [r for r in out if path_contains in r["path"]]
        return out

    def clear_requests(self) -> None:
        """Reset the captured-requests log. Call before an action so subsequent
        assertions only see what THAT action produced."""
        with self._req_lock:
            self.requests.clear()

    def wait_for_request(self, path_contains: str, method: str = "GET",
                         timeout: float = 3.0,
                         match: callable | None = None) -> dict | None:
        """Wait up to `timeout` seconds for a captured request matching path/method.
        Optional `match` callable is given the request dict and must return True."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for r in self.captured(method=method, path_contains=path_contains):
                if match is None or match(r):
                    return r
            time.sleep(0.05)
        return None


class _Handler(BaseHTTPRequestHandler):
    gateway: StubGateway = None  # type: ignore[assignment]

    def log_message(self, *a, **k):  # silence default access logging
        pass

    def _capture(self, body: bytes = b"") -> dict:
        u = urlparse(self.path)
        rec = {
            "method": self.command,
            "path": u.path,
            "query": {k: v[0] if len(v) == 1 else v
                      for k, v in parse_qs(u.query).items()},
            "body": body.decode("utf-8", errors="replace") if body else "",
            "headers": dict(self.headers),
        }
        with self.gateway._req_lock:
            self.gateway.requests.append(rec)
        return rec

    def _safe_write(self, data: bytes) -> None:
        # Browser may close the connection before we finish (SW prefetch,
        # tab close, etc.). Don't pollute stderr with BrokenPipe.
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._safe_write(data)

    def _send_static(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self._safe_write(data)

    # ---- routing ----
    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] if len(v) == 1 else v
             for k, v in parse_qs(u.query).items()}
        path = u.path
        self._capture()
        gw = self.gateway

        # Static files
        if path in ("/", "/index.html"):
            self._send_static(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if path == "/static/app.js":
            self._send_static(STATIC_DIR / "app.js", "application/javascript")
            return
        if path == "/static/app.css":
            self._send_static(STATIC_DIR / "app.css", "text/css")
            return
        if path == "/sw.js":
            self._send_static(STATIC_DIR / "sw.js", "application/javascript")
            return
        if path == "/manifest.json":
            # Mirror the real backend manifest (dlna_server.py:294–311) so the
            # PWA test asserts the same install contract production serves.
            data = json.dumps({
                "name": "DLNA Gateway",
                "short_name": "DLNA GW",
                "description": "Personal music gateway — browse, search and play your library",
                "start_url": "/",
                "display": "standalone",
                "orientation": "portrait",
                "background_color": "#0e0d0b",
                "theme_color": "#0e0d0b",
                "icons": [
                    {"src": "/icon-192.png", "sizes": "192x192",
                     "type": "image/png", "purpose": "any maskable"},
                    {"src": "/icon-512.png", "sizes": "512x512",
                     "type": "image/png", "purpose": "any maskable"},
                ],
                "categories": ["music", "entertainment"],
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self._safe_write(data)
            return
        if path in ("/icon-192.png", "/icon-512.png"):
            # 1x1 transparent PNG
            png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
                   b"\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc"
                   b"\xfc\xcf\xc0\x00\x00\x00\x05\x00\x01\rJ\xb4\xfe\x00\x00\x00\x00IEND\xaeB`\x82")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self._safe_write(png)
            return

        # API endpoints
        if path == "/api/servers":
            self._send_json(gw.servers)
            return
        if path == "/api/renderers":
            self._send_json(gw.renderers)
            return
        if path == "/api/browse_letter":
            mode = q.get("mode", "artists")
            letter = q.get("letter", "A")
            offset = int(q.get("offset", "0") or 0)
            limit = int(q.get("limit", "100") or 100)
            key = (mode, letter)
            page = gw.browse_pages.get(key)
            if page is not None:
                self._send_json(page)
                return
            # Default: filter the *_default lists by initial letter
            if mode == "artists":
                items = [a for a in gw.artists_default
                         if (a["artist"][:1].upper() == letter.upper()
                             or (letter == "#" and not a["artist"][:1].isalnum()))]
            elif mode == "albums":
                items = [a for a in gw.albums_default
                         if a["album"][:1].upper() == letter.upper()]
            elif mode == "tracks":
                items = [t for t in gw.tracks_default
                         if t["title"][:1].upper() == letter.upper()]
            else:
                items = []
            total = len(items)
            self._send_json({"items": items[offset:offset + limit],
                             "total": total, "offset": offset, "limit": limit})
            return
        if path == "/api/genres":
            self._send_json(gw.genres)
            return
        if path == "/api/genre_albums":
            g = q.get("genre", "")
            self._send_json([a for a in gw.albums_default
                             if a.get("genre") == g])
            return
        if path == "/api/genre_tracks":
            g = q.get("genre", "")
            self._send_json([t for t in gw.tracks_default
                             if t.get("genre") == g])
            return
        if path == "/api/artist_albums":
            artist = q.get("artist", "")
            albums = gw.artist_albums.get(artist)
            if albums is None:
                albums = [a for a in gw.albums_default if a["artist"] == artist]
            self._send_json(albums)
            return
        if path == "/api/album_tracks":
            artist = q.get("artist", "")
            album = q.get("album", "")
            album_key = q.get("album_key", "")
            # LocalFs opens an album by folder identity (album_key);
            # legacy callers by (artist, album).
            if album_key:
                tracks = gw.album_tracks_by_key.get(album_key, [])
            else:
                tracks = gw.album_tracks.get((artist, album), [])
            self._send_json({"tracks": tracks})
            return
        if path == "/api/search":
            qstr = (q.get("q") or "").lower()
            res = gw.search_results.get(qstr)
            if res is None:
                # Default empty result
                res = {"tracks": [], "albums": [], "artists": []}
            self._send_json(res)
            return
        if path == "/api/playlists":
            # Front-end expects [{id, name, count}]
            self._send_json([{"id": p["id"], "name": p["name"],
                              "count": len(p.get("tracks", []))}
                             for p in gw.playlists])
            return
        if path == "/api/playlist":
            pid = q.get("id", "")
            for p in gw.playlists:
                if p["id"] == pid:
                    self._send_json(p)
                    return
            self._send_json({"error": "not found"}, status=404)
            return
        if path == "/api/playlist/create":
            name = q.get("name", "Untitled")
            pid = f"pl-{len(gw.playlists)}"
            gw.add_playlist(pid, name, [])
            self._send_json({"ok": True, "id": pid})
            return
        if path == "/api/playlist/delete":
            pid = q.get("id", "")
            gw.playlists = [p for p in gw.playlists if p["id"] != pid]
            self._send_json({"ok": True})
            return
        if path == "/api/playlist/add":
            pid = q.get("pl", "")
            url = q.get("url", "")
            for p in gw.playlists:
                if p["id"] == pid:
                    if any(t.get("url") == url for t in p["tracks"]):
                        self._send_json({"ok": True, "duplicate": True})
                        return
                    p["tracks"].append({
                        "url": url, "title": q.get("title", ""),
                        "artist": q.get("artist", ""),
                        "album": q.get("album", ""),
                        "duration": q.get("duration", ""),
                        "art": q.get("art", ""),
                    })
                    p["count"] = len(p["tracks"])
                    self._send_json({"ok": True, "duplicate": False})
                    return
            self._send_json({"ok": False, "error": "not found"}, status=404)
            return
        if path == "/api/playlist/remove":
            pid = q.get("pl", "")
            url = q.get("url", "")
            for p in gw.playlists:
                if p["id"] == pid:
                    p["tracks"] = [t for t in p["tracks"] if t.get("url") != url]
                    p["count"] = len(p["tracks"])
                    break
            self._send_json({"ok": True})
            return
        if path == "/api/album_favourites":
            self._send_json(gw.album_favourites)
            return
        if path == "/api/album_favourites/check":
            artist = q.get("artist", "")
            album  = q.get("album", "")
            is_fav = any(f["artist"] == artist and f["album"] == album
                         for f in gw.album_favourites)
            self._send_json({"is_favourite": is_fav})
            return
        if path == "/api/album_favourites/add":
            artist = q.get("artist", "")
            album  = q.get("album", "")
            if not album:
                self._send_json({"error": "Missing album"}, status=400)
                return
            existing = any(f["artist"] == artist and f["album"] == album
                           for f in gw.album_favourites)
            if not existing:
                gw.album_favourites.insert(0, {
                    "artist": artist, "album": album,
                    "art": "", "track_count": 0,
                    "udn": gw.servers[0]["udn"] if gw.servers else "",
                    "added_at": int(time.time()),
                })
            self._send_json({"ok": True, "created": not existing})
            return
        if path == "/api/album_favourites/remove":
            artist = q.get("artist", "")
            album  = q.get("album", "")
            before = len(gw.album_favourites)
            gw.album_favourites = [f for f in gw.album_favourites
                                   if not (f["artist"] == artist
                                           and f["album"] == album)]
            self._send_json({"ok": before != len(gw.album_favourites)})
            return
        if path == "/api/radio":
            limit = int(q.get("limit", "100") or 100)
            self._send_json({"tracks": gw.radio_tracks[:limit]})
            return
        if path == "/api/renderer_state":
            self._send_json(gw.renderer_state)
            return
        if path == "/api/index/status":
            self._send_json(gw.index_status)
            return
        if path == "/api/index/rebuild":
            gw.index_status = {"status": "running", "progress": 0, "total": 100,
                               "tracks": 0, "db_tracks": 0}
            self._send_json({"ok": True})
            return
        if path == "/api/browse":
            # UPnP container browse — used by playAlbum() in app.js
            self._send_json({"items": [], "containers": []})
            return
        if path == "/art":
            png = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
                   b"\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc"
                   b"\xfc\xcf\xc0\x00\x00\x00\x05\x00\x01\rJ\xb4\xfe\x00\x00\x00\x00IEND\xaeB`\x82")
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png)))
            self.end_headers()
            self._safe_write(png)
            return
        if path == "/stream":
            # Tiny silent WAV — enough for <audio> to load and "play"
            wav = self._silent_wav(seconds=1)
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self._safe_write(wav)
            return
        if path == "/api/radio/favourites":
            self._send_json({"stations": gw.radio_favourites, "limit": 25})
            return
        if path == "/api/radio/search":
            # The real endpoint filters HLS / proxies radio-browser; the
            # stub just returns whatever the test seeded.
            self._send_json(gw.radio_search_results)
            return
        if path == "/api/radio/nowplaying":
            self._send_json({"title": gw.icy_title, "source": "icy"})
            return
        if path == "/radio_stream":
            wav = self._silent_wav(seconds=1)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(wav)))
            self.end_headers()
            self._safe_write(wav)
            return

        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._capture(body=body)
        u = urlparse(self.path)
        path = u.path
        gw = self.gateway

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {}

        if path == "/api/render_queue":
            if gw.render_queue_busy and not payload.get("force"):
                self._send_json({"error": "renderer_busy",
                                 "busy_with": gw.render_queue_busy},
                                status=409)
                # busy state is one-shot — clear after first 409
                gw.render_queue_busy = None
                return
            self._send_json({"ok": True})
            return
        if path == "/api/control":
            self._send_json({"ok": True})
            return
        if path == "/api/edit_track":
            self._send_json({"ok": True})
            return
        if path == "/api/client_log":
            self._send_json({"ok": True})
            return
        if path == "/api/radio/favourites/add":
            if gw.radio_fav_full or len(gw.radio_favourites) >= 25:
                self._send_json({"error": "favourites_full", "limit": 25},
                                status=409)
                return
            uuid = payload.get("station_uuid", "")
            if uuid and not any(s.get("station_uuid") == uuid
                                for s in gw.radio_favourites):
                gw.radio_favourites.append(payload)
            self._send_json({"ok": True, "created": True})
            return
        if path == "/api/radio/favourites/remove":
            uuid = payload.get("station_uuid", "")
            before = len(gw.radio_favourites)
            gw.radio_favourites = [s for s in gw.radio_favourites
                                   if s.get("station_uuid") != uuid]
            self._send_json({"ok": before != len(gw.radio_favourites)})
            return
        if path == "/api/radio/favourites/reorder":
            self._send_json({"ok": True})
            return

        self.send_error(404)

    @staticmethod
    def _silent_wav(seconds: int = 1, rate: int = 8000) -> bytes:
        """Minimal WAV header + N seconds of silence (8-bit PCM, mono)."""
        n = rate * seconds
        # 8-bit PCM silence = 0x80 (midpoint for unsigned)
        data = b"\x80" * n
        riff_size = 36 + len(data)
        header = (
            b"RIFF" + riff_size.to_bytes(4, "little") + b"WAVE"
            + b"fmt " + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little")          # PCM
            + (1).to_bytes(2, "little")          # mono
            + rate.to_bytes(4, "little")
            + rate.to_bytes(4, "little")         # byte rate
            + (1).to_bytes(2, "little")          # block align
            + (8).to_bytes(2, "little")          # bits/sample
            + b"data" + len(data).to_bytes(4, "little")
        )
        return header + data


class StubServer:
    """Run a StubGateway in a background thread on an ephemeral port."""

    def __init__(self) -> None:
        self.gateway = StubGateway()
        # Bind a fresh handler class per server so multiple stubs don't share state
        handler_cls = type("_Handler", (_Handler,),
                           {"gateway": self.gateway})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


if __name__ == "__main__":
    # Manual smoke test: start a stub on a fixed port for ad-hoc browser tests
    s = StubServer()
    s.start()
    s.gateway.add_artist("ABBA", album_count=2, track_count=15)
    s.gateway.add_album("ABBA", "Arrival", track_count=10)
    print(f"Stub gateway running at {s.base_url}")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        s.stop()
