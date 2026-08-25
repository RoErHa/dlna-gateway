#!/usr/bin/env python3
"""
dlna_library_worklist.py — `WorklistMixin`: the "- Unknown Artists -"
playlist, the one place the gateway admits it cannot do a job in code
and hands it to a person.

Split from dlna_library_collections.py (2026-08-25), which the sweep
pushed past the 400-line target. It sits between `CollectionsMixin` and
`RadioFavouritesMixin` in the MRO and, like every collection here, keeps
the invariant that `clear(udn)` never touches it: the outstanding work
must outlive a re-index, or a rebuild silently discards the list of
files somebody was halfway through tagging.
"""
from __future__ import annotations

import logging

from dlna_library_radio import RadioFavouritesMixin
from dlna_library_sql import UNKNOWN_ARTISTS_PLAYLIST

log = logging.getLogger("dlna.library")


class WorklistMixin(RadioFavouritesMixin):
    """See module docstring. Mixed into `LibraryDB` via
    `CollectionsMixin`; relies on `self._pool` and on the `pl_*` methods
    from the host class."""

    def sync_unknown_artist_playlist(self, udn: str) -> dict:
        """Keep the "- Unknown Artists -" worklist in step with `udn`.

        A track the indexer could not attribute is not a browse problem
        that can be solved in code — `tools/tag_from_filename.py` reads
        what a filename will give up, and past that point only a person
        knows who the performer is. So the gateway stops guessing and
        hands them over: every artist-less track lands in one playlist to
        be tagged by hand.

        It is a WORKLIST, so it syncs both ways — newly-untagged tracks are
        added, and a row whose track has since gained an artist is pruned
        because that work is done. The consequence worth knowing: removing
        a row by hand does NOT make it stay gone, since the next scan still
        sees a track with no artist. Give the file any artist tag at all to
        settle it.

        Rows are only ever pruned when they map to a CURRENT track of this
        `udn` that now has an artist. A row pointing at nothing is left for
        `tools/audit_playlist_orphans.py`, and a row belonging to another
        source is never touched — this must not become a second, silent
        thing that edits playlists.

        Returns `{'added': n, 'pruned': n, 'total': n}`. Creates the
        playlist only when there is something to put in it, so a fully
        tagged library never grows an empty one."""
        with self._pool.read() as conn:
            untagged = [r["url"] for r in conn.execute(
                "SELECT url FROM tracks "
                " WHERE udn=? AND COALESCE(artist,'')='' AND url<>'' "
                " ORDER BY file_path COLLATE NOCASE", (udn,)).fetchall()]
            pl = conn.execute(
                "SELECT id FROM playlists WHERE name=?",
                (UNKNOWN_ARTISTS_PLAYLIST,)).fetchone()

        pl_id = pl["id"] if pl else ""
        if not pl_id and not untagged:
            return {"added": 0, "pruned": 0, "total": 0}
        if not pl_id:
            pl_id = self.pl_create(UNKNOWN_ARTISTS_PLAYLIST)

        with self._pool.read() as conn:
            have = {r["url"] for r in conn.execute(
                "SELECT url FROM playlist_tracks WHERE pl_id=?",
                (pl_id,)).fetchall()}

        added = 0
        for url in untagged:
            if url in have:
                continue
            with self._pool.read() as conn:
                t = conn.execute(
                    "SELECT url, title, artist, album, duration, art "
                    "  FROM tracks WHERE udn=? AND url=?",
                    (udn, url)).fetchone()
            if not t:
                continue
            row = dict(t)
            # These carry no album tag either; name the folder so the row
            # is identifiable while editing. Playlist copy only — nothing
            # here is written back to `tracks` or to the file.
            row["album"] = row.get("album") or "Unknown Album"
            if self.pl_add_track(pl_id, row) == "added":
                added += 1

        with self._pool.write() as conn:
            pruned = conn.execute(
                "DELETE FROM playlist_tracks "
                " WHERE pl_id=? AND url IN ("
                "   SELECT url FROM tracks "
                "    WHERE udn=? AND COALESCE(artist,'')<>'')",
                (pl_id, udn)).rowcount or 0
            total = conn.execute(
                "SELECT COUNT(*) c FROM playlist_tracks WHERE pl_id=?",
                (pl_id,)).fetchone()["c"]

        if added or pruned:
            log.info(f"{UNKNOWN_ARTISTS_PLAYLIST}: +{added} -{pruned} "
                     f"→ {total} track(s) awaiting a hand-written artist")
        return {"added": added, "pruned": pruned, "total": total}
