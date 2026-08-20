#!/usr/bin/env python3
"""
api_playback_art.py — the cover-art subsystem behind `/art` and
Subsonic `getCoverArt`: fetch, disk cache, downscale, serve.

Split out of api_playback.py on 2026-08-20, when that module reached 749
lines mixing cover art, playback control, and the metadata/position layer:

    api_playback_state.py  the shared handles every module binds against
    api_playback_art.py    the /art subsystem: fetch, cache, downscale, serve
    api_playback_meta.py   track metadata, lyrics, positions, book meta
    api_playback.py        playback control, index, status + re-exports

api_playback re-exports every public name, so callers (dlna_asgi_*,
dlna_routes, api_subsonic_media) and the ~36 test patch sites that reach
through it keep working.

WHY `/art` EXISTS AT ALL: iOS MediaSession refuses to load CROSS-ORIGIN
lock-screen artwork, so the PWA rewrites every art URL to `/art?url=…` to
make the fetch same-origin. That is the whole reason the gateway proxies
images it could otherwise link to directly.

Layers, outermost first — `art()` → `art_fetch_scaled()` →
`art_fetch_cached()` → `art_fetch()`. Each adds one thing:
  * `art_fetch` does the HTTP, follows redirects (coverartarchive's
    front-500 307s to archive.org), enforces a 12 MB cap, and REJECTS a
    non-image Content-Type so an upstream HTML 404 page cannot poison the
    Service Worker cache.
  * `art_fetch_cached` fronts it with the on-disk byte cache, including a
    short-TTL NEGATIVE entry for deterministic failures (a file with no
    embedded art, a CAA 404) so Amperfy's repeated requests stop re-decoding
    the same dead candidate. A TRANSIENT failure (503) is never
    negative-cached — a momentary localfs blip must retry at once.
  * `art_fetch_scaled` snaps `size` to a 96/256/512/1024 bucket ladder and
    caches each bucket separately, so a bucket is scaled at most once and
    the original fetched at most once regardless of how many sizes are
    asked for. Fixed buckets, deliberately NOT devicePixelRatio-derived: a
    dpr-derived size would fragment the cache per device for no visible gain.

Pillow is OPTIONAL. Without it the full-resolution original is served — the
pre-scaling behaviour. Degrading to more bandwidth is acceptable; degrading
to a broken image is not.
"""
import http.client
import io
import logging
import ssl
import urllib.parse

import dlna_art_cache
import api_playback_state as _st  # noqa: F401
from dlna_config import close_quietly

# Pillow is an OPTIONAL dependency (same pattern as rich/dotenv): when
# present, a requested `size` is honoured by downscaling before serving.
# Without it the original full-resolution image is served — no hard failure.
try:
    from PIL import Image as _PILImage
    # Resampling enum is the canonical home since Pillow 9.1; the bare
    # Image.LANCZOS alias is deprecated and may be removed in a major.
    _PIL_LANCZOS = getattr(getattr(_PILImage, "Resampling", _PILImage),
                           "LANCZOS", None)
except ImportError:
    _PILImage = None
    _PIL_LANCZOS = None

log = logging.getLogger("dlna.api.playback")


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
        except (ValueError, AttributeError) as e:
            log.debug(f"art_fetch: unparseable url {url[:120]!r}: {e}")
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
            # 503 (not 502) marks a TRANSIENT failure — upstream unreachable /
            # timed out / TLS error. art_fetch_cached must NOT negative-cache
            # these (a momentary localfs restart mustn't suppress a cover for an
            # hour); the deterministic 502s above (not-an-image / too-big) it may.
            log.debug(f"art proxy: {url[:80]}  {type(e).__name__}: {e}")
            return 503, str(e), b""
        finally:
            if conn:
                close_quietly(conn)
    return 508, "Too many redirects", b""


def art_fetch_cached(upstream: str):
    """`art_fetch` fronted by an on-disk byte cache (dlna_art_cache).

    Covers are fetched over and over — Amperfy syncs every cover in the library,
    and the same album cover is requested once per song. `art_fetch` re-hits the
    source every time (external coverartarchive over the network, or
    `/localfs/art/<id>` re-decoding the audio file). The cache serves repeat
    requests — across clients AND gateway restarts — from disk. Only 200s are
    cached; covers for a URL don't meaningfully change (TTL-bounded; delete the
    cache dir to force-refresh). Same `(status, ctype_or_msg, body)` contract.

    A deterministic FAILURE (a candidate whose file has no embedded art, a CAA
    404, a not-an-image body) is remembered under a short-TTL negative marker so
    Amperfy's repeated getCoverArt doesn't re-decode the same dead candidate
    each time. Transient failures (503 — upstream unreachable) are never cached,
    so a momentary blip is retried on the next request."""
    if upstream:
        hit = dlna_art_cache.get(upstream)
        if hit is not None:
            return 200, hit[0], hit[1]
        neg = dlna_art_cache.get_negative(upstream)
        if neg is not None:
            return neg[0], neg[1], b""
    code, ctype, body = art_fetch(upstream)
    if code == 200 and body:
        dlna_art_cache.put(upstream, ctype, body)
    elif upstream and code != 200 and code != 503:
        # code 503 = transient (unreachable); everything else here is a
        # deterministic miss worth remembering briefly (ctype holds the message).
        dlna_art_cache.put_negative(upstream, code, ctype)
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
    `art_fetch_scaled` with this. No Range (art is small, one-shot).

    `size` (optional) snaps to the shared bucket ladder, same as the Subsonic
    getCoverArt route — absent/0 serves the unmodified original."""
    try:
        size = int(params.get("size", 0) or 0)
    except (TypeError, ValueError):
        size = 0                                   # a junk size is not an error
    code, ctype, body = art_fetch_scaled(params.get("url", ""), size)
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
