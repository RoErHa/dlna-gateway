#!/usr/bin/env python3
"""
dlna_library_browse.py — `BrowseMixin`: the artist/album read paths
the PWA, the Naim UPnP tree and the Subsonic API browse through —
artists, albums, the letter bar and FTS5 search.

Split out of dlna_library.py (2026-08-20). See dlna_library_schema.py
for why these are mixins rather than collaborators. The tag-sliced
facets (genres, decades, their track listings and the radio picker)
moved to `dlna_library_facets.FacetsMixin`, which this class INHERITS —
so `LibraryDB`'s composition and the public method surface are unchanged.

Two cross-cutting rules live here rather than in the callers:
  * `_dedup_clause` hides lower-quality duplicate rows (16-bit next to
    24-bit) from BROWSE views only — playlists and radio must still see
    every URL.
  * `_is_localfs(udn)` switches album identity from (artist, album) to
    the FOLDER (`album_key`), which is what makes a Various-Artists
    compilation resolve as one album.
"""
from __future__ import annotations

import logging

from dlna_library_facets import FacetsMixin
from dlna_library_sql import (
    _dedup_clause,
    _is_localfs,
    _localfs_album_artist,
    VARIOUS_ARTISTS,
    _localfs_album_group,
    _localfs_album_name,
)

log = logging.getLogger("dlna.library")


class BrowseMixin(FacetsMixin):
    """See module docstring. Mixed into `LibraryDB`; never instantiated
    on its own — it relies on `self._pool` from the host class."""

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
    def primary_udn(self) -> str:
        """The udn of the library to expose as 'the' gateway MediaServer —
        the server owning the most tracks (in this single-library deployment,
        the LocalFs backend). Used by the gateway-as-MediaServer UPnP browse
        (api_upnp._gw_browse) to back the Artists/Albums/Genres tree. Returns
        '' when no library is indexed yet."""
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT udn FROM tracks GROUP BY udn "
                "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        return row["udn"] if row else ""
    def all_artists(self, udn: str) -> list:
        """Return all artists with album/track counts. Track count is
        the browse-visible (deduped) count."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.artist,
                          COUNT(DISTINCT t.album) as album_count,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks t
                   WHERE t.udn=? AND t.artist != ''
                     AND {dedup}
                   GROUP BY t.artist
                   ORDER BY t.artist COLLATE NOCASE""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]
    def album_tracks(self, udn: str, artist: str, album: str,
                     album_key: str = "") -> list:
        """Return all tracks for an album, with lower-quality 16/24-bit
        duplicates hidden from the browse view (see `_dedup_clause`).

        Two addressing modes:
          * `album_key` set → folder-based identity (LocalFs). Returns
            every track in that folder, ordered by `file_path` so
            disc/track order is preserved. This is what makes a
            Various-Artists compilation open as one album.

            A NAMED `artist` narrows it to that performer, mirroring
            `_localfs_album_group`: a folder whose tracks declare no album
            tag is grouped per artist, so it must RESOLVE per artist too or
            the browse row and the queue it produces disagree — which is
            exactly the bug, a Marsh & Quinn row that played 247 tracks by
            43 artists. `Various Artists` is the sentinel for a genuinely
            mixed folder and deliberately does NOT narrow. For every other
            folder this is a no-op: `_localfs_album_artist` only yields a
            real name when `COUNT(DISTINCT artist)=1`, i.e. when every row
            already carries it.
          * otherwise → the legacy `(artist, album)` pair (UPnP and any
            caller that hasn't moved to album_key — favourites, UPnP,
            Subsonic). Unchanged behaviour."""
        dedup = _dedup_clause("t")
        cols = ("t.obj_id as id, t.url, t.title, t.artist, t.album, "
                "t.album_key, t.duration, t.art, t.mime, t.genre, "
                "'audio' as type")
        with self._pool.read() as conn:
            if album_key:
                base = f"""SELECT {cols} FROM tracks t
                            WHERE t.udn=? AND t.album_key=? {{extra}}
                              AND {dedup}
                            ORDER BY t.file_path COLLATE NOCASE, t.title"""
                rows = []
                if artist not in ("", VARIOUS_ARTISTS):
                    # Mirror `_localfs_album_group` exactly: rows carrying NO
                    # album tag are grouped per performer, so they must
                    # RESOLVE per performer too, or the browse row and the
                    # queue it produces disagree — the 2026-08-25 bug, where
                    # one Marsh & Quinn row played 247 tracks by 43 artists.
                    #
                    # Keyed on the ROW, not the folder: the real junk drawer
                    # held four stray tagged files, and a folder-level test
                    # ("does anything here name an album?") was defeated by
                    # them. Tagged rows keep folder identity, which is what
                    # leaves genuine compilations — and every normal album —
                    # untouched.
                    rows = conn.execute(
                        base.format(extra="AND COALESCE(t.album,'')='' "
                                          "AND t.artist=?"),
                        (udn, album_key, artist)).fetchall()
                if not rows:
                    # Either the caller named a real album's performer, or the
                    # artist is stale — a favourite saved before a retag, an id
                    # a Subsonic client cached. Narrowing must never EMPTY an
                    # album: that reads as data loss, which is worse than the
                    # over-broad queue this exists to prevent.
                    rows = conn.execute(
                        base.format(extra=""), (udn, album_key)).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT {cols} FROM tracks t
                       WHERE t.udn=? AND t.album=?
                         AND (? = '' OR t.artist=?)
                         AND {dedup}
                       ORDER BY t.title""",
                    (udn, album, artist, artist)).fetchall()
        return [dict(r) for r in rows]
    def all_albums(self, udn: str, *, order: str = "album",
                   limit: int | None = None, offset: int = 0) -> list:
        """All distinct albums, grouping compilations under 'Various Artists'.
        Track count reflects browse-visible (deduped) tracks only.
        LocalFs sources group by FOLDER (album_key) and carry it as the
        album identity; other sources keep (artist, album) grouping.

        `order` ∈ {'album', 'artist'} chooses the sort (both COLLATE NOCASE).
        `limit`/`offset` push pagination into SQL so a paged consumer
        (Subsonic getAlbumList2) fetches one page's worth of rows instead of
        the whole library per page. `limit=None` returns everything (default)."""
        order_sql = ("artist COLLATE NOCASE, album COLLATE NOCASE"
                     if order == "artist" else "album COLLATE NOCASE")
        page_sql = ""
        extra: tuple = ()
        if limit is not None:
            page_sql = " LIMIT ? OFFSET ?"
            extra = (int(limit), max(0, int(offset)))
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
                         AND {dedup}
                       GROUP BY {_localfs_album_group("t")}
                       ORDER BY {order_sql}{page_sql}""",
                    (udn,) + extra).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT t.album,
                              CASE WHEN COUNT(DISTINCT t.artist) > 1
                                   THEN 'Various Artists'
                                   ELSE MAX(t.artist) END as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks t
                       WHERE t.udn=? AND t.album != ''
                         AND {dedup}
                       GROUP BY t.album
                       ORDER BY {order_sql}{page_sql}""",
                    (udn,) + extra).fetchall()
        return [dict(r) for r in rows]
    def artist_albums(self, udn: str, artist: str) -> list:
        """All albums for a given artist, A-Z. Track count is the
        browse-visible (deduped) count. LocalFs groups by FOLDER: the
        albums are the folders that contain a track by this artist
        (so opening a performer on a compilation lands on the whole
        comp folder); other sources keep (artist, album) grouping."""
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
                              WHERE udn=? AND artist=? AND album_key != '')
                         AND {dedup}
                       GROUP BY {_localfs_album_group("t")}
                       ORDER BY album COLLATE NOCASE""",
                    (udn, udn, artist)).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT t.album, t.artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks t
                       WHERE t.udn=? AND t.artist=?
                         AND {dedup}
                       GROUP BY t.album
                       ORDER BY album COLLATE NOCASE""",
                    (udn, artist)).fetchall()
        return [dict(r) for r in rows]
    def browse_letter(self, udn: str, mode: str, letter: str,
                      offset: int = 0, limit: int = 100) -> dict:
        """
        Return paginated Artists, Albums or Tracks starting with `letter`.
        letter: 'A'..'Z', '0' (digits), or '#' (everything else).
        mode: 'artists' | 'albums' | 'tracks'
        Returns: {items, total, offset, limit, letter, mode}
        """
        if letter == "0":
            like, where_extra = None, "AND SUBSTR(UPPER({col}),1,1) BETWEEN '0' AND '9'"
        elif letter == "#":
            like, where_extra = None, (
                "AND SUBSTR(UPPER({col}),1,1) NOT BETWEEN 'A' AND 'Z' "
                "AND SUBSTR(UPPER({col}),1,1) NOT BETWEEN '0' AND '9'")
        else:
            like = letter.upper() + "%"
            where_extra = "AND UPPER({col}) LIKE ?"

        def _q(col, select, group_by=""):
            we = where_extra.format(col=col)
            params = [udn] + ([like] if like else [])
            cnt_q = f"SELECT COUNT(*) FROM (SELECT {col} FROM tracks WHERE udn=? {we} AND {col}!='' {group_by})"
            tot = conn.execute(cnt_q, params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT {select} FROM tracks
                    WHERE udn=? {we} AND {col}!=''
                    {group_by}
                    ORDER BY {col} COLLATE NOCASE
                    LIMIT ? OFFSET ?""",
                params + [limit, offset]).fetchall()
            return tot, rows

        with self._pool.read() as conn:
            if mode == "artists":
                total, rows = _q(
                    "artist",
                    "artist, COUNT(DISTINCT album) as album_count, COUNT(*) as track_count, MAX(art) as art",
                    "GROUP BY artist")
                items = [dict(r) for r in rows]
            elif mode == "albums" and _is_localfs(udn):
                # Folder-based grouping: one album = one folder. The
                # letter filter applies to the DISPLAY name, which is an
                # aggregate, so it moves from WHERE to HAVING.
                name        = _localfs_album_name("t")
                artist_expr = _localfs_album_artist("t")
                dedup       = _dedup_clause("t")
                having      = where_extra.format(col=name)
                params      = [udn] + ([like] if like else [])
                base = (f"FROM tracks t WHERE t.udn=? AND t.album_key!='' "
                        f"AND {dedup} GROUP BY {_localfs_album_group('t')} HAVING 1=1 {having}")
                total = conn.execute(
                    f"SELECT COUNT(*) FROM (SELECT t.album_key {base})",
                    params).fetchone()[0]
                rows = conn.execute(
                    f"""SELECT t.album_key,
                              {name} as album,
                              {artist_expr} as artist,
                              COUNT(*) as track_count, MAX(t.art) as art
                       {base}
                       ORDER BY album COLLATE NOCASE
                       LIMIT ? OFFSET ?""",
                    params + [limit, offset]).fetchall()
                items = [dict(r) for r in rows]
            elif mode == "albums":
                total, rows = _q(
                    "album",
                    """album,
                       CASE WHEN COUNT(DISTINCT artist)>1 THEN 'Various Artists'
                            ELSE MAX(artist) END as artist,
                       COUNT(*) as track_count, MAX(art) as art""",
                    "GROUP BY album")
                items = [dict(r) for r in rows]
            elif mode == "genres":
                total, rows = _q(
                    "genre",
                    "genre, COUNT(DISTINCT album) as album_count, COUNT(*) as track_count",
                    "GROUP BY genre")
                items = [dict(r) for r in rows]
            else:  # tracks
                total, rows = _q(
                    "title",
                    "obj_id as id, url, title, artist, album, duration, art, mime, genre, 'audio' as type",
                    "")
                items = [dict(r) for r in rows]

        return {"items": items, "total": total, "offset": offset,
                "limit": limit, "letter": letter, "mode": mode}
