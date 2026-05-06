#!/usr/bin/env python3
"""
api_playback.py — Playback, state, and stream proxy API handlers.

Handles: /api/renderer_state, /api/index/status, /api/index/rebuild,
         /api/render_queue, /api/render, /api/control,
         /api/edit_track, /stream, /art, /api/client_log
"""
import http.client
import json
import logging
import ssl
import threading
import urllib.parse

from dlna_content import avtransport_send
from dlna_discovery import RENDERERS, SERVERS
from dlna_library import DB, INDEXER
from dlna_player import QUEUES, proxy_stream

log = logging.getLogger("dlna.api.playback")


def _parse_json_or_400(h, body):
    """Parse a JSON request body into a dict. On failure (malformed JSON
    OR top-level non-object like '[]' / '"string"' / '42'), send 400
    and return None so the caller can bail.

    Malformed input is a client error, not a server error — returning
    500 would be wrong and trip the chaos suite's 5xx gate. The dict
    check is important: json.loads('[]') succeeds but then data.get()
    raises AttributeError, which is what surfaced in the chaos run."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as e:
        h._json(400, {"error": f"invalid JSON: {e}"})
        return None
    if not isinstance(data, dict):
        h._json(400, {"error": f"expected JSON object, got {type(data).__name__}"})
        return None
    return data


# ── GET handlers ──────────────────────────────────────────────────

def renderer_state(h, params):
    udn = params.get("udn", "")
    if udn:
        h._json(200, QUEUES.get(udn).snapshot())
        return
    # No UDN → "what's playing anywhere". Return every queue's snapshot,
    # plus a flat legacy view of the first alive one so old UI tabs that
    # haven't been updated still render something sensible.
    all_snaps = QUEUES.snapshot_all()
    legacy = {"state": "stopped", "alive": False, "renderer": "",
              "queue_len": 0, "queue_pos": 0}
    for snap in all_snaps.values():
        if snap.get("alive"):
            legacy = snap
            break
    h._json(200, {**legacy, "queues": all_snaps})


def index_status(h, params):
    udn   = params.get("udn", "")
    count = DB.track_count(udn) if udn else 0
    h._json(200, {**INDEXER.state.get(), "db_tracks": count})


def loudness_status(h, params):
    """Per-track loudness scanner progress. Frontend reads this for the
    progress UI (out of scope for Phase 1) and the suite asserts the
    contract shape. `total` includes both analysed AND sticky-negative
    rows, since both count as "the scanner has done its job."""
    from dlna_library import LOUDNESS_SCANNER
    from dlna_loudness import TARGET_PEAK_DBTP
    with DB._pool.read() as conn:
        scanned = conn.execute(
            "SELECT COUNT(*) AS n FROM track_loudness").fetchone()["n"]
        bare = conn.execute(
            "SELECT COUNT(*) AS n FROM tracks t "
            "WHERE t.url != '' "
            "  AND NOT EXISTS (SELECT 1 FROM track_loudness l "
            "                   WHERE l.url = t.url)").fetchone()["n"]
    in_progress = bool(LOUDNESS_SCANNER._thread
                       and LOUDNESS_SCANNER._thread.is_alive())
    h._json(200, {
        "scanned":          int(scanned),
        "total":            int(scanned + bare),
        "in_progress":      in_progress,
        "target_peak_dbtp": float(TARGET_PEAK_DBTP),
    })


def index_rebuild(h, params):
    udn = params.get("udn", "")
    srv = SERVERS.get(udn)
    if not srv:
        h._json(404, {"error": "Server not found"})
        return
    INDEXER.start(srv, force=True)
    h._json(200, {"ok": True, "message": "Reindex started"})


def stream(h, params):
    url = params.get("url", "")
    if not url:
        h.send_error(400, "Missing url")
        return
    proxy_stream(url, h)


# Cap art payload at 5MB — real album art is <1MB; this just prevents a
# malicious/broken upstream from making the gateway allocate arbitrary memory.
_ART_MAX_BYTES = 5 * 1024 * 1024
_ART_TIMEOUT   = 10


def art(h, params):
    """Proxy an arbitrary image URL through the gateway.

    Purpose: iOS MediaSession will NOT load cross-origin artwork on the
    lock screen. The PWA rewrites track art URLs to `/art?url=<external>`
    so the lock-screen fetch is same-origin as the app. Same story for
    the Service Worker's art cache — same-origin URLs are cacheable
    without CORS headaches.

    No Range support (art is small, one-shot). Short timeout (10s) —
    slow upstream just fails fast.
    """
    upstream = params.get("url", "")
    if not upstream:
        h.send_error(400, "Missing url")
        return

    try:
        parsed = urllib.parse.urlparse(upstream)
    except Exception:
        h.send_error(400, "Bad url")
        return
    if parsed.scheme not in ("http", "https"):
        h.send_error(400, "Bad scheme")
        return
    host = parsed.netloc
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    conn = None
    try:
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                host, timeout=_ART_TIMEOUT,
                context=ssl._create_unverified_context())
        else:
            conn = http.client.HTTPConnection(host, timeout=_ART_TIMEOUT)

        conn.request("GET", path, headers={"User-Agent": "DLNAGateway/1.0"})
        resp = conn.getresponse()
        if resp.status != 200:
            h.send_error(resp.status, f"Upstream {resp.status}")
            return

        body = resp.read(_ART_MAX_BYTES + 1)
        if len(body) > _ART_MAX_BYTES:
            h.send_error(502, "Image too large")
            return

        ctype = resp.getheader("Content-Type") or "image/jpeg"
        # Refuse non-image responses — a 200 with HTML body (upstream's
        # prettier 404 page) would otherwise be served as-is and confuse
        # the browser/SW cache.
        if not ctype.lower().startswith("image/"):
            h.send_error(502, f"Not an image: {ctype}")
            return

        h.send_response(200)
        h.send_header("Content-Type",  ctype)
        h.send_header("Content-Length", str(len(body)))
        # Art rarely changes — let the SW + browser cache it aggressively.
        h.send_header("Cache-Control",  "public, max-age=86400")
        h.send_header("Access-Control-Allow-Origin", "*")
        h.end_headers()
        h.wfile.write(body)
    except Exception as e:
        log.debug(f"art proxy: {upstream[:80]}  {type(e).__name__}: {e}")
        try:
            h.send_error(502, str(e))
        except Exception:
            pass
    finally:
        if conn:
            try: conn.close()
            except Exception: pass


def lyrics(h, params):
    """GET /api/lyrics?url=<track-url>

    Cache-first: returns from the `lyrics` table if any row exists
    (success OR sticky-notfound). Cache miss → query lrclib once, cache
    the outcome, return. Network is hit at most once per track URL.

    Response shape:
      { plain: str|null, synced: str|null, source: str, cached: bool }
        source ∈ {'lrclib', 'notfound', 'manual'}
    """
    from dlna_player import _dur_to_sec
    import dlna_lyrics

    url = params.get("url", "")
    if not url:
        h._json(400, {"error": "missing url"})
        return

    cached = DB.get_lyrics(url)
    if cached is not None:
        h._json(200, {
            "plain":  cached["plain"],
            "synced": cached["synced"],
            "source": cached["source"],
            "cached": True,
        })
        return

    meta = DB.track_meta_by_url(url)
    if not meta or not (meta.get("title") and meta.get("artist")):
        h._json(404, {"error": "track not in library", "source": "notfound"})
        return

    duration_sec = _dur_to_sec(meta.get("duration") or 0)
    try:
        result = dlna_lyrics.fetch_lrclib(
            meta["title"], meta["artist"],
            meta.get("album") or "", duration_sec)
    except dlna_lyrics.LrclibNotFound:
        DB.set_lyrics(url, None, None, "notfound")
        h._json(200, {"plain": None, "synced": None,
                      "source": "notfound", "cached": False})
        return

    if not result:
        # Network error — DON'T cache, so the next tap retries.
        h._json(502, {"error": "lyrics provider unreachable",
                      "source": "error"})
        return

    DB.set_lyrics(url, result.get("plain"), result.get("synced"), "lrclib")
    h._json(200, {
        "plain":  result.get("plain"),
        "synced": result.get("synced"),
        "source": "lrclib",
        "cached": False,
    })


# ── POST handlers ─────────────────────────────────────────────────

def render_queue(h, body):
    try:
        data = _parse_json_or_400(h, body)
        if data is None:
            return
        udn    = data.get("udn", "")
        tracks = data.get("tracks", [])
        force  = bool(data.get("force", False))
        rnd    = RENDERERS.get(udn)
        if not rnd:
            h._json(404, {"error": f"Renderer {udn!r} not found"})
            return
        if not tracks:
            h._json(400, {"error": "No tracks"})
            return

        # Busy check: one physical output can only play one thing. If the
        # renderer is already active for another session, refuse with 409
        # unless the client explicitly opted to take over via force=true.
        if not force and QUEUES.is_busy(udn):
            busy = QUEUES.get(udn).snapshot()
            log.info(f"render_queue 409 busy {rnd.name}: already playing "
                     f"{busy.get('title','?')!r}")
            h._json(409, {
                "error":     "renderer_busy",
                "message":   f"{rnd.name} is already playing. "
                             f"Pass force=true to take over.",
                "busy_with": {
                    "title":    busy.get("title", ""),
                    "artist":   busy.get("artist", ""),
                    "renderer": busy.get("renderer", rnd.name),
                },
            })
            return

        log.info(f"POST /api/render_queue  {len(tracks)} tracks → {rnd.name}"
                 f"{'  (force=True, taking over)' if force else ''}")
        queue = QUEUES.get(udn)

        def _start_safe(q=queue, av_url=rnd.av_url, rc_url=rnd.rc_url,
                        tracks=tracks, name=rnd.name):
            try:
                q.start(av_url, tracks, name, rc_url=rc_url)
            except Exception:
                log.exception(
                    f"RendererQueue.start crashed for {name} — "
                    f"queue of {len(tracks)} track(s) aborted")

        threading.Thread(target=_start_safe, daemon=True).start()
        h._json(200, {"ok": True, "tracks": len(tracks)})
    except Exception as e:
        log.exception(f"render_queue error: {e}")
        h._json(500, {"error": str(e)})


def render(h, body):
    try:
        data = _parse_json_or_400(h, body)
        if data is None:
            return
        udn   = data.get("udn", "")
        url   = data.get("url", "")
        title = data.get("title", "")
        mime  = data.get("mime", "")
        rnd   = RENDERERS.get(udn)
        if not rnd:
            h._json(404, {"error": f"Renderer {udn!r} not found"})
            return
        ok = avtransport_send(rnd.av_url, url, title, mime)
        log.info(f"POST /api/render  {title!r} → {rnd.name}  ok={ok}")
        h._json(200 if ok else 502, {"ok": ok})
    except Exception as e:
        log.exception(f"render error: {e}")
        h._json(500, {"error": str(e)})


def control(h, body):
    try:
        cmd = _parse_json_or_400(h, body)
        if cmd is None:
            return
        action = cmd.get("action", "")
        device = cmd.get("device", "")

        if device.startswith("upnp:"):
            udn = device.replace("upnp:", "")
            rnd = RENDERERS.get(udn)
            if not rnd:
                h._json(404, {"error": "Renderer not found"})
                return
            queue = QUEUES.get(udn)
            if action == "pause":
                queue.pause()
            elif action == "stop":
                queue.stop()
            elif action == "next":
                queue.next_track()
            elif action == "prev":
                queue.prev_track()
            elif action == "trim_db":
                # User moved the gateway volume slider while OUT was UPnP.
                # The slider is a relative trim around the renderer's
                # natural volume (NOT an absolute SetVolume) — default 0
                # so a tap can't accidentally blast the room. Clamped
                # ±5 dB inside RendererQueue.set_user_trim_db.
                try:
                    trim = float(cmd.get("value", 0))
                except (TypeError, ValueError):
                    h._json(400, {"error": "value must be a float (dB)"})
                    return
                queue.set_user_trim_db(trim)
            else:
                log.debug(f"Renderer control: {action!r} not implemented")
            h._json(200, {"ok": True})
        else:
            h._json(400, {"error": f"Unknown device: {device!r}"})

    except Exception as e:
        log.warning(f"control error: {e}")
        h._json(400, {"error": str(e)})


def edit_track(h, body):
    try:
        data = _parse_json_or_400(h, body)
        if data is None:
            return
        url    = data.get("url", "")
        artist = data.get("artist")
        album  = data.get("album")
        title  = data.get("title")
        genre  = data.get("genre")
        if not url:
            h._json(400, {"error": "Missing url"})
            return
        ok = DB.update_track_meta(url, artist=artist, album=album,
                                  title=title, genre=genre)
        log.info(f"edit_track: {url[:60]}  "
                 f"fields={[k for k, v in [('artist', artist), ('album', album), ('title', title), ('genre', genre)] if v is not None]}")
        h._json(200, {"ok": ok})
    except Exception as e:
        log.exception(f"edit_track error: {e}")
        h._json(500, {"error": str(e)})


# Dedicated logger for browser-side events so they're easy to grep
# ("grep dlna.client gateway.log") and don't drown in the API chatter.
_client_log = logging.getLogger("dlna.client")


def client_log(h, body):
    """Accept a structured error report from the PWA and write it to
    gateway.log. This is what gives us OBSERVABILITY of browser-side
    events that happen on a phone three rooms away — autoplay blocked,
    MediaError codes, retry outcomes.

    Body shape is free-form {kind: str, ...}. The handler clamps sizes
    defensively so a broken or malicious client can't flood the log."""
    try:
        data = _parse_json_or_400(h, body)
        if data is None:
            return
        kind = str(data.get("kind", "unknown"))[:40]
        # Pull out and clamp common fields that we care about; stringify
        # the rest into a single compact tail.
        fields = []
        for key in ("code", "codeName", "err", "reason", "retries",
                    "title", "artist", "ready_state", "network_state"):
            if key in data:
                v = str(data[key])[:120]
                fields.append(f"{key}={v}")
        ua = str(data.get("ua") or data.get("user_agent", ""))[:80]
        msg = str(data.get("message", ""))[:200]
        tail = "  ".join(fields)
        _client_log.info(
            f"client_log[{kind}]  {tail}"
            f"{'  msg=' + msg if msg else ''}"
            f"{'  ua=' + ua if ua else ''}")
        h._json(200, {"ok": True})
    except Exception as e:
        log.exception(f"client_log error: {e}")
        h._json(500, {"error": str(e)})
