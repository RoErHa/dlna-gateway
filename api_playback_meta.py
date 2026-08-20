#!/usr/bin/env python3
"""
api_playback_meta.py — per-track metadata, lyrics, audiobook
positions and OpenLibrary book metadata.

Split out of api_playback.py on 2026-08-20, when that module reached 749
lines mixing cover art, playback control, and the metadata/position layer:

    api_playback_state.py  the shared handles every module binds against
    api_playback_art.py    the /art subsystem: fetch, cache, downscale, serve
    api_playback_meta.py   track metadata, lyrics, positions, book meta
    api_playback.py        playback control, index, status + re-exports

api_playback re-exports every public name, so callers (dlna_asgi_*,
dlna_routes, api_subsonic_media) and the ~36 test patch sites that reach
through it keep working.

The YEAR MODEL is the subtle part. `tracks.year` is the FILE-TAG year (the
edition you own); `metadata_overrides.year` is the MusicBrainz ORIGINAL
release year. Display prefers the override and marks a 3+ year gap as a
remaster. The override is display-only and is NEVER COALESCEd back into
`tracks` — that separation is what lets a beets retag change the file year
without destroying the original-year correction.

`edit_track` uses a `_SENTINEL` default rather than None to tell "year not
supplied" from "year cleared to NULL". sqlite3 cannot bind a bare `object()`,
so the sentinel must be resolved BEFORE it reaches a query — that was a real
bug (2026-06-01) which broke every edit that did not change the year.

Positions survive `clear(udn)` like the rest of the user-owned tables, and
are keyed by the book's FOLDER (`album_key`) so a bookmark set in CarPlay
resumes in the PWA.
"""
import logging
import os

import api_playback_state as _st
from api_playback_state import _parse_json_or_400
from dlna_config import close_quietly  # noqa: F401

log = logging.getLogger("dlna.api.playback")


def track_meta(h, params):
    """GET /api/track_meta?url=<track-url>

    Returns metadata for one track, including both year fields:
      - `year`: file-tag year from DIDL-Lite (the edition you own)
      - `year_original`: MusicBrainz first-release-date year if filled

    Frontend uses this to render the year line in the now-playing panel
    (prefers `year_original`; annotates `1987 (remastered)` when the
    edition year differs by 3+).
    Response: {title, artist, album, duration, year, year_original}
    or 404 if not in library."""
    url = params.get("url", "")
    if not url:
        h._json(400, {"error": "missing url"})
        return
    meta = _st.DB.track_meta_by_url(url)
    if not meta:
        h._json(404, {"error": "track not in library"})
        return
    h._json(200, meta)


def lyrics(h, params):
    """GET /api/lyrics?url=<track-url>

    Cache-first: returns from the `lyrics` table if any row exists
    (success OR sticky-notfound). Cache miss → query lrclib once, cache
    the outcome, return. Network is hit at most once per track URL.

    Response shape:
      { plain: str|null, synced: str|null, source: str, cached: bool }
        source ∈ {'lrclib', 'notfound', 'manual'}
    """
    code, body = lyrics_payload(params)
    h._json(code, body)


def lyrics_payload(params) -> tuple:
    """Core of GET /api/lyrics → (status, body). Cache-first; one lrclib call
    per URL on a miss. Shared by the legacy handler and the 2.0 native route
    (the lrclib network call runs in a threadpool there)."""
    from dlna_player import _dur_to_sec
    import dlna_lyrics

    url = params.get("url", "")
    if not url:
        return 400, {"error": "missing url"}

    cached = _st.DB.get_lyrics(url)
    if cached is not None:
        return 200, {
            "plain":  cached["plain"],
            "synced": cached["synced"],
            "source": cached["source"],
            "cached": True,
        }

    meta = _st.DB.track_meta_by_url(url)
    if not meta or not (meta.get("title") and meta.get("artist")):
        return 404, {"error": "track not in library", "source": "notfound"}

    duration_sec = _dur_to_sec(meta.get("duration") or 0)
    try:
        result = dlna_lyrics.fetch_lrclib(
            meta["title"], meta["artist"],
            meta.get("album") or "", duration_sec)
    except dlna_lyrics.LrclibNotFound:
        _st.DB.set_lyrics(url, None, None, "notfound")
        return 200, {"plain": None, "synced": None,
                     "source": "notfound", "cached": False}

    if not result:
        # Network error — DON'T cache, so the next tap retries.
        return 502, {"error": "lyrics provider unreachable", "source": "error"}

    _st.DB.set_lyrics(url, result.get("plain"), result.get("synced"), "lrclib")
    return 200, {
        "plain":  result.get("plain"),
        "synced": result.get("synced"),
        "source": "lrclib",
        "cached": False,
    }


def position_save_payload(payload: dict) -> tuple:
    """Core of POST /api/position → (status, body). Audiobook resume-
    position save — the PWA fires this every ~20s while an audiobook
    plays, plus on pause/end and via sendBeacon on tab hide. Fields are
    clamped defensively (same posture as client_log): a broken client
    can't grow the DB unboundedly or 500 the endpoint."""
    if not isinstance(payload, dict):
        return 400, {"error": "invalid body"}
    album_key = str(payload.get("album_key") or "")[:512]
    url = str(payload.get("url") or "")[:1024]
    if not album_key or not url:
        return 400, {"error": "missing album_key or url"}
    ok = _st.DB.position_set(
        album_key, url,
        payload.get("position_sec"),
        payload.get("duration_sec"),
        finished=bool(payload.get("finished")))
    if not ok:
        return 400, {"error": "invalid position_sec"}
    return 200, {"ok": True}


def position_get_payload(params) -> tuple:
    """Core of GET /api/position?album_key= → (status, body).
    `position` is null when the book has never been played."""
    album_key = (params.get("album_key") or "").strip()
    if not album_key:
        return 400, {"error": "missing album_key"}
    return 200, {"position": _st.DB.position_get(album_key)}


def book_meta_all_payload(params) -> tuple:
    """Core of GET /api/book_meta_all → (status, body). The whole
    audiobook metadata overlay (one row per book) — the PWA fetches it
    once per source switch and annotates browse rows client-side."""
    return 200, {"books": _st.DB.book_meta_all()}


def positions_list_payload(params) -> tuple:
    """Core of GET /api/positions → (status, body). Newest-first list of
    every book with a saved position, enriched with the chapter's track
    row (book/author/art + chapter title) so the PWA's continue-listening
    shelf renders without N follow-up queries. Orphan rows (chapter file
    gone) still appear with their bare position fields."""
    try:
        limit = int(params.get("limit", "50"))
    except ValueError:
        limit = 50
    out = []
    for p in _st.DB.positions_list(limit):
        t = _st.DB.track_by_url(p["url"]) or {}
        p = dict(p)
        p["book"]          = t.get("album", "")
        p["author"]        = t.get("artist", "")
        p["art"]           = t.get("art", "")
        p["chapter_title"] = t.get("title", "")
        out.append(p)
    return 200, {"positions": out}


# In-memory chapter cache keyed by (url, file mtime) — chapter atoms only
# change when the file does, and ffprobe on a local file is ~100 ms; the
# cache makes the PWA's per-track fetch free on repeats.
_chapters_cache: dict = {}


def chapters_payload(params) -> tuple:
    """Core of GET /api/chapters?url= → (status, body). Chapter atoms of
    a (typically single-file m4b) audiobook track. {"chapters": []} when
    the file has none or ffprobe is unavailable — the PWA just shows no
    chapter picker."""
    import dlna_ffmpeg
    url = (params.get("url") or "").strip()
    if not url:
        return 400, {"error": "missing url"}
    t = _st.DB.track_by_url(url)
    if not t:
        return 404, {"error": "track not in library"}
    path = t.get("file_path") or ""
    if not path or not os.path.exists(path):
        return 200, {"chapters": []}
    key = (url, os.path.getmtime(path))
    if key not in _chapters_cache:
        if len(_chapters_cache) > 500:
            _chapters_cache.clear()
        _chapters_cache[key] = dlna_ffmpeg.probe_chapters(path)
    return 200, {"chapters": _chapters_cache[key]}


_SENTINEL = object()   # distinguishes "field omitted" from "field=None"


def edit_track(h, body):
    try:
        data = _parse_json_or_400(h, body)
        if data is None:
            return
        url    = data.get("url", "")
        artist = data.get("artist")
        album  = data.get("album")
        title  = data.get("title")
        genre  = data.get("genre")
        if not url:
            h._json(400, {"error": "Missing url"})
            return
        # year may be: omitted (don't touch), an int (set), or null
        # (clear the override). _SENTINEL distinguishes "not in body"
        # from "explicitly null".
        year_raw = data.get("year", _SENTINEL)
        year_arg = _SENTINEL
        if year_raw is not _SENTINEL:
            if year_raw is None:
                year_arg = None
            else:
                try:
                    y = int(year_raw)
                except (TypeError, ValueError):
                    h._json(400, {"error": "year must be an integer or null"})
                    return
                if y < 1900 or y > 2100:
                    h._json(400, {"error": "year must be between 1900 and 2100"})
                    return
                year_arg = y
        # Only pass `year` when it was actually in the body. Passing this
        # handler's `_SENTINEL` would NOT match update_track_meta's own
        # `_YEAR_UNSET` sentinel, so it'd be mistaken for a real value and
        # fail to bind ("type 'object' is not supported") — breaking every
        # edit that doesn't change the year. (Fixed 2026-06-01.)
        meta_kwargs = {"artist": artist, "album": album, "title": title,
                       "genre": genre}
        if year_raw is not _SENTINEL:
            meta_kwargs["year"] = year_arg   # None (clear) or validated int
        ok = _st.DB.update_track_meta(url, **meta_kwargs)
        fields = [k for k, v in [('artist', artist), ('album', album),
                                 ('title', title), ('genre', genre)]
                  if v is not None]
        if year_arg is not _SENTINEL:
            fields.append(f"year={year_arg}")
        log.info(f"edit_track: {url[:60]}  fields={fields}")
        h._json(200, {"ok": ok})
    except Exception as e:
        log.exception(f"edit_track error: {e}")
        h._json(500, {"error": str(e)})
