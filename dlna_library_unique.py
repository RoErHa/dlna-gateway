#!/usr/bin/env python3
"""
dlna_library_unique.py — `UniqueMigrationsMixin`: the three migrations
that WIDEN the `tracks` UNIQUE constraint, each by rebuilding the table.

Split out of dlna_library_migrations.py (2026-08-20), which had reached
460 lines — these three account for ~260 of them. `MigrationsMixin`
INHERITS this mixin, so `LibraryDB`'s composition and the order the
migrations run in (driven by `SchemaMixin._init_schema`) are unchanged.

They are grouped because they share one dangerous shape: a table rebuild
DROPs the FTS5 sync triggers, so each must recreate them, and each must
run BEFORE `_migrate_fts_update_trigger` adds `tracks_au` on top. Their
history, in order:
  * bit_depth + sample_rate joined the UNIQUE so a 16- and a 24-bit copy
    of one album coexist as distinct rows.
  * UNIQUE(udn, url) was added separately, because the widened tuple let
    same-URL raw-vs-corrected metadata insert twice (~18k phantom rows).
  * album_key joined it so two distinct files in different FOLDERS with
    identical tags both index (the 2026-07-12 completeness-audit fix).
"""
from __future__ import annotations

import logging
import re
import sqlite3

from dlna_library_sql import _parse_audio_params

log = logging.getLogger("dlna.library")


class UniqueMigrationsMixin:
    """See module docstring. Mixed into `LibraryDB` via `MigrationsMixin`;
    never instantiated on its own — it relies on `self._pool` from the
    host class."""

    def _migrate_widen_tracks_unique(self, conn: sqlite3.Connection):
        """One-time migration: widen `tracks` UNIQUE from
        (udn,artist,album,title) to (udn,artist,album,title,bit_depth,sample_rate)
        so 16/24-bit copies of the same album don't collide.

        Detection: if `tracks.bit_depth` column already exists, the
        migration has run. Otherwise: drop FTS triggers, rename old
        table, create new table with widened UNIQUE and the two new
        columns, INSERT-with-parsed-URLs into new, drop old, recreate
        triggers, rebuild FTS5 index. Idempotent.

        Heavy on row count but fast in practice (~100ms on 25k rows).
        Locks the DB briefly — acceptable at startup."""
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='tracks'").fetchone()
        if not sql_row:
            # Fresh DB — CREATE TABLE in _init_schema already used new schema.
            return
        if "bit_depth" in (sql_row[0] or ""):
            # Already migrated.
            return

        old_rows = conn.execute(
            "SELECT id, udn, obj_id, url, title, artist, album, duration, "
            "       art, mime, genre, file_path "
            "  FROM tracks").fetchall()
        n_old = len(old_rows)
        log.info(f"DB migration: widening tracks UNIQUE; rebuilding "
                 f"{n_old:,} row(s) with parsed bit_depth + sample_rate")

        # Triggers reference the table being renamed — drop first so
        # they don't end up pointing at the obsolete renamed table.
        conn.execute("DROP TRIGGER IF EXISTS tracks_ai")
        conn.execute("DROP TRIGGER IF EXISTS tracks_ad")
        conn.execute("ALTER TABLE tracks RENAME TO _tracks_pre_widen")
        conn.execute("""
            CREATE TABLE tracks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                udn         TEXT NOT NULL,
                obj_id      TEXT,
                url         TEXT NOT NULL,
                title       TEXT,
                artist      TEXT,
                album       TEXT,
                duration    TEXT,
                art         TEXT,
                mime        TEXT,
                genre       TEXT DEFAULT '',
                file_path   TEXT DEFAULT '',
                bit_depth   INTEGER,
                sample_rate INTEGER,
                album_key   TEXT DEFAULT '',
                UNIQUE(udn, artist, album, title, bit_depth, sample_rate)
            )
        """)
        inserts = []
        for r in old_rows:
            bd, sr = _parse_audio_params(r["url"])
            # album_key may predate this row if the column-add migration
            # ran first (it normally does); carry it, default '' otherwise.
            ak = r["album_key"] if "album_key" in r.keys() else ""
            inserts.append((r["id"], r["udn"], r["obj_id"], r["url"],
                            r["title"], r["artist"], r["album"],
                            r["duration"], r["art"], r["mime"],
                            r["genre"], r["file_path"], bd, sr, ak))
        conn.executemany(
            "INSERT OR IGNORE INTO tracks "
            "(id, udn, obj_id, url, title, artist, album, duration, art, "
            " mime, genre, file_path, bit_depth, sample_rate, album_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", inserts)
        n_new = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

        conn.execute("DROP TABLE _tracks_pre_widen")
        # Recreate the FTS5 sync triggers (they were dropped above).
        conn.execute("""
            CREATE TRIGGER tracks_ai
                AFTER INSERT ON tracks BEGIN
                    INSERT INTO tracks_fts(rowid, title, artist, album)
                    VALUES (new.id, new.title, new.artist, new.album);
                END
        """)
        conn.execute("""
            CREATE TRIGGER tracks_ad
                AFTER DELETE ON tracks BEGIN
                    INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                    VALUES ('delete', old.id, old.title, old.artist, old.album);
                END
        """)
        conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

        skipped = n_old - n_new
        log.info(f"DB migration: tracks rebuilt — {n_old:,} → {n_new:,} rows "
                 f"({skipped} skipped on new UNIQUE), FTS5 rebuilt")
    def _migrate_unique_url(self, conn: sqlite3.Connection):
        """Enforce `UNIQUE(udn, url)` so the indexer can't accumulate
        same-URL duplicates when a URL's raw vs AcoustID-corrected
        metadata differ. Pre-2026-05-25 the narrow UNIQUE collapsed
        these implicitly; after the bit_depth/sample_rate widening
        they survive as phantom dup rows. This migration:

          1. DELETEs duplicate rows, keeping the MIN(id) per (udn,url)
             (the original migration-inserted, AcoustID-corrected row).
          2. CREATEs UNIQUE INDEX idx_tracks_udn_url.

        Idempotent: detects the index by name and no-ops if present.
        FTS triggers are dropped during the delete (avoiding 18k trigger
        invocations) then recreated; FTS5 is rebuilt at the tail."""
        idx = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_tracks_udn_url'"
        ).fetchone()
        if idx:
            return

        n_dup = conn.execute("""
            SELECT COUNT(*) FROM tracks
             WHERE id NOT IN (
               SELECT MIN(id) FROM tracks GROUP BY udn, url
             )
        """).fetchone()[0]

        if n_dup > 0:
            log.info(f"DB migration: removing {n_dup:,} same-URL "
                     f"duplicate rows from tracks "
                     f"(keeping MIN(id) per (udn,url))")
            # Drop triggers so the FTS5 ad trigger doesn't fire per row.
            conn.execute("DROP TRIGGER IF EXISTS tracks_ai")
            conn.execute("DROP TRIGGER IF EXISTS tracks_ad")
            conn.execute("""
                DELETE FROM tracks
                 WHERE id NOT IN (
                   SELECT MIN(id) FROM tracks GROUP BY udn, url
                 )
            """)

        conn.execute(
            "CREATE UNIQUE INDEX idx_tracks_udn_url ON tracks(udn, url)")
        log.info("DB migration: added UNIQUE(udn, url) index")

        if n_dup > 0:
            # Recreate triggers + rebuild FTS5 from the surviving rows.
            conn.execute("""
                CREATE TRIGGER tracks_ai
                    AFTER INSERT ON tracks BEGIN
                        INSERT INTO tracks_fts(rowid, title, artist, album)
                        VALUES (new.id, new.title, new.artist, new.album);
                    END
            """)
            conn.execute("""
                CREATE TRIGGER tracks_ad
                    AFTER DELETE ON tracks BEGIN
                        INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                        VALUES ('delete', old.id, old.title, old.artist, old.album);
                    END
            """)
            conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

    def _migrate_widen_unique_album_key(self, conn: sqlite3.Connection):
        """2026-07-12: widen the tracks UNIQUE to include `album_key`.

        The 2026-07-12 completeness audit found 236 on-disk files with NO
        tracks row: the old UNIQUE(udn, artist, album, title, bit_depth,
        sample_rate) swallowed any file whose tag tuple collided with a
        file in ANOTHER folder — duplicate-edition folders (deluxe vs
        standard) and scene compilations sharing tag tuples. Those are
        genuinely distinct files; for LocalFs every physical file deserves
        a row (per-file URL uniqueness is idx_tracks_udn_url's job, and
        browse-level dupe hiding is _dedup_clause's job — the UNIQUE was
        doing display work at the wrong layer). Adding the FOLDER identity
        to the UNIQUE admits them; UPnP rows carry album_key='' so their
        collision semantics are byte-identical to before.

        Table-rebuild migration, same shape as _migrate_widen_tracks_unique.
        Detects by inspecting the UNIQUE clause in sqlite_master (the
        album_key COLUMN exists on old DBs — only the constraint changes).
        Recreates idx_tracks_udn_url + idx_tracks_udn_album_key (dropped
        with the old table) and the ai/ad triggers; tracks_au is left to
        _migrate_fts_update_trigger, which runs right after and does the
        FTS rebuild on (re)creating it."""
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='tracks'").fetchone()
        if not sql_row:
            return
        m = re.search(r"UNIQUE\s*\(([^)]*)\)", sql_row[0] or "")
        if m and "album_key" in m.group(1):
            return  # already widened (or fresh DB)

        n_old = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        log.info(f"DB migration: widening tracks UNIQUE with album_key; "
                 f"rebuilding {n_old:,} row(s)")

        conn.execute("DROP TRIGGER IF EXISTS tracks_ai")
        conn.execute("DROP TRIGGER IF EXISTS tracks_ad")
        conn.execute("DROP TRIGGER IF EXISTS tracks_au")
        conn.execute("ALTER TABLE tracks RENAME TO _tracks_pre_akwiden")
        conn.execute("""
            CREATE TABLE tracks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                udn         TEXT NOT NULL,
                obj_id      TEXT,
                url         TEXT NOT NULL,
                title       TEXT,
                artist      TEXT,
                album       TEXT,
                duration    TEXT,
                art         TEXT,
                mime        TEXT,
                genre       TEXT DEFAULT '',
                file_path   TEXT DEFAULT '',
                bit_depth   INTEGER,
                sample_rate INTEGER,
                year        INTEGER,
                album_key   TEXT DEFAULT '',
                UNIQUE(udn, artist, album, title, album_key, bit_depth, sample_rate)
            )
        """)
        # Column list built against what the OLD table actually has: a DB
        # that just went through _migrate_widen_tracks_unique in this same
        # boot is missing `year` (that rebuild predates the column and
        # drops it; the ALTER loop re-adds it only on the NEXT boot).
        old_cols = {r[1] for r in
                    conn.execute("PRAGMA table_info(_tracks_pre_akwiden)")}
        # album_key backfills as '' (NULLs are DISTINCT in UNIQUE — a NULL
        # would exempt those rows from collision checks entirely).
        select_cols = ", ".join(
            c if c in old_cols
            else ("'' AS album_key" if c == "album_key" else f"NULL AS {c}")
            for c in self._TRACK_COLS.split(", "))
        conn.execute(
            f"INSERT OR IGNORE INTO tracks ({self._TRACK_COLS}) "
            f"SELECT {select_cols} FROM _tracks_pre_akwiden")
        conn.execute("DROP TABLE _tracks_pre_akwiden")
        # Indexes were attached to the renamed table — recreate both.
        # (_migrate_unique_url already no-opped this boot on seeing the old
        # index, and the album_key index's IF NOT EXISTS runs later; doing
        # them here keeps this migration self-contained.)
        conn.execute(
            "CREATE UNIQUE INDEX idx_tracks_udn_url ON tracks(udn, url)")
        conn.execute(
            "CREATE INDEX idx_tracks_udn_album_key ON tracks(udn, album_key)")
        conn.execute("""
            CREATE TRIGGER tracks_ai
                AFTER INSERT ON tracks BEGIN
                    INSERT INTO tracks_fts(rowid, title, artist, album)
                    VALUES (new.id, new.title, new.artist, new.album);
                END
        """)
        conn.execute("""
            CREATE TRIGGER tracks_ad
                AFTER DELETE ON tracks BEGIN
                    INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                    VALUES ('delete', old.id, old.title, old.artist, old.album);
                END
        """)
        conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

        n_new = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        log.info(f"DB migration: tracks UNIQUE widened with album_key — "
                 f"{n_old:,} → {n_new:,} rows, FTS5 rebuilt")
