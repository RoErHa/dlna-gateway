#!/usr/bin/env python3
"""
api_playback.py — Playback, state, and stream proxy API handlers.

Handles: /api/renderer_state, /api/index/status, /api/index/rebuild,
         /api/render_queue, /api/render, /api/control,
         /api/edit_track, /stream, /art, /api/client_log
"""
import http.client
import io
import json
import logging
import os
import ssl
import threading
import urllib.parse

import dlna_art_cache
from dlna_avtransport import avtransport_send

# Pillow is an OPTIONAL dependency (same pattern as rich/dotenv): when present,
# Subsonic `getCoverArt?size=N` is honoured by downscaling the cover to the
# requested box before serving. Without it, the original full-resolution image
# is served (the pre-scaling behaviour) — no hard failure.
try:
    from PIL import Image as _PILImage
    # Resampling enum is the canonical home since Pillow 9.1; the bare
    # Image.LANCZOS alias is deprecated and may be removed in a future major.
    _PIL_LANCZOS = getattr(getattr(_PILImage, "Resampling", _PILImage),
                           "LANCZOS", None)
except ImportError:                                      # noqa: BLE001
    _PILImage = None
    _PIL_LANCZOS = None
from dlna_config import VERSION
from dlna_discovery import RENDERERS, SERVERS
from dlna_library import DB, INDEXER
from dlna_player import QUEUES, proxy_stream
from dlna_providers import get_provider

log = logging.getLogger("dlna.api.playback")


def version(h, params):
    """Report the running gateway version (release-line marker). Lets a
    side-by-side 1.x / 2.0 instance be told apart from the PWA and curl."""
    h._json(200, {"version": VERSION})


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


# AcoustID enrichment is fully removed in 2.0 (Option A: beets is the sole
# metadata authority). The endpoints, the dlna_acoustid worker module, and its
# wiring are all gone; historical metadata_overrides rows (incl. source=
# 'acoustid') stay as data and are cleaned by tools/post_beets_reindex.py.


def index_rebuild(h, params):
    udn = params.get("udn", "")
    srv = SERVERS.get(udn)
    if not srv:
        h._json(404, {"error": "Server not found"})
        return

    # LocalFs-style providers don't speak UPnP ContentDirectory — the
    # generic Indexer crawls via provider.cd_browse(), which a
    # LocalFsProvider doesn't have (it crashed the rebuild before this
    # fix). Dispatch them to their own mutagen rescan instead. Detect by
    # capability (has rescan, no cd_browse) so any future filesystem-style
    # provider works without a hard import.
    provider = get_provider(udn)
    if provider is not None and hasattr(provider, "rescan") \
            and not hasattr(provider, "cd_browse"):
        def _localfs_rebuild():
            try:
                INDEXER.state.update(status="running", progress=0, total=0,
                                     tracks=0, server=srv.name, error="")
                stats = provider.rescan(force=True)
                INDEXER.state.update(status="idle",
                                     tracks=stats.get("scanned", 0), error="")
                log.info(f"LocalFs rebuild complete for {srv.name}: {stats}")
            except Exception as e:                   # noqa: BLE001
                log.exception(f"LocalFs rebuild failed: {e}")
                INDEXER.state.update(status="error", error=str(e))
        threading.Thread(target=_localfs_rebuild, daemon=True,
                         name="localfs-rebuild").start()
        h._json(200, {"ok": True, "message": "LocalFs rescan started"})
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
# 12 MB — aligned with dlna_localfs_server's embedded-art cap (2026-07-03).
# The old 5 MB cap silently 404'd every album whose embedded cover was
# bigger (e.g. a real 5.25 MB cover made getCoverArt fail deterministically
# for all 12 candidates of one album while each URL served fine directly).
_ART_MAX_BYTES   = 12 * 1024 * 1024
_ART_MIN_BYTES   = 64            # below this it isn't a real cover (junk/empty 200)
_ART_TIMEOUT     = 10
_ART_MAX_REDIRECTS = 4           # coverartarchive front-500 → archive.org CDN


def art_fetch(upstream: str):
    """Fetch + validate an image for the /art proxy. Returns
    (status, content_type_or_error_message, body_bytes). Pure/blocking —
    shared by the legacy stdlib handler and the native ASGI route so there's
    a single source of truth.

    Follows up to `_ART_MAX_REDIRECTS` redirects — `coverartarchive.org`'s
    `front-500` URLs answer 307 to the archive.org CDN, and http.client does
    NOT auto-follow, so without this those covers never load. Caps at 5 MB,
    rejects non-image responses (an upstream HTML 404 page) and suspiciously
    tiny (<64 B) bodies (junk/empty 200s) so neither can poison the cache."""
    if not upstream:
        return 400, "Missing url", b""
    url = upstream
    for _hop in range(_ART_MAX_REDIRECTS + 1):
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception:
            return 400, "Bad url", b""
        if parsed.scheme not in ("http", "https"):
            return 400, "Bad scheme", b""
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
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    return resp.status, f"Upstream {resp.status} (no Location)", b""
                url = urllib.parse.urljoin(url, loc)   # resolves relative + absolute
                continue
            if resp.status != 200:
                return resp.status, f"Upstream {resp.status}", b""
            body = resp.read(_ART_MAX_BYTES + 1)
            if len(body) > _ART_MAX_BYTES:
                return 502, "Image too large", b""
            ctype = resp.getheader("Content-Type") or "image/jpeg"
            if not ctype.lower().startswith("image/"):
                return 502, f"Not an image: {ctype}", b""
            if len(body) < _ART_MIN_BYTES:
                return 502, "Image too small", b""
            return 200, ctype, body
        except Exception as e:                       # noqa: BLE001
            log.debug(f"art proxy: {url[:80]}  {type(e).__name__}: {e}")
            return 502, str(e), b""
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
    return 508, "Too many redirects", b""


def art_fetch_cached(upstream: str):
    """`art_fetch` fronted by an on-disk byte cache (dlna_art_cache).

    Covers are fetched over and over — Amperfy syncs every cover in the library,
    and the same album cover is requested once per song. `art_fetch` re-hits the
    source every time (external coverartarchive over the network, or
    `/localfs/art/<id>` re-decoding the audio file). The cache serves repeat
    requests — across clients AND gateway restarts — from disk. Only 200s are
    cached; covers for a URL don't meaningfully change (TTL-bounded; delete the
    cache dir to force-refresh). Same `(status, ctype_or_msg, body)` contract."""
    if upstream:
        hit = dlna_art_cache.get(upstream)
        if hit is not None:
            return 200, hit[0], hit[1]
    code, ctype, body = art_fetch(upstream)
    if code == 200 and body:
        dlna_art_cache.put(upstream, ctype, body)
    return code, ctype, body


# Snap arbitrary client-requested sizes onto a small ladder so the scaled-variant
# cache holds a handful of copies per cover, not one per distinct pixel value a
# client happens to ask for. A request is served the smallest bucket that still
# covers it; anything above the top bucket falls through to the original (scaling
# UP is pointless and a >1024 cover is already the full image on a phone).
_ART_SIZE_BUCKETS = (96, 256, 512, 1024)
_ART_JPEG_QUALITY = 85


def _size_bucket(size: int) -> int:
    """Nearest bucket >= size, or 0 (= serve original) when size is 0/negative
    or larger than the top bucket."""
    if size <= 0:
        return 0
    for b in _ART_SIZE_BUCKETS:
        if size <= b:
            return b
    return 0


def _scale_image(body: bytes, ctype: str, box: int):
    """Downscale `body` to fit a `box`×`box` square (aspect-preserving), returning
    `(content_type, bytes)`. Returns the ORIGINAL `(ctype, body)` unchanged when
    the image is already within the box, or on any decode/encode error — the
    caller caches whatever comes back under the size variant so a subsequent
    request is a pure disk read either way."""
    if _PILImage is None or _PIL_LANCZOS is None:
        return ctype, body
    try:
        im = _PILImage.open(io.BytesIO(body))
        if max(im.size) <= box:
            return ctype, body                       # already small enough
        im.thumbnail((box, box), _PIL_LANCZOS)
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")                   # JPEG can't hold alpha/palette
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=_ART_JPEG_QUALITY, optimize=True)
        return "image/jpeg", out.getvalue()
    except Exception as e:                            # noqa: BLE001
        log.debug(f"art scale failed (box={box}): {type(e).__name__}: {e}")
        return ctype, body


def art_fetch_scaled(upstream: str, size: int = 0):
    """`art_fetch_cached` plus optional downscaling to the Subsonic `size` box.

    Honours `getCoverArt?size=N` so Amperfy/CarPlay pull a right-sized thumbnail
    (a few KB) for list rows instead of the full embedded cover (often multiple
    MB) — the dominant cost of an Amperfy library sync over the tailnet. `size=0`
    (or no Pillow, or a size above the top bucket) is exactly the old behaviour.
    Scaled copies are cached per size bucket (`dlna_art_cache` variant)."""
    box = _size_bucket(size)
    if box == 0 or _PILImage is None or _PIL_LANCZOS is None or not upstream:
        return art_fetch_cached(upstream)
    variant = f"s{box}"
    hit = dlna_art_cache.get(upstream, variant)
    if hit is not None:
        return 200, hit[0], hit[1]
    code, ctype, body = art_fetch_cached(upstream)   # warms the original cache too
    if code != 200 or not body:
        return code, ctype, body
    sctype, sbody = _scale_image(body, ctype, box)
    dlna_art_cache.put(upstream, sctype, sbody, variant)
    return 200, sctype, sbody


def art(h, params):
    """Proxy an arbitrary image URL through the gateway (legacy stdlib path).

    Purpose: iOS MediaSession will NOT load cross-origin artwork on the lock
    screen. The PWA rewrites track art URLs to `/art?url=<external>` so the
    fetch is same-origin. The 2.0 ASGI route (dlna_asgi.art) shares
    `art_fetch_cached` with this. No Range (art is small, one-shot)."""
    code, ctype, body = art_fetch_cached(params.get("url", ""))
    if code != 200:
        h.send_error(code, ctype)
        return
    h.send_response(200)
    h.send_header("Content-Type",  ctype)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control",  "public, max-age=86400")  # art rarely changes
    h.send_header("Access-Control-Allow-Origin", "*")
    h.end_headers()
    try:
        h.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def track_meta(h, params):
    """GET /api/track_meta?url=<track-url>

    Returns metadata for one track, including both year fields:
      - `year`: file-tag year from DIDL-Lite (the edition you own)
      - `year_original`: MusicBrainz first-release-date year if filled

    Frontend uses this to render the year line in the now-playing panel
    (prefers `year_original`; annotates `1987 (remastered)` when the
    edition year differs by 3+).
    Response: {title, artist, album, duration, year, year_original}
    or 404 if not in library."""
    url = params.get("url", "")
    if not url:
        h._json(400, {"error": "missing url"})
        return
    meta = DB.track_meta_by_url(url)
    if not meta:
        h._json(404, {"error": "track not in library"})
        return
    h._json(200, meta)


def lyrics(h, params):
    """GET /api/lyrics?url=<track-url>

    Cache-first: returns from the `lyrics` table if any row exists
    (success OR sticky-notfound). Cache miss → query lrclib once, cache
    the outcome, return. Network is hit at most once per track URL.

    Response shape:
      { plain: str|null, synced: str|null, source: str, cached: bool }
        source ∈ {'lrclib', 'notfound', 'manual'}
    """
    code, body = lyrics_payload(params)
    h._json(code, body)


def position_save_payload(payload: dict) -> tuple:
    """Core of POST /api/position → (status, body). Audiobook resume-
    position save — the PWA fires this every ~20s while an audiobook
    plays, plus on pause/end and via sendBeacon on tab hide. Fields are
    clamped defensively (same posture as client_log): a broken client
    can't grow the DB unboundedly or 500 the endpoint."""
    if not isinstance(payload, dict):
        return 400, {"error": "invalid body"}
    album_key = str(payload.get("album_key") or "")[:512]
    url = str(payload.get("url") or "")[:1024]
    if not album_key or not url:
        return 400, {"error": "missing album_key or url"}
    ok = DB.position_set(
        album_key, url,
        payload.get("position_sec"),
        payload.get("duration_sec"),
        finished=bool(payload.get("finished")))
    if not ok:
        return 400, {"error": "invalid position_sec"}
    return 200, {"ok": True}


def position_get_payload(params) -> tuple:
    """Core of GET /api/position?album_key= → (status, body).
    `position` is null when the book has never been played."""
    album_key = (params.get("album_key") or "").strip()
    if not album_key:
        return 400, {"error": "missing album_key"}
    return 200, {"position": DB.position_get(album_key)}


def book_meta_all_payload(params) -> tuple:
    """Core of GET /api/book_meta_all → (status, body). The whole
    audiobook metadata overlay (one row per book) — the PWA fetches it
    once per source switch and annotates browse rows client-side."""
    return 200, {"books": DB.book_meta_all()}


def positions_list_payload(params) -> tuple:
    """Core of GET /api/positions → (status, body). Newest-first list of
    every book with a saved position, enriched with the chapter's track
    row (book/author/art + chapter title) so the PWA's continue-listening
    shelf renders without N follow-up queries. Orphan rows (chapter file
    gone) still appear with their bare position fields."""
    try:
        limit = int(params.get("limit", "50"))
    except ValueError:
        limit = 50
    out = []
    for p in DB.positions_list(limit):
        t = DB.track_by_url(p["url"]) or {}
        p = dict(p)
        p["book"]          = t.get("album", "")
        p["author"]        = t.get("artist", "")
        p["art"]           = t.get("art", "")
        p["chapter_title"] = t.get("title", "")
        out.append(p)
    return 200, {"positions": out}


# In-memory chapter cache keyed by (url, file mtime) — chapter atoms only
# change when the file does, and ffprobe on a local file is ~100 ms; the
# cache makes the PWA's per-track fetch free on repeats.
_chapters_cache: dict = {}


def chapters_payload(params) -> tuple:
    """Core of GET /api/chapters?url= → (status, body). Chapter atoms of
    a (typically single-file m4b) audiobook track. {"chapters": []} when
    the file has none or ffprobe is unavailable — the PWA just shows no
    chapter picker."""
    import dlna_ffmpeg
    url = (params.get("url") or "").strip()
    if not url:
        return 400, {"error": "missing url"}
    t = DB.track_by_url(url)
    if not t:
        return 404, {"error": "track not in library"}
    path = t.get("file_path") or ""
    if not path or not os.path.exists(path):
        return 200, {"chapters": []}
    key = (url, os.path.getmtime(path))
    if key not in _chapters_cache:
        if len(_chapters_cache) > 500:
            _chapters_cache.clear()
        _chapters_cache[key] = dlna_ffmpeg.probe_chapters(path)
    return 200, {"chapters": _chapters_cache[key]}


def lyrics_payload(params) -> tuple:
    """Core of GET /api/lyrics → (status, body). Cache-first; one lrclib call
    per URL on a miss. Shared by the legacy handler and the 2.0 native route
    (the lrclib network call runs in a threadpool there)."""
    from dlna_player import _dur_to_sec
    import dlna_lyrics

    url = params.get("url", "")
    if not url:
        return 400, {"error": "missing url"}

    cached = DB.get_lyrics(url)
    if cached is not None:
        return 200, {
            "plain":  cached["plain"],
            "synced": cached["synced"],
            "source": cached["source"],
            "cached": True,
        }

    meta = DB.track_meta_by_url(url)
    if not meta or not (meta.get("title") and meta.get("artist")):
        return 404, {"error": "track not in library", "source": "notfound"}

    duration_sec = _dur_to_sec(meta.get("duration") or 0)
    try:
        result = dlna_lyrics.fetch_lrclib(
            meta["title"], meta["artist"],
            meta.get("album") or "", duration_sec)
    except dlna_lyrics.LrclibNotFound:
        DB.set_lyrics(url, None, None, "notfound")
        return 200, {"plain": None, "synced": None,
                     "source": "notfound", "cached": False}

    if not result:
        # Network error — DON'T cache, so the next tap retries.
        return 502, {"error": "lyrics provider unreachable", "source": "error"}

    DB.set_lyrics(url, result.get("plain"), result.get("synced"), "lrclib")
    return 200, {
        "plain":  result.get("plain"),
        "synced": result.get("synced"),
        "source": "lrclib",
        "cached": False,
    }


# ── POST handlers ─────────────────────────────────────────────────

def render_queue(h, body):
    try:
        data = _parse_json_or_400(h, body)
        if data is None:
            return
        udn    = data.get("udn", "")
        tracks = data.get("tracks", [])
        force  = bool(data.get("force", False))
        # Audiobooks (P3): resume offset within the first track + the
        # book flag that turns on the monitor's position persistence.
        is_book = bool(data.get("book", False))
        try:
            start_at = max(0.0, float(data.get("start_at_sec") or 0))
        except (TypeError, ValueError):
            start_at = 0.0
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
                q.start(av_url, tracks, name, rc_url=rc_url,
                        start_at_sec=start_at, is_book=is_book)
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


_SENTINEL = object()   # distinguishes "field omitted" from "field=None"


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
        # year may be: omitted (don't touch), an int (set), or null
        # (clear the override). _SENTINEL distinguishes "not in body"
        # from "explicitly null".
        year_raw = data.get("year", _SENTINEL)
        year_arg = _SENTINEL
        if year_raw is not _SENTINEL:
            if year_raw is None:
                year_arg = None
            else:
                try:
                    y = int(year_raw)
                except (TypeError, ValueError):
                    h._json(400, {"error": "year must be an integer or null"})
                    return
                if y < 1900 or y > 2100:
                    h._json(400, {"error": "year must be between 1900 and 2100"})
                    return
                year_arg = y
        # Only pass `year` when it was actually in the body. Passing this
        # handler's `_SENTINEL` would NOT match update_track_meta's own
        # `_YEAR_UNSET` sentinel, so it'd be mistaken for a real value and
        # fail to bind ("type 'object' is not supported") — breaking every
        # edit that doesn't change the year. (Fixed 2026-06-01.)
        meta_kwargs = dict(artist=artist, album=album, title=title,
                           genre=genre)
        if year_raw is not _SENTINEL:
            meta_kwargs["year"] = year_arg   # None (clear) or validated int
        ok = DB.update_track_meta(url, **meta_kwargs)
        fields = [k for k, v in [('artist', artist), ('album', album),
                                 ('title', title), ('genre', genre)]
                  if v is not None]
        if year_arg is not _SENTINEL:
            fields.append(f"year={year_arg}")
        log.info(f"edit_track: {url[:60]}  fields={fields}")
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
