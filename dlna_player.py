#!/usr/bin/env python3
"""
dlna_player.py — UPnP renderer queue and browser stream proxy.

Standalone test:
    python dlna_player.py
"""
import http.client
import json
import logging
import select
import ssl
import threading
import time
import urllib.parse
from typing import Optional

log = logging.getLogger("dlna.player")


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

        self._send_current()

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
                return
        self._send_current()

    def prev_track(self):
        """Go back to the previous track."""
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
        POLL_SEC   = 2.0
        prev_state = "UNKNOWN"
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

            if (prev_state in ("PLAYING", "TRANSITIONING") and
                    cur_state in ("STOPPED", "NO_MEDIA_PRESENT")):
                with self._lock:
                    more = self._index < len(self._tracks) - 1
                    if more:
                        self._index += 1

                if more:
                    log.info("RendererQueue: track ended, advancing…")
                    self._send_current()
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

RENDERER_QUEUE = RendererQueue()


# ── Stream proxy ──────────────────────────────────────────────────

def proxy_stream(upstream_url: str, handler):
    """
    HTTP Range-aware proxy: relay AssetUPnP bytes to the browser.
    Forwards Range header so browser seek works without hitting AssetUPnP directly.
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
            "audio/x-ms-wma": "audio/x-ms-wma",
        }
        handler.send_response(resp.status)
        for h in ("Content-Type", "Content-Length", "Content-Range",
                  "Accept-Ranges", "Last-Modified", "ETag"):
            v = resp.getheader(h)
            if v:
                if h == "Content-Type":
                    base = v.split(";")[0].strip().lower()
                    v = _MIME_MAP.get(base, base)
                handler.send_header(h, v)
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Connection", "close")
        handler.end_headers()

        CHUNK    = 65_536
        IDLE_SEC = 30
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
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
        pass
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
    from dlna_config import setup_logging
    setup_logging(debug=True)
    log.info("=== dlna_player self-test ===")
    snap = RENDERER_QUEUE.snapshot()
    log.info(f"RendererQueue state: {snap['state']}")
    log.info("PASS — dlna_player OK")


if __name__ == "__main__":
    _test()
