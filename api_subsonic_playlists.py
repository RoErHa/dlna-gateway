#!/usr/bin/env python3
"""
api_subsonic_playlists.py — playlists, album starring, and scrobble.

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

Starring maps onto the gateway's ALBUM favourites, not a track-level table:
`star`/`unstar`/`getStarred2` all go through album_favourites, so a star set
in Amperfy shows up on the PWA and in the Naim's ⭐ tree. Track-level starring
is deliberately unimplemented and no-ops gracefully.

`scrobble` bumps play_counts, which is what keeps the radio freshness bias
working from the car.
"""
import logging

import api_subsonic_proto as _proto
from api_subsonic_ids import (
    _album_id_decode,
    _so_album,
    _so_song,
    _track_id_decode,
)
from api_subsonic_proto import (
    ERR_MISSING_PARAM,
    ERR_NOT_FOUND,
    _fail,
    _ok,
    _subsonic_user,
)

log = logging.getLogger("dlna.api.subsonic")


def _get_playlists(h, params):
    pls = _proto.DB.pl_list()
    _ok(h, {"playlists": {"playlist": [
        {
            "id":        f"pl:{p['id']}",
            "name":      p["name"],
            "songCount": int(p.get("count", 0) or 0),
            "duration":  0,
            "public":    True,
            "owner":     _subsonic_user(),
            "created":   "",
        }
        for p in pls
    ]}})


def _get_playlist(h, params):
    pid_raw = params.get("id", "")
    if not pid_raw.startswith("pl:"):
        _fail(h, ERR_NOT_FOUND, f"Unknown playlist id: {pid_raw}")
        return
    pid = pid_raw[3:]
    pl = _proto.DB.pl_get(pid)
    if pl is None:
        _fail(h, ERR_NOT_FOUND, "Playlist not found")
        return
    _ok(h, {"playlist": {
        "id":        pid_raw,
        "name":      pl["name"],
        "songCount": len(pl["tracks"]),
        "duration":  0,
        "public":    True,
        "owner":     _subsonic_user(),
        "entry":     [_so_song(t) for t in pl["tracks"]],
    }})


def _create_playlist(h, params):
    """Subsonic merges create + replace under one endpoint:
       - With playlistId: update an existing playlist's track list.
       - With name: create a new playlist.
       songId may repeat (multiple ?songId=tr:… params).
    """
    pid_raw = params.get("playlistId", "")
    name    = params.get("name", "")
    song_ids = params.get("songId__all", [])  # populated below

    if pid_raw and pid_raw.startswith("pl:"):
        pid = pid_raw[3:]
        pl = _proto.DB.pl_get(pid)
        if pl is None:
            _fail(h, ERR_NOT_FOUND, "Playlist not found")
            return
    elif name:
        pid = _proto.DB.pl_create(name)
    else:
        _fail(h, ERR_MISSING_PARAM, "Need playlistId or name")
        return

    # Replace the playlist's tracks with the given song list. Walk the
    # current track URLs vs the requested ones; remove what's not in
    # the new list and add what's missing.
    cur_pl = _proto.DB.pl_get(pid)
    cur_urls = {t["url"] for t in cur_pl["tracks"]} if cur_pl else set()
    new_urls = []
    for sid in song_ids:
        u = _track_id_decode(sid)
        if u is not None:
            new_urls.append(u)
    new_set = set(new_urls)

    # Remove URLs that aren't in the new list (only if the client
    # actually sent songIds — otherwise leave existing tracks alone).
    if song_ids:
        for old in cur_urls - new_set:
            _proto.DB.pl_remove_track(pid, old)
        for u in new_urls:
            if u in cur_urls:
                continue
            meta = _proto.DB.track_meta_by_url(u) or {}
            _proto.DB.pl_add_track(pid, {
                "url":      u,
                "title":    meta.get("title", ""),
                "artist":   meta.get("artist", ""),
                "album":    meta.get("album", ""),
                "duration": meta.get("duration", ""),
                "art":      "",
            })

    _get_playlist(h, {"id": f"pl:{pid}"})


def _update_playlist(h, params):
    """Subsonic's updatePlaylist appends songIdToAdd and removes
    songIndexToRemove (by position). We accept songIdToAdd / songIdToRemove
    (some clients send by URL/ID rather than index)."""
    pid_raw = params.get("playlistId", "")
    if not pid_raw.startswith("pl:"):
        _fail(h, ERR_MISSING_PARAM, "Missing or bad playlistId")
        return
    pid = pid_raw[3:]
    pl = _proto.DB.pl_get(pid)
    if pl is None:
        _fail(h, ERR_NOT_FOUND, "Playlist not found")
        return

    name = params.get("name", "")
    if name:
        with _proto.DB._pool.write() as c:
            c.execute("UPDATE playlists SET name=? WHERE id=?", (name, pid))

    for sid in params.get("songIdToAdd__all", []):
        u = _track_id_decode(sid)
        if u is None:
            continue
        meta = _proto.DB.track_meta_by_url(u) or {}
        _proto.DB.pl_add_track(pid, {
            "url":      u,
            "title":    meta.get("title", ""),
            "artist":   meta.get("artist", ""),
            "album":    meta.get("album", ""),
            "duration": meta.get("duration", ""),
            "art":      "",
        })

    for sid in params.get("songIdToRemove__all", []):
        u = _track_id_decode(sid)
        if u is not None:
            _proto.DB.pl_remove_track(pid, u)

    # By-index removal too (Subsonic's documented mode).
    if params.get("songIndexToRemove__all"):
        cur = _proto.DB.pl_get(pid)
        for idx_s in params.get("songIndexToRemove__all", []):
            try:
                idx = int(idx_s)
            except ValueError:
                continue
            if 0 <= idx < len(cur["tracks"]):
                _proto.DB.pl_remove_track(pid, cur["tracks"][idx]["url"])
                # re-fetch so subsequent indices stay correct
                cur = _proto.DB.pl_get(pid)

    _ok(h, {})


def _delete_playlist(h, params):
    pid_raw = params.get("id", "")
    if not pid_raw.startswith("pl:"):
        _fail(h, ERR_MISSING_PARAM, "Missing or bad id")
        return
    pid = pid_raw[3:]
    _proto.DB.pl_delete(pid)
    _ok(h, {})


def _star(h, params):
    """Subsonic clients usually send either `id` (song or album) or
    `albumId`. We treat ANY album-style id as an album favourite;
    song-level starring is a no-op (we don't implement a
    track_favourites table yet)."""
    aid = params.get("albumId", "") or params.get("id", "")
    decoded = _album_id_decode(aid) if aid.startswith("al:") else None
    if decoded:
        _proto.DB.album_fav_add(*decoded)
    _ok(h, {})


def _unstar(h, params):
    aid = params.get("albumId", "") or params.get("id", "")
    decoded = _album_id_decode(aid) if aid.startswith("al:") else None
    if decoded:
        _proto.DB.album_fav_remove(*decoded)
    _ok(h, {})


def _get_starred2(h, params):
    favs = _proto.DB.album_fav_list()
    _ok(h, {"starred2": {
        "album": [_so_album(f)
                  for f in favs],
        "song":   [],
        "artist": [],
    }})


def _scrobble(h, params):
    """Bump play_counts.count when the client reports a finished play.
    We honour the `submission` flag — submission=false means 'now
    playing' (don't count yet); submission=true (or absent) means
    'finished, count it'."""
    sub = params.get("submission", "true").lower()
    if sub == "false":
        _ok(h, {})
        return
    sid = params.get("id", "")
    url = _track_id_decode(sid)
    if not url:
        _ok(h, {})  # silent ignore — don't fail the client
        return
    with _proto.DB._pool.write() as c:
        c.execute(
            "INSERT INTO play_counts (url, count, last_played) "
            "VALUES (?, 1, strftime('%s','now')) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  count = count + 1, "
            "  last_played = strftime('%s','now')",
            (url,))
    _ok(h, {})
