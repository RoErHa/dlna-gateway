#!/usr/bin/env python3
"""
dlna_radio_proxy.py — the internet-radio relay: opening an ICY upstream,
de-interleaving Icecast/Shoutcast metadata out of the audio, and parking
the current `StreamTitle` for /api/radio/nowplaying.

Split out of dlna_stream_proxy.py (2026-08-20), which had reached 446
lines. The seam is real rather than cosmetic: `proxy_stream` is a
BYTE-PERFECT Range pass-through for library audio, whereas this relay
must *rewrite* the byte stream — it strips the interleaved metadata
blocks, because a browser `<audio>` element cannot handle them. Keeping
the two in one file invited exactly the wrong kind of code sharing.

`proxy_stream` and the ICY helpers are re-exported from
dlna_stream_proxy for backwards compatibility, so existing imports of
either name keep working from either module.

Caveat worth remembering: ICY titles exist for MP3/AAC only. OGG/FLAC
streams carry metadata as in-band Vorbis comments, so `icy_now` stays
empty there and the UI falls back to the station name.
"""
from __future__ import annotations

import http.client
import logging
import re
import selectors
import ssl
import threading
import time
import urllib.parse

from dlna_config import close_quietly
from dlna_proxy_common import PROXY_IDLE_SEC, _MIME_MAP

log = logging.getLogger("dlna.proxy")


def open_radio_upstream(upstream_url: str):
    """Open an upstream GET for the /radio_stream relay with `Icy-MetaData: 1`
    and return `(conn, resp, metaint, ctype)`. `metaint` is the icy-metaint
    block size (0 when the server ignored the header — relay verbatim).
    Returns `(None, None, 0, "")` on connection error or a non-2xx upstream.

    Used by the 2.0 ASGI route (dlna_asgi.radio_stream) as a StreamingResponse
    source; the legacy `proxy_radio_stream` keeps its own inline open (it
    drives the stdlib handler's wfile via selectors — left untouched)."""
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
        conn.request("GET", path, headers={
            "User-Agent":   "DLNAGateway/1.0",
            "Icy-MetaData": "1",
            "Connection":   "close",
        })
        resp = conn.getresponse()
        if resp.status not in (200, 206):
            log.warning(f"open_radio_upstream {host}{path[:60]}: "
                        f"HTTP {resp.status}")
            conn.close()
            return None, None, 0, ""
        try:
            metaint = int(resp.getheader("icy-metaint") or 0)
        except (TypeError, ValueError):
            metaint = 0
        ctype = resp.getheader("Content-Type") or "audio/mpeg"
        return conn, resp, metaint, ctype
    except Exception as e:                           # noqa: BLE001
        log.warning(f"open_radio_upstream {host}{path[:60]}: "
                    f"{type(e).__name__}: {e}")
        if conn:
            close_quietly(conn)
        return None, None, 0, ""


def iter_radio_audio(resp, metaint: int, upstream_url: str):
    """Sync generator: yield clean audio chunks from an ICY radio response,
    parking each StreamTitle via `_icy_set(upstream_url, …)` for
    /api/radio/nowplaying. When `metaint <= 0` the server sent no metadata —
    relay verbatim. Mirrors the pure `_deinterleave_icy` loop, but YIELDS
    instead of calling a write callback so it can drive an ASGI
    StreamingResponse."""
    if metaint <= 0:
        while True:
            chunk = resp.read(65_536)
            if not chunk:
                return
            yield chunk
    while True:
        audio = _read_exact(resp, metaint)
        if audio:
            yield audio
        if len(audio) < metaint:
            return                               # upstream EOF
        lenbyte = resp.read(1)
        if not lenbyte:
            return
        mlen = lenbyte[0] * 16
        if mlen:
            title = _parse_icy_title(_read_exact(resp, mlen))
            if title is not None:
                _icy_set(upstream_url, title)



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
        except Exception as se:      # client already gone — nothing to send to
            log.debug(f"could not deliver 502 to client ({se})")
    finally:
        elapsed = time.monotonic() - t_start
        log.info(f"proxy_radio_stream ■ END   {host}{path[:60]} "
                 f"sent={sent_bytes} bytes in {elapsed:.1f}s "
                 f"reason={reason}")
        if conn:
            close_quietly(conn)
