#!/usr/bin/env python3
"""
api_subsonic.py — Subsonic-compatible REST API.

Mounted under /rest/*. Lets any Subsonic iOS client (Amperfy, substreamer,
play:Sub, …) browse the gateway's library and stream from it. Primary
motivator: CarPlay over Tailscale — the PWA fundamentally can't do
CarPlay; native iOS Subsonic apps can.

The client connects to the gateway, NOT AssetUPnP. AssetUPnP is invisible —
just a byte source the gateway proxies for /rest/stream. All gateway state
(playlists, album_favourites, play_counts) is exposed.

Auth: SUBSONIC_USER (default "user") and SUBSONIC_PASSWORD (required) are
read from env at module load. If SUBSONIC_PASSWORD is unset, every call
returns 503 — defence-in-depth. The primary access control is Tailscale.

JSON responses only (regardless of the ?f= param). The XML format is not
implemented; every modern Subsonic client handles JSON.
"""
import base64
import hashlib
import json
import logging
import os
import time
import urllib.parse
from typing import Callable, Optional

from dlna_discovery import SERVERS
from dlna_library import DB
from dlna_player import proxy_stream

log = logging.getLogger("dlna.api.subsonic")


# ── Config / auth ────────────────────────────────────────────────

# Read at call time (not import time) so a `launchctl setenv` change is
# picked up without a gateway restart, and so tests can monkey-patch
# os.environ directly. Module-level fall-backs only used by tests
# that prefer to set the password via attribute (see test_subsonic.py).
SUBSONIC_USER_DEFAULT     = "user"
SUBSONIC_PASSWORD_OVERRIDE: Optional[str] = None  # tests may set


def _subsonic_user() -> str:
    return os.environ.get("SUBSONIC_USER", SUBSONIC_USER_DEFAULT)


def _subsonic_password() -> str:
    if SUBSONIC_PASSWORD_OVERRIDE is not None:
        return SUBSONIC_PASSWORD_OVERRIDE
    return os.environ.get("SUBSONIC_PASSWORD", "")

# Subsonic API version we advertise. 1.16.1 is the modern baseline that
# every contemporary client handles; we don't actually implement all of
# its surface (~15 of 60+ endpoints) but version negotiation is by
# string match in clients.
API_VERSION = "1.16.1"
# Reporting as "navidrome" rather than "dlna-gateway" because some
# clients (notably Amperfy) appear to whitelist server types and
# silently reject unknown ones. Nautiline + other tested clients
# happily accept either; navidrome is the safer Subsonic-flavoured
# identifier for compatibility across the iOS client ecosystem.
SERVER_TYPE = "navidrome"
SERVER_VERSION = "1.0.0"


# ── Subsonic error codes (from the spec) ─────────────────────────
ERR_GENERIC          = 0
ERR_MISSING_PARAM    = 10
ERR_VERSION_INCOMPAT = 30  # client > server
ERR_WRONG_AUTH       = 40
ERR_TOKEN_AUTH_NA    = 41   # ldap-only servers; not us
ERR_NOT_AUTHORIZED   = 50
ERR_NOT_FOUND        = 70


def _check_auth(params: dict) -> bool:
    """Validate Subsonic-flavoured auth params. Accepts:
        ?u=&t=MD5(password+salt)&s=<salt>     (token+salt; modern)
        ?u=&p=<password>                       (plaintext legacy)
        ?u=&p=enc:<hex(password)>             (hex-encoded legacy)
    Returns True on success, False otherwise."""
    pwd = _subsonic_password()
    if not pwd:
        return False  # explicit refuse-all when env not set
    if params.get("u", "") != _subsonic_user():
        return False
    # Modern token+salt
    t = params.get("t", "")
    s = params.get("s", "")
    if t and s:
        expected = hashlib.md5((pwd + s).encode("utf-8")).hexdigest()
        return t.lower() == expected.lower()
    # Legacy plaintext / hex (also reached when t is present but s isn't)
    p = params.get("p", "")
    if p.startswith("enc:"):
        try:
            p = bytes.fromhex(p[4:]).decode("utf-8")
        except Exception:
            return False
    return p == pwd and p != ""


# ── Response helpers ─────────────────────────────────────────────

def _wrap(payload: dict) -> dict:
    out = {
        "status":        "ok",
        "version":       API_VERSION,
        "type":          SERVER_TYPE,
        "serverVersion": SERVER_VERSION,
        # OpenSubsonic compatibility hint — clients like Amperfy use
        # this to decide "this is a real Subsonic-flavoured server"
        # rather than rejecting unknown server types.
        "openSubsonic":  True,
    }
    out.update(payload)
    return {"subsonic-response": out}


def _wrap_error(code: int, message: str) -> dict:
    return {"subsonic-response": {
        "status":        "failed",
        "version":       API_VERSION,
        "type":          SERVER_TYPE,
        "serverVersion": SERVER_VERSION,
        "openSubsonic":  True,
        "error":         {"code": code, "message": message},
    }}


def _ok(h, payload: dict, http_code: int = 200):
    _send_response(h, _wrap(payload), http_code)


def _fail(h, code: int, message: str, http_code: int = 200):
    # Subsonic clients want HTTP 200 even on logical errors; the
    # error code is in the wrapper. Use http_code=503 only for the
    # "password not configured" hard-failure.
    _send_response(h, _wrap_error(code, message), http_code)


def _send_response(h, full_payload: dict, http_code: int):
    """Dispatch JSON or XML based on the f= param. Subsonic spec
    defaults to XML; clients must ask for JSON. Amperfy notably does
    NOT send f=json — it expects XML. Tested clients (Nautiline,
    substreamer) send f=json and prefer that."""
    fmt = getattr(h, "_subsonic_format", "xml")
    if fmt in ("json", "jsonp"):
        # f=jsonp would need callback wrapping; we don't ship it.
        # JSON works for every modern client and is what our tests use.
        h._json(http_code, full_payload)
        return
    # Default / explicit f=xml
    body = _to_xml_doc(full_payload).encode("utf-8")
    h._xml_response(http_code, body)


# ── JSON → Subsonic XML serialiser ──────────────────────────────
# Subsonic XML pattern:
#   - The wrapper is <subsonic-response xmlns="..." status="..." .../>
#   - Inside any element: scalar properties become attributes;
#     nested dicts become child elements with the property name;
#     arrays of objects become repeated child elements with the
#     property name (so JSON {"playlist": [...]} → <playlist .../>
#     elements, no <playlists><playlist/>... unless that nesting is
#     already in the JSON shape — which it is in our handlers).
# This mechanical conversion works because we already JSON-shape
# responses the way Subsonic XML expects.

def _xml_escape(s) -> str:
    return (str(s).replace("&", "&amp;")
                  .replace("<", "&lt;")
                  .replace(">", "&gt;")
                  .replace('"', "&quot;")
                  .replace("'", "&apos;"))


def _xml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    return str(v)


def _to_xml(payload, tag: str, with_ns: bool) -> str:
    if isinstance(payload, dict):
        attrs:    list[tuple[str, str]] = []
        children: list[tuple[str, object]] = []
        for k, v in payload.items():
            if isinstance(v, (dict, list)):
                children.append((k, v))
            else:
                attrs.append((k, _xml_scalar(v)))
        if with_ns:
            attrs.insert(0, ("xmlns", "http://subsonic.org/restapi"))
        attr_str = "".join(f' {k}="{_xml_escape(v)}"' for k, v in attrs)
        if not children:
            return f"<{tag}{attr_str}/>"
        inner = "".join(_to_xml(v, k, False) for k, v in children)
        return f"<{tag}{attr_str}>{inner}</{tag}>"
    if isinstance(payload, list):
        # Each item gets its own element with the same tag.
        return "".join(_to_xml(item, tag, False) for item in payload)
    # Bare scalar — shouldn't happen at the response level; emit as text.
    return f"<{tag}>{_xml_escape(_xml_scalar(payload))}</{tag}>"


def _to_xml_doc(wrapped: dict) -> str:
    """`wrapped` is {"subsonic-response": {...}}. Emit XML with the
    namespace on the root element."""
    inner = wrapped.get("subsonic-response", {})
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            + _to_xml(inner, "subsonic-response", with_ns=True))


# ── ID encoding ──────────────────────────────────────────────────
# Subsonic clients treat IDs as opaque strings. We base64-urlsafe
# encode UTF-8 payloads, same trick as api_upnp._encode_album_id.

def _enc(prefix: str, payload: str) -> str:
    raw = payload.encode("utf-8")
    return prefix + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _dec(prefix: str, encoded: str) -> Optional[str]:
    if not encoded.startswith(prefix):
        return None
    rest = encoded[len(prefix):]
    rest += "=" * (-len(rest) % 4)
    try:
        return base64.urlsafe_b64decode(rest).decode("utf-8")
    except Exception:
        return None


def _track_id(url: str) -> str:           return _enc("tr:", url)
def _track_id_decode(s: str) -> Optional[str]: return _dec("tr:", s)

def _album_id(artist: str, album: str) -> str:
    return _enc("al:", f"{artist}\x00{album}")

def _album_id_decode(s: str) -> Optional[tuple]:
    raw = _dec("al:", s)
    if raw is None: return None
    artist, _, album = raw.partition("\x00")
    return (artist, album)

def _artist_id(artist: str) -> str:        return _enc("ar:", artist)
def _artist_id_decode(s: str) -> Optional[str]: return _dec("ar:", s)


# ── udn resolution ────────────────────────────────────────────────

def _default_udn() -> str:
    """Subsonic clients have no notion of UPnP servers; pick one. The
    user's setup has exactly one MediaServer (AssetUPnP). Prefer the
    first online server, fall back to any known one, fall back to a
    udn pulled straight from the tracks table for offline / cold-cache
    cases."""
    try:
        online = SERVERS.online()
        if online:
            return online[0].udn
        any_srv = SERVERS.all()
        if any_srv:
            return any_srv[0].udn
    except Exception:
        pass
    try:
        with DB._pool.read() as c:
            row = c.execute("SELECT udn FROM tracks LIMIT 1").fetchone()
        return row["udn"] if row else ""
    except Exception:
        return ""


# ── Subsonic object builders ─────────────────────────────────────
# Map gateway rows into the field names Subsonic clients expect.

def _so_artist(row: dict) -> dict:
    return {
        "id":         _artist_id(row["artist"]),
        "name":       row["artist"],
        "albumCount": int(row.get("album_count", 0) or 0),
        "coverArt":   _artist_id(row["artist"]),   # serves the same b64
    }


def _so_album(row: dict, *, with_artist_id: bool = True) -> dict:
    out = {
        "id":         _album_id(row.get("artist") or "", row["album"]),
        "name":       row["album"],
        "title":      row["album"],
        "artist":     row.get("artist") or "",
        "songCount":  int(row.get("track_count", 0) or 0),
        "coverArt":   _album_id(row.get("artist") or "", row["album"]),
        "duration":   0,
        "created":    "",
    }
    if with_artist_id and row.get("artist"):
        out["artistId"] = _artist_id(row["artist"])
    return out


def _so_song(t: dict) -> dict:
    # Subsonic stores duration in seconds (int). The gateway stores it
    # as the UPnP "H:MM:SS(.fff)" string; convert tolerantly.
    dur = t.get("duration") or ""
    try:
        if isinstance(dur, (int, float)):
            secs = int(dur)
        else:
            parts = str(dur).split(":")
            secs = 0
            for p in parts:
                secs = secs * 60 + int(float(p))
    except (ValueError, TypeError):
        secs = 0

    return {
        "id":       _track_id(t["url"]),
        "parent":   _album_id(t.get("artist", ""), t.get("album", "")),
        "title":    t.get("title", "") or "",
        "artist":   t.get("artist", "") or "",
        "album":    t.get("album", "") or "",
        "duration": secs,
        "isDir":    False,
        "isVideo":  False,
        "type":     "music",
        "coverArt": _album_id(t.get("artist", ""), t.get("album", "")),
        "albumId":  _album_id(t.get("artist", ""), t.get("album", "")),
        "artistId": _artist_id(t.get("artist", "")) if t.get("artist") else "",
        "suffix":   (t.get("url", "").rsplit(".", 1)[-1] or "").lower(),
        "contentType": t.get("mime") or "audio/flac",
    }


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
    rows = DB.all_genres(udn) if udn else []
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
    favs = DB.album_fav_list()
    _ok(h, {"starred": {
        "album":  [_so_album({"artist": f["artist"],
                              "album":  f["album"],
                              "track_count": f.get("track_count", 0)})
                   for f in favs],
        "song":   [],
        "artist": [],
    }})


def _get_indexes(h, params):
    """Legacy artist index, grouped A-Z, plus "#" for non-alphanumeric."""
    udn = _default_udn()
    artists = DB.all_artists(udn) if udn else []
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
    artists = DB.all_artists(udn) if udn else []
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
        return _fail(h, ERR_NOT_FOUND, f"Unknown artist id: {aid}")
    udn = _default_udn()
    albums = DB.artist_albums(udn, artist) if udn else []
    _ok(h, {"artist": {
        "id":         aid,
        "name":       artist,
        "coverArt":   _artist_id(artist),
        "albumCount": len(albums),
        "album":      [_so_album(a) for a in albums],
    }})


def _get_album(h, params):
    aid = params.get("id", "")
    decoded = _album_id_decode(aid)
    if decoded is None:
        return _fail(h, ERR_NOT_FOUND, f"Unknown album id: {aid}")
    artist, album = decoded
    udn = _default_udn()
    tracks = DB.album_tracks(udn, artist, album) if udn else []
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
        alphabeticalByName, alphabeticalByArtist → all albums (sorted)
        newest, recent, frequent → all albums (we don't track add/play
            dates per album; degrade gracefully)
        random → random.shuffle of all
        starred → album_favourites
    """
    import random as _rnd
    typ    = params.get("type", "alphabeticalByName")
    size   = int(params.get("size", "10") or 10)
    offset = int(params.get("offset", "0") or 0)
    size   = max(1, min(size, 500))

    if typ == "starred":
        favs = DB.album_fav_list()
        page = favs[offset:offset + size]
        _ok(h, {"albumList2": {"album": [
            _so_album({"artist": f["artist"],
                       "album":  f["album"],
                       "track_count": f.get("track_count", 0)})
            for f in page
        ]}})
        return

    udn = _default_udn()
    albums = DB.all_albums(udn) if udn else []
    if typ in ("random",):
        _rnd.shuffle(albums)
    elif typ in ("alphabeticalByArtist",):
        albums.sort(key=lambda a: ((a.get("artist") or "").lower(),
                                    (a.get("album")  or "").lower()))
    # alphabeticalByName / newest / recent / frequent → as-is
    # (all_albums already sorts by album name COLLATE NOCASE)
    page = albums[offset:offset + size]
    _ok(h, {"albumList2": {"album": [_so_album(a) for a in page]}})


def _search3(h, params):
    q = (params.get("query") or "").strip()
    if not q:
        return _ok(h, {"searchResult3": {}})
    udn = _default_udn()
    if not udn:
        return _ok(h, {"searchResult3": {}})
    res = DB.search(udn, q)
    _ok(h, {"searchResult3": {
        "artist": [_so_artist({"artist": r["artist"],
                               "album_count": r.get("album_count", 0)})
                   for r in res.get("artists", [])],
        "album":  [_so_album({"artist": r["artist"], "album": r["album"],
                              "track_count": r.get("track_count", 0)})
                   for r in res.get("albums", [])],
        "song":   [_so_song(t) for t in res.get("tracks", [])],
    }})


def _get_playlists(h, params):
    pls = DB.pl_list()
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
        return _fail(h, ERR_NOT_FOUND, f"Unknown playlist id: {pid_raw}")
    pid = pid_raw[3:]
    pl = DB.pl_get(pid)
    if pl is None:
        return _fail(h, ERR_NOT_FOUND, "Playlist not found")
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
        pl = DB.pl_get(pid)
        if pl is None:
            return _fail(h, ERR_NOT_FOUND, "Playlist not found")
    elif name:
        pid = DB.pl_create(name)
    else:
        return _fail(h, ERR_MISSING_PARAM, "Need playlistId or name")

    # Replace the playlist's tracks with the given song list. Walk the
    # current track URLs vs the requested ones; remove what's not in
    # the new list and add what's missing.
    cur_pl = DB.pl_get(pid)
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
            DB.pl_remove_track(pid, old)
        for u in new_urls:
            if u in cur_urls:
                continue
            meta = DB.track_meta_by_url(u) or {}
            DB.pl_add_track(pid, {
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
        return _fail(h, ERR_MISSING_PARAM, "Missing or bad playlistId")
    pid = pid_raw[3:]
    pl = DB.pl_get(pid)
    if pl is None:
        return _fail(h, ERR_NOT_FOUND, "Playlist not found")

    name = params.get("name", "")
    if name:
        with DB._pool.write() as c:
            c.execute("UPDATE playlists SET name=? WHERE id=?", (name, pid))

    for sid in params.get("songIdToAdd__all", []):
        u = _track_id_decode(sid)
        if u is None:
            continue
        meta = DB.track_meta_by_url(u) or {}
        DB.pl_add_track(pid, {
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
            DB.pl_remove_track(pid, u)

    # By-index removal too (Subsonic's documented mode).
    if params.get("songIndexToRemove__all"):
        cur = DB.pl_get(pid)
        for idx_s in params.get("songIndexToRemove__all", []):
            try:
                idx = int(idx_s)
            except ValueError:
                continue
            if 0 <= idx < len(cur["tracks"]):
                DB.pl_remove_track(pid, cur["tracks"][idx]["url"])
                # re-fetch so subsequent indices stay correct
                cur = DB.pl_get(pid)

    _ok(h, {})


def _delete_playlist(h, params):
    pid_raw = params.get("id", "")
    if not pid_raw.startswith("pl:"):
        return _fail(h, ERR_MISSING_PARAM, "Missing or bad id")
    pid = pid_raw[3:]
    DB.pl_delete(pid)
    _ok(h, {})


def _star(h, params):
    """Subsonic clients usually send either `id` (song or album) or
    `albumId`. We treat ANY album-style id as an album favourite;
    song-level starring is a no-op (we don't implement a
    track_favourites table yet)."""
    aid = params.get("albumId", "") or params.get("id", "")
    decoded = _album_id_decode(aid) if aid.startswith("al:") else None
    if decoded:
        DB.album_fav_add(*decoded)
    _ok(h, {})


def _unstar(h, params):
    aid = params.get("albumId", "") or params.get("id", "")
    decoded = _album_id_decode(aid) if aid.startswith("al:") else None
    if decoded:
        DB.album_fav_remove(*decoded)
    _ok(h, {})


def _get_starred2(h, params):
    favs = DB.album_fav_list()
    _ok(h, {"starred2": {
        "album": [_so_album({"artist": f["artist"],
                             "album":  f["album"],
                             "track_count": f.get("track_count", 0)})
                  for f in favs],
        "song":   [],
        "artist": [],
    }})


def _stream(h, params):
    sid = params.get("id", "")
    url = _track_id_decode(sid)
    if not url:
        return _fail(h, ERR_NOT_FOUND, f"Unknown track id: {sid}")
    # Reuse the existing byte-perfect Range-aware proxy.
    proxy_stream(url, h)


def _get_cover_art(h, params):
    """Resolve a Subsonic cover ID back to a URL, then reuse the
    existing /art proxy. IDs can be track / album / artist; we look
    each up against the DB."""
    from api_playback import art as art_handler
    sid = params.get("id", "")

    art_url = ""
    if sid.startswith("al:"):
        decoded = _album_id_decode(sid)
        if decoded:
            artist, album = decoded
            with DB._pool.read() as c:
                row = c.execute(
                    "SELECT art_url FROM album_art "
                    "WHERE artist=? AND album=?", (artist, album)).fetchone()
                if row and row["art_url"]:
                    art_url = row["art_url"]
                else:
                    row = c.execute(
                        "SELECT art FROM tracks WHERE artist=? AND album=? "
                        "AND art != '' LIMIT 1", (artist, album)).fetchone()
                    if row:
                        art_url = row["art"]
    elif sid.startswith("tr:"):
        u = _track_id_decode(sid)
        if u:
            with DB._pool.read() as c:
                row = c.execute(
                    "SELECT art FROM tracks WHERE url=?", (u,)).fetchone()
                if row:
                    art_url = row["art"]
    elif sid.startswith("ar:"):
        artist = _artist_id_decode(sid)
        if artist:
            with DB._pool.read() as c:
                row = c.execute(
                    "SELECT MAX(art) AS art FROM tracks WHERE artist=? "
                    "AND art != ''", (artist,)).fetchone()
                if row:
                    art_url = row["art"] or ""

    if not art_url:
        # Subsonic clients tolerate a 404 here gracefully.
        h.send_error(404, "no art")
        return

    p2 = dict(params)
    p2["url"] = art_url
    art_handler(h, p2)


def _scrobble(h, params):
    """Bump play_counts.count when the client reports a finished play.
    We honour the `submission` flag — submission=false means 'now
    playing' (don't count yet); submission=true (or absent) means
    'finished, count it'."""
    sub = params.get("submission", "true").lower()
    if sub == "false":
        return _ok(h, {})
    sid = params.get("id", "")
    url = _track_id_decode(sid)
    if not url:
        return _ok(h, {})  # silent ignore — don't fail the client
    with DB._pool.write() as c:
        c.execute(
            "INSERT INTO play_counts (url, count, last_played) "
            "VALUES (?, 1, strftime('%s','now')) "
            "ON CONFLICT(url) DO UPDATE SET "
            "  count = count + 1, "
            "  last_played = strftime('%s','now')",
            (url,))
    _ok(h, {})


# ── Dispatcher ───────────────────────────────────────────────────

_METHODS: dict[str, Callable] = {
    "ping":             _ping,
    "getLicense":       _get_license,
    "getMusicFolders":  _get_music_folders,
    "getIndexes":       _get_indexes,
    "getArtists":       _get_artists,
    "getArtist":        _get_artist,
    "getAlbum":         _get_album,
    "getAlbumList2":    _get_album_list2,
    "search3":          _search3,
    "getPlaylists":     _get_playlists,
    "getPlaylist":      _get_playlist,
    "createPlaylist":   _create_playlist,
    "updatePlaylist":   _update_playlist,
    "deletePlaylist":   _delete_playlist,
    "star":             _star,
    "unstar":           _unstar,
    "getStarred":       _get_starred,
    "getStarred2":      _get_starred2,
    "stream":           _stream,
    "download":         _stream,   # treat download == stream
    "getCoverArt":      _get_cover_art,
    "scrobble":         _scrobble,
    "getUser":          _get_user,
    "getGenres":        _get_genres,
    "getScanStatus":    _get_scan_status,
    "getOpenSubsonicExtensions": _get_open_subsonic_extensions,
}


def _method_from_path(path: str) -> str:
    """`/rest/ping` → 'ping'. `/rest/ping.view` → 'ping' (legacy form
    some old clients still send)."""
    tail = path[len("/rest/"):] if path.startswith("/rest/") else path
    if tail.endswith(".view"):
        tail = tail[:-5]
    return tail


def _parse_params(query, body: bytes = b"") -> dict:
    """Build the handler param dict from a raw query string (+ optional
    form-encoded POST body). Subsonic clients repeat params for
    multi-value fields (songId, id, …); parse_qsl into a dict keeps only
    the LAST value, so every key's full value list is also surfaced
    under a `<name>__all` key for handlers that need to iterate. Body
    params override / extend the query string.

    `query` may also be a dict — the test driver calls `handle()`
    directly with an already-built param dict; it's used as-is and only
    the `__all` mirror keys are (re)derived."""
    if isinstance(query, dict):
        pairs = [(k, v) for k, v in query.items() if not k.endswith("__all")]
    else:
        pairs = urllib.parse.parse_qsl(query)
        if body:
            try:
                pairs += urllib.parse.parse_qsl(
                    body.decode("utf-8", errors="replace"))
            except Exception:
                pass
    params: dict = {}
    multi: dict[str, list[str]] = {}
    for k, v in pairs:
        params[k] = v
        multi.setdefault(k, []).append(v)
    for k, vs in multi.items():
        params[f"{k}__all"] = vs
    return params


def handle(h, http_method: str, path: str, query, body: bytes = b""):
    """Single entry point — called from dlna_server when path starts
    with /rest/. Parses params, authenticates, dispatches to the
    per-method handler. `query` is the raw query string in production;
    tests may pass a pre-built param dict."""
    query_params = _parse_params(query, body)
    method = _method_from_path(path)
    client = query_params.get("c", "?")
    fmt_raw = (query_params.get("f", "") or "").lower()
    # Stash the format choice on the handler so _send_response can read
    # it from any deeper handler. Spec default is XML; clients (Amperfy)
    # really do rely on that default. JSON-aware clients send f=json.
    h._subsonic_format = "json" if fmt_raw in ("json", "jsonp") else "xml"
    log.debug(f"Subsonic {http_method} {method!r} client={client!r} "
              f"u={query_params.get('u', '')!r} f={fmt_raw or '<unset>'} "
              f"→ resp={h._subsonic_format}")
    if not method:
        return _fail(h, ERR_NOT_FOUND, "missing method", http_code=404)

    fn = _METHODS.get(method)
    if fn is None:
        log.debug(f"Subsonic: unimplemented method {method!r}")
        return _fail(h, ERR_NOT_FOUND,
                     f"Method not implemented: {method}", http_code=404)

    if not _subsonic_password():
        # Distinct from auth-failure: deliberate refuse-all when env
        # not set so a misconfigured deploy doesn't accidentally
        # expose data with an empty-password match.
        log.warning("Subsonic call rejected — SUBSONIC_PASSWORD env not set")
        return _fail(h, ERR_NOT_AUTHORIZED,
                     "Server not configured (SUBSONIC_PASSWORD unset)",
                     http_code=503)

    if not _check_auth(query_params):
        log.info(f"Subsonic auth failed for user={query_params.get('u', '')!r} "
                 f"method={method}")
        return _fail(h, ERR_WRONG_AUTH, "Wrong username or password")

    try:
        fn(h, query_params)
    except (BrokenPipeError, ConnectionResetError) as e:
        # Client closed the connection mid-response (Amperfy and other
        # mobile clients do this aggressively — back-button, app
        # switcher, network handover). Not a server bug; just noise.
        log.debug(f"Subsonic {method}: client disconnected ({e})")
    except Exception as e:
        log.exception(f"Subsonic {method} crashed: {e}")
        _fail(h, ERR_GENERIC, f"Server error: {e}")
