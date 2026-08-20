#!/usr/bin/env python3
"""
api_subsonic_extras.py — internet radio stations and audiobook
bookmarks.

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

Radio maps onto `radio_favourites` (the server-side 25-station cap is
enforced in LibraryDB, never trusted to the client). Subsonic clients play a
station by fetching its streamUrl DIRECTLY and parse ICY themselves, so the
gateway proxy is not in that path.

Bookmarks map onto the SAME `playback_positions` table the PWA and the Naim
use, keyed by the chapter's `album_key` (the book's folder), so a position
saved in CarPlay resumes in the PWA and vice versa. Subsonic positions are in
MILLISECONDS; the table stores seconds.
"""
import logging
import uuid

import os

import api_subsonic_proto as _proto
from api_subsonic_ids import (
    _iso,
    _radio_id,
    _radio_id_decode,
    _so_song,
    _track_id_decode,
)
from api_subsonic_proto import (
    ERR_GENERIC,
    ERR_MISSING_PARAM,
    ERR_NOT_FOUND,
    _fail,
    _ok,
)

log = logging.getLogger("dlna.api.subsonic")


# ── Internet radio ───────────────────────────────────────────────
# Subsonic's native radio methods, mapped onto radio_favourites.
# Subsonic clients see the gateway's ≤25 favourite stations and can
# create / update / delete them — bidirectional sync with the PWA's
# "📡 Stations" view.

def _so_radio(st: dict) -> dict:
    """radio_favourites row → Subsonic <internetRadioStation>."""
    return {
        "id":          _radio_id(st.get("station_uuid", "")),
        "name":        st.get("name", ""),
        "streamUrl":   st.get("stream_url", ""),
        "homepageUrl": st.get("homepage", ""),
    }


def _get_internet_radio_stations(h, params):
    stations = _proto.DB.radio_fav_list()
    _ok(h, {"internetRadioStations": {
        "internetRadioStation": [_so_radio(s) for s in stations],
    }})


def _create_internet_radio_station(h, params):
    """createInternetRadioStation?streamUrl=&name=&homepageUrl=
    The Subsonic client has no radio-browser UUID, so we synthesise
    one. Honours the 25-cap — a full cache returns a generic error."""
    stream = (params.get("streamUrl") or "").strip()
    name   = (params.get("name") or "").strip()
    if not stream or not name:
        _fail(h, ERR_MISSING_PARAM, "streamUrl and name required")
        return
    result = _proto.DB.radio_fav_add({
        "station_uuid": str(uuid.uuid4()),
        "name":         name,
        "stream_url":   stream,
        "homepage":     (params.get("homepageUrl") or "").strip(),
        "favicon": "", "codec": "", "bitrate": 0, "country": "", "tags": "",
    })
    if result == "full":
        _fail(h, ERR_GENERIC,
              f"Radio favourites full (limit {_proto.DB.RADIO_FAV_MAX})")
        return
    _ok(h, {})


def _update_internet_radio_station(h, params):
    """updateInternetRadioStation?id=&streamUrl=&name=&homepageUrl="""
    sid = _radio_id_decode(params.get("id", ""))
    stream = (params.get("streamUrl") or "").strip()
    name   = (params.get("name") or "").strip()
    if not sid or not stream or not name:
        _fail(h, ERR_MISSING_PARAM,
              "id, streamUrl and name required")
        return
    _proto.DB.radio_fav_update(sid, name=name, stream_url=stream,
                        homepage=(params.get("homepageUrl") or "").strip())
    _ok(h, {})


def _delete_internet_radio_station(h, params):
    """deleteInternetRadioStation?id="""
    sid = _radio_id_decode(params.get("id", ""))
    if not sid:
        _fail(h, ERR_MISSING_PARAM, "valid id required")
        return
    _proto.DB.radio_fav_remove(sid)
    _ok(h, {})


def _get_bookmarks(h, params):
    """getBookmarks → every unfinished book with a saved position whose
    chapter still resolves to a live track."""
    out = []
    for p in _proto.DB.positions_list(limit=200):
        if p.get("finished"):
            continue
        t = _proto.DB.track_by_url(p["url"])
        if not t:
            continue   # orphan row (file gone / retagged) — skip, keep row
        out.append({
            "position": int(float(p["position_sec"]) * 1000),   # ms
            "username": os.environ.get("SUBSONIC_USER", "user"),
            "comment":  "",
            "created":  _iso(p.get("updated_at")),
            "changed":  _iso(p.get("updated_at")),
            "entry":    _so_song(t),
        })
    _ok(h, {"bookmarks": {"bookmark": out}})


def _create_bookmark(h, params):
    """createBookmark?id=<track>&position=<ms>[&comment=] — save the
    book's resume point at this chapter + offset."""
    url = _track_id_decode(params.get("id", ""))
    if not url:
        _fail(h, ERR_MISSING_PARAM, "id required")
        return
    try:
        pos_ms = max(0, int(params.get("position", "0")))
    except ValueError:
        _fail(h, ERR_MISSING_PARAM, "position must be an integer")
        return
    t = _proto.DB.track_by_url(url)
    if not t:
        _fail(h, ERR_NOT_FOUND, "track not found")
        return
    key = t.get("album_key") or url   # root-level single-file book
    _proto.DB.position_set(key, url, pos_ms / 1000.0)
    _ok(h, {})


def _delete_bookmark(h, params):
    """deleteBookmark?id=<track> — clear the whole book's position."""
    url = _track_id_decode(params.get("id", ""))
    if not url:
        _fail(h, ERR_MISSING_PARAM, "id required")
        return
    t = _proto.DB.track_by_url(url)
    key = (t.get("album_key") if t else "") or url
    _proto.DB.position_clear(key)
    _ok(h, {})
