#!/usr/bin/env python3
"""
dlna_lyrics.py — On-demand lyrics fetch via lrclib.net.

lrclib has a public read API (no auth, no rate limit per their FAQ as of
2025) that returns plain + LRC-synced lyrics keyed by track/artist/album
and optionally duration. We hit it once per track, cache the result in
the `lyrics` table, and never call again for that URL (sticky negative
cache, same pattern as album_art).

Public surface:
    fetch_lrclib(track, artist, album="", duration=0) -> dict | None
        dict has keys: plain (str|None), synced (str|None)
        None on hard failure (network, non-2xx other than 404)
        404 from lrclib is "no match found" — caller should cache as
        source='notfound', NOT retry.

Test:
    python3 dlna_lyrics.py "Wish You Were Here" "Pink Floyd"
"""
import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("dlna.lyrics")

_LRCLIB_BASE = "https://lrclib.net/api/get"
_USER_AGENT  = "DLNAGateway/1.0 ( https://github.com/ronhamersma/dlna-gateway )"
_TIMEOUT     = 8.0


class LrclibNotFound(Exception):
    """Raised on 404 from lrclib — track exists in our library but they
    don't have lyrics for it. Caller should cache as 'notfound' so we
    don't retry every tap."""


def fetch_lrclib(track: str, artist: str, album: str = "", duration: int = 0):
    if not track or not artist:
        return None
    qs = {"track_name": track, "artist_name": artist}
    if album:
        qs["album_name"] = album
    if duration and duration > 0:
        qs["duration"] = int(duration)
    url = _LRCLIB_BASE + "?" + urllib.parse.urlencode(qs)
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise LrclibNotFound() from e
        log.warning(f"lrclib HTTP {e.code} for {artist!r}/{track!r}")
        return None
    except Exception as e:
        log.warning(f"lrclib error for {artist!r}/{track!r}: {e}")
        return None
    try:
        data = json.loads(body)
    except Exception as e:
        log.warning(f"lrclib bad JSON for {artist!r}/{track!r}: {e}")
        return None
    plain  = data.get("plainLyrics") or None
    synced = data.get("syncedLyrics") or None
    if not plain and not synced:
        raise LrclibNotFound()  # row exists but is empty — same as 404
    return {"plain": plain, "synced": synced}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if len(sys.argv) < 3:
        print("usage: python3 dlna_lyrics.py 'Track Title' 'Artist Name' [Album]")
        sys.exit(1)
    t, a = sys.argv[1], sys.argv[2]
    al = sys.argv[3] if len(sys.argv) > 3 else ""
    try:
        r = fetch_lrclib(t, a, al)
    except LrclibNotFound:
        print("(no match)")
        sys.exit(0)
    if not r:
        print("(error)")
        sys.exit(1)
    if r["plain"]:
        print(r["plain"])
    elif r["synced"]:
        print(r["synced"])
