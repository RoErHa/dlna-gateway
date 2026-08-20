#!/usr/bin/env python3
"""
dlna_library_schema.py — `SchemaMixin`: the CREATE TABLE / CREATE
INDEX DDL for every table LibraryDB owns, plus the Phase-A album-art
sibling backfill that runs at the tail of each index pass.

Split out of dlna_library.py (2026-08-20), which had grown to 2,912
lines and 95 methods across 11 unrelated responsibilities. This is a
MIXIN, not a collaborator: `LibraryDB` inherits it, so `self._pool` and
every sibling method resolve normally through the MRO and the public
`DB.<method>` API is byte-for-byte unchanged at all ~240 call sites.

`schema.sql` is the committed dump of what this file produces — after
ANY change here run `python3 tools/regen_schema.py`
(`tests/test_schema_sync.py` fails the suite otherwise).
"""
from __future__ import annotations

import logging
import sqlite3

from dlna_library_ddl import ADD_COLUMN_SQL, SCHEMA_DDL
from dlna_library_sql import (
    FAVOURITES_ID,
)

log = logging.getLogger("dlna.library")


class SchemaMixin:
    """See module docstring. Mixed into `LibraryDB`; never instantiated
    on its own — it relies on `self._pool` from the host class."""

    # ── Schema ────────────────────────────────────────────────────

    def _init_schema(self):
        self._pool.execute_script(SCHEMA_DDL)
        # ADD COLUMN migrations for pre-existing DBs (see dlna_library_ddl).
        for col_sql in ADD_COLUMN_SQL:
            try:
                with self._pool.write() as conn:
                    conn.execute(col_sql)
                log.info(f"DB migration: {col_sql[:60]}")
            except sqlite3.OperationalError as e:
                # Almost always "duplicate column name" — the migration is
                # idempotent by design. Anything ELSE here is a real schema
                # problem, so name it rather than assuming.
                if "duplicate column" not in str(e).lower():
                    log.warning(f"DB migration {col_sql[:60]!r} failed: {e}")
        # Loudness normalization removed (2026-05-31): peak-mode gain gave
        # negligible perceptual benefit, was already disabled in the
        # playback path, and broke bit-perfect on the browser path. Drop
        # the now-unused measurements table. Idempotent.
        try:
            with self._pool.write() as conn:
                conn.execute("DROP TABLE IF EXISTS track_loudness")
            log.info("DB migration: dropped track_loudness "
                     "(loudness normalization removed)")
        except sqlite3.Error as e:
            log.debug(f"track_loudness drop skipped ({e})")
        # 2026-05-25: widen tracks UNIQUE to include bit_depth + sample_rate
        # so 16-bit and 24-bit copies of the same album coexist without
        # collision. Detect old schema by absence of the bit_depth column;
        # if present, this migration is a no-op. Backfills the new columns
        # by parsing existing rows' URLs.
        with self._pool.write() as conn:
            self._migrate_widen_tracks_unique(conn)
        # 2026-05-25 follow-up: the widened UNIQUE accidentally let same-URL
        # duplicates accumulate. The indexer's INSERT OR IGNORE uses the
        # wider tuple, but same URL with raw-vs-corrected metadata becomes
        # two distinct tuples → both insert → ~18k phantom dup rows. Add
        # a UNIQUE(udn, url) index so URL uniqueness is enforced
        # independently. Deduplicate existing rows first (keep MIN(id) per
        # (udn,url) — those are the migration-inserted, AcoustID-corrected
        # ones, since the indexer-inserted dupes have higher AUTOINCREMENT
        # ids and raw metadata).
        with self._pool.write() as conn:
            self._migrate_unique_url(conn)
        # 2026-07-12: widen the tracks UNIQUE with album_key so distinct
        # files in different folders with identical tags coexist (the
        # completeness-audit fix). Table rebuild; drops all FTS triggers.
        with self._pool.write() as conn:
            self._migrate_widen_unique_album_key(conn)
        # 2026-07-12: AFTER UPDATE FTS sync trigger. Must run AFTER the
        # table-rebuild migrations above (they only recreate ai/ad; on a
        # legacy DB the trigger doesn't exist yet when they run).
        with self._pool.write() as conn:
            self._migrate_fts_update_trigger(conn)
        # Ensure Favourites always exists
        with self._pool.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO playlists (id, name, sort_order) "
                "VALUES (?,?,?)",
                (FAVOURITES_ID, "⭐ Favourites", -1))
            self._migrate_json(conn)
            self._migrate_device_roles(conn)
            self._migrate_album_fav_key(conn)
            # Covering index for the folder-grouped Albums browse query
            # (GROUP BY album_key within a udn). Removes the TEMP B-TREE the
            # GROUP BY otherwise builds — roughly halves the cold
            # /api/browse_letter albums query (~150 ms → ~70 ms). Created
            # here (after the table-rebuild migrations, so it isn't dropped)
            # and idempotent. NB: regenerate schema.sql when this changes —
            # tests/test_schema_sync.py enforces it.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_udn_album_key "
                         "ON tracks(udn, album_key)")
            # One-shot album-art backfill across all existing tracks. Cheap
            # and idempotent — harvest is INSERT OR IGNORE, backfill only
            # touches tracks whose art is empty AND whose album_art exists.
            harvested, filled = self._backfill_album_art(conn)
            if harvested or filled:
                log.info(f"album_art backfill: harvested={harvested} "
                         f"album(s), filled={filled} track(s)")
        log.debug(f"LibraryDB ready: {self._pool.db_file}")
    def _backfill_album_art(self, conn: sqlite3.Connection,
                            udn: str = "") -> tuple:
        """
        Harvest per-album cover art from tracks that have it into the
        album_art cache, then apply the cache onto sibling tracks of the
        same (artist, album) that don't.

        Idempotent. Safe to re-run on every index pass — INSERT OR IGNORE
        keeps the first art URL seen per album, and the UPDATE only fires
        on tracks whose art is empty.

        Pass udn to scope to a single server's tracks; pass "" to operate
        across all UDNs (used at startup migration).

        Returns (harvested_albums, filled_tracks) for logging.
        """
        udn_clause, params = ("", ())
        if udn:
            udn_clause = " AND udn = ?"
            params     = (udn,)

        before_rowid = conn.execute(
            "SELECT COALESCE(MAX(ROWID), 0) FROM album_art").fetchone()[0]
        conn.execute(f"""
            INSERT OR IGNORE INTO album_art (artist, album, art_url, source)
            SELECT artist, album, MIN(art), 'sibling'
              FROM tracks
             WHERE artist != '' AND album != '' AND art != ''{udn_clause}
             GROUP BY artist, album
        """, params)

        new_rows = conn.execute(
            "SELECT artist, album, source FROM album_art WHERE ROWID > ?",
            (before_rowid,)).fetchall()
        harvested = len(new_rows)
        scope = f"[{udn[:12]}…]" if udn else "[migration]"
        for r in new_rows:
            log.info(f"album-art ← harvest {scope} "
                     f"{r['artist']!r} / {r['album']!r} "
                     f"[source={r['source']}]")

        cur = conn.execute(f"""
            UPDATE tracks
               SET art = (
                   SELECT art_url FROM album_art
                    WHERE album_art.artist = tracks.artist
                      AND album_art.album  = tracks.album)
             WHERE (art IS NULL OR art = '')
               AND EXISTS (
                   SELECT 1 FROM album_art
                    WHERE album_art.artist = tracks.artist
                      AND album_art.album  = tracks.album){udn_clause}
        """, params)
        return harvested, cur.rowcount or 0
