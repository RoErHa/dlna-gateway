#!/usr/bin/env python3
"""
dlna_library_facets.py — `FacetsMixin`: the browse facets that slice the
library by a TAG rather than by artist/album — genres and decades — plus
the flat track listings those views play from and the play-count-biased
radio picker.

Split out of dlna_library_browse.py (2026-08-20), which had reached 580
lines. `BrowseMixin` INHERITS this mixin, so `LibraryDB`'s composition is
unchanged and every `DB.<method>` call site keeps resolving exactly as
before — the two are one browse surface split along a readable seam, not
a new layer.

`_EFFECTIVE_YEAR` lives here because decade bucketing is its only
consumer; see the comment on the attribute for why it takes MIN of the
file-tag and MusicBrainz years.
"""
from __future__ import annotations

import logging

from dlna_library_sql import (
    _dedup_clause,
    _is_localfs,
    _localfs_album_artist,
    _localfs_album_name,
)

log = logging.getLogger("dlna.library")


class FacetsMixin:
    """See module docstring. Mixed into `LibraryDB` via `BrowseMixin`;
    never instantiated on its own — it relies on `self._pool` from the
    host class."""

    # ── Decade browse ────────────────────────────────────────────
    # A track's "effective year" for decade bucketing is
    # MIN(file_year, mb_year) when both are present, else whichever is
    # set. Rationale: AcoustID often resolves a fingerprint to a LATER
    # re-release recording on MusicBrainz (an anniversary edition's
    # /recording/<id>'s first-release-date is when *that* recording
    # was first released, e.g. 2002 for the 30th-anniversary remaster
    # of a 1972 album). When mb_year > file_year, the file tag is the
    # more reliable signal — so we take the smaller of the two.
    # Tracks with no year at all are excluded from decade listings
    # (would show as "decade 0" otherwise). Now-playing displays a
    # different rule (prefer mb_year as "the original recording",
    # annotate as "(remastered)" when file>>mb) — see _renderNpYear.
    _EFFECTIVE_YEAR = (
        "COALESCE("
        "CASE WHEN t.year > 0 AND m.year > 0 THEN MIN(t.year, m.year) END, "
        "NULLIF(t.year, 0), "
        "NULLIF(m.year, 0))"
    )

    def all_genres(self, udn: str) -> list:
        """All distinct genres with album/track counts, A-Z. Counts are
        browse-visible (deduped)."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.genre,
                          COUNT(DISTINCT t.album) as album_count,
                          COUNT(*) as track_count
                   FROM tracks t
                   WHERE t.udn=? AND t.genre != ''
                     AND {dedup}
                   GROUP BY t.genre
                   ORDER BY t.genre COLLATE NOCASE""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]
    def genre_albums(self, udn: str, genre: str) -> list:
        """All albums in a genre, grouping compilations under 'Various Artists'.
        Track count is browse-visible (deduped). LocalFs groups by FOLDER:
        the albums are the folders that contain a track of this genre."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            if _is_localfs(udn):
                rows = conn.execute(
                    f"""SELECT t.album_key,
                              {_localfs_album_name("t")} as album,
                              {_localfs_album_artist("t")} as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks t
                       WHERE t.udn=? AND t.album_key != ''
                         AND t.album_key IN (
                             SELECT album_key FROM tracks
                              WHERE udn=? AND genre=? AND album_key != '')
                         AND {dedup}
                       GROUP BY t.album_key
                       ORDER BY album COLLATE NOCASE""",
                    (udn, udn, genre)).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT t.album,
                              CASE WHEN COUNT(DISTINCT t.artist)>1 THEN 'Various Artists'
                                   ELSE MAX(t.artist) END as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks t
                       WHERE t.udn=? AND t.genre=?
                         AND {dedup}
                       GROUP BY t.album
                       ORDER BY t.album COLLATE NOCASE""",
                    (udn, genre)).fetchall()
        return [dict(r) for r in rows]

    def all_decades(self, udn: str) -> list:
        """All decades present in the library with track + album counts.
        Browse-side dedup is applied: a 16-bit duplicate of a 24-bit
        track doesn't double-count. Ordered chronologically (oldest to
        newest)."""
        dedup = _dedup_clause("t")
        eff = self._EFFECTIVE_YEAR
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT ({eff} / 10) * 10 AS decade,
                          COUNT(*) AS track_count,
                          COUNT(DISTINCT t.album) AS album_count
                     FROM tracks t
                LEFT JOIN metadata_overrides m ON m.url = t.url
                    WHERE t.udn = ?
                      AND {eff} IS NOT NULL
                      AND {dedup}
                 GROUP BY decade
                 ORDER BY decade ASC""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]
    def decade_albums(self, udn: str, decade: int) -> list:
        """All albums whose effective year falls in [decade, decade+10).
        Grouping mirrors `all_albums`: compilations with multiple
        distinct artists collapse to 'Various Artists'. LocalFs groups by
        FOLDER: the albums are the folders that contain a track in the
        decade."""
        dedup = _dedup_clause("t")
        eff = self._EFFECTIVE_YEAR
        with self._pool.read() as conn:
            if _is_localfs(udn):
                # Effective-year expression for the inner per-track filter
                # (tracks aliased t2 there; metadata_overrides stays m).
                eff2 = eff.replace("t.year", "t2.year")
                rows = conn.execute(
                    f"""SELECT t.album_key,
                              {_localfs_album_name("t")} as album,
                              {_localfs_album_artist("t")} as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks t
                       WHERE t.udn = ? AND t.album_key != ''
                         AND t.album_key IN (
                             SELECT t2.album_key FROM tracks t2
                        LEFT JOIN metadata_overrides m ON m.url = t2.url
                             WHERE t2.udn = ? AND t2.album_key != ''
                               AND ({eff2} / 10) * 10 = ?)
                         AND {dedup}
                       GROUP BY t.album_key
                       ORDER BY album COLLATE NOCASE""",
                    (udn, udn, decade)).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT t.album,
                              CASE WHEN COUNT(DISTINCT t.artist) > 1
                                   THEN 'Various Artists'
                                   ELSE MAX(t.artist) END as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                         FROM tracks t
                    LEFT JOIN metadata_overrides m ON m.url = t.url
                        WHERE t.udn = ? AND t.album != ''
                          AND ({eff} / 10) * 10 = ?
                          AND {dedup}
                     GROUP BY t.album
                     ORDER BY t.album COLLATE NOCASE""",
                    (udn, decade)).fetchall()
        return [dict(r) for r in rows]
    def artist_tracks(self, udn: str, artist: str) -> list:
        """All tracks by a given artist, with browse-side dedup applied
        (16-bit hidden when 24-bit exists). Sorted by album then title
        so a "Play all" of this list plays the artist's catalogue in a
        reasonable order. Used by the artist-view "Play all" button —
        replaces the prior search-and-filter hack."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, t.genre, 'audio' as type
                     FROM tracks t
                    WHERE t.udn = ? AND t.artist = ?
                      AND {dedup}
                 ORDER BY t.album COLLATE NOCASE,
                          t.title COLLATE NOCASE""",
                (udn, artist)).fetchall()
        return [dict(r) for r in rows]
    def decade_tracks(self, udn: str, decade: int) -> list:
        """Flat list of every track whose effective year falls in
        [decade, decade+10)."""
        dedup = _dedup_clause("t")
        eff = self._EFFECTIVE_YEAR
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, t.genre, 'audio' as type
                     FROM tracks t
                LEFT JOIN metadata_overrides m ON m.url = t.url
                    WHERE t.udn = ?
                      AND ({eff} / 10) * 10 = ?
                      AND {dedup}
                 ORDER BY t.artist COLLATE NOCASE,
                          t.album COLLATE NOCASE,
                          t.title COLLATE NOCASE""",
                (udn, decade)).fetchall()
        return [dict(r) for r in rows]
    def genre_tracks(self, udn: str, genre: str) -> list:
        """All tracks in a genre, browse-visible (deduped)."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, t.genre, 'audio' as type
                   FROM tracks t
                   WHERE t.udn=? AND t.genre=?
                     AND {dedup}
                   ORDER BY t.album COLLATE NOCASE, t.title COLLATE NOCASE""",
                (udn, genre)).fetchall()
        return [dict(r) for r in rows]
    def radio_tracks(self, udn: str, limit: int = 100) -> list:
        """Pick `limit` tracks for the Radio feature, biased toward those
        the user hasn't heard often — lowest play count wins, random
        tiebreak within a tier. Bumps each selected track's count in
        play_counts so the NEXT radio call picks different tracks.

        Rebuild-index doesn't touch play_counts, so play history persists
        forever (same invariant as album_art). The table is keyed by URL;
        if the upstream server ever renames URLs, those entries become
        orphaned and that track resets to count=0 — acceptable because
        this is a soft preference, not a correctness requirement."""
        with self._pool.write() as conn:
            rows = conn.execute(
                """SELECT t.obj_id AS id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, 'audio' AS type
                     FROM tracks t
                     LEFT JOIN play_counts p ON p.url = t.url
                    WHERE t.udn = ?
                    ORDER BY COALESCE(p.count, 0) ASC, RANDOM()
                    LIMIT ?""",
                (udn, limit)).fetchall()
            # Bulk-increment the selected URLs so next radio call picks
            # fresher material. Using an UPSERT so brand-new URLs insert
            # at count=1 and existing URLs increment in place.
            conn.executemany(
                """INSERT INTO play_counts (url, count, last_played)
                   VALUES (?, 1, strftime('%s','now'))
                   ON CONFLICT(url) DO UPDATE
                      SET count = count + 1,
                          last_played = strftime('%s','now')""",
                [(r["url"],) for r in rows if r["url"]])
        return [dict(r) for r in rows]
