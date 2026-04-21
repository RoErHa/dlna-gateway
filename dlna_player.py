#!/usr/bin/env python3
"""
dlna_player.py — IINA / mpv launcher, JSON IPC control, stream proxy.

Standalone test:
    python dlna_player.py                     # show current state
    python dlna_player.py http://<url>        # play a URL in IINA
"""
import http.client
import json
import logging
import os
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from typing import Optional

from dlna_config import IPC_SOCK

log = logging.getLogger("dlna.player")

IINA_PATHS = [
    "/Applications/IINA.app/Contents/MacOS/iina-cli",
    "iina",
]

_DEBOUNCE_SEC = 1.5   # ignore duplicate play() calls within this window


# ── Launch helper ─────────────────────────────────────────────────

_last_launch_time: float = 0.0
_last_launch_uri:  str   = ""
_launch_lock = threading.Lock()


def launch_player(uri: str) -> Optional[subprocess.Popen]:
    """
    Launch IINA (preferred) or mpv with the IPC socket enabled.
    Debounces identical URIs within _DEBOUNCE_SEC (prevents double-click launches).
    Different URIs always launch immediately.
    """
    global _last_launch_time, _last_launch_uri

    with _launch_lock:
        now = time.monotonic()
        if (uri == _last_launch_uri and
                now - _last_launch_time < _DEBOUNCE_SEC):
            log.debug(f"launch_player: debounced duplicate ({_DEBOUNCE_SEC}s)")
            return None
        _last_launch_time = now
        _last_launch_uri  = uri

    # Remove stale IPC socket so mpv creates a fresh one
    try:
        os.unlink(IPC_SOCK)
    except FileNotFoundError:
        pass

    for iina in IINA_PATHS:
        try:
            p = subprocess.Popen(
                [iina,
                 "--keep-running",
                 f"--mpv-input-ipc-server={IPC_SOCK}",
                 "--mpv-loop-file=no",
                 "--mpv-loop-playlist=no",
                 uri],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            log.info(f"▶ IINA launched via {iina}  (IPC: {IPC_SOCK})")
            return p
        except FileNotFoundError:
            continue

    try:
        p = subprocess.Popen(
            ["mpv",
             f"--input-ipc-server={IPC_SOCK}",
             "--no-terminal",
             "--loop-file=no",
             "--loop-playlist=no",
             uri],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        log.info(f"▶ mpv launched  (IPC: {IPC_SOCK})")
        return p
    except FileNotFoundError:
        pass

    log.error("Neither iina-cli nor mpv found — please install IINA")
    return None


# ── PlayerState ───────────────────────────────────────────────────

class PlayerState:
    """
    Wraps IINA / mpv process management and JSON IPC communication.
    Thread-safe. Singleton: import PLAYER from this module.
    """

    def __init__(self):
        self.uri:   str = ""
        self.title: str = ""
        self.state: str = "stopped"
        self.proc:  Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # ── Playback ──────────────────────────────────────────────────

    def play(self, uri: str, title: str = ""):
        """
        Start playing a URI (direct URL or /tmp/*.m3u path).
        Reuses a running IINA/mpv via IPC loadfile; only launches fresh if none is running.
        """
        with self._lock:
            self.uri   = uri
            self.title = title
            self.state = "playing"

        log.info(f"play → {title!r}  ({uri[:80]}{'…' if len(uri)>80 else ''})")

        # Prefer IPC — avoids opening a second IINA window when one is already running.
        # iina-cli exits after queuing but IINA.app stays alive (--keep-running),
        # so self.proc.poll() can be non-None even though the IPC socket is still live.
        is_playlist = uri.endswith(".m3u") or uri.endswith(".m3u8")
        ipc_cmd = ({"command": ["loadlist", uri, "replace"]} if is_playlist
                   else {"command": ["loadfile", uri, "replace"]})
        r = self.ipc(ipc_cmd, retries=1)
        if r is not None:
            log.info("play: reused existing IINA/mpv via IPC")
            return

        # No running player — kill any stale CLI process and launch fresh.
        with self._lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=2)
                except Exception:
                    pass

        p = launch_player(uri)
        with self._lock:
            self.proc = p

    # ── IPC ───────────────────────────────────────────────────────

    def ipc(self, cmd: dict, retries: int = 8) -> Optional[dict]:
        """
        Send a JSON command to the mpv IPC socket.
        Retries while mpv is starting up (socket not yet ready).
        """
        for attempt in range(retries):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.settimeout(1.0)
                s.connect(IPC_SOCK)
                s.sendall((json.dumps(cmd) + "\n").encode())
                raw = b""
                while b"\n" not in raw:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    raw += chunk
                s.close()
                return json.loads(raw.decode().split("\n")[0])
            except FileNotFoundError:
                if attempt < retries - 1:
                    time.sleep(0.25)
            except Exception:
                return None
        return None

    def get_prop(self, name: str):
        r = self.ipc({"command": ["get_property", name]}, retries=1)
        return r.get("data") if r and r.get("error") == "success" else None

    # ── State snapshot ────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return a JSON-serialisable player state dict."""
        alive = bool(self.proc and self.proc.poll() is None)
        pos = dur = paused = volume = media_title = shuffle = artist = album = None
        if alive:
            pos         = self.get_prop("time-pos")
            dur         = self.get_prop("duration")
            paused      = self.get_prop("pause")
            volume      = self.get_prop("volume")
            media_title = self.get_prop("media-title")
            shuffle     = self.get_prop("shuffle")
            artist      = self.get_prop("metadata/by-key/Artist")
            album       = self.get_prop("metadata/by-key/Album")
            with self._lock:
                self.state = ("paused" if paused is True
                              else ("playing" if paused is False else self.state))
        elif self.state == "playing":
            with self._lock:
                self.state = "stopped"
        with self._lock:
            return {
                "state":       self.state,
                "uri":         self.uri,
                "title":       self.title,
                "position":    pos,
                "duration":    dur,
                "paused":      paused,
                "volume":      volume,
                "alive":       alive,
                "media_title": media_title,
                "shuffle":     shuffle,
                "artist":      artist,
                "album":       album,
            }


# ── RendererQueue — sequential playback for UPnP renderers ────────

class RendererQueue:
    """
    Manages sequential track playback on a UPnP AVTransport renderer.

    When a track finishes (state goes STOPPED after PLAYING) the next
    track in the queue is automatically sent via SetAVTransportURI+Play.

    Only one queue is active at a time; starting a new queue cancels the old one.
    Thread-safe. Singleton: import RENDERER_QUEUE from this module.
    """

    def __init__(self):
        self._lock    = threading.Lock()
        self._tracks: list  = []
        self._index:  int   = 0
        self._av_url: str   = ""
        self._rnd_name: str = ""
        self._stop_event    = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Public API ────────────────────────────────────────────────

    def start(self, av_url: str, tracks: list, renderer_name: str = ""):
        """
        Start playing a list of tracks on the renderer.
        Cancels any currently active queue.
        """
        self._cancel()
        with self._lock:
            self._av_url   = av_url
            self._tracks   = list(tracks)
            self._index    = 0
            self._rnd_name = renderer_name
            self._stop_event.clear()

        if not tracks:
            return

        # Send first track immediately
        self._send_current()

        # Start background monitor thread
        self._thread = threading.Thread(
            target=self._monitor, daemon=True, name="renderer-queue")
        self._thread.start()

    def stop(self):
        """Stop playback and cancel the queue."""
        from dlna_content import avtransport_stop
        self._cancel()
        with self._lock:
            url = self._av_url
        if url:
            avtransport_stop(url)

    def pause(self):
        """Toggle pause on the renderer."""
        from dlna_content import avtransport_pause
        with self._lock:
            url = self._av_url
        if url:
            avtransport_pause(url)

    def next_track(self):
        """Skip to the next track immediately."""
        with self._lock:
            if self._index < len(self._tracks) - 1:
                self._index += 1
            else:
                return   # already at end
        self._send_current()

    def prev_track(self):
        """Go back to the previous track (or restart current if > 3 s in)."""
        with self._lock:
            if self._index > 0:
                self._index -= 1
        self._send_current()

    def snapshot(self) -> dict:
        """Return current queue state for the UI state poll."""
        from dlna_content import avtransport_get_state, avtransport_get_position
        with self._lock:
            av_url  = self._av_url
            tracks  = list(self._tracks)
            idx     = self._index
            rname   = self._rnd_name

        if not av_url or not tracks:
            return {"state": "stopped", "alive": False,
                    "renderer": rname, "queue_len": 0, "queue_pos": 0}

        state = avtransport_get_state(av_url)
        pos   = avtransport_get_position(av_url)

        cur = tracks[idx] if 0 <= idx < len(tracks) else {}
        return {
            "state":       _av_state_to_ui(state),
            "alive":       state in ("PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"),
            "paused":      state == "PAUSED_PLAYBACK",
            "renderer":    rname,
            "title":       pos.get("title") or cur.get("title", ""),
            "artist":      cur.get("artist", ""),
            "album":       cur.get("album", ""),
            "art":         cur.get("art", ""),
            "position":    pos.get("position"),
            "duration":    pos.get("duration"),
            "queue_len":   len(tracks),
            "queue_pos":   idx + 1,
            "uri":         cur.get("url", ""),
            "media_title": pos.get("title") or cur.get("title", ""),
        }

    # ── Internal ──────────────────────────────────────────────────

    def _cancel(self):
        self._stop_event.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3)

    def _send_current(self):
        from dlna_content import avtransport_send
        with self._lock:
            if not self._tracks or not self._av_url:
                return
            idx    = self._index
            tracks = self._tracks
            av_url = self._av_url
        if 0 <= idx < len(tracks):
            t = tracks[idx]
            log.info(f"RendererQueue [{idx+1}/{len(tracks)}] "
                     f"{t.get('title','?')!r} → {self._rnd_name}")
            avtransport_send(av_url, t.get("url",""),
                             t.get("title",""), t.get("mime",""))

    def _monitor(self):
        """
        Poll GetTransportInfo every 2 s.
        When a track finishes (PLAYING → STOPPED) advance to the next one.
        """
        from dlna_content import avtransport_get_state
        POLL_SEC  = 2.0
        prev_state = "UNKNOWN"
        # Give the renderer time to start playing before monitoring
        self._stop_event.wait(4.0)

        while not self._stop_event.is_set():
            with self._lock:
                av_url = self._av_url
                idx    = self._index
                total  = len(self._tracks)

            if not av_url:
                break

            cur_state = avtransport_get_state(av_url)
            log.debug(f"RendererQueue monitor: state={cur_state} "
                      f"prev={prev_state} [{idx+1}/{total}]")

            # Track ended: was playing, now stopped/no media
            if (prev_state in ("PLAYING", "TRANSITIONING") and
                    cur_state in ("STOPPED", "NO_MEDIA_PRESENT")):
                with self._lock:
                    more = self._index < len(self._tracks) - 1
                    if more:
                        self._index += 1

                if more:
                    log.info("RendererQueue: track ended, advancing…")
                    self._send_current()
                    # Give renderer time to start before next poll
                    self._stop_event.wait(4.0)
                else:
                    log.info("RendererQueue: playlist finished")
                    self._stop_event.set()
                    break

            prev_state = cur_state
            self._stop_event.wait(POLL_SEC)


def _av_state_to_ui(state: str) -> str:
    return {
        "PLAYING":          "playing",
        "PAUSED_PLAYBACK":  "paused",
        "STOPPED":          "stopped",
        "NO_MEDIA_PRESENT": "stopped",
        "TRANSITIONING":    "playing",
    }.get(state, "stopped")


# ── Singleton ─────────────────────────────────────────────────────

PLAYER         = PlayerState()
RENDERER_QUEUE = RendererQueue()



# ── Stream proxy ──────────────────────────────────────────────────

def proxy_stream(upstream_url: str, handler):
    """
    HTTP Range-aware proxy: relay AssetUPnP bytes to IINA.
    Forwards Range header so IINA seek works without hitting AssetUPnP directly.
    `handler` is a BaseHTTPRequestHandler instance.
    """
    parsed  = urllib.parse.urlparse(upstream_url)
    host    = parsed.netloc
    path    = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    use_ssl = parsed.scheme == "https"

    conn = None
    try:
        if use_ssl:
            conn = http.client.HTTPSConnection(
                host, timeout=20,
                context=ssl._create_unverified_context())
        else:
            conn = http.client.HTTPConnection(host, timeout=20)

        req_headers = {"User-Agent": "DLNAGateway/1.0", "Connection": "close"}
        range_hdr   = handler.headers.get("Range", "")
        if range_hdr:
            req_headers["Range"] = range_hdr

        conn.request("GET", path, headers=req_headers)
        resp = conn.getresponse()

        # Normalise MIME types for browser compatibility.
        # Safari requires exact types — audio/x-flac won't play, audio/flac will.
        _MIME_MAP = {
            "audio/x-flac":   "audio/flac",
            "audio/x-m4a":    "audio/mp4",
            "audio/x-alac":   "audio/mp4",
            "audio/x-aiff":   "audio/aiff",
            "audio/x-wav":    "audio/wav",
            "audio/x-ms-wma": "audio/x-ms-wma",  # keep — no better option
        }
        handler.send_response(resp.status)
        for h in ("Content-Type", "Content-Length", "Content-Range",
                  "Accept-Ranges", "Last-Modified", "ETag"):
            v = resp.getheader(h)
            if v:
                if h == "Content-Type":
                    # Strip codec parameters before mapping, then re-add
                    base = v.split(";")[0].strip().lower()
                    v = _MIME_MAP.get(base, base)
                handler.send_header(h, v)
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Connection", "close")
        handler.end_headers()

        # Stream with idle timeout — if client stops consuming for IDLE_SEC
        # seconds, close the connection. Prevents iOS background tabs from
        # holding the stream open indefinitely (and draining iPhone battery).
        CHUNK    = 65_536   # smaller chunks → more responsive idle detection
        IDLE_SEC = 30       # close if no data sent for this long
        import select
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            # Check if client socket is still writable before sending
            try:
                rdy = select.select([], [handler.wfile], [], IDLE_SEC)[1]
                if not rdy:
                    log.debug("proxy_stream: client idle timeout — closing")
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except (BrokenPipeError, OSError):
                break

    except BrokenPipeError:
        pass   # client closed connection — normal (seek, skip, close tab)
    except Exception as e:
        log.debug(f"proxy_stream: {e}")
        try:
            handler.send_error(502, str(e))
        except Exception:
            pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Standalone test ───────────────────────────────────────────────

def _test():
    import sys
    from dlna_config import setup_logging
    setup_logging(debug=True)
    log.info("=== dlna_player self-test ===")

    snap = PLAYER.snapshot()
    log.info(f"Current state : {snap['state']}")
    log.info(f"IPC socket    : {IPC_SOCK}  exists={os.path.exists(IPC_SOCK)}")
    log.info(f"IINA alive    : {snap['alive']}")
    if snap["alive"]:
        log.info(f"  Playing     : {snap.get('media_title') or snap['uri']}")
        log.info(f"  Position    : {snap.get('position')} / {snap.get('duration')}")

    if len(sys.argv) > 1:
        url = sys.argv[1]
        log.info(f"Playing test URL: {url}")
        PLAYER.play(url, title="Test track")
        time.sleep(3)
        snap2 = PLAYER.snapshot()
        log.info(f"State after play: {snap2['state']}, alive={snap2['alive']}")
        if snap2["alive"]:
            log.info("PASS — IINA/mpv launched successfully")
        else:
            log.error("FAIL — player did not start")
    else:
        log.info("(Pass a URL as argument to test playback)")
        log.info("PASS — dlna_player OK")


if __name__ == "__main__":
    _test()
