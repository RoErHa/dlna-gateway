#!/usr/bin/env python3
"""
api_playlists.py — Playlist CRUD API handlers.

Handles: /api/playlists, /api/playlist, /api/playlist/create,
         /api/playlist/delete, /api/playlist/add, /api/playlist/remove
"""
import logging

from dlna_library import DB

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
