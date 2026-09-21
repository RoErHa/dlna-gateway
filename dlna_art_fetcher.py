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
import os
import threading
import time
import urllib.parse
from dlna_art_query import art_queries
from dlna_xml import read_capped

# Ceiling on a JSON body from an external service. These are answers to
# our own queries over verified TLS, so this is a backstop against an
# upstream misbehaving rather than a hostile-peer defence — but there is
# no reason for any read here to be unbounded either.
_JSON_MAX = 8 * 1024 * 1024

log = logging.getLogger("dlna.library")


# MusicBrainz contract (see CLAUDE.md "External services"):
#   - UA must identify + include contact info
#   - 1 req/sec sustained max; we go 1.1s for a safety margin
#   - 10s per-connection timeout; failures become sticky notfound
# Contact email is read from GATEWAY_CONTACT_EMAIL — set it in .env so
# MusicBrainz can identify you per their ToS. A clearly-placeholder
# fallback ("you@example.com") triggers a one-time WARN at startup so
# you don't silently look anonymous to MB.
_CONTACT_EMAIL     = os.environ.get("GATEWAY_CONTACT_EMAIL",
                                    "you@example.com").strip() or "you@example.com"
_MB_USER_AGENT     = f"DLNAGateway/1.0 ( {_CONTACT_EMAIL} )"
_MB_RATE_LIMIT_SEC = 1.1
_MB_TIMEOUT        = 10.0
if _CONTACT_EMAIL == "you@example.com":
    log.warning("GATEWAY_CONTACT_EMAIL not set in .env — using placeholder "
                "in MusicBrainz / Cover Art Archive User-Agent. MusicBrainz "
                "may throttle anonymous-looking requests.")


def _lucene_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _audiobooks_udn() -> str:
    """The audiobooks LocalFs root's udn, or '' when none is configured.

    Imported lazily: `dlna_localfs_wiring` sets it at boot, and this
    module is imported long before that happens."""
    try:
        from dlna_localfs_wiring import AUDIOBOOKS_UDN
        return AUDIOBOOKS_UDN or ""
    except Exception as e:                                       # noqa: BLE001
        log.debug(f"audiobooks udn unavailable ({e}); not skipping any root")
        return ""


def _mb_lookup_cover(artist: str, album: str,
                     entity: str = "release-group") -> str | None:
    """Look up a cover art URL for (artist, album) via MusicBrainz + CAA.
    Returns a coverartarchive.org URL string on success, None otherwise.
    Chatty on purpose — every lookup is a single user-visible event.

    `entity` selects which MusicBrainz level to search, and they are NOT
    interchangeable. Cover art is attached to a RELEASE — a specific
    pressing — and a release-group only reports art when one of its
    releases is flagged as the group cover. Measured on this library's
    failures, the release level found covers for 10% where the
    release-group level found 4%:

        Harry Nilsson / Voices of the 70s
          release-group : 0 groups   -> no art
          release       : 1 release  -> art FOUND

    Release-group stays FIRST because it is the better answer when it
    works (one abstract album rather than forty pressings); release is
    the fallback that rescues compilations."""
    log.info(f"MB → query  artist={artist!r} album={album!r} "
             f"as={entity}")
    try:
        # An EMPTY artist means "search by title alone" — the only form
        # that can find a compilation, where the stored artist is one
        # contributing performer rather than the record's artist.
        if artist:
            q = (f'artist:"{_lucene_escape(artist)}" '
                 f'AND {entity.replace("-", "")}:"{_lucene_escape(album)}"')
        else:
            q = f'releasegroup:"{_lucene_escape(album)}"'
        path = f"/ws/2/{entity}/?" + urllib.parse.urlencode({
            "query": q, "fmt": "json", "limit": "5",
        })
        conn = http.client.HTTPSConnection("musicbrainz.org", timeout=_MB_TIMEOUT)
        resp = None
        try:
            conn.request("GET", path, headers={"User-Agent": _MB_USER_AGENT})
            resp = conn.getresponse()
            body = read_capped(resp, what="MusicBrainz", max_bytes=_JSON_MAX)
            if resp.status != 200:
                log.warning(f"MB ← HTTP {resp.status} for "
                            f"{artist!r} / {album!r}")
                return None
            data = json.loads(body)
        finally:
            # Close the response BEFORE the connection — an unclosed
            # HTTPResponse triggers Python 3.14's noisy "Exception ignored
            # while finalizing … I/O operation on closed file" GC warning
            # (harmless, but it reads like a crash). Closing here is clean.
            if resp is not None:
                resp.close()
            conn.close()

        groups = data.get(f"{entity}s" if entity == "release"
                          else "release-groups") or []
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
        resp = None
        try:
            conn.request("HEAD", f"/{entity}/{mbid}/front-500",
                         headers={"User-Agent": _MB_USER_AGENT})
            resp = conn.getresponse()
            read_capped(resp, what="CoverArtArchive", max_bytes=_JSON_MAX)
            if resp.status in (200, 301, 302, 307):
                log.info(f"CAA ← HTTP {resp.status} — cover available "
                         f"for mbid={mbid}")
                return (f"https://coverartarchive.org/{entity}/{mbid}"
                        f"/front-500")
            log.info(f"CAA ← HTTP {resp.status} — no front cover for "
                     f"mbid={mbid}")
            return None
        finally:
            if resp is not None:
                resp.close()
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
        self._thread: threading.Thread | None = None

    def bare_albums(self, *, skip_udns: tuple = (), min_tracks: int = 0,
                    with_performers: bool = False) -> list:
        """Albums with no art and no `album_art` row of any source
        (including 'notfound'). Largest first, so the albums a listener
        is likeliest to open resolve early.

        The filters are OPT-IN so no existing caller silently narrows,
        but a live pass showed both are needed — of 1,497 albums it
        asked MusicBrainz about, 169 were AUDIOBOOKS (the very first
        query of the run was 'Patrick Rothfuss / The Kingkiller
        Chronicle Book 2') and 90 were one- or two-track strays: a
        loose file in its own folder, which has no cover because it is
        not an album. Both are guaranteed misses that cost a
        rate-limited request each and cache a `notfound` afterwards.

        `with_performers` adds the folder's distinct-artist count, which
        is what tells `art_queries` a compilation is a compilation —
        without it the title-only query, the only form that can find
        one, never fires."""
        extra = ""
        params: list = []
        if skip_udns:
            extra += f" AND t.udn NOT IN ({','.join('?' * len(skip_udns))})"
            params.extend(skip_udns)
        having = f"HAVING COUNT(*) >= {int(min_tracks)}" if min_tracks else ""
        with self._db._pool.read() as conn:
            rows = conn.execute(f"""
                SELECT t.artist, t.album, COUNT(*) AS n,
                       COUNT(DISTINCT t2.artist) AS performers
                  FROM tracks t
             LEFT JOIN tracks t2
                    ON t2.udn = t.udn AND t2.album_key = t.album_key
                   AND t.album_key != ''
                 WHERE (t.art IS NULL OR t.art = '')
                   AND t.artist != '' AND t.album != ''
                   {extra}
                   AND NOT EXISTS (
                       SELECT 1 FROM album_art a
                        WHERE a.artist = t.artist
                          AND a.album  = t.album)
                 GROUP BY t.artist, t.album
                 {having}
                 ORDER BY n DESC
            """, params).fetchall()
        if with_performers:
            return [(r["artist"], r["album"], r["n"],
                     max(r["performers"] or 1, 1)) for r in rows]
        return [(r["artist"], r["album"], r["n"]) for r in rows]

    def run_once(self) -> dict:
        """Process bare albums until none remain. Re-queries bare_albums()
        between batches so that triggers arriving mid-run are absorbed
        into the current pass (rather than racing as a second thread)."""
        stats = {"total": 0, "found": 0, "notfound": 0, "tracks_updated": 0}
        while not self._stop.is_set():
            # Skip what cannot have an answer: a SECOND LocalFs root
            # (audiobooks — a music database has never heard of them)
            # and one-or-two-track strays, which are loose files in
            # their own folder rather than albums. Measured on a live
            # pass: 169 and 90 of 1,497 respectively, every one a
            # guaranteed miss costing a rate-limited request.
            skip = tuple(u for u in (_audiobooks_udn(),) if u)
            albums = self.bare_albums(skip_udns=skip, min_tracks=3,
                                      with_performers=True)
            if not albums:
                break
            stats["total"] += len(albums)
            eta_s = int(len(albums) * _MB_RATE_LIMIT_SEC)
            log.info(f"AlbumArtFetcher: looking up {len(albums)} bare album(s) "
                     f"(~{eta_s}s at MB rate limit)")
            for artist, album, n, performers in albums:
                if self._stop.is_set():
                    log.info("AlbumArtFetcher: stop requested — exiting early")
                    break
                # The exact (artist, album) is always tried first, so
                # nothing that resolves today can regress; the looser
                # forms — a tidied title, then title-only for a
                # compilation — are fallbacks. See dlna_art_query.
                url = None
                for q_artist, q_album in art_queries(
                        artist, album, multi_artist=performers > 1):
                    for entity in ("release-group", "release"):
                        url = _mb_lookup_cover(q_artist, q_album, entity)
                        if url or self._stop.is_set():
                            break
                        time.sleep(_MB_RATE_LIMIT_SEC)
                    if url or self._stop.is_set():
                        break
                    time.sleep(_MB_RATE_LIMIT_SEC)
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
