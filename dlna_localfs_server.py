"""
dlna_localfs_server.py — the LocalFsProvider's in-process file server.

Phase 3 of the AssetUPnP migration. Serves the bytes that
LocalFsProvider's track_ids point at, with:

  * Range support (`Accept-Ranges: bytes`, `206 Partial Content`,
    `Content-Range`) — required by the Naim for seek + safe playback
    start.
  * DLNA headers (`contentFeatures.dlna.org`, `transferMode.dlna.org`)
    — required by some renderers, ignored by others; included
    defensively.
  * **Bit-perfect**: serves the original file bytes via `read()` in
    fixed-size chunks. No transcoding, no decompression. The
    `sha256` of served bytes equals the source file's `sha256` —
    that's the P3 done-when proof.
  * Own port, default 8200 (configurable via `LOCALFS_PORT`). Binds
    on `0.0.0.0` so the Naim can reach it on the LAN.

URL shape:

    GET /localfs/stream/<track_id>

`track_id` is the 16-char `sha1(rel_path)` prefix that
`LocalFsProvider` writes to `tracks.obj_id`. The handler resolves
it to a file_path via `library.db` and streams the file.

Threading:
    `ThreadingHTTPServer` handles each connection in its own thread.
    File IO is synchronous and per-request — that matches the Naim's
    single-stream behaviour and keeps the implementation small.
    A future high-concurrency case would warrant a connection cap
    + a small file-descriptor pool; not needed at P3.

Wiring:
    P3 ships only the server + a CLI driver (`tools/localfs_serve.py`).
    Auto-starting it from `dlna_gateway.main()` is P4 work, when
    LocalFs becomes a real playback path for the renderer.
"""
from __future__ import annotations

import http.server
import logging
import os
import sqlite3
import threading
from pathlib import Path
from urllib.parse import unquote

from dlna_localfs_http import (
    _FALLBACK_MIME,
    _dlna_headers_for_mime,
    _parse_range_header,
)
from dlna_providers.localfs import _extract_art_bytes

log = logging.getLogger("dlna.localfs.server")

_CHUNK = 64 * 1024
_STREAM_PREFIX = "/localfs/stream/"
_ART_PREFIX = "/localfs/art/"
_VIDEO_PREFIX = "/localfs/video/"
_POSTER_PREFIX = "/localfs/poster/"
# Cap embedded-art responses — covers are KB-to-low-MB; anything past
# this is almost certainly not a cover and we refuse rather than buffer.
_ART_MAX_BYTES = 12 * 1024 * 1024



class LocalFsHTTPHandler(http.server.BaseHTTPRequestHandler):
    """Per-connection handler. Class-level attributes for the shared
    `LibraryDB` path + the audio-root validator are set by
    `make_handler_class()` so each server instance has its own pair.

    Path layout:
        GET  /localfs/stream/<track_id>           audio bytes
        HEAD /localfs/stream/<track_id>           same headers, no body
    """

    library_db_path: str = ""
    allowed_roots:   tuple[str, ...] = ()
    # Override stdlib's slow default; we log through `dlna.localfs.server`.
    server_version = "DLNAGateway-LocalFs/1.0"

    # ── stdlib hooks ─────────────────────────────────────────────

    def log_message(self, fmt: str, *args) -> None:
        # Route stdlib's verbose `127.0.0.1 - - [...]` lines through
        # our structured logger at DEBUG so they don't drown
        # gateway.log at INFO.
        log.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path.startswith(_ART_PREFIX):
            self._serve_art(send_body=True)
        elif self.path.startswith(_POSTER_PREFIX):
            self._serve_poster(send_body=True)
        elif self.path.startswith(_VIDEO_PREFIX):
            self._serve(send_body=True, prefix=_VIDEO_PREFIX,
                        resolver=self._resolve_video)
        else:
            self._serve(send_body=True)

    def do_HEAD(self):
        if self.path.startswith(_ART_PREFIX):
            self._serve_art(send_body=False)
        elif self.path.startswith(_POSTER_PREFIX):
            self._serve_poster(send_body=False)
        elif self.path.startswith(_VIDEO_PREFIX):
            self._serve(send_body=False, prefix=_VIDEO_PREFIX,
                        resolver=self._resolve_video)
        else:
            self._serve(send_body=False)

    # ── Core serve logic ─────────────────────────────────────────

    def _serve(self, *, send_body: bool, prefix: str = _STREAM_PREFIX,
               resolver=None):
        if not self.path.startswith(prefix):
            self.send_error(404, "Not found")
            return
        track_id = unquote(self.path[len(prefix):]).split("?", 1)[0]
        if not track_id:
            self.send_error(404, "Missing id")
            return

        file_path, mime = (resolver or self._resolve)(track_id)
        if not file_path:
            self.send_error(404, f"Unknown track id: {track_id}")
            return

        # Path-traversal defence: if the resolved path is outside any
        # allowed root, refuse. (The DB column is normally trustworthy
        # — only LocalFsProvider writes to it — but defending here keeps
        # the server safe if something downstream gets compromised.)
        if self.allowed_roots:
            try:
                resolved = Path(file_path).resolve()
            except OSError as e:
                log.warning(f"resolve failed for {file_path}: {e}")
                self.send_error(404, "File not accessible")
                return
            if not any(str(resolved).startswith(r)
                       for r in self.allowed_roots):
                log.warning(f"path-traversal blocked: {file_path} "
                            f"not under {self.allowed_roots}")
                self.send_error(403, "Path not under any allowed root")
                return

        try:
            size = os.path.getsize(file_path)
        except OSError as e:
            log.warning(f"size lookup failed for {file_path}: {e}")
            self.send_error(404, "File not found on disk")
            return

        range_hdr = self.headers.get("Range", "")
        rng = _parse_range_header(range_hdr, size) if range_hdr else None
        if range_hdr and rng is None:
            # Malformed or unsatisfiable. Per RFC 7233, respond 416
            # with a `Content-Range: bytes */<size>` so the client
            # learns the actual length.
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if rng is None:
            start, end = 0, size - 1
            status   = 200
        else:
            start, end = rng
            status   = 206
        length = end - start + 1

        self.send_response(status)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Type", mime or _FALLBACK_MIME)
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range",
                             f"bytes {start}-{end}/{size}")
        for k, v in _dlna_headers_for_mime(mime).items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()

        if not send_body:
            return

        try:
            self._stream_file(file_path, start, length)
        except (BrokenPipeError, ConnectionResetError):
            # Renderer / curl client disconnected mid-stream. Normal
            # for short Range probes (the Naim issues a 0-1 byte probe
            # then disconnects). Log at DEBUG.
            log.debug(f"client closed connection mid-stream: {file_path}")
        except Exception as e:                                # noqa: BLE001
            log.exception(f"stream failed for {file_path}: {e}")

    # ── Embedded cover art ───────────────────────────────────────

    def _serve_art(self, *, send_body: bool):
        """GET/HEAD /localfs/art/<track_id> → the file's first embedded
        cover picture. 404 when the id is unknown, the file is gone, or
        there's no embedded art (the PWA's <img> onerror just hides the
        thumbnail, same as any missing cover)."""
        track_id = unquote(self.path[len(_ART_PREFIX):]).split("?", 1)[0]
        if not track_id:
            self.send_error(404, "Missing track id")
            return

        file_path, _ = self._resolve(track_id)
        if not file_path:
            self.send_error(404, f"Unknown track id: {track_id}")
            return

        # Same path-traversal defence as the audio route.
        if self.allowed_roots:
            try:
                resolved = Path(file_path).resolve()
            except OSError as e:
                log.warning(f"art resolve failed for {file_path}: {e}")
                self.send_error(404, "File not accessible")
                return
            if not any(str(resolved).startswith(r)
                       for r in self.allowed_roots):
                log.warning(f"art path-traversal blocked: {file_path}")
                self.send_error(403, "Path not under any allowed root")
                return

        got = _extract_art_bytes(Path(file_path))
        if not got:
            self.send_error(404, "No embedded art")
            return
        data, mime = got
        if len(data) > _ART_MAX_BYTES:
            self.send_error(502, "Embedded art too large")
            return

        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        # Embedded art never changes for a given file id — cache hard.
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        if send_body:
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                log.debug(f"client closed during art write: {file_path}")

    # ── Helpers ──────────────────────────────────────────────────

    def _resolve(self, track_id: str) -> tuple[str, str]:
        """track_id → (file_path, mime). Empty strings on miss.
        Trusts that LocalFsProvider already wrote `obj_id` =
        `sha1(rel_path)[:16]` and `file_path` = absolute path."""
        try:
            conn = sqlite3.connect(self.library_db_path)
            row = conn.execute(
                "SELECT file_path, mime FROM tracks "
                "WHERE obj_id=? AND udn LIKE 'uuid:localfs-%' LIMIT 1",
                (track_id,)).fetchone()
            conn.close()
        except sqlite3.Error as e:
            log.warning(f"DB lookup failed for {track_id}: {e}")
            return ("", "")
        if not row or not row[0]:
            return ("", "")
        return (row[0], row[1] or _FALLBACK_MIME)

    def _serve_poster(self, *, send_body: bool):
        """GET/HEAD /localfs/poster/<id> → the extracted poster JPEG from
        dlna_ffmpeg.POSTER_DIR. 404 when there's no poster for that id."""
        import dlna_ffmpeg
        vid = unquote(self.path[len(_POSTER_PREFIX):]).split("?", 1)[0]
        path = os.path.join(dlna_ffmpeg.POSTER_DIR, f"{os.path.basename(vid)}.jpg")
        if not vid or not os.path.isfile(path):
            self.send_error(404, "No poster")
            return
        try:
            data = open(path, "rb").read()
        except OSError:
            self.send_error(404, "No poster")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Connection", "close")
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _resolve_video(self, vid: str) -> tuple[str, str]:
        """video id → (file_path, mime) from the `videos` table. Empty on miss."""
        try:
            conn = sqlite3.connect(self.library_db_path)
            row = conn.execute(
                "SELECT file_path, mime FROM videos WHERE id=? LIMIT 1",
                (vid,)).fetchone()
            conn.close()
        except sqlite3.Error as e:
            log.warning(f"video DB lookup failed for {vid}: {e}")
            return ("", "")
        if not row or not row[0]:
            return ("", "")
        return (row[0], row[1] or _FALLBACK_MIME)

    def _stream_file(self, path: str, start: int, length: int):
        """Stream `length` bytes from `path`, starting at `start`."""
        remaining = length
        with open(path, "rb") as f:
            if start:
                f.seek(start)
            while remaining > 0:
                data = f.read(min(_CHUNK, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)


def make_handler_class(library_db_path: str,
                       allowed_roots: tuple[str, ...] = ()) -> type:
    """Subclass `LocalFsHTTPHandler` with class-level attributes set
    for THIS server instance — needed so multiple servers (different
    DB paths, different roots) can run in the same process without
    stepping on each other.

    `allowed_roots` are canonicalised via `Path.resolve()` so the
    startswith check works on macOS where `/var/folders/...`
    resolves to `/private/var/folders/...`."""
    canonical = tuple(str(Path(r).resolve()) for r in allowed_roots)
    return type(
        "BoundLocalFsHTTPHandler",
        (LocalFsHTTPHandler,),
        {"library_db_path": library_db_path,
         "allowed_roots":   canonical})


def start_server(library_db_path: str,
                 port: int = 8200,
                 *,
                 host: str = "0.0.0.0",
                 allowed_roots: tuple[str, ...] = ()
                 ) -> http.server.ThreadingHTTPServer:
    """Spawn the file server on its own daemon thread and return the
    server object (so a caller can later call `.shutdown()`).

    Default port is 8200 (configurable via `LOCALFS_PORT` in the CLI
    driver). Default host is `0.0.0.0` because the Naim has to reach
    the server on the LAN — `127.0.0.1` would silently work in tests
    but break real playback."""
    handler_cls = make_handler_class(library_db_path,
                                     allowed_roots=allowed_roots)
    srv = http.server.ThreadingHTTPServer((host, port), handler_cls)
    t = threading.Thread(target=srv.serve_forever,
                         daemon=True, name="localfs-http")
    t.start()
    log.info(f"LocalFs server listening on http://{host}:{port}/"
             f"localfs/stream/<id>")
    return srv


__all__ = [
    "LocalFsHTTPHandler",
    "make_handler_class",
    "start_server",
    "_parse_range_header",          # for tests
    "_dlna_headers_for_mime",        # for tests
]
