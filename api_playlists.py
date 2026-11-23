#!/usr/bin/env python3
"""
api_playlists.py — Playlist CRUD API handlers.

Handles: /api/playlists, /api/playlist, /api/playlist/create,
         /api/playlist/delete, /api/playlist/add, /api/playlist/remove,
         /api/playlist/rename, /api/playlist/add_many,
         /api/playlist/remove_many, /api/playlist/reorder

The last four follow docs/gateway-contract-playlists.md: every reply has
`ok`, and every refusal a fixed `code` a client can act on without reading
`error`.
"""
import functools
import json
import logging

from dlna_library import DB
from dlna_library_playlists import PL_NAME_MAX

log = logging.getLogger("dlna.api.playlists")


def playlists(h, params):
    h._json(200, DB.pl_list())


def playlist(h, params):
    pl_id = params.get("id", "")
    pl = DB.pl_get(pl_id)
    if pl is None:
        h._json(404, {"error": "Playlist not found"})
        return
    h._json(200, pl)


def playlist_create(h, params):
    name  = params.get("name", "").strip() or "New Playlist"
    pl_id = DB.pl_create(name)
    h._json(200, {"id": pl_id, "name": name})


def playlist_delete(h, params):
    pl_id = params.get("id", "")
    ok    = DB.pl_delete(pl_id)
    h._json(200 if ok else 400, {"ok": ok})


def playlist_add(h, params):
    pl_id = params.get("pl", "")
    track = {k: params.get(k, "") for k in
             ("url", "title", "artist", "album", "duration", "art")}
    if not track["url"] or not pl_id:
        h._json(400, {"error": "Missing pl or url"})
        return
    result = DB.pl_add_track(pl_id, track)
    if result == "not_found":
        h._json(404, {"ok": False, "error": "Playlist not found"})
    elif result == "duplicate":
        h._json(200, {"ok": True, "duplicate": True})
    else:
        h._json(200, {"ok": True, "duplicate": False})


def playlist_remove(h, params):
    pl_id = params.get("pl", "")
    url   = params.get("url", "")
    ok    = DB.pl_remove_track(pl_id, url)
    h._json(200, {"ok": ok})


# ── Playlist editing (docs/gateway-contract-playlists.md) ────────
MAX_PER_CALL = 2000
_TRACK_FIELDS = ("url", "title", "artist", "album", "duration", "art")


class _BadRequest(Exception):
    """The request breaks the contract; the message says how."""


def _refuse(h, status, code, error):
    h._json(status, {"ok": False, "code": code, "error": error})


def _no_playlist(h):
    _refuse(h, 404, "playlist_not_found", "Playlist not found")


def _text(value, what):
    """`value` if it is a string SQLite can store. JSON can spell a lone
    surrogate (a "\\ud800" escape); that is a str here, and raises only when
    something encodes it."""
    if not isinstance(value, str):
        raise _BadRequest(f"{what} must be a string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as e:
        raise _BadRequest(f"{what} is not valid text") from e
    return value


def _required(value, what):
    if not _text(value, what).strip():
        raise _BadRequest(f"{what} is empty")
    return value


def _object(body):
    try:
        data = json.loads(body)
    except (ValueError, RecursionError) as e:
        raise _BadRequest("body is not valid JSON") from e
    if not isinstance(data, dict):
        raise _BadRequest("body must be a JSON object")
    return data


def _list(data, key):
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise _BadRequest(f"{key} must be a list that is not empty")
    if len(value) > MAX_PER_CALL:
        raise _BadRequest(f"{key} is over the limit of {MAX_PER_CALL}")
    return value


def _urls(data):
    return [_required(u, "url") for u in _list(data, "urls")]


def _track(value):
    if not isinstance(value, dict):
        raise _BadRequest("each track must be an object")
    _required(value.get("url"), "url")
    return {k: _text(value.get(k, ""), k) for k in _TRACK_FIELDS}


def _checked(handler):
    """Turn a `_BadRequest` into the contract's 400."""
    @functools.wraps(handler)
    def wrapper(h, arg):
        try:
            handler(h, arg)
        except _BadRequest as e:
            _refuse(h, 400, "bad_request", str(e))
    return wrapper


@_checked
def playlist_rename(h, params):
    pl_id = _required(params.get("id"), "id")
    name  = _required(params.get("name"), "name").strip()
    result = DB.pl_rename(pl_id, name)
    if result == "bad_name":
        raise _BadRequest(f"name is over {PL_NAME_MAX} characters or has "
                          f"control characters")
    if result == "not_found":
        _no_playlist(h)
    elif result == "protected":
        _refuse(h, 400, "protected", "This playlist name is protected")
    else:
        h._json(200, {"ok": True, "id": pl_id, "name": name})


@_checked
def playlist_add_many(h, body):
    data   = _object(body)
    pl_id  = _required(data.get("pl"), "pl")
    tracks = [_track(t) for t in _list(data, "tracks")]
    result = DB.pl_add_tracks(pl_id, tracks)
    if result is None:
        _no_playlist(h)
    else:
        h._json(200, {"ok": True, **result})


@_checked
def playlist_remove_many(h, body):
    data   = _object(body)
    pl_id  = _required(data.get("pl"), "pl")
    result = DB.pl_remove_tracks(pl_id, _urls(data))
    if result is None:
        _no_playlist(h)
    else:
        h._json(200, {"ok": True, **result})


@_checked
def playlist_reorder(h, body):
    data  = _object(body)
    pl_id = _required(data.get("id"), "id")
    urls  = _urls(data)
    if len(set(urls)) != len(urls):
        raise _BadRequest("a url is repeated")
    result = DB.pl_reorder(pl_id, urls)
    if result == "not_found":
        _no_playlist(h)
    elif result == "changed":
        _refuse(h, 409, "playlist_changed", "The playlist has changed")
    else:
        h._json(200, {"ok": True, "count": len(urls)})


# ── Album favourites ─────────────────────────────────────────────
# Whole-album bookmarks, distinct from the track-level Favourites
# playlist. Identity = (artist, album); see LibraryDB.album_fav_*.

def album_favourites(h, params):
    """List every favourited album (newest first)."""
    h._json(200, DB.album_fav_list())


def album_favourite_check(h, params):
    artist    = params.get("artist", "")
    album     = params.get("album", "")
    album_key = params.get("album_key", "")
    if not (album or album_key):
        h._json(400, {"error": "Missing album/album_key"})
        return
    h._json(200, {"is_favourite": DB.album_fav_is(artist, album, album_key)})


def album_favourite_add(h, params):
    artist    = params.get("artist", "")
    album     = params.get("album", "")
    album_key = params.get("album_key", "")
    if not (album or album_key):
        h._json(400, {"error": "Missing album/album_key"})
        return
    created = DB.album_fav_add(artist, album, album_key)
    h._json(200, {"ok": True, "created": created})


def album_favourite_remove(h, params):
    artist    = params.get("artist", "")
    album     = params.get("album", "")
    album_key = params.get("album_key", "")
    if not (album or album_key):
        h._json(400, {"error": "Missing album/album_key"})
        return
    ok = DB.album_fav_remove(artist, album, album_key)
    h._json(200, {"ok": ok})
