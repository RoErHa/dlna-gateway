#!/usr/bin/env python3
"""
api_subsonic_media.py — the byte endpoints: stream and cover art.

Split out of api_subsonic.py on 2026-08-20, when that module reached
1,174 lines covering auth, wire format, id codecs, and 33 endpoint handlers.

    api_subsonic_proto.py      auth + response wrapping + the XML serialiser
    api_subsonic_ids.py        id codecs, udn resolution, Subsonic object builders
    api_subsonic_browse.py     ping/artists/albums/search/genres endpoints
    api_subsonic_playlists.py  playlists, starring, scrobble
    api_subsonic_media.py      stream + cover art (the byte endpoints)
    api_subsonic_extras.py     internet radio + audiobook bookmarks
    api_subsonic.py            the _METHODS table, param parsing, handle()

api_subsonic re-exports every public name, so `import api_subsonic` and
`api_subsonic.<anything>` behave exactly as before for callers and tests.

`stream` relays through the shared proxy and NEVER transcodes — `maxBitRate`
is ignored on purpose (bit-perfect is a project invariant).

Cover art tries several candidates per album and downscales via
`art_fetch_scaled` to a 96/256/512/1024 bucket. That matters more than it
looks: Amperfy requests a thumbnail per list row, and serving the full
multi-MB embedded original for each was the whole of the "cover art is slow
in the car" complaint. Buckets are shared across clients so each is scaled at
most once.
"""
import logging

from dlna_player import proxy_stream

import api_subsonic_proto as _proto
from api_subsonic_ids import (
    _album_id_decode,
    _artist_id_decode,
    _track_id_decode,
)
from api_subsonic_proto import ERR_NOT_FOUND, _fail

log = logging.getLogger("dlna.api.subsonic")


def _stream(h, params):
    sid = params.get("id", "")
    url = _track_id_decode(sid)
    if not url:
        _fail(h, ERR_NOT_FOUND, f"Unknown track id: {sid}")
        return
    # Reuse the existing byte-perfect Range-aware proxy.
    proxy_stream(url, h)


def _cover_art_candidates(sid: str) -> list:
    """Ordered candidate art URLs for a Subsonic cover id (al:/tr:/ar:).

    A folder album's tracks each carry their OWN `/localfs/art/<id>` URL, and
    some files have no embedded picture (that id 404s). The old code took an
    arbitrary `LIMIT 1`, so getCoverArt could 404 even though OTHER tracks in
    the same folder have art. We return ALL distinct candidates and let
    `_resolve_cover` serve the first that actually fetches 200. Pure DB lookups."""
    out = []
    if sid.startswith("al:"):
        decoded = _album_id_decode(sid)
        if decoded:
            artist, album, album_key = decoded
            with _proto.DB._pool.read() as c:
                if album_key:
                    # LocalFs folder identity: art lives on the folder's tracks.
                    rows = c.execute(
                        "SELECT DISTINCT art FROM tracks WHERE album_key=? "
                        "AND art != '' ORDER BY url LIMIT 12", (album_key,)).fetchall()
                    out = [r["art"] for r in rows]
                else:
                    row = c.execute(
                        "SELECT art_url FROM album_art "
                        "WHERE artist=? AND album=?", (artist, album)).fetchone()
                    if row and row["art_url"]:
                        out.append(row["art_url"])
                    rows = c.execute(
                        "SELECT DISTINCT art FROM tracks WHERE artist=? AND album=? "
                        "AND art != '' ORDER BY url LIMIT 12", (artist, album)).fetchall()
                    out.extend(r["art"] for r in rows if r["art"] not in out)
    elif sid.startswith("tr:"):
        u = _track_id_decode(sid)
        if u:
            with _proto.DB._pool.read() as c:
                row = c.execute(
                    "SELECT art FROM tracks WHERE url=?", (u,)).fetchone()
                if row and row["art"]:
                    out.append(row["art"])
    elif sid.startswith("ar:"):
        artist = _artist_id_decode(sid)
        if artist:
            with _proto.DB._pool.read() as c:
                row = c.execute(
                    "SELECT MAX(art) AS art FROM tracks WHERE artist=? "
                    "AND art != ''", (artist,)).fetchone()
                if row and row["art"]:
                    out.append(row["art"])
    return out


def _cover_art_url(sid: str) -> str:
    """First candidate art URL for a Subsonic cover id, or '' if none.
    Back-compat single-URL accessor; prefer `_resolve_cover` for serving."""
    c = _cover_art_candidates(sid)
    return c[0] if c else ""


def _resolve_cover(sid: str, fetch):
    """Serve a cover: try each candidate art URL via `fetch` (art_fetch_cached),
    return the first `(status, ctype, body)` that comes back 200; else
    `(404, 'no art', b'')`. `fetch` is injected so this is unit-testable."""
    for url in _cover_art_candidates(sid):
        code, ctype, body = fetch(url)
        if code == 200:
            return code, ctype, body
    return 404, "no art", b""


def _get_cover_art(h, params):
    """Legacy byte handler: resolve the cover ID to the first candidate art URL
    that actually fetches, then reuse the /art proxy to serve the bytes. Tries
    every candidate so a folder album with a dead-art first track still serves
    (see _cover_art_candidates)."""
    # Import from the OWNER module, not the api_playback facade: the facade's
    # names are re-exports, so patching them would not reach the definitions
    # here. One consistent target keeps test injection honest.
    from api_playback_art import art as art_handler, art_fetch_cached
    sid = params.get("id", "")
    for url in _cover_art_candidates(sid):
        code, _ct, _b = art_fetch_cached(url)   # probe (warms the cache too)
        if code == 200:
            p2 = dict(params)
            p2["url"] = url
            art_handler(h, p2)                   # serves from the warmed cache
            return
    # Subsonic clients tolerate a 404 here gracefully.
    h.send_error(404, "no art")
