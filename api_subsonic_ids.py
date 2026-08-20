#!/usr/bin/env python3
"""
api_subsonic_ids.py — opaque id codecs, source-udn resolution, and
the builders that turn LibraryDB rows into Subsonic objects.

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

Subsonic treats ids as opaque strings, so everything is base64-urlsafe over a
NUL-joined tuple — that is what lets arbitrary unicode survive XML, JSON and
URL transport. Radio ids are the one exception (`rs:<uuid>`): radio-browser
UUIDs are already safe, so wrapping them would only obscure them.

`album_key` rides in the album id as a THIRD field, appended only when set,
so non-LocalFs ids stay byte-identical to the pre-2026-07 two-field form (no
client cache churn) while a Various-Artists compilation still round-trips as
one folder-album.
"""
import base64
import binascii
import logging

import api_subsonic_proto as _proto

log = logging.getLogger("dlna.api.subsonic")


# ── ID encoding ──────────────────────────────────────────────────
# Subsonic clients treat IDs as opaque strings. We base64-urlsafe
# encode UTF-8 payloads, same trick as api_upnp._encode_album_id.

def _enc(prefix: str, payload: str) -> str:
    raw = payload.encode("utf-8")
    return prefix + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _dec(prefix: str, encoded: str) -> str | None:
    if not encoded.startswith(prefix):
        return None
    rest = encoded[len(prefix):]
    rest += "=" * (-len(rest) % 4)
    try:
        return base64.urlsafe_b64decode(rest).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error) as e:
        # Client sent an id we did not mint. Caller turns None into a
        # "not found" fault; log so a systematically-mangled id from some
        # client is diagnosable instead of silently 404-ing forever.
        log.debug(f"Subsonic id decode failed for {encoded[:60]!r}: {e}")
        return None


def _track_id(url: str) -> str:           return _enc("tr:", url)


def _track_id_decode(s: str) -> str | None: return _dec("tr:", s)


def _album_id(artist: str, album: str, album_key: str = "") -> str:
    # album_key (LocalFs folder identity) is appended ONLY when set, so a
    # non-LocalFs album's id stays byte-identical to the pre-A3b 2-field
    # form (no client/cache churn). A LocalFs compilation gets the folder
    # in the id so it round-trips as one album.
    payload = f"{artist}\x00{album}"
    if album_key:
        payload += f"\x00{album_key}"
    return _enc("al:", payload)


def _album_id_decode(s: str) -> tuple | None:
    """Return (artist, album, album_key). Legacy 2-field ids decode with
    album_key=''."""
    raw = _dec("al:", s)
    if raw is None: return None
    parts = raw.split("\x00")
    artist    = parts[0] if len(parts) > 0 else ""
    album     = parts[1] if len(parts) > 1 else ""
    album_key = parts[2] if len(parts) > 2 else ""
    return (artist, album, album_key)


def _artist_id(artist: str) -> str:        return _enc("ar:", artist)


def _artist_id_decode(s: str) -> str | None: return _dec("ar:", s)


# Radio station ids are NOT base64-wrapped — radio-browser station
# UUIDs (and our uuid4 for client-created stations) are already
# URL/XML/JSON-safe, so a bare `rs:` prefix round-trips fine.
def _radio_id(station_uuid: str) -> str:   return "rs:" + (station_uuid or "")


def _radio_id_decode(s: str) -> str | None:
    return s[3:] if s and s.startswith("rs:") else None


# ── udn resolution ────────────────────────────────────────────────

def _default_udn() -> str:
    """Subsonic clients have no notion of UPnP servers; pick one. The
    user's setup has exactly one MediaServer (AssetUPnP). Prefer the
    first online server, fall back to any known one, fall back to a
    udn pulled straight from the tracks table for offline / cold-cache
    cases."""
    try:
        online = _proto.SERVERS.online()
        if online:
            return online[0].udn
        any_srv = _proto.SERVERS.all()
        if any_srv:
            return any_srv[0].udn
    except Exception as e:                       # registry may be mid-init
        log.debug(f"_default_udn: server registry unavailable ({e}) — "
                  f"falling back to the tracks table")
    try:
        with _proto.DB._pool.read() as c:
            row = c.execute("SELECT udn FROM tracks LIMIT 1").fetchone()
        return row["udn"] if row else ""
    except Exception as e:
        log.debug(f"_default_udn: tracks-table fallback failed ({e})")
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
    ak  = row.get("album_key") or ""
    aid = _album_id(row.get("artist") or "", row["album"], ak)
    out = {
        "id":         aid,
        "name":       row["album"],
        "title":      row["album"],
        "artist":     row.get("artist") or "",
        "songCount":  int(row.get("track_count", 0) or 0),
        "coverArt":   aid,
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

    t_ak  = t.get("album_key") or ""
    t_aid = _album_id(t.get("artist", ""), t.get("album", ""), t_ak)
    return {
        "id":       _track_id(t["url"]),
        "parent":   t_aid,
        "title":    t.get("title", "") or "",
        "artist":   t.get("artist", "") or "",
        "album":    t.get("album", "") or "",
        "duration": secs,
        "isDir":    False,
        "isVideo":  False,
        "type":     "music",
        "coverArt": t_aid,
        "albumId":  t_aid,
        "artistId": _artist_id(t.get("artist", "")) if t.get("artist") else "",
        "suffix":   (t.get("url", "").rsplit(".", 1)[-1] or "").lower(),
        "contentType": t.get("mime") or "audio/flac",
    }


def _int_param(val, default: int) -> int:
    """Tolerant int parse for client-supplied query params — a non-numeric
    `size`/`offset` degrades to the default instead of 500-ing the request."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ── Dispatcher ───────────────────────────────────────────────────

# ── Bookmarks (P4, 2026-07-14) — CarPlay audiobook resume ───────────
# Subsonic's bookmark = a saved position on a media file. Mapped onto
# `playback_positions` (ONE row per book, keyed by album_key), the same
# table the PWA and the Naim monitor write — so pausing in the car
# resumes on the couch and vice versa. createBookmark on a chapter
# replaces the book's row; deleteBookmark clears the book.

def _iso(ts) -> str:
    import datetime
    try:
        return datetime.datetime.fromtimestamp(
            int(ts), datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return "1970-01-01T00:00:00Z"
