#!/usr/bin/env python3
"""
dlna_stream_proxy.py — HTTP proxy for in-browser audio playback.

The PWA's `<audio>` element fetches from `/stream?url=<upstream>`. This
module relays upstream bytes to the browser, forwarding Range headers
for seek support and normalising MIME types Safari rejects
(audio/x-flac → audio/flac, etc.).

Separated from dlna_player so the renderer-queue logic and the
browser-audio HTTP proxy live in their own single-responsibility
modules — changes to one don't drag the other in.

A 5-minute client-idle timeout frees upstream resources when a browser
stops consuming bytes (laptop suspended, tab closed without a clean
FIN, network drop). `PROXY_IDLE_SEC` is module-level so tests can
monkey-patch a shorter window for chaos runs without waiting 5 min.
"""
import http.client
import logging
import selectors
import ssl
import time
import urllib.parse

log = logging.getLogger("dlna.player")


# Module-level so tests can monkey-patch.
PROXY_IDLE_SEC = 300  # 5 min — covers a closed-laptop / sleeping-browser gap


# Browser-compat MIME normalisation. Safari refuses audio/x-flac but
# accepts audio/flac; same story for x-m4a and a few others.
_MIME_MAP = {
    "audio/x-flac":   "audio/flac",
    "audio/x-m4a":    "audio/mp4",
    "audio/x-alac":   "audio/mp4",
    "audio/x-aiff":   "audio/aiff",
    "audio/x-wav":    "audio/wav",
    "audio/x-ms-wma": "audio/x-ms-wma",
}


def proxy_stream(upstream_url: str, handler):
    """
    HTTP Range-aware proxy: relay upstream bytes to the browser.
    Forwards the browser's Range header so <audio> seeks don't hit the
    media server directly (same-origin only — no mixed-content issues).
    """
    parsed  = urllib.parse.urlparse(upstream_url)
    host    = parsed.netloc
    path    = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    use_ssl = parsed.scheme == "https"

    sent_bytes = 0
    reason     = "unknown"
    t_start    = time.monotonic()
    range_hdr  = handler.headers.get("Range", "")
    log.info(f"proxy_stream ▶ START {host}{path[:80]}"
             f"{' range=' + range_hdr if range_hdr else ''}")

    conn = None
    try:
        if use_ssl:
            conn = http.client.HTTPSConnection(
                host, timeout=20,
                context=ssl._create_unverified_context())
        else:
            conn = http.client.HTTPConnection(host, timeout=20)

        req_headers = {"User-Agent": "DLNAGateway/1.0", "Connection": "close"}
        if range_hdr:
            req_headers["Range"] = range_hdr

        conn.request("GET", path, headers=req_headers)
        resp = conn.getresponse()

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

        # Use selectors (kqueue on macOS) instead of select.select() — the
        # latter has an FD_SETSIZE=1024 ceiling and raises ValueError once any
        # fd in the process exceeds 1023. Under load that turns every /stream
        # request into a 0-byte response and the browser <audio> element
        # surfaces it as MediaError.code=4 "unsupported format".
        sel = selectors.DefaultSelector()
        sel.register(handler.wfile, selectors.EVENT_WRITE)
        try:
            CHUNK = 65_536
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    reason = "upstream_eof"
                    break
                try:
                    if not sel.select(PROXY_IDLE_SEC):
                        reason = "client_idle_timeout"
                        break
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
                    sent_bytes += len(chunk)
                except (BrokenPipeError, OSError):
                    reason = "client_closed"
                    break
        finally:
            sel.close()

    except BrokenPipeError:
        reason = "client_closed"
    except Exception as e:
        reason = f"error:{type(e).__name__}"
        log.warning(f"proxy_stream error: {e}")
        try:
            handler.send_error(502, str(e))
        except Exception:
            pass
    finally:
        elapsed = time.monotonic() - t_start
        log.info(f"proxy_stream ■ END   {host}{path[:60]} "
                 f"sent={sent_bytes} bytes in {elapsed:.1f}s "
                 f"reason={reason}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
