"""
dlna_geocode.py — reverse-geocode GPS coords → place name (Nominatim/OSM),
cache-first via library.db's geocode_cache.

Per the video-feature spec we ALWAYS reverse-geocode when online. Usage policy
(OSM Nominatim): an identifying contact User-Agent, ≤1 req/sec, and a persistent
cache so each place is fetched once, ever — same discipline as the MusicBrainz /
Cover-Art-Archive fetchers. Offline / failed lookups return None and are NOT
cached (so they retry later); a definitive "no name" is cached as '' (sticky).
"""
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger("dlna.geocode")

_NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
_RATE_SEC = 1.1                 # OSM policy: max 1 req/sec (small margin)
_last = [0.0]
_lock = threading.Lock()


def _user_agent() -> str:
    email = (os.environ.get("GATEWAY_CONTACT_EMAIL", "") or "").strip()
    return f"DLNAGateway/1.0 ( {email or 'unknown'} )"


def _place_from_nominatim(data) -> str:
    """Pick a concise place name from a Nominatim reverse response → name, or
    '' if the response carried no usable place."""
    addr = (data or {}).get("address") or {}
    for k in ("city", "town", "village", "municipality", "suburb",
              "county", "state"):
        if addr.get(k):
            return str(addr[k])
    dn = (data or {}).get("display_name") or ""
    return dn.split(",")[0].strip()


def reverse_geocode(lat, lon, timeout: float = 8.0):
    """One rate-limited Nominatim lookup. Returns a place name, '' (definitive
    no-name), or None on a network/HTTP failure (transient — caller shouldn't
    cache it)."""
    params = urllib.parse.urlencode({
        "lat": f"{float(lat):.5f}", "lon": f"{float(lon):.5f}",
        "format": "jsonv2", "zoom": "14", "addressdetails": "1"})
    with _lock:                                   # global ≤1 req/sec
        wait = _RATE_SEC - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
    try:
        req = urllib.request.Request(
            f"{_NOMINATIM}?{params}", headers={"User-Agent": _user_agent()})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as e:                        # noqa: BLE001 (transient)
        log.debug("geocode failed (%s,%s): %s", lat, lon, e)
        return None
    return _place_from_nominatim(data)


def place_for(db, lat, lon):
    """Cache-first place name for coords. Checks geocode_cache; on a miss does
    one Nominatim lookup and stores the result ('' = sticky no-name). Returns
    the place name, '' (known no-name), or None when offline/failed (NOT cached
    → retries on a later scan)."""
    place, hit = db.geocode_get(lat, lon)
    if hit:
        return place                              # '' or a name
    name = reverse_geocode(lat, lon)
    if name is None:
        return None                               # transient — don't cache
    db.geocode_put(lat, lon, name)                # name or '' (sticky)
    return name
