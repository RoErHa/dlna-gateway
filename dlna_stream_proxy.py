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
import re
import selectors
import ssl
import threading
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


def normalize_audio_ctype(ctype: str) -> str:
    """Map Safari-rejected MIME types (audio/x-flac → audio/flac, …). Pure."""
    if not ctype:
        return "application/octet-stream"
    base = ctype.split(";")[0].strip().lower()
    return _MIME_MAP.get(base, base)


def open_stream_upstream(upstream_url: str, range_hdr: str = ""):
    """Open an upstream GET for the /stream relay, forwarding the browser's
    Range header, and return `(conn, resp)`. The caller streams `resp` in
    chunks and closes `conn`. Returns `(None, None)` on connection error.

    Used by the 2.0 ASGI route (dlna_asgi.stream) as a StreamingResponse
    source; the legacy `proxy_stream` keeps its own inline open (it's the
    chaos-tested selectors relay — left untouched)."""
    parsed  = urllib.parse.urlparse(upstream_url)
    host    = parsed.netloc
    path    = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    use_ssl = parsed.scheme == "https"
    conn = None
    try:
        if use_ssl:
            conn = http.client.HTTPSConnection(
                host, timeout=20, context=ssl._create_unverified_context())
        else:
            conn = http.client.HTTPConnection(host, timeout=20)
        req_headers = {"User-Agent": "DLNAGateway/1.0", "Connection": "close"}
        if range_hdr:
            req_headers["Range"] = range_hdr
        conn.request("GET", path, headers=req_headers)
        return conn, conn.getresponse()
    except Exception as e:                           # noqa: BLE001
        log.warning(f"open_stream_upstream {host}{path}: "
                    f"{type(e).__name__}: {e}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return None, None


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

    sent_bytes      = 0
    reason          = "unknown"
    upstream_status = 0
    t_start         = time.monotonic()
    range_hdr       = handler.headers.get("Range", "")
    log.info(f"proxy_stream ▶ START {host}{path}"
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
        upstream_status = resp.status

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
        log.info(f"proxy_stream ■ END   {host}{path} "
                 f"upstream={upstream_status} sent={sent_bytes} bytes "
                 f"in {elapsed:.1f}s reason={reason}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ── Internet-radio ICY metadata ───────────────────────────────────
# Icecast/Shoutcast streams interleave "now playing" text into the
# audio when the client sends `Icy-MetaData: 1`. proxy_radio_stream()
# de-interleaves it: the browser <audio> element gets clean audio (it
# cannot handle interleaved metadata), and the StreamTitle is parked
# in _ICY_NOW for /api/radio/nowplaying to read.

_ICY_NOW: dict = {}        # upstream_url → {"title": str, "updated": ts}
_ICY_LOCK      = threading.Lock()
_ICY_MAX       = 64        # bound the dict so a churn of URLs can't grow it

_ICY_TITLE_RE = re.compile(rb"StreamTitle='(.*?)';", re.DOTALL)


def icy_now(upstream_url: str):
    """Return {'title','updated'} for a radio stream the proxy has
    seen, or None. Read by api_radio.nowplaying."""
    with _ICY_LOCK:
        v = _ICY_NOW.get(upstream_url)
        return dict(v) if v else None


def _icy_set(upstream_url: str, title: str):
    with _ICY_LOCK:
        if upstream_url not in _ICY_NOW and len(_ICY_NOW) >= _ICY_MAX:
            # Evict the stalest entry — keeps memory bounded.
            oldest = min(_ICY_NOW, key=lambda k: _ICY_NOW[k]["updated"])
            _ICY_NOW.pop(oldest, None)
        _ICY_NOW[upstream_url] = {"title": title, "updated": time.time()}


def _read_exact(resp, n: int) -> bytes:
    """Read exactly n bytes from an HTTPResponse, or fewer at EOF.
    HTTPResponse.read(n) may return short mid-stream, so loop."""
    buf = bytearray()
    while len(buf) < n:
        chunk = resp.read(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _parse_icy_title(meta: bytes):
    """Extract StreamTitle from an ICY metadata block. Returns the
    title string (may be empty), or None if the block has no
    StreamTitle field at all."""
    m = _ICY_TITLE_RE.search(meta or b"")
    if not m:
        return None
    return (m.group(1).split(b"\x00", 1)[0]
            .decode("utf-8", "replace").strip())


def _deinterleave_icy(resp, metaint: int, write, on_title) -> str:
    """Pure ICY de-interleave loop.

    Reads `[metaint audio bytes][1 length byte][metadata]` repeating
    from `resp`. Calls ``write(audio_bytes)`` for every audio block and
    ``on_title(str)`` for every StreamTitle seen. ``write`` returns
    False to abort (client gone / idle).

    Returns ``'upstream_eof'`` when the stream ends, or ``''`` when
    ``write`` asked to abort (the caller keeps its own reason then).
    """
    while True:
        audio = _read_exact(resp, metaint)
        if audio and not write(audio):
            return ""
        if len(audio) < metaint:
            return "upstream_eof"
        lenbyte = resp.read(1)
        if not lenbyte:
            return "upstream_eof"
        mlen = lenbyte[0] * 16
        if mlen:
            title = _parse_icy_title(_read_exact(resp, mlen))
            if title is not None:
                on_title(title)


def proxy_radio_stream(upstream_url: str, handler):
    """Relay an internet-radio stream to the browser, de-interleaving
    ICY metadata.

    Unlike proxy_stream() this serves an endless live stream: no Range,
    no Content-Length. We request `Icy-MetaData: 1`; if the server
    honours it (returns `icy-metaint`) the metadata blocks are stripped
    and StreamTitle is parked via _icy_set(). A server that ignores the
    header is relayed verbatim (no metadata, still plays).
    """
    parsed  = urllib.parse.urlparse(upstream_url)
    host    = parsed.netloc
    path    = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    use_ssl = parsed.scheme == "https"

    sent_bytes = 0
    reason     = "unknown"
    t_start    = time.monotonic()
    log.info(f"proxy_radio_stream ▶ START {host}{path[:80]}")

    conn = None
    try:
        if use_ssl:
            conn = http.client.HTTPSConnection(
                host, timeout=20,
                context=ssl._create_unverified_context())
        else:
            conn = http.client.HTTPConnection(host, timeout=20)

        conn.request("GET", path, headers={
            "User-Agent":   "DLNAGateway/1.0",
            "Icy-MetaData": "1",
            "Connection":   "close",
        })
        resp = conn.getresponse()
        if resp.status not in (200, 206):
            reason = f"upstream_http_{resp.status}"
            handler.send_error(502, f"radio upstream HTTP {resp.status}")
            return

        try:
            metaint = int(resp.getheader("icy-metaint") or 0)
        except (TypeError, ValueError):
            metaint = 0

        ctype = (resp.getheader("Content-Type") or "audio/mpeg")
        base  = ctype.split(";")[0].strip().lower()
        handler.send_response(200)
        handler.send_header("Content-Type", _MIME_MAP.get(base, base))
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()

        sel = selectors.DefaultSelector()
        sel.register(handler.wfile, selectors.EVENT_WRITE)

        def _write(buf) -> bool:
            """Write audio to the browser with the idle-timeout guard.
            False on client-gone / idle timeout."""
            nonlocal sent_bytes, reason
            if not sel.select(PROXY_IDLE_SEC):
                reason = "client_idle_timeout"
                return False
            try:
                handler.wfile.write(buf)
                handler.wfile.flush()
            except (BrokenPipeError, OSError):
                reason = "client_closed"
                return False
            sent_bytes += len(buf)
            return True

        try:
            if metaint <= 0:
                # Server ignored Icy-MetaData — plain relay, no metadata.
                log.info(f"proxy_radio_stream: no icy-metaint, "
                         f"plain relay {host}")
                while True:
                    chunk = resp.read(65_536)
                    if not chunk:
                        reason = "upstream_eof"
                        break
                    if not _write(chunk):
                        break
            else:
                r = _deinterleave_icy(
                    resp, metaint, _write,
                    lambda t: _icy_set(upstream_url, t))
                reason = r or reason
        finally:
            sel.close()

    except BrokenPipeError:
        reason = "client_closed"
    except Exception as e:
        reason = f"error:{type(e).__name__}"
        log.warning(f"proxy_radio_stream error: {e}")
        try:
            handler.send_error(502, str(e))
        except Exception:
            pass
    finally:
        elapsed = time.monotonic() - t_start
        log.info(f"proxy_radio_stream ■ END   {host}{path[:60]} "
                 f"sent={sent_bytes} bytes in {elapsed:.1f}s "
                 f"reason={reason}")
        if conn:
            try:
                conn.close()
            except Exception:
                pass
