#!/usr/bin/env python3
"""
api_subsonic_browse.py — the read/browse endpoints: ping, license,
music folders, artists, albums, album lists, search, genres, and the
OpenSubsonic capability probes.

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

`getAlbumList2` paginates in SQL (LIMIT/OFFSET) rather than slicing a
full-library list per page — Amperfy's initial sync walks every page, and the
naive form re-read all ~2,200 albums each time. `random` is DAY-SEEDED so
paging stays coherent instead of returning duplicates and gaps.
"""
import logging

import time

import api_subsonic_proto as _proto
from api_subsonic_ids import (
    _album_id_decode,
    _artist_id,
    _artist_id_decode,
    _default_udn,
    _int_param,
    _so_album,
    _so_artist,
    _so_song,
)
from api_subsonic_proto import ERR_NOT_FOUND, _fail, _ok, _subsonic_user

log = logging.getLogger("dlna.api.subsonic")


# ── Endpoint handlers ────────────────────────────────────────────

def _ping(h, params):
    _ok(h, {})


def _get_license(h, params):
    _ok(h, {"license": {"valid": True}})


def _get_music_folders(h, params):
    _ok(h, {"musicFolders": {"musicFolder": [
        {"id": 1, "name": "Music"},
    ]}})


def _get_open_subsonic_extensions(h, params):
    """OpenSubsonic compatibility probe. We don't implement any
    extensions, but return an empty list so OS-aware clients (Amperfy
    et al.) know we're a real OS server and proceed past login."""
    _ok(h, {"openSubsonicExtensions": []})


def _get_user(h, params):
    """Minimal user record — Amperfy and friends probe this during
    login to confirm the username is real."""
    u = _subsonic_user()
    _ok(h, {"user": {
        "username":        u,
        "email":           "",
        "scrobblingEnabled":  True,
        "adminRole":          False,
        "settingsRole":       False,
        "downloadRole":       True,
        "uploadRole":         False,
        "playlistRole":       True,
        "coverArtRole":       True,
        "commentRole":        False,
        "podcastRole":        False,
        "streamRole":         True,
        "jukeboxRole":        False,
        "shareRole":          False,
        "videoConversionRole":False,
        "folder":             [1],
    }})


def _get_genres(h, params):
    udn = _default_udn()
    rows = _proto.DB.all_genres(udn) if udn else []
    _ok(h, {"genres": {"genre": [
        {"value": r["genre"], "songCount": r.get("track_count", 0),
         "albumCount": 0}
        for r in rows
    ]}})


def _get_scan_status(h, params):
    """Always idle for our gateway — re-indexing is triggered by the
    PWA, not via Subsonic. Some clients call this on connect."""
    _ok(h, {"scanStatus": {"scanning": False, "count": 0}})


def _get_starred(h, params):
    """Legacy alias for getStarred2 (same payload, just under a
    different wrapper). Some clients fall back to this if getStarred2
    isn't recognised."""
    favs = _proto.DB.album_fav_list()
    _ok(h, {"starred": {
        "album":  [_so_album(f)
                   for f in favs],
        "song":   [],
        "artist": [],
    }})


def _get_indexes(h, params):
    """Legacy artist index, grouped A-Z, plus "#" for non-alphanumeric."""
    udn = _default_udn()
    artists = _proto.DB.all_artists(udn) if udn else []
    buckets: dict[str, list] = {}
    for r in artists:
        first = (r["artist"][:1] or "#").upper()
        key = first if first.isalpha() else "#"
        buckets.setdefault(key, []).append(_so_artist(r))
    index = [{"name": k, "artist": buckets[k]}
             for k in sorted(buckets.keys())]
    _ok(h, {"indexes": {
        "lastModified":  int(time.time() * 1000),
        "ignoredArticles": "The El La Los Las Le Les",
        "index":         index,
    }})


def _get_artists(h, params):
    """Modern artist list (same structure as getIndexes, slightly different
    wrapping — Subsonic clients call one or the other)."""
    udn = _default_udn()
    artists = _proto.DB.all_artists(udn) if udn else []
    buckets: dict[str, list] = {}
    for r in artists:
        first = (r["artist"][:1] or "#").upper()
        key = first if first.isalpha() else "#"
        buckets.setdefault(key, []).append(_so_artist(r))
    index = [{"name": k, "artist": buckets[k]}
             for k in sorted(buckets.keys())]
    _ok(h, {"artists": {
        "ignoredArticles": "The El La Los Las Le Les",
        "index":           index,
    }})


def _get_artist(h, params):
    aid = params.get("id", "")
    artist = _artist_id_decode(aid)
    if artist is None:
        _fail(h, ERR_NOT_FOUND, f"Unknown artist id: {aid}")
        return
    udn = _default_udn()
    albums = _proto.DB.artist_albums(udn, artist) if udn else []
    # Subsonic has no grouping in getArtist — no dividers, no
    # sub-containers — so the only honest tools are ORDER and the name.
    # Their own records come first; the compilations they merely appear
    # on follow, each marked with how little of it is theirs, which is
    # the same signal the PWA's "1 track of 67" subtitle carries.
    own     = [a for a in albums if a.get("own", True)]
    appears = [a for a in albums if not a.get("own", True)]

    def _mark(a):
        so = _so_album(a)
        n, of = a.get("track_count") or 0, a.get("folder_tracks") or 0
        if of:
            so["name"] = so["title"] = (
                f"{a['album']}  ·  {n} of {of}")
        return so

    _ok(h, {"artist": {
        "id":         aid,
        "name":       artist,
        "coverArt":   _artist_id(artist),
        "albumCount": len(albums),
        "album":      [_so_album(a) for a in own] + [_mark(a) for a in appears],
    }})


def _get_album(h, params):
    aid = params.get("id", "")
    decoded = _album_id_decode(aid)
    if decoded is None:
        _fail(h, ERR_NOT_FOUND, f"Unknown album id: {aid}")
        return
    artist, album, album_key = decoded
    udn = _default_udn()
    tracks = (_proto.DB.album_tracks(udn, artist, album, album_key=album_key)
              if udn else [])
    _ok(h, {"album": {
        "id":         aid,
        "name":       album,
        "artist":     artist,
        "artistId":   _artist_id(artist) if artist else "",
        "coverArt":   aid,
        "songCount":  len(tracks),
        "song":       [_so_song(t) for t in tracks],
    }})


def _get_album_list2(h, params):
    """Subsonic supports several `type` values. We map:
        alphabeticalByName, alphabeticalByArtist → all albums (sorted; the
            page is fetched with SQL LIMIT/OFFSET so Amperfy's full-library
            sync doesn't reload every album per page)
        newest, recent, frequent → all albums by name (we don't track add/play
            dates per album; degrade gracefully)
        random → day-seeded shuffle (stable pagination within a session)
        starred → album_favourites
    """
    import random as _rnd
    typ    = params.get("type", "alphabeticalByName")
    size   = max(1, min(_int_param(params.get("size"), 10), 500))
    offset = max(0, _int_param(params.get("offset"), 0))

    if typ == "starred":
        favs = _proto.DB.album_fav_list()
        page = favs[offset:offset + size]
        _ok(h, {"albumList2": {"album": [_so_album(f) for f in page]}})
        return

    udn = _default_udn()
    if not udn:
        _ok(h, {"albumList2": {}})
        return

    if typ == "random":
        # Stateless random with COHERENT pagination: a per-day seed means a
        # client paging through gets non-overlapping pages within a session
        # (re-shuffling per call returned duplicate/missing albums across
        # pages). random isn't the sync hot path, so the O(n) materialise is
        # acceptable; the alphabetical types below page in SQL.
        albums = _proto.DB.all_albums(udn)
        _rnd.Random(int(time.time() // 86400)).shuffle(albums)
        page = albums[offset:offset + size]
    else:
        order = "artist" if typ == "alphabeticalByArtist" else "album"
        page = _proto.DB.all_albums(udn, order=order, limit=size, offset=offset)
    _ok(h, {"albumList2": {"album": [_so_album(a) for a in page]}})


def _search3(h, params):
    q = (params.get("query") or "").strip()
    if not q:
        _ok(h, {"searchResult3": {}})
        return
    udn = _default_udn()
    if not udn:
        _ok(h, {"searchResult3": {}})
        return
    res = _proto.DB.search(udn, q)
    _ok(h, {"searchResult3": {
        "artist": [_so_artist({"artist": r["artist"],
                               "album_count": r.get("album_count", 0)})
                   for r in res.get("artists", [])],
        "album":  [_so_album({"artist": r["artist"], "album": r["album"],
                              "album_key": r.get("album_key", ""),
                              "track_count": r.get("track_count", 0)})
                   for r in res.get("albums", [])],
        "song":   [_so_song(t) for t in res.get("tracks", [])],
    }})
