#!/usr/bin/env python3
"""
dlna_library_credits.py — `CreditsMixin`: songwriting credits fetched
from MusicBrainz for the tracks whose files carry none.

Its own mixin from birth: `CollectionsMixin` had 31 lines of headroom,
and this is a distinct contract anyway.

**The invariant:** `clear(udn)` never touches `track_credits`. A rescan
is already blank-safe for `tracks.composer`, but a rebuild-index DELETEs
`tracks` outright — and each row here cost a rate-limited round-trip.
Same family as `lyrics` and `album_art`.

**The file tag wins on read.** `track_meta_by_url` prefers
`tracks.composer` and falls back to this. MusicBrainz is filling the
gap, not correcting what the owner actually has.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("dlna.library")


class CreditsMixin:
    """Mixed into `LibraryDB`; relies on `self._pool` from the host."""

    def track_credits_set(self, url: str, composer, lyricist,
                          work_mbid: str = "", source: str = "musicbrainz"):
        """Record (or negatively cache) a track's credits.

        `source='notfound'` with NULL fields is the sticky negative that
        keeps a second pass from re-asking — the difference between a
        resumable job and another full night."""
        if not url:
            return False
        with self._pool.write() as conn:
            conn.execute(
                "INSERT INTO track_credits "
                "  (url, composer, lyricist, work_mbid, source, fetched_at) "
                "VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(url) DO UPDATE SET "
                "  composer=excluded.composer, lyricist=excluded.lyricist, "
                "  work_mbid=excluded.work_mbid, source=excluded.source, "
                "  fetched_at=excluded.fetched_at",
                (url, composer or None, lyricist or None, work_mbid or "",
                 source, int(time.time())))
        return True

    def track_credits_get(self, url: str):
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT * FROM track_credits WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    def tracks_needing_credits(self, udn: str, limit: int = 0) -> list[dict]:
        """Tracks with no composer in the file AND no row here yet.

        A row of ANY source counts as asked, including the sticky
        negative, so a re-run resumes instead of re-spending the API
        budget. Ordered by artist so the sweep can work one artist's
        whole catalogue per MusicBrainz request."""
        sql = ("SELECT t.url, t.title, t.artist, t.album "
               "  FROM tracks t "
               "  LEFT JOIN track_credits c ON c.url = t.url "
               " WHERE t.udn=? AND (t.composer='' OR t.composer IS NULL) "
               "   AND c.url IS NULL "
               "   AND t.artist <> '' AND t.title <> '' "
               " ORDER BY t.artist COLLATE NOCASE, t.title COLLATE NOCASE")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._pool.read() as conn:
            return [dict(r) for r in conn.execute(sql, (udn,))]
