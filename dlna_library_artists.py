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

    # Columns artist_info_set may write. Deliberately an ALLOWLIST:
    # `mbid` and `source` describe how the artist was RESOLVED, not
    # where the biography came from, and a fetch that rewrote them
    # would silently turn a hand-made 'manual' decision into 'search'.
    _INFO_COLS = ("mb_type", "gender", "born", "died", "birth_place",
                  "country", "genres", "disambiguation", "bio", "bio_url",
                  "image_url", "notable", "top_tracks")

    def artist_info_set(self, artist: str, fields: dict) -> bool:
        """Store the display facts for an artist already in the table.

        `meta_fetched_at` is stamped here and is what
        `artists_needing_info` reads, so a sweep resumes rather than
        re-spending the API budget."""
        key = norm_artist(artist)
        if not key:
            return False
        cols = [c for c in self._INFO_COLS if c in fields]
        sets = ", ".join(f"{c}=?" for c in cols) + ", meta_fetched_at=?"
        vals = [fields[c] for c in cols] + [int(time.time()), key]
        with self._pool.write() as conn:
            cur = conn.execute(
                f"UPDATE artist_meta SET {sets} WHERE artist_key=?", vals)
        return cur.rowcount > 0

    def artist_info_get(self, artist: str):
        return self.artist_meta_get(artist)

    def artist_members_set(self, artist: str, members: list) -> int:
        """REPLACE this artist's line-up. A re-fetch is a SYNC, not a
        merge: a member MusicBrainz has since corrected away must
        disappear, or the line-up only ever grows. Same semantics as
        tools/immich_people_sync.py."""
        key = norm_artist(artist)
        if not key:
            return 0
        now = int(time.time())
        rows = [(key, (m.get("name") or "").strip(), m.get("mbid") or "",
                 m.get("instruments") or "", m.get("begin") or "",
                 m.get("end") or "", now)
                for m in (members or []) if (m.get("name") or "").strip()]
        with self._pool.write() as conn:
            conn.execute("DELETE FROM artist_members WHERE artist_key=?",
                         (key,))
            conn.executemany(
                "INSERT OR REPLACE INTO artist_members "
                "(artist_key, member_name, member_mbid, instruments, "
                " begin_date, end_date, updated_at) VALUES (?,?,?,?,?,?,?)",
                rows)
        return len(rows)

    def artist_members_get(self, artist: str) -> list[dict]:
        """The stored line-up, oldest joiner first — which is also the
        order a reader expects to meet a band in."""
        key = norm_artist(artist)
        if not key:
            return []
        with self._pool.read() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM artist_members WHERE artist_key=? "
                " ORDER BY begin_date, member_name", (key,))]

    def artist_track_count(self, artist: str) -> int:
        """How many tracks this artist has, matched on the NORMALISED
        name so a browse spelling counts a tag spelling's rows too."""
        key = norm_artist(artist)
        if not key:
            return 0
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT artist, COUNT(*) n FROM tracks "
                " WHERE artist <> '' GROUP BY artist").fetchall()
        return sum(r["n"] for r in rows if norm_artist(r["artist"]) == key)

    def artists_needing_info(self, limit: int = 0) -> list[str]:
        """Artists with an mbid but no metadata fetched yet. A row
        without an mbid is skipped — there is nothing to fetch with."""
        sql = ("SELECT artist FROM artist_meta "
               " WHERE mbid IS NOT NULL AND mbid <> '' "
               "   AND (meta_fetched_at IS NULL OR meta_fetched_at = 0) "
               " ORDER BY artist COLLATE NOCASE")
        if limit:
            sql += f" LIMIT {int(limit)}"
        with self._pool.read() as conn:
            return [r["artist"] for r in conn.execute(sql)]

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
