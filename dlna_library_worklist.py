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

from dlna_artist_infer import ANON_ARTIST, infer_artist
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
            rows = conn.execute(
                "SELECT url, album_key FROM tracks "
                " WHERE udn=? AND url<>'' "
                "   AND (COALESCE(artist,'')='' OR artist=?) "
                " ORDER BY file_path COLLATE NOCASE",
                (udn, ANON_ARTIST)).fetchall()
            # Which folders can name their own performer? Asked ONCE per
            # folder rather than per track — a folder is an album, so the
            # answer cannot differ between two tracks that share one.
            sibs: dict[str, list] = {}
            for k in {r["album_key"] for r in rows}:
                # `Anon` is the ABSENCE of an answer, so it must never
                # count as sibling evidence — a folder of Anon tracks
                # would otherwise reach "unanimity" and name them all Anon
                # forever, which is exactly the guess this avoids.
                sibs[k] = [x["artist"] for x in conn.execute(
                    "SELECT DISTINCT artist FROM tracks "
                    " WHERE udn=? AND album_key=? "
                    "   AND COALESCE(artist,'')<>'' AND artist<>?",
                    (udn, k, ANON_ARTIST)).fetchall()]
            pl = conn.execute(
                "SELECT id FROM playlists WHERE name=?",
                (UNKNOWN_ARTISTS_PLAYLIST,)).fetchone()

        # Only what NOTHING can attribute reaches the worklist. A track
        # whose folder names its performer is `tools/artist_from_folder.py`
        # work, not hand work, and both sides ask `infer_artist` so they
        # can never disagree about which is which.
        untagged = [r["url"] for r in rows
                    if not infer_artist(r["album_key"],
                                        sibs.get(r["album_key"]))]

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

        # Prune what is no longer OUTSTANDING — which is not the same as
        # "now has an artist". A track can leave the worklist two ways:
        # somebody tagged it, or inference improved and
        # `tools/artist_from_folder.py` can now do it. Pruning only the
        # first left 25 RVM and Mira Calvo rows stranded here after the
        # sweep was narrowed, so the rule is stated once, positively:
        # a row survives only while its track is still in `untagged`.
        want = set(untagged)
        with self._pool.read() as conn:
            mine = {r["url"] for r in conn.execute(
                "SELECT url FROM playlist_tracks p WHERE p.pl_id=? AND EXISTS ("
                "  SELECT 1 FROM tracks t WHERE t.udn=? AND t.url=p.url)",
                (pl_id, udn)).fetchall()}
            # A row pointing at NO track at all. In a playlist a person
            # curated that is a repair for `audit_playlist_orphans.py`,
            # never something to delete quietly — but this list is
            # generated, so a row whose file is gone is not outstanding
            # work, it is litter. Leaving them made the worklist read 44
            # items when 15 were real, right while somebody was working
            # through it.
            gone = {r["url"] for r in conn.execute(
                "SELECT url FROM playlist_tracks p WHERE p.pl_id=? "
                "  AND NOT EXISTS (SELECT 1 FROM tracks t WHERE t.url=p.url)",
                (pl_id,)).fetchall()}
        stale = sorted((mine - want) | gone)

        pruned = 0
        with self._pool.write() as conn:
            for i in range(0, len(stale), 400):
                batch = stale[i:i + 400]
                ph = ",".join("?" * len(batch))
                pruned += conn.execute(
                    f"DELETE FROM playlist_tracks WHERE pl_id=? "
                    f"AND url IN ({ph})", [pl_id, *batch]).rowcount or 0
        with self._pool.read() as conn:
            total = conn.execute(
                "SELECT COUNT(*) c FROM playlist_tracks WHERE pl_id=?",
                (pl_id,)).fetchone()["c"]

        if added or pruned:
            log.info(f"{UNKNOWN_ARTISTS_PLAYLIST}: +{added} -{pruned} "
                     f"→ {total} track(s) awaiting a hand-written artist")
        return {"added": added, "pruned": pruned, "total": total}
