#!/usr/bin/env python3
"""
dlna_library_search.py — `SearchMixin`: FTS5 search over the track
index.

Split out of `dlna_library_browse.py` on 2026-09-21, which had reached
exactly its 400-line budget. The seam is real rather than arbitrary:
everything left in `browse` is NAVIGATION — you know where you are and
move through a hierarchy — while this answers a free-text question and
owns the query semantics that come with it (implicit AND across terms,
the LAST term as a prefix so type-ahead works per keystroke,
punctuation-only tokens dropped because they would AND-blank the query).

`BrowseMixin` inherits this, so `LibraryDB`'s composition is unchanged
and `DB.search(...)` still resolves exactly as before.
"""
from __future__ import annotations

import logging

from dlna_library_facets import FacetsMixin
from dlna_library_sql import (
    _dedup_clause,
    _is_localfs,
    _localfs_album_artist,
    _localfs_album_group,
    _localfs_album_name,
)

log = logging.getLogger("dlna.library")


class SearchMixin(FacetsMixin):
    """See module docstring. Mixed into `LibraryDB` via `BrowseMixin`;
    relies on `self._pool` from the host class."""

    # ── FTS5 search ───────────────────────────────────────────────

    def search(self, udn: str, query: str, limit: int = 300) -> dict:
        """
        Full-text search returning tracks, distinct albums, distinct artists.
        Browse-side dedup is applied: lower-quality 16-bit duplicates of
        a 24-bit track are hidden. See `_dedup_clause` docstring.
        """
        # Type-ahead semantics (2026-07-03): each whitespace-separated
        # term must match (FTS5 implicit AND) and the LAST term matches
        # as a prefix — "essential chil" finds "Essential Classical
        # Chillout". The old single-quoted-phrase form made any partial
        # final word match NOTHING, which read as missing content in
        # clients that search per keystroke (Amperfy, the PWA box).
        # Punctuation-only tokens ("-", "&", "/") tokenize to nothing in
        # FTS5 and would AND-blank the whole query — drop them.
        terms = [t.replace('"', '""') for t in query.split()
                 if any(c.isalnum() for c in t)]
        if not terms:
            return {"tracks": [], "albums": [], "artists": []}
        fts_q = " ".join(f'"{t}"' for t in terms[:-1]) + \
                (" " if len(terms) > 1 else "") + f'"{terms[-1]}"*'
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:

            tracks = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.album_key, t.duration, t.art, t.mime, 'audio' as type
                   FROM tracks_fts f
                   JOIN tracks t ON t.id = f.rowid
                   WHERE tracks_fts MATCH ? AND t.udn = ?
                     AND {dedup}
                   ORDER BY t.artist, t.album, t.title
                   LIMIT ?""",
                (fts_q, udn, limit)).fetchall()

            if _is_localfs(udn):
                albums = conn.execute(
                    f"""SELECT t.album_key,
                              {_localfs_album_name("t")} as album,
                              {_localfs_album_artist("t")} as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks_fts f
                       JOIN tracks t ON t.id = f.rowid
                       WHERE tracks_fts MATCH ? AND t.udn = ?
                         AND t.album_key != ''
                         AND {dedup}
                       GROUP BY {_localfs_album_group("t")}
                       ORDER BY album
                       LIMIT 100""",
                    (fts_q, udn)).fetchall()
            else:
                albums = conn.execute(
                    f"""SELECT t.artist, t.album,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks_fts f
                       JOIN tracks t ON t.id = f.rowid
                       WHERE tracks_fts MATCH ? AND t.udn = ?
                         AND t.album != ''
                         AND {dedup}
                       GROUP BY t.artist, t.album
                       ORDER BY t.artist, t.album
                       LIMIT 100""",
                    (fts_q, udn)).fetchall()

            artists = conn.execute(
                f"""SELECT t.artist,
                          COUNT(DISTINCT t.album) as album_count,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks_fts f
                   JOIN tracks t ON t.id = f.rowid
                   WHERE tracks_fts MATCH ? AND t.udn = ?
                     AND t.artist != ''
                     AND {dedup}
                   GROUP BY t.artist
                   ORDER BY t.artist
                   LIMIT 50""",
                (fts_q, udn)).fetchall()

        return {
            "tracks":  [dict(r) for r in tracks],
            "albums":  [dict(r) for r in albums],
            "artists": [dict(r) for r in artists],
        }
