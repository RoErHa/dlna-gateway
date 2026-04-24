#!/usr/bin/env python3
"""
api_browse.py — Library browse/search API handlers.

Handles: /api/servers, /api/renderers, /api/browse, /api/artists,
         /api/albums, /api/genres, /api/genre_albums, /api/genre_tracks,
         /api/artist_albums, /api/album_tracks, /api/search,
         /api/browse_letter, /api/radio
"""
import logging
import threading
import time

from dlna_content import cd_browse
from dlna_discovery import SERVERS, RENDERERS, _STALE_SEC
from dlna_library import DB, INDEXER

log = logging.getLogger("dlna.api.browse")

# Throttle re-probe attempts: udn → last_reprobe_timestamp
_reprobe_times: dict = {}


def servers(h, params):
    now = time.time()
    result = []
    for s in SERVERS.all():
        if RENDERERS.get(s.udn):
            continue
        d = s.to_dict()
        d["online"] = (now - s.last_seen) < _STALE_SEC
        d["tracks"] = DB.track_count(s.udn)
        result.append(d)
    h._json(200, result)


def renderers(h, params):
    h._json(200, [r.to_dict() for r in RENDERERS.all()])


def browse(h, params):
    udn = params.get("udn", "")
    oid = params.get("id", "0")
    srv = SERVERS.get(udn)
    if not srv:
        h._json(404, {"error": "Server not found — still discovering?"})
        return
    result = cd_browse(srv.control_url, oid)
    if "error" not in result:
        SERVERS.touch(udn)
        _reprobe_times.pop(udn, None)
    else:
        now = time.time()
        last = _reprobe_times.get(udn, 0)
        if now - last > 60:
            _reprobe_times[udn] = now
            loc = srv.location
            log.warning(f"Browse failed for {srv.name!r} — re-probing {loc}")
            import dlna_discovery as _disc
            from api_upnp import GW_UDN
            threading.Thread(
                target=_disc.probe_url,
                args=(loc, GW_UDN), daemon=True).start()
        else:
            log.debug(f"Browse failed for {srv.name!r} — re-probe throttled")
    h._json(200, result)


def artists(h, params):
    udn = params.get("udn", "")
    if not udn:
        h._json(400, {"error": "Missing udn"})
        return
    h._json(200, DB.all_artists(udn))


def radio(h, params):
    """Return a batch of tracks for the Radio feature, biased toward
    lowest play count so the same 100 don't keep surfacing. Each
    returned URL's play_count is incremented server-side so the next
    call picks fresher material."""
    udn = params.get("udn", "")
    if not udn:
        h._json(400, {"error": "Missing udn"})
        return
    try:
        limit = int(params.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, 500))   # sanity-cap so a broken client
                                       # can't grab the whole library
    tracks = DB.radio_tracks(udn, limit)
    log.info(f"/api/radio  {len(tracks)} tracks  udn={udn[:16]}…  limit={limit}")
    h._json(200, {"tracks": tracks})


def search(h, params):
    udn   = params.get("udn", "")
    query = params.get("q", "").strip()
    if not query:
        h._json(400, {"error": "Missing q"})
        return
    if not udn:
        h._json(400, {"error": "Missing udn"})
        return
    if INDEXER.state.status == "running" and DB.track_count(udn) == 0:
        h._json(200, {"tracks": [], "albums": [], "artists": [],
                      "info": "Indexing — please wait"})
        return
    result = DB.search(udn, query)
    SERVERS.touch(udn)
    log.debug(f"Search {query!r}: {len(result['tracks'])} tracks, "
              f"{len(result['albums'])} albums")
    h._json(200, result)


def album_tracks(h, params):
    udn    = params.get("udn", "")
    artist = params.get("artist", "")
    album  = params.get("album", "")
    if not udn or not album:
        h._json(400, {"error": "Missing udn or album"})
        return
    tracks = DB.album_tracks(udn, artist, album)
    SERVERS.touch(udn)
    h._json(200, {"tracks": tracks})


def albums(h, params):
    udn = params.get("udn", "")
    if not udn:
        h._json(400, {"error": "Missing udn"})
        return
    h._json(200, DB.all_albums(udn))


def genres(h, params):
    udn = params.get("udn", "")
    if not udn:
        h._json(400, {"error": "Missing udn"})
        return
    h._json(200, DB.all_genres(udn))


def genre_albums(h, params):
    udn   = params.get("udn", "")
    genre = params.get("genre", "")
    if not udn or not genre:
        h._json(400, {"error": "Missing udn or genre"})
        return
    h._json(200, DB.genre_albums(udn, genre))


def genre_tracks(h, params):
    udn   = params.get("udn", "")
    genre = params.get("genre", "")
    if not udn or not genre:
        h._json(400, {"error": "Missing udn or genre"})
        return
    h._json(200, {"tracks": DB.genre_tracks(udn, genre)})


def artist_albums(h, params):
    udn    = params.get("udn", "")
    artist = params.get("artist", "")
    if not udn or not artist:
        h._json(400, {"error": "Missing udn or artist"})
        return
    h._json(200, DB.artist_albums(udn, artist))


def browse_letter(h, params):
    udn    = params.get("udn", "")
    mode   = params.get("mode", "artists")
    letter = params.get("letter", "A").upper()
    offset = int(params.get("offset", 0))
    limit  = int(params.get("limit", 100))
    if not udn:
        h._json(400, {"error": "Missing udn"})
        return
    h._json(200, DB.browse_letter(udn, mode, letter, offset, limit))
