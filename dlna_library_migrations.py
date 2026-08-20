#!/usr/bin/env python3
"""
dlna_library_migrations.py — `MigrationsMixin`: the in-place schema
migrations LibraryDB runs at construction, plus the FTS5 repair/heal
machinery.

Split out of dlna_library.py (2026-08-20). See dlna_library_schema.py
for why these are mixins rather than collaborators.

⚠ These run on IMPORT of dlna_library (`DB = LibraryDB()` is a
module-level singleton), so merely importing the module — from a test,
a tool, or a REPL in the repo dir — applies every pending migration to
the real `library.db`, even while the gateway is live. Safe by design
(idempotent, WAL, per-call pool connections) but surprising.

`run_with_fts_heal` is the single heal implementation wrapping every
mass-write path; the FTS5 shadow-table corruption it recovers from has
recurred 6+ times since Apr 2026.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3

from dlna_library_unique import UniqueMigrationsMixin

log = logging.getLogger("dlna.library")


class MigrationsMixin(UniqueMigrationsMixin):
    """See module docstring. Mixed into `LibraryDB`; never instantiated
    on its own — it relies on `self._pool` from the host class."""

    _TRACK_COLS = ("id, udn, obj_id, url, title, artist, album, duration, "
                   "art, mime, genre, file_path, bit_depth, sample_rate, "
                   "year, album_key")

    def _migrate_fts_update_trigger(self, conn: sqlite3.Connection):
        """2026-07-12: add `tracks_au`, the AFTER UPDATE FTS sync trigger.

        The original schema had only tracks_ai/tracks_ad, so ANY in-place
        UPDATE of title/artist/album left tracks_fts stale until the next
        full rebuild — the metadata-refresh pass in upsert_tracks (the
        retagged-file fix this trigger ships with), the overrides COALESCE
        pass, and metadata_override_set's live push all desynced it.
        Scoped to the three indexed columns so genre/art updates and the
        LocalFs URL-heal don't churn FTS.

        One-time on existing DBs: rebuilds FTS after creating the trigger
        to flush any desync accumulated before it existed — an external-
        content 'delete' whose old values don't match the indexed ones
        corrupts the index rather than cleaning it, so the trigger must
        start from a known-good state. Idempotent (detects by name)."""
        if conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name='tracks_au'").fetchone():
            return
        conn.execute("""
            CREATE TRIGGER tracks_au
                AFTER UPDATE OF title, artist, album ON tracks BEGIN
                    INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                    VALUES ('delete', old.id, old.title, old.artist, old.album);
                    INSERT INTO tracks_fts(rowid, title, artist, album)
                    VALUES (new.id, new.title, new.artist, new.album);
                END
        """)
        conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")
        log.info("DB migration: added tracks_au FTS update trigger; "
                 "FTS5 rebuilt")
    def _migrate_json(self, conn: sqlite3.Connection):
        """One-time import from old playlists.json if present."""
        old = os.path.join(os.path.dirname(self._pool.db_file), "playlists.json")
        if not os.path.exists(old):
            return
        try:
            with open(old, encoding="utf-8") as f:
                data = json.load(f)
            for pl_id, pl in data.items():
                conn.execute(
                    "INSERT OR IGNORE INTO playlists (id, name) VALUES (?,?)",
                    (pl_id, pl.get("name", pl_id)))
                for t in pl.get("tracks", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO playlist_tracks "
                        "(pl_id, url, title, artist, album, duration, art) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (pl_id, t.get("url",""), t.get("title",""),
                         t.get("artist",""), t.get("album",""),
                         t.get("duration",""), t.get("art","")))
            conn.commit()
            os.rename(old, old + ".migrated")
            log.info("Migrated playlists.json → SQLite")
        except Exception as e:
            log.warning(f"JSON playlist migration: {e}")
    def _migrate_device_roles(self, conn: sqlite3.Connection):
        """Add columns to device_roles if upgrading from older schema."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(device_roles)")}
        if "location" not in cols:
            conn.execute("ALTER TABLE device_roles ADD COLUMN location TEXT")
            conn.commit()
            log.info("DB migration: added location column to device_roles")
        if "host" not in cols:
            conn.execute("ALTER TABLE device_roles ADD COLUMN host TEXT")
            conn.commit()
            log.info("DB migration: added host column to device_roles")
    def _migrate_album_fav_key(self, conn: sqlite3.Connection):
        """Add `album_key` to album_favourites and widen the PK to
        (artist, album, album_key) so a LocalFs compilation can be
        favourited by FOLDER. Many comps share artist='Various Artists'
        plus a repeatable display name, so (artist, album) alone would
        collide. Idempotent: detects the column's absence; rebuilds the
        table (it's tiny), carrying existing rows forward with
        album_key=''. SQLite can't ALTER a PRIMARY KEY in place, hence the
        rebuild."""
        cols = {row[1] for row in
                conn.execute("PRAGMA table_info(album_favourites)")}
        if not cols or "album_key" in cols:
            return  # fresh DB already has the new shape, or already migrated
        conn.executescript("""
            CREATE TABLE album_favourites_new (
                artist     TEXT NOT NULL,
                album      TEXT NOT NULL,
                album_key  TEXT NOT NULL DEFAULT '',
                added_at   INTEGER NOT NULL,
                PRIMARY KEY (artist, album, album_key)
            );
            INSERT OR IGNORE INTO album_favourites_new
                (artist, album, album_key, added_at)
                SELECT artist, album, '', added_at FROM album_favourites;
            DROP TABLE album_favourites;
            ALTER TABLE album_favourites_new RENAME TO album_favourites;
        """)
        conn.commit()
        log.info("DB migration: album_favourites gained album_key "
                 "(PK widened to artist, album, album_key)")
    def rebuild_fts(self):
        """Force a full FTS5 shadow-table rebuild."""
        with self._pool.write() as conn:
            conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

        log.info("FTS5 rebuild complete")
    def repair_fts(self) -> None:
        """Drop, recreate, and rebuild ``tracks_fts`` from the ``tracks``
        table — used to recover from FTS5 shadow-table corruption (the
        recurring ``database disk image is malformed`` error in
        ``clear()`` / ``upsert_tracks()`` that ``PRAGMA integrity_check``
        does NOT catch because it only validates B-trees, not FTS5
        internals).

        Track rows are not touched. ``tracks_fts`` is an external-content
        FTS table (``content=tracks``), so a full rebuild produces an
        identical index from the live row data. The ``tracks_ai`` /
        ``tracks_ad`` / ``tracks_au`` triggers reference ``tracks_fts``
        by name and keep working after the recreate.
        """
        with self._pool.write() as conn:
            conn.execute("DROP TABLE IF EXISTS tracks_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE tracks_fts USING fts5("
                "title, artist, album, content=tracks, content_rowid=id, "
                "tokenize='unicode61 remove_diacritics 1')")
            conn.execute("INSERT INTO tracks_fts(tracks_fts) "
                         "VALUES('rebuild')")
        log.warning("LibraryDB.repair_fts: tracks_fts dropped, recreated, "
                    "and rebuilt from tracks")
    def run_with_fts_heal(self, body_fn, *args, **kwargs):
        """Run ``body_fn(*args, **kwargs)``; if it raises a
        ``sqlite3.DatabaseError`` whose message contains "malformed" (the
        FTS5 shadow-table corruption symptom — see ``repair_fts``), repair
        the FTS index and retry the body ONCE. Any other exception, or a
        malformed error on the retry, is re-raised — never loops.

        This is the single heal implementation: ``clear`` and
        ``upsert_tracks`` route their bodies through it (mass DELETEs /
        INSERTs fire the FTS triggers straight into the corrupt index —
        the 5th and 6th occurrences, 2026-07-03, were exactly that), the
        Indexer wraps its crawl with it, and the LocalFs provider wraps
        its rescan write-transaction. The body must be idempotent /
        retry-safe (all current callers are: DELETE, INSERT OR IGNORE,
        INSERT OR REPLACE).
        """
        for attempt in (1, 2):
            try:
                return body_fn(*args, **kwargs)
            except sqlite3.DatabaseError as e:
                if attempt == 1 and "malformed" in str(e).lower():
                    log.warning(f"LibraryDB: FTS index malformed ({e}) — "
                                f"rebuilding tracks_fts and retrying once")
                    self.repair_fts()
                    continue
                raise
        # Unreachable: attempt 2 either returns or re-raises. Explicit so
        # the control flow is obvious at the tail (and RET503-clean).
        return None
