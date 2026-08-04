"""On-disk cover-art byte cache.

Cover art is fetched repeatedly: Subsonic clients (Amperfy in particular) sync
the whole library and pull every cover, and the PWA `/art` proxy fetches on
first paint. `api_playback.art_fetch()` re-fetches every single time — external
`coverartarchive.org` URLs over the network, and `/localfs/art/<id>` URLs that
re-decode the audio file to extract embedded art. None of that is amortised.

This cache stores the resolved **200 image bytes** on disk, keyed by the source
URL, so repeat fetches — across clients AND gateway restarts — are served from
disk instantly. Covers for a given URL don't meaningfully change (a localfs id
is path-derived; a CAA URL is MBID-derived), so a long TTL is safe; a re-tag is
picked up when the entry expires, or immediately by deleting the cache dir.

Entry file format: ``<content-type>\n`` followed by the raw image bytes. Only
200s are cached. TTL-bounded; total size soft-capped with oldest-first eviction
(checked periodically to keep writes O(1) amortised during a big sync).

Tunables (env):
  ART_CACHE_DIR        cache directory (default: <module dir>/art_cache)
  ART_CACHE_TTL_SEC    entry lifetime (default: 14 days; 0 = never expire)
  ART_CACHE_MAX_BYTES  soft total-size cap (default: 1 GiB; 0 = unbounded)
"""
import hashlib
import logging
import os
import threading
import time

log = logging.getLogger("dlna.artcache")

CACHE_DIR = os.environ.get("ART_CACHE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "art_cache")
TTL_SEC = int(os.environ.get("ART_CACHE_TTL_SEC", str(14 * 24 * 3600)))
MAX_BYTES = int(os.environ.get("ART_CACHE_MAX_BYTES", str(1024 * 1024 * 1024)))

# Run the (O(n)) size-eviction sweep only once every N puts so a multi-thousand
# cover sync isn't O(n^2). The cap is soft — a small overshoot between sweeps is
# fine.
_EVICT_EVERY = 200

_lock = threading.Lock()
_put_count = 0


def _key(url: str, variant: str = "") -> str:
    # An empty variant hashes the bare url so pre-variant entries (the original
    # full-size covers already on disk) keep the SAME key — no cache churn. A
    # scaled variant (e.g. "s256") is folded into the hash as a distinct entry.
    payload = url if not variant else f"{url}\n{variant}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _path(url: str, variant: str = "") -> str:
    return os.path.join(CACHE_DIR, _key(url, variant))


def get(url: str, variant: str = ""):
    """Return ``(content_type, body)`` for a fresh cached entry, else ``None``.
    A stale (TTL-expired) or corrupt entry is treated as a miss (and dropped).
    ``variant`` selects a size-scaled copy (e.g. ``"s256"``); empty = original."""
    if not url:
        return None
    p = _path(url, variant)
    try:
        st = os.stat(p)
    except OSError:
        return None
    if TTL_SEC > 0 and (time.time() - st.st_mtime) > TTL_SEC:
        try:
            os.remove(p)
        except OSError:
            pass
        return None
    try:
        with open(p, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    nl = raw.find(b"\n")
    if nl <= 0:                       # missing header line → corrupt
        return None
    body = raw[nl + 1:]
    if not body:
        return None
    ctype = raw[:nl].decode("ascii", "replace")
    return ctype, body


def put(url: str, ctype: str, body: bytes, variant: str = "") -> None:
    """Store ``body`` (an image) for ``url``. No-op on empty url/body. Writes
    atomically (tmp + os.replace) so a concurrent reader never sees a partial
    file. ``variant`` stores a size-scaled copy under a distinct key."""
    if not url or not body:
        return
    ctype = (ctype or "image/jpeg").splitlines()[0][:120] or "image/jpeg"
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except OSError as e:
        log.debug(f"art_cache: cannot create {CACHE_DIR}: {e}")
        return
    p = _path(url, variant)
    tmp = f"{p}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp, "wb") as f:
            f.write(ctype.encode("ascii", "replace") + b"\n")
            f.write(body)
        os.replace(tmp, p)
    except OSError as e:
        log.debug(f"art_cache: put failed for {url[:80]}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return

    global _put_count
    with _lock:
        _put_count += 1
        do_evict = (_put_count % _EVICT_EVERY == 0)
    if do_evict:
        _evict_if_needed()


def _evict_if_needed() -> None:
    """If the cache exceeds MAX_BYTES, delete oldest-mtime entries until under."""
    if MAX_BYTES <= 0:
        return
    try:
        entries = []
        total = 0
        with os.scandir(CACHE_DIR) as it:
            for e in it:
                if not e.is_file() or e.name.find(".tmp.") != -1:
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, e.path))
                total += st.st_size
        if total <= MAX_BYTES:
            return
        entries.sort()                # oldest first
        for _mtime, size, path in entries:
            if total <= MAX_BYTES:
                break
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass
    except OSError:
        pass


def clear() -> int:
    """Remove every cached entry. Returns the count removed."""
    n = 0
    try:
        with os.scandir(CACHE_DIR) as it:
            for e in it:
                if e.is_file():
                    try:
                        os.remove(e.path)
                        n += 1
                    except OSError:
                        pass
    except OSError:
        pass
    return n


def stats() -> dict:
    files = 0
    total = 0
    try:
        with os.scandir(CACHE_DIR) as it:
            for e in it:
                if e.is_file() and e.name.find(".tmp.") == -1:
                    files += 1
                    try:
                        total += e.stat().st_size
                    except OSError:
                        pass
    except OSError:
        pass
    return {"dir": CACHE_DIR, "entries": files, "bytes": total,
            "ttl_sec": TTL_SEC, "max_bytes": MAX_BYTES}
