#!/usr/bin/env python3
"""
dlna_art_fetcher.py — Phase B album-art background worker.

Rate-limited worker that resolves bare (artist, album) pairs to cover
art URLs via MusicBrainz release-group search + Cover Art Archive. Hits
are written to the album_art cache as source='musicbrainz'; misses as
source='notfound' (sticky — won't retry).

Sticky-notfound means the file's `notfound` rows need to be deleted
manually if you've fixed artist/album metadata and want to retry —
see CLAUDE.md for the SQL.

The `ART_FETCHER` singleton is created in dlna_library (composition
root) and re-exported from there for backward compat.
"""
import http.client
import json
import logging
import threading
import urllib.parse
from typing import Optional

log = logging.getLogger("dlna.library")


# MusicBrainz contract (see CLAUDE.md "External services"):
#   - UA must identify + include contact info
#   - 1 req/sec sustained max; we go 1.1s for a safety margin
#   - 10s per-connection timeout; failures become sticky notfound
_MB_USER_AGENT     = "DLNAGateway/1.0 ( hintt@me.com )"
_MB_RATE_LIMIT_SEC = 1.1
_MB_TIMEOUT        = 10.0


def _lucene_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _mb_lookup_cover(artist: str, album: str) -> Optional[str]:
    """Look up a cover art URL for (artist, album) via MusicBrainz + CAA.
    Returns a coverartarchive.org URL string on success, None otherwise.
    Chatty on purpose — every lookup is a single user-visible event."""
    log.info(f"MB → query  artist={artist!r} album={album!r}")
    try:
        q = (f'artist:"{_lucene_escape(artist)}" '
             f'AND releasegroup:"{_lucene_escape(album)}"')
        path = "/ws/2/release-group/?" + urllib.parse.urlencode({
            "query": q, "fmt": "json", "limit": "5",
        })
        conn = http.client.HTTPSConnection("musicbrainz.org", timeout=_MB_TIMEOUT)
        try:
            conn.request("GET", path, headers={"User-Agent": _MB_USER_AGENT})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                log.warning(f"MB ← HTTP {resp.status} for "
                            f"{artist!r} / {album!r}")
                return None
            data = json.loads(body)
        finally:
            conn.close()

        groups = data.get("release-groups") or []
        if not groups:
            log.info(f"MB ← no match for {artist!r} / {album!r}")
            return None
        g     = groups[0]
        mbid  = g.get("id")
        title = g.get("title", "?")
        score = g.get("score", "?")
        if not mbid:
            log.info(f"MB ← match had no id for {artist!r} / {album!r}")
            return None
        log.info(f"MB ← matched mbid={mbid} title={title!r} score={score}")

        conn = http.client.HTTPSConnection(
            "coverartarchive.org", timeout=_MB_TIMEOUT)
        try:
            conn.request("HEAD", f"/release-group/{mbid}/front-500",
                         headers={"User-Agent": _MB_USER_AGENT})
            resp = conn.getresponse()
            resp.read()
            if resp.status in (200, 301, 302, 307):
                log.info(f"CAA ← HTTP {resp.status} — cover available "
                         f"for mbid={mbid}")
                return f"https://coverartarchive.org/release-group/{mbid}/front-500"
            log.info(f"CAA ← HTTP {resp.status} — no front cover for "
                     f"mbid={mbid}")
            return None
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"MB/CAA lookup error for {artist!r} / {album!r}: {e}")
        return None


class AlbumArtFetcher:
    """Rate-limited background worker that looks up cover art on
    MusicBrainz + Cover Art Archive for bare albums and writes the
    result into the album_art cache (source='musicbrainz' or
    'notfound')."""

    def __init__(self, db):
        self._db     = db
        self._stop   = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def bare_albums(self) -> list:
        """Albums that still have no art and have no album_art entry
        of any source (including 'notfound'). Ordered by track count
        descending so large albums are resolved first."""
        with self._db._pool.read() as conn:
            rows = conn.execute("""
                SELECT t.artist, t.album, COUNT(*) AS n
                  FROM tracks t
                 WHERE (t.art IS NULL OR t.art = '')
                   AND t.artist != '' AND t.album != ''
                   AND NOT EXISTS (
                       SELECT 1 FROM album_art a
                        WHERE a.artist = t.artist
                          AND a.album  = t.album)
                 GROUP BY t.artist, t.album
                 ORDER BY n DESC
            """).fetchall()
        return [(r["artist"], r["album"], r["n"]) for r in rows]

    def run_once(self) -> dict:
        """Process bare albums until none remain. Re-queries bare_albums()
        between batches so that triggers arriving mid-run are absorbed
        into the current pass (rather than racing as a second thread)."""
        stats = {"total": 0, "found": 0, "notfound": 0, "tracks_updated": 0}
        while not self._stop.is_set():
            albums = self.bare_albums()
            if not albums:
                break
            stats["total"] += len(albums)
            eta_s = int(len(albums) * _MB_RATE_LIMIT_SEC)
            log.info(f"AlbumArtFetcher: looking up {len(albums)} bare album(s) "
                     f"(~{eta_s}s at MB rate limit)")
            for artist, album, n in albums:
                if self._stop.is_set():
                    log.info("AlbumArtFetcher: stop requested — exiting early")
                    break
                url = _mb_lookup_cover(artist, album)
                if url:
                    with self._db._pool.write() as conn:
                        conn.execute(
                            "INSERT OR REPLACE INTO album_art "
                            "(artist, album, art_url, source) VALUES (?,?,?,?)",
                            (artist, album, url, "musicbrainz"))
                        cur = conn.execute(
                            "UPDATE tracks SET art = ? "
                            " WHERE (art IS NULL OR art = '') "
                            "   AND artist = ? AND album = ?",
                            (url, artist, album))
                        stats["tracks_updated"] += cur.rowcount or 0
                    stats["found"] += 1
                    log.info(f"AlbumArtFetcher ✓ {artist!r} / {album!r} "
                             f"→ updated {n} track(s)")
                else:
                    with self._db._pool.write() as conn:
                        conn.execute(
                            "INSERT OR IGNORE INTO album_art "
                            "(artist, album, art_url, source) VALUES (?,?,?,?)",
                            (artist, album, "", "notfound"))
                    stats["notfound"] += 1
                    log.info(f"AlbumArtFetcher ✗ {artist!r} / {album!r} "
                             f"— cached as notfound")
                if self._stop.wait(_MB_RATE_LIMIT_SEC):
                    break
        if stats["total"] == 0:
            log.info("AlbumArtFetcher: no bare albums to look up")
        else:
            log.info(f"AlbumArtFetcher: done — found={stats['found']}, "
                     f"notfound={stats['notfound']}, "
                     f"tracks_updated={stats['tracks_updated']}")
        return stats

    def trigger(self, delay: float = 0.0):
        """Fire run_once() in a background thread. If a scan is already
        in flight, this is a no-op — the ongoing run re-queries
        bare_albums() between batches and will pick up anything new."""
        if self._thread and self._thread.is_alive():
            log.debug("AlbumArtFetcher: trigger ignored — scan already in progress")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._delayed_run, args=(delay,),
            daemon=True, name="art-fetch")
        self._thread.start()

    def _delayed_run(self, delay: float):
        if delay > 0 and self._stop.wait(delay):
            return
        try:
            self.run_once()
        except Exception as e:
            log.exception(f"AlbumArtFetcher: run_once error: {e}")

    def start_initial_scan(self, delay: float = 120.0):
        """Kick off the one-shot startup scan. Called from gateway main.
        Picks up any bare albums left over from a previous interrupted
        run; steady-state refills come from Indexer triggering on
        successful crawls."""
        log.info(f"AlbumArtFetcher: initial scan scheduled in {int(delay)}s")
        self.trigger(delay=delay)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
