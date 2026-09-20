#!/usr/bin/env python3
"""
dlna_library_artists.py — `ArtistsMixin`: the artist → MusicBrainz-id
store, and the worklist of artists still to resolve.

Split into its own mixin from birth (2026-09-20) rather than added to
`CollectionsMixin`, which had 31 lines of headroom: this table is the
anchor the rest of the artist-metadata work hangs off (biography,
birth/death, place, band line-up), so it grows.

**The invariant this module exists to protect**, shared with
album_art / play_counts / lyrics / book_meta: `clear(udn)` never touches
`artist_meta`. Each row costs a rate-limited MusicBrainz round-trip
(~1.2 s), so a full sweep of this library is about an hour. A
rebuild-index must not spend that again.

Identity is `dlna_mbid.norm_artist(artist)`, not the raw tag: the same
act is spelled 'The Bad Seeds' / 'the bad seeds' / 'Bad Seeds' across a
library, and they must share one row or the sweep asks MusicBrainz the
same question three times and can get three answers.

`source` ∈ {'tag', 'search', 'notfound', 'manual'}:
  * `tag`      — the file carried a musicbrainz_artistid. Free, exact.
  * `search`   — resolved by name, accepted by `dlna_mbid.accept_match`.
  * `notfound` — asked and refused. STICKY, so the next run skips it.
  * `manual`   — a person decided. **Never overwritten by automation.**
"""
from __future__ import annotations

import logging
import time

from dlna_mbid import norm_artist

log = logging.getLogger("dlna.library")

# Sentinels, not performers (see dlna_artist_infer): asking MusicBrainz
# about them burns the rate-limit budget and can only answer wrongly.
_NOT_PERFORMERS = ("", "Anon", "Various Artists")


class ArtistsMixin:
    """Mixed into `LibraryDB`; relies on `self._pool` from the host."""

    def artist_meta_set(self, artist: str, mbid: str | None,
                        source: str) -> bool:
        """Record what we know about an artist. Returns False when a
        `manual` row was protected from an automated write."""
        key = norm_artist(artist)
        if not key:
            return False
        with self._pool.write() as conn:
            if source != "manual":
                cur = conn.execute(
                    "SELECT source FROM artist_meta WHERE artist_key=?",
                    (key,)).fetchone()
                if cur and cur["source"] == "manual":
                    return False
            conn.execute(
                "INSERT INTO artist_meta "
                "  (artist_key, artist, mbid, source, fetched_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(artist_key) DO UPDATE SET "
                "  artist=excluded.artist, mbid=excluded.mbid, "
                "  source=excluded.source, fetched_at=excluded.fetched_at",
                (key, artist, mbid or None, source, int(time.time())))
        return True

    def artist_meta_get(self, artist: str):
        key = norm_artist(artist)
        if not key:
            return None
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT * FROM artist_meta WHERE artist_key=?",
                (key,)).fetchone()
        return dict(row) if row else None

    def artist_meta_all(self) -> list[dict]:
        with self._pool.read() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM artist_meta ORDER BY artist COLLATE NOCASE")]

    def one_file_path_for_artist(self, udn: str, artist: str) -> str:
        """Any readable file by this artist, for the free tag-reading
        phase of the sweep. Matched on the NORMALISED name so a browse
        spelling finds a tag spelling; rows with no `file_path` (UPnP
        sources) are skipped rather than returned empty, since one such
        row would otherwise hide a sibling that does have a file."""
        key = norm_artist(artist)
        if not key:
            return ""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT artist, file_path FROM tracks "
                " WHERE udn=? AND file_path<>'' ", (udn,)).fetchall()
        for r in rows:
            if norm_artist(r["artist"]) == key:
                return r["file_path"]
        return ""

    def artists_without_mbid(self, udn: str,
                             limit: int = 0) -> list[str]:
        """Artists in `udn` that have never been asked about.

        A row of ANY source counts as asked — including `notfound`,
        which is what makes a second run resume in minutes instead of
        re-spending the hour. Deliberately matched on the NORMALISED
        key, so three spellings of one act are one question."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT DISTINCT artist FROM tracks "
                " WHERE udn=? AND artist NOT IN (?,?,?) "
                " ORDER BY artist COLLATE NOCASE",
                (udn, *_NOT_PERFORMERS)).fetchall()
            seen = {r["artist_key"] for r in conn.execute(
                "SELECT artist_key FROM artist_meta")}
        out: list[str] = []
        for r in rows:
            name = r["artist"]
            key = norm_artist(name)
            if not key or key in seen:
                continue
            seen.add(key)          # collapse spelling variants within one run
            out.append(name)
            if limit and len(out) >= limit:
                break
        return out
