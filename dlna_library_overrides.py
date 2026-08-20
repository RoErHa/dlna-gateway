#!/usr/bin/env python3
"""
dlna_library_overrides.py — `OverridesMixin`: the `metadata_overrides`
display layer — the user's / a tool's per-URL corrections, and the
per-track metadata read that merges them back over the file tags.

Split out of dlna_library_tracks.py (2026-08-20), which had reached 480
lines. `TracksMixin` INHERITS this mixin, so `LibraryDB`'s composition
and the public `DB.<method>` surface are unchanged.

The invariants this module exists to protect:
  * `source='manual'` always wins — a tool pass must never overwrite a
    hand edit; `notfound` / `video_skip` rows carry NULL metadata so
    they mask nothing.
  * `year` here is the MusicBrainz ORIGINAL release year and is
    DISPLAY-ONLY — it is never COALESCEd back into `tracks`, which
    holds the file-tag (edition) year.
"""
from __future__ import annotations

import logging
import sqlite3


log = logging.getLogger("dlna.library")


class OverridesMixin:
    """See module docstring. Mixed into `LibraryDB` via `TracksMixin`;
    never instantiated on its own — it relies on `self._pool` from the
    host class."""

    # Sentinel distinguishing "year omitted" from "year=None (clear it)".
    _YEAR_UNSET = object()

    def update_track_meta(self, url: str,
                          artist: str = None, album: str = None,
                          title: str = None, genre: str = None,
                          year=_YEAR_UNSET) -> bool:
        """
        Update artist/album/title/genre/year for a track in the DB.
        For string fields, only provided (non-None) values are changed.
        For year: pass `_YEAR_UNSET` (the default) to leave untouched,
        an int to set, or None to clear the override.
        All edits land in metadata_overrides with source='manual' so
        they survive re-index. Artist/album/title/genre are also
        pushed onto the live tracks row; year is NOT — tracks.year
        stays the file-tag year, the user's year sets the override.
        Returns True if any row was updated.
        """
        str_fields = {k: v for k, v in
                      [("artist", artist), ("album", album),
                       ("title", title), ("genre", genre)]
                      if v is not None}
        year_touched = year is not self._YEAR_UNSET
        if not str_fields and not year_touched:
            return False
        with self._pool.write() as conn:
            # 1. Live tracks row: only string fields go here (year on
            # tracks is the file-tag year, untouched by user edits).
            if str_fields:
                set_clause = ", ".join(f"{k}=?" for k in str_fields)
                conn.execute(
                    f"UPDATE tracks SET {set_clause} WHERE url=?",
                    list(str_fields.values()) + [url])
            # 2. metadata_overrides upsert (single source of truth).
            existing = conn.execute(
                "SELECT artist, album, title, genre, year "
                "FROM metadata_overrides WHERE url=?", (url,)).fetchone()
            if existing:
                merged = dict(existing)
                merged.update(str_fields)
                if year_touched:
                    merged["year"] = year
                conn.execute(
                    "UPDATE metadata_overrides "
                    "SET artist=?, album=?, title=?, genre=?, year=?, "
                    "    source='manual', updated_at=datetime('now') "
                    "WHERE url=?",
                    (merged["artist"], merged["album"], merged["title"],
                     merged["genre"], merged["year"], url))
            else:
                # Fill blanks from current track record
                row = conn.execute(
                    "SELECT artist, album, title, genre FROM tracks WHERE url=?",
                    (url,)).fetchone()
                base = dict(row) if row else {"artist":"","album":"","title":"","genre":""}
                base.update(str_fields)
                yr = year if year_touched else None
                conn.execute(
                    "INSERT INTO metadata_overrides "
                    "(url, artist, album, title, genre, year, source) "
                    "VALUES (?,?,?,?,?,?, 'manual')",
                    (url, base["artist"], base["album"],
                     base["title"], base["genre"], yr))

            changed = conn.execute("SELECT changes()").fetchone()[0]
        return changed > 0
    def metadata_override_set(self, url: str, source: str,
                              artist: str | None = None,
                              album: str | None = None,
                              title: str | None = None,
                              genre: str | None = None,
                              year: int | None = None,
                              update_tracks: bool = True) -> bool:
        """Write a metadata_overrides row from a non-user source (e.g.
        AcoustID match) AND apply it onto `tracks` so the UI sees it
        without waiting for the next re-index. Only non-None fields are
        carried through — a missing album stays as whatever `tracks`
        already had.

        `year` here is the **original release year** (MusicBrainz
        release-group's first-release-date); `tracks.year` is the
        file-tag/edition year. The two are kept as separate fields so
        the frontend can render "1987 (remastered)" when they differ.

        This method is for positive metadata; a 'notfound' sticky-negative
        row is written directly (INSERT OR IGNORE) where needed.

        Returns True if a row was written (always True for non-empty
        URLs)."""
        if not url:
            return False
        # Keep `year` separate from `fields` — it's stored in
        # metadata_overrides ONLY (it's the original release year), not
        # in `tracks` (where year means file-tag/edition year, a
        # different concept). The "push onto tracks" UPDATE below uses
        # `fields` which must NOT include year.
        fields = {k: v for k, v in
                  (("artist", artist), ("album", album),
                   ("title", title), ("genre", genre))
                  if v is not None and v != ""}
        override_year = year if (year is not None and year > 0) else None
        with self._pool.write() as conn:
            # Upsert into overrides preserving any non-overwritten fields.
            existing = conn.execute(
                "SELECT artist, album, title, genre, year FROM metadata_overrides "
                "WHERE url=?", (url,)).fetchone()
            if existing:
                merged = dict(existing)
                merged.update(fields)
                if override_year is not None:
                    merged["year"] = override_year
                conn.execute(
                    "UPDATE metadata_overrides "
                    "SET artist=?, album=?, title=?, genre=?, year=?, source=?, "
                    "    updated_at=datetime('now') WHERE url=?",
                    (merged["artist"], merged["album"], merged["title"],
                     merged["genre"], merged["year"], source, url))
            else:
                row = conn.execute(
                    "SELECT artist, album, title, genre FROM tracks WHERE url=?",
                    (url,)).fetchone()
                base = dict(row) if row else {"artist": "", "album": "",
                                              "title": "", "genre": ""}
                base.update(fields)
                conn.execute(
                    "INSERT INTO metadata_overrides "
                    "(url, artist, album, title, genre, year, source) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (url, base["artist"], base["album"], base["title"],
                     base["genre"], override_year, source))
            # Push the change onto tracks too — only the fields the caller
            # supplied; others are untouched. The Indexer's COALESCE pass
            # would do this on next re-index, but that may be weeks away.
            #
            # Catch UNIQUE(udn,artist,album,title) collisions: if two tracks
            # would end up with identical artist/album/title after this
            # update (e.g. AcoustID returned the same metadata for two
            # duplicate-uploads), the override row is still the source of
            # truth — we just can't push it onto `tracks` live. A future
            # re-index will skip it too (same constraint); user-visible
            # impact is that one of the duplicates keeps its old metadata
            # in the tracks list. Acceptable; flagged at WARN so it's
            # diagnosable.
            if fields and update_tracks:
                # Push the override onto the live tracks row so browse / search /
                # Subsonic see it immediately. (`update_tracks=False` is a
                # vestigial opt-out from the removed AcoustID bulk worker — no
                # current caller passes it.)
                set_clause = ", ".join(f"{k}=?" for k in fields)
                try:
                    conn.execute(
                        f"UPDATE tracks SET {set_clause} WHERE url=?",
                        list(fields.values()) + [url])
                except sqlite3.IntegrityError as e:
                    log.warning(
                        f"metadata_override_set: tracks UPDATE collided on "
                        f"UNIQUE(udn,artist,album,title) for {url[:80]} "
                        f"({e}); override row saved, tracks row unchanged")
        return True
    def track_meta_by_url(self, url: str):
        """Metadata for a single track, used by /api/track_meta and the
        Subsonic /rest/* methods. Returns both year fields separately:
          - `year`: file-tag year from DIDL-Lite (the edition you own)
          - `year_original`: MusicBrainz first-release-date year if the
            AcoustID worker has filled it in (the recording's original
            release year).
        Frontend prefers `year_original`; if it differs from `year` by
        more than 2 years, renders as e.g. '1987 (remastered)'."""
        with self._pool.read() as conn:
            row = conn.execute("""
                SELECT t.title, t.artist, t.album, t.duration, t.year,
                       m.year AS year_original
                  FROM tracks t
             LEFT JOIN metadata_overrides m ON m.url = t.url
                 WHERE t.url = ? LIMIT 1
            """, (url,)).fetchone()
        return dict(row) if row else None
