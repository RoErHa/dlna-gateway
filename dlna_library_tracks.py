#!/usr/bin/env python3
"""
dlna_library_tracks.py — `TracksMixin`: writing and reading the
`tracks` table — the indexer's upsert path, per-URL metadata lookups,
and the `metadata_overrides` display layer.

Split out of dlna_library.py (2026-08-20). See dlna_library_schema.py
for why these are mixins rather than collaborators.

Two invariants worth keeping in view when editing:
  * `upsert_tracks` dedups AssetUPnP virtual-album aliases by
    (d_id, _norm_title(title)) — d-id alone is NOT a per-file id and
    collides across ~41% of the library.
  * `metadata_overrides.source='manual'` always wins; `year` there is
    the MusicBrainz ORIGINAL year and is display-only — it is never
    COALESCEd back into `tracks`.
"""
from __future__ import annotations

import logging
import sqlite3

from dlna_library_sql import (
    _d_id,
    _dur_to_secs,
    _is_localfs,
    _norm_title,
    _parse_audio_params,
)

log = logging.getLogger("dlna.library")


class TracksMixin:
    """See module docstring. Mixed into `LibraryDB`; never instantiated
    on its own — it relies on `self._pool` from the host class."""

    # Sentinel distinguishing "year omitted" from "year=None (clear it)".
    _YEAR_UNSET = object()

    # ── Track index ───────────────────────────────────────────────

    def track_count(self, udn: str) -> int:
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE udn=?", (udn,)).fetchone()
            return row[0] if row else 0
    def album_count(self, udn: str) -> int:
        """Number of albums for the source's browse view. LocalFs counts
        distinct FOLDERS (album_key) — matching the folder-grouped Albums
        list; other sources count distinct (artist, album) pairs, matching
        AssetUPnP's display count."""
        with self._pool.read() as conn:
            if _is_localfs(udn):
                row = conn.execute(
                    "SELECT COUNT(DISTINCT album_key) FROM tracks "
                    "WHERE udn=? AND album_key != ''", (udn,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) FROM "
                    "(SELECT DISTINCT artist, album FROM tracks WHERE udn=?)",
                    (udn,)).fetchone()
            return row[0] if row else 0
    def upsert_tracks(self, udn: str, tracks: list) -> int:
        """
        Insert tracks, deduplicating on (udn, d_id, lower(title)) where
        d_id is the `d-<n>` AssetUPnP URL segment.

        Why d_id+title and not just url: AssetUPnP exposes the SAME
        physical file under multiple "browse-tree paths" — both the
        real album (e.g. "Kasabian") AND any compilation albums the
        user has set up ("Music From the OC: Mix 5"). Each path gets
        a different `co-<hash>` segment in the URL, but the `d-<n>`
        part stays the same. Without this dedup, the index sees the
        same file 2x and the row count balloons (confirmed 2026-05-28:
        22k physical files → 40k rows). HTTP HEAD of the duplicate
        URLs confirms byte-identical Content-Length on both sides.

        Dedup is in Python (within-batch) and against any pre-existing
        rows for this UDN. URLs without a recognisable d-id (non-
        AssetUPnP servers) fall through to the wide UNIQUE constraint
        and never trigger d-id dedup. The FIRST URL seen wins; later
        aliases for the same (d_id, title) are skipped.

        Returns number of rows actually inserted.
        """
        if not tracks:
            return 0
        # Parse bit_depth + sample_rate from the URL at row-build time.
        # AssetUPnP URLs include `/b<bits>/f<rate>/`; non-AssetUPnP
        # servers usually don't, in which case both stay None and the
        # UNIQUE treats NULLs as distinct (so no cross-server collisions).
        # year is the file-tag year (DIDL-Lite dc:date / upnp:originalTrackDate),
        # parsed in dlna_content._parse_didl.
        def _make_row(t: dict) -> dict:
            url = t.get("url", "")
            # bit_depth + sample_rate: prefer caller-supplied values
            # (LocalFsProvider reads them straight from the audio
            # container via mutagen) and fall back to the AssetUPnP
            # URL-pattern parser. UPnP items don't have the fields →
            # URL parse; LocalFs items do → mutagen wins.
            bd_in = t.get("bit_depth")
            sr_in = t.get("sample_rate")
            bd_url, sr_url = _parse_audio_params(url)
            return {
                "udn": udn,
                "obj_id": t.get("id", ""),
                "url": url,
                "title": t.get("title", ""),
                "artist": t.get("artist", ""),
                "album": t.get("album", ""),
                "duration": t.get("duration", ""),
                "art": t.get("art", ""),
                "mime": t.get("mime", ""),
                "genre": t.get("genre", ""),
                "file_path": t.get("file_path", ""),
                "bit_depth": bd_in if bd_in is not None else bd_url,
                "sample_rate": sr_in if sr_in is not None else sr_url,
                "year": t.get("year"),
                "album_key": t.get("album_key", ""),
            }
        rows_raw = [_make_row(t) for t in tracks if t.get("url")]
        # Mass INSERTs fire the FTS triggers; heal-and-retry on the
        # recurring shadow-table corruption. Body is retry-safe
        # (INSERT OR IGNORE / UPDATE OR IGNORE throughout).
        return self.run_with_fts_heal(self._upsert_tracks_body, udn, rows_raw)
    def _upsert_tracks_body(self, udn: str, rows_raw: list) -> int:
        with self._pool.write() as conn:
            # Build the (d_id, _norm_title(title)) dedup set: existing
            # rows for this UDN + within-batch tracking. Non-AssetUPnP
            # URLs have d_id=None and are NOT deduped this way — they
            # fall through to the wider UNIQUE constraint.
            #
            # The post-COALESCE-mismatch race that motivated an earlier
            # override-aware path is already resolved by _norm_title's
            # apostrophe/diacritic normalisation: the new raw title and
            # the existing post-COALESCE title both normalise to the
            # same key. Considering the override title here would over-
            # collapse legitimately-distinct recordings that happen to
            # share a d-id (e.g. 3 Doors Down "Be Like That" vs
            # "Be Like That (acoustic)"), so we don't.
            seen: set[tuple[str, str]] = set()
            for (existing_url, existing_title) in conn.execute(
                "SELECT url, title FROM tracks WHERE udn=?", (udn,)
            ).fetchall():
                d = _d_id(existing_url)
                if d:
                    seen.add((d, _norm_title(existing_title)))

            rows: list[dict] = []
            n_aliased = 0
            for r in rows_raw:
                d = _d_id(r["url"])
                if d:
                    key = (d, _norm_title(r["title"]))
                    if key in seen:
                        n_aliased += 1
                        continue
                    seen.add(key)
                rows.append(r)
            if n_aliased:
                log.info(f"upsert_tracks [{udn[:12]}…]: dropped "
                         f"{n_aliased} alias row(s) "
                         f"(same d-id + title via different browse path)")

            # Step 1: insert new tracks (skip duplicates)
            conn.executemany(
                "INSERT OR IGNORE INTO tracks "
                "(udn, obj_id, url, title, artist, album, duration, art, "
                " mime, genre, file_path, bit_depth, sample_rate, year, "
                " album_key) "
                "VALUES (:udn,:obj_id,:url,:title,:artist,:album,:duration,"
                "        :art,:mime,:genre,:file_path,:bit_depth,:sample_rate,"
                "        :year,:album_key)",
                rows)
            inserted = conn.execute("SELECT changes()").fetchone()[0]
            # Step 2a: refresh metadata on already-indexed URLs. Step 1's
            # INSERT OR IGNORE swallows rows whose URL already exists, and
            # before 2026-07-12 only genre/art were then patched — so
            # in-place retagging (beets) was invisible to any rescan and
            # the workaround was DELETE FROM tracks + rebuild. LocalFs
            # URLs are path-based (sha1(rel_path)), stable across retags,
            # so (udn, url) is the right key. The change-guard keeps this
            # a no-op for untouched rows (no FTS trigger churn on a force
            # rescan); IS NOT is the null-safe comparison for the
            # nullable year/bit_depth/sample_rate. Incoming-empty genre
            # and art never blank an existing value (art may have been
            # backfilled from album_art; genre from an override). OR
            # IGNORE tolerates the wide-UNIQUE collision — same trade-off
            # as the overrides pass below: the colliding row keeps its
            # old metadata.
            refresh_cur = conn.executemany(
                "UPDATE OR IGNORE tracks SET "
                "  obj_id=:obj_id, title=:title, artist=:artist, "
                "  album=:album, duration=:duration, mime=:mime, "
                "  year=:year, bit_depth=:bit_depth, "
                "  sample_rate=:sample_rate, album_key=:album_key, "
                "  file_path=:file_path, "
                "  genre = CASE WHEN :genre != '' THEN :genre ELSE genre END, "
                "  art   = CASE WHEN :art   != '' THEN :art   ELSE art   END "
                "WHERE udn=:udn AND url=:url "
                "  AND (obj_id IS NOT :obj_id OR title IS NOT :title "
                "       OR artist IS NOT :artist OR album IS NOT :album "
                "       OR duration IS NOT :duration OR mime IS NOT :mime "
                "       OR year IS NOT :year OR bit_depth IS NOT :bit_depth "
                "       OR sample_rate IS NOT :sample_rate "
                "       OR album_key IS NOT :album_key "
                "       OR file_path IS NOT :file_path "
                "       OR (:genre != '' AND genre IS NOT :genre) "
                "       OR (:art != '' AND art IS NOT :art))",
                rows)
            refreshed = max(refresh_cur.rowcount, 0)
            if refreshed:
                log.info(f"upsert_tracks [{udn[:12]}…]: refreshed metadata "
                         f"on {refreshed} existing row(s)")
            # Step 2b: update genre + art on already-indexed tracks keyed
            # by identity (covers the UPnP case where the same file
            # arrives via a different URL; only fills empty genre)
            conn.executemany(
                "UPDATE tracks SET genre=:genre, art=:art "
                "WHERE udn=:udn AND artist=:artist AND album=:album AND title=:title "
                "  AND (genre='' OR genre IS NULL)",
                rows)
            # Apply any saved metadata overrides (survive re-index).
            # OR IGNORE tolerates UNIQUE(udn,artist,album,title) collisions:
            # the AcoustID worker may have resolved two different track URLs
            # to the same metadata (duplicate uploads, 16-bit + 24-bit pairs,
            # compilation appearances). Without OR IGNORE a SINGLE colliding
            # row aborts the entire UPDATE → indexer crashes → tracks table
            # stays empty after clear(). User-visible impact of the IGNORE:
            # one of the colliding duplicates keeps its pre-override metadata
            # in the tracks row. That's identical to the live-update path in
            # LibraryDB.metadata_override_set (which catches IntegrityError
            # for the same reason).
            conn.execute("""
                UPDATE OR IGNORE tracks SET
                    artist    = COALESCE((SELECT artist FROM metadata_overrides WHERE url=tracks.url), artist),
                    album     = COALESCE((SELECT album  FROM metadata_overrides WHERE url=tracks.url), album),
                    title     = COALESCE((SELECT title  FROM metadata_overrides WHERE url=tracks.url), title),
                    genre     = COALESCE((SELECT genre  FROM metadata_overrides WHERE url=tracks.url), genre)
                WHERE udn=?
                  AND url IN (SELECT url FROM metadata_overrides)
            """, (udn,))

            # Harvest new album art from this index pass and backfill any
            # sibling tracks that ended up without it. The album_art cache
            # means re-indexing never loses art we've already resolved.
            harvested, filled = self._backfill_album_art(conn, udn=udn)
            if harvested or filled:
                log.info(f"album_art [{udn[:12]}…]: harvested={harvested} "
                         f"album(s), filled={filled} track(s)")

            inserted = inserted  # already captured above
        return inserted
    def clear(self, udn: str):
        """
        Wipe track index for this UDN. Playlists untouched.
        Forces FTS5 rebuild so shadow tables are clean.

        The mass DELETE fires the FTS delete triggers row-by-row — on a
        corrupted tracks_fts that raises "database disk image is
        malformed" before the rebuild line is ever reached (the 5th/6th
        real-world occurrences, 2026-07-03). Routed through
        run_with_fts_heal so the corruption self-heals.
        """
        self.run_with_fts_heal(self._clear_body, udn)
        log.info(f"Track index cleared for {udn}")
    def _clear_body(self, udn: str):
        with self._pool.write() as conn:
            conn.execute("DELETE FROM tracks WHERE udn=?", (udn,))
            conn.execute("DELETE FROM index_meta WHERE udn=?", (udn,))
            conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")
    def mark_indexed(self, udn: str):
        with self._pool.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (udn, indexed_at) "
                "VALUES (?, datetime('now'))", (udn,))

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
    def get_track_file_path(self, url: str) -> str:
        """Return stored file_path for a track URL, or empty string."""
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT file_path FROM tracks WHERE url=?", (url,)).fetchone()
        return (row["file_path"] or "") if row else ""
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
    def track_by_url(self, url: str) -> dict | None:
        """Full tracks row for one URL — the Subsonic bookmark methods
        need the complete song shape (album_key, mime, art, …), not the
        display subset track_meta_by_url returns."""
        if not url:
            return None
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT udn, obj_id AS id, url, title, artist, album, "
                "       album_key, duration, art, mime, genre, file_path "
                "FROM tracks WHERE url = ? LIMIT 1", (url,)).fetchone()
        return dict(row) if row else None
    def tracks_to_m3u(self, tracks: list,
                      output_path: str = "/tmp/dlna-gw-current.m3u") -> str:
        """Write a list of track dicts to an M3U file. Returns the path."""
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for t in tracks:
                dur  = t.get("duration", "")
                secs = _dur_to_secs(dur)
                f.write(f"#EXTINF:{secs},{t.get('title','')}\n{t['url']}\n")
        return output_path
