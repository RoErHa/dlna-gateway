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
from dlna_xml import read_capped

# Ceiling on a JSON body from an external service. These are answers to
# our own queries over verified TLS, so this is a backstop against an
# upstream misbehaving rather than a hostile-peer defence — but there is
# no reason for any read here to be unbounded either.
_JSON_MAX = 8 * 1024 * 1024

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


def _country_from_nominatim(data) -> str:
    """ISO country code (uppercase, e.g. 'NL') from a Nominatim reverse
    response, '' when absent — video titles are country_location_date_time
    (2026-07-06)."""
    addr = (data or {}).get("address") or {}
    return str(addr.get("country_code") or "").upper()


def reverse_geocode(lat, lon, timeout: float = 8.0):
    """One rate-limited Nominatim lookup. Returns (place, country) — either
    may be '' (definitive no-value) — or None on a network/HTTP failure
    (transient — caller shouldn't cache it)."""
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
            data = json.loads(read_capped(
                r, what="Nominatim", max_bytes=_JSON_MAX).decode("utf-8"))
    except Exception as e:                        # noqa: BLE001 (transient)
        log.debug("geocode failed (%s,%s): %s", lat, lon, e)
        return None
    return _place_from_nominatim(data), _country_from_nominatim(data)


def place_for(db, lat, lon):
    """Cache-first (place, country) for coords. Checks geocode_cache; on a
    miss does one Nominatim lookup and stores the result ('' = sticky
    no-value). A legacy cache row without country (NULL — pre-2026-07-06)
    is UPGRADED with one re-fetch; if that fails transiently the cached
    place is served with country '' and the row stays NULL (retried on a
    later scan). Returns (place, country) or None when offline/failed on a
    full miss (NOT cached → retries later)."""
    place, country, hit = db.geocode_get(lat, lon)
    if hit and country is not None:
        return place, country
    fresh = reverse_geocode(lat, lon)
    if fresh is None:                             # transient
        if hit:
            return place, ""                      # keep serving; retry later
        return None
    name, cc = fresh
    if hit and name == "" and place:
        name = place       # don't downgrade a known place on the upgrade pass
    db.geocode_put(lat, lon, name, cc)            # sticky ('' allowed)
    return name, cc
