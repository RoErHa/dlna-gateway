#!/usr/bin/env python3
"""
tests/test_library.py — LibraryDB unit tests against a temp SQLite file.

Focus: the radio play-count biasing — the whole reason the feature
exists. An in-memory DB keeps the test fast and independent of the
live library.db.
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB


class TestRadioPlayCountBias(unittest.TestCase):

    def setUp(self):
        # Fresh DB per test. Using a temp file rather than :memory: because
        # db_pool uses thread-local connections and :memory: doesn't share
        # across connections.
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"
        # Insert 10 tracks directly (bypassing upsert_tracks which expects
        # DIDL-Lite-shaped rows — we just need rows in `tracks`)
        with self.db._pool.write() as conn:
            for i in range(10):
                conn.execute(
                    "INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (self.udn, f"obj{i}", f"http://t/{i}", f"Track {i}",
                     "A", f"Album {i // 3}"))

    def tearDown(self):
        os.unlink(self._path)

    def _play_count(self, url):
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT count FROM play_counts WHERE url=?", (url,)).fetchone()
            return row["count"] if row else 0

    def test_first_call_returns_all_zero_counts(self):
        """All tracks start with no play_counts entry → count=0 → eligible."""
        tracks = self.db.radio_tracks(self.udn, limit=5)
        self.assertEqual(len(tracks), 5)
        # Every returned URL is now at count=1
        for t in tracks:
            self.assertEqual(self._play_count(t["url"]), 1)

    def test_subsequent_call_prefers_unseen_tracks(self):
        """The second call must NOT return the same 5 — it must pick
        from the remaining count=0 tracks first. This is the core of
        the 'radio freshness' feature."""
        first = self.db.radio_tracks(self.udn, limit=5)
        first_urls = {t["url"] for t in first}
        second = self.db.radio_tracks(self.udn, limit=5)
        second_urls = {t["url"] for t in second}
        self.assertTrue(first_urls.isdisjoint(second_urls),
                        f"second radio call returned already-picked tracks: "
                        f"{first_urls & second_urls}")

    def test_cycle_exhausts_library_before_repeating(self):
        """With 10 tracks and limit=5 per call, two calls should cover
        every track once before any repeats appear."""
        seen = set()
        for _ in range(2):
            for t in self.db.radio_tracks(self.udn, limit=5):
                seen.add(t["url"])
        self.assertEqual(len(seen), 10, "library cycle did not exhaust all tracks")
        # After the second pass, every track is at count=1 (each picked once)
        with self.db._pool.read() as conn:
            counts = [r["count"] for r in conn.execute(
                "SELECT count FROM play_counts").fetchall()]
        self.assertEqual(counts, [1] * 10)

    def test_third_pass_increments_to_two(self):
        """After all tracks are at count=1, the next radio call picks
        from the count=1 tier (all tied) and bumps them to count=2."""
        for _ in range(2):
            self.db.radio_tracks(self.udn, limit=5)   # exhaust to count=1
        self.db.radio_tracks(self.udn, limit=5)
        with self.db._pool.read() as conn:
            rows = conn.execute(
                "SELECT count, COUNT(*) AS n FROM play_counts "
                "GROUP BY count ORDER BY count").fetchall()
        # Five still at 1, five bumped to 2
        tier = {r["count"]: r["n"] for r in rows}
        self.assertEqual(tier, {1: 5, 2: 5})

    def test_play_counts_persist_across_clear(self):
        """The whole point of a separate table: rebuild-index wipes
        `tracks` but leaves `play_counts` intact. Without this, every
        reindex resets radio back to 'same 100 every time'."""
        self.db.radio_tracks(self.udn, limit=3)
        # Simulate a rebuild-index — tracks table gets wiped
        self.db.clear(self.udn)
        # play_counts entries survive
        with self.db._pool.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM play_counts").fetchone()["n"]
        self.assertEqual(n, 3, "play_counts must survive clear()")

    def test_limit_of_zero_is_safe(self):
        """A broken caller passing limit=0 should return [] without
        crashing or touching play_counts."""
        tracks = self.db.radio_tracks(self.udn, limit=0)
        self.assertEqual(tracks, [])
        with self.db._pool.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM play_counts").fetchone()["n"]
        self.assertEqual(n, 0)


# ── FTS5 repair — recovers from "database disk image is malformed" ──
# The data-behaviour side of LibraryDB.repair_fts(); the indexer's
# heal+retry wrapper around it is unit-tested in tests/test_indexer.py.

class TestRepairFts(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        # Seed a handful of tracks so FTS has something to match on.
        with self.db._pool.write() as c:
            for i in range(5):
                c.execute(
                    "INSERT INTO tracks(udn, obj_id, url, title, artist, "
                    "album, duration, art, mime, genre, file_path) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("uuid:srv1", f"t{i}", f"http://x/t{i}.flac",
                     f"Love Song {i}", "Artist A", "Album X",
                     "0:03:00", "", "audio/flac", "", ""))

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def _count(self):
        with self.db._pool.read() as c:
            return c.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    def _fts_hits(self, term):
        with self.db._pool.read() as c:
            return c.execute(
                "SELECT COUNT(*) FROM tracks_fts WHERE tracks_fts MATCH ?",
                (term,)).fetchone()[0]

    def test_repair_preserves_tracks(self):
        before = self._count()
        self.db.repair_fts()
        self.assertEqual(self._count(), before)

    def test_repair_regenerates_searchable_index(self):
        self.db.repair_fts()
        # All 5 seeded "Love Song" titles are searchable again.
        self.assertEqual(self._fts_hits("love"), 5)

    def test_repair_is_idempotent(self):
        self.db.repair_fts()
        self.db.repair_fts()   # second call must not raise
        self.assertEqual(self._fts_hits("love"), 5)

    def test_triggers_still_fire_after_repair(self):
        """The tracks_ai / tracks_ad triggers write into tracks_fts by
        name; after a DROP+CREATE the new vtable picks them up."""
        self.db.repair_fts()
        # tracks_ai: an INSERT must populate the FTS index for the new row.
        with self.db._pool.write() as c:
            c.execute(
                "INSERT INTO tracks(udn, obj_id, url, title, artist, "
                "album, duration, art, mime, genre, file_path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("uuid:srv1", "t99", "http://x/t99.flac",
                 "Fresh Track After Repair", "Artist B", "Album Y",
                 "0:03:00", "", "audio/flac", "", ""))
        self.assertEqual(self._fts_hits("fresh"), 1)
        # tracks_ad: the DELETE that fails on a corrupt FTS index now
        # succeeds — this is the exact path that died in the wild.
        with self.db._pool.write() as c:
            c.execute("DELETE FROM tracks WHERE obj_id='t99'")
        self.assertEqual(self._count(), 5)
        self.assertEqual(self._fts_hits("fresh"), 0)


class TestUpsertMetadataRefresh(unittest.TestCase):
    """Re-upserting an EXISTING url must refresh its metadata (the
    2026-07-12 fix). Before it, INSERT OR IGNORE swallowed the fresh
    row and only genre/art were patched — so in-place retagging
    (beets) was invisible to any rescan and the workaround was
    `DELETE FROM tracks` + rebuild."""

    URL = "http://gw:8200/localfs/stream/abc123"

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:localfs-test"

    def tearDown(self):
        os.unlink(self._path)

    def _upsert(self, **kw):
        row = {"id": "abc123", "url": self.URL, "title": "Old Title",
               "artist": "Old Artist", "album": "Old Album",
               "genre": "Rock", "art": "localfs-art:aa", "year": 1999,
               "duration": "0:03:00", "mime": "audio/flac",
               "bit_depth": 16, "sample_rate": 44100,
               "album_key": "Old Artist/Old Album"}
        row.update(kw)
        return self.db.upsert_tracks(self.udn, [row])

    def _row(self, url=None):
        with self.db._pool.read() as conn:
            r = conn.execute("SELECT * FROM tracks WHERE url=?",
                             (url or self.URL,)).fetchone()
        return dict(r) if r else None

    def test_reupsert_same_url_updates_metadata(self):
        self._upsert()
        self._upsert(title="New Title", artist="New Artist",
                     album="New Album", year=1975, genre="Jazz",
                     album_key="New Artist/New Album")
        row = self._row()
        self.assertEqual(row["title"], "New Title")
        self.assertEqual(row["artist"], "New Artist")
        self.assertEqual(row["album"], "New Album")
        self.assertEqual(row["year"], 1975)
        self.assertEqual(row["genre"], "Jazz")
        self.assertEqual(row["album_key"], "New Artist/New Album")
        # Still one row — refreshed in place, not duplicated.
        self.assertEqual(self.db.track_count(self.udn), 1)

    def test_refresh_keeps_existing_genre_and_art_when_incoming_empty(self):
        """An incoming empty genre/art must not blank a value that was
        backfilled from album_art or set by an override."""
        self._upsert()
        self._upsert(title="New Title", genre="", art="")
        row = self._row()
        self.assertEqual(row["title"], "New Title")
        self.assertEqual(row["genre"], "Rock")
        self.assertEqual(row["art"], "localfs-art:aa")

    def test_refresh_updates_fts_index(self):
        """The in-place UPDATE must keep tracks_fts in sync so the new
        title is searchable and the old one is gone."""
        self._upsert()
        self._upsert(title="Completely Different Song")
        hits = self.db.search(self.udn, "completely")
        self.assertEqual(len(hits["tracks"]), 1)
        hits_old = self.db.search(self.udn, "old title")
        self.assertEqual(len(hits_old["tracks"]), 0)

    def test_manual_override_still_wins_after_refresh(self):
        """The overrides COALESCE pass runs after the refresh, so a
        manual override keeps outranking fresh file tags."""
        self._upsert()
        self.db.metadata_override_set(
            self.URL, title="Corrected Title", source="manual")
        self._upsert(title="Retagged Title")
        self.assertEqual(self._row()["title"], "Corrected Title")

    def test_refresh_unique_collision_is_ignored(self):
        """Refreshing a row into an identity that collides with the
        wide UNIQUE must be skipped silently (OR IGNORE), never raise."""
        other = "http://gw:8200/localfs/stream/def456"
        self._upsert()
        self._upsert(id="def456", url=other, title="Second Title")
        # Try to refresh the second row into the first row's identity.
        self._upsert(id="def456", url=other, title="Old Title")
        row = self._row(other)
        self.assertEqual(row["title"], "Second Title")   # kept old
        self.assertEqual(self.db.track_count(self.udn), 2)


class TestAlbumKeyUniqueMigration(unittest.TestCase):
    """_migrate_widen_unique_album_key — the 2026-07-12 completeness fix:
    two DISTINCT files in different folders with identical tags must both
    get a tracks row (old wide UNIQUE swallowed the second)."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)

    def tearDown(self):
        os.unlink(self._path)

    OLD_SCHEMA = """
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
            UNIQUE(udn, artist, album, title, bit_depth, sample_rate)
        );
        CREATE UNIQUE INDEX idx_tracks_udn_url ON tracks(udn, url);
    """

    def _make_old_db(self, n_rows=3):
        import sqlite3 as s3
        conn = s3.connect(self._path)
        conn.executescript(self.OLD_SCHEMA)
        for i in range(n_rows):
            conn.execute(
                "INSERT INTO tracks (udn, obj_id, url, title, artist, album,"
                " album_key) VALUES (?,?,?,?,?,?,?)",
                ("uuid:localfs-x", f"o{i}", f"http://x/{i}", f"T{i}", "A",
                 "Al", f"folder{i}"))
        conn.commit()
        conn.close()

    def _tracks_sql(self, db):
        with db._pool.read() as c:
            return c.execute("SELECT sql FROM sqlite_master WHERE "
                             "type='table' AND name='tracks'").fetchone()[0]

    def test_migration_widens_unique_and_preserves_rows(self):
        self._make_old_db(3)
        db = LibraryDB(db_file=self._path)
        sql = self._tracks_sql(db)
        self.assertIn("album_key", sql[sql.index("UNIQUE"):])
        self.assertEqual(db.track_count("uuid:localfs-x"), 3)
        with db._pool.read() as c:
            triggers = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'")}
            indexes = {r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='tracks'")}
        self.assertLessEqual({"tracks_ai", "tracks_ad", "tracks_au"}, triggers)
        self.assertLessEqual({"idx_tracks_udn_url", "idx_tracks_udn_album_key"},
                             indexes)

    def test_migration_is_idempotent(self):
        self._make_old_db(2)
        LibraryDB(db_file=self._path)
        db2 = LibraryDB(db_file=self._path)   # second init: no-op
        self.assertEqual(db2.track_count("uuid:localfs-x"), 2)

    def test_same_tags_different_folder_coexist_after_migration(self):
        """The audit case: identical (artist, album, title, bd, sr) in two
        folders — deluxe vs standard edition — both rows survive."""
        self._make_old_db(0)
        db = LibraryDB(db_file=self._path)
        rows = [
            {"id": "a1", "url": "http://x/a1", "title": "Gravity Eyelids",
             "artist": "Porcupine Tree", "album": "In Absentia",
             "bit_depth": 24, "sample_rate": 44100,
             "album_key": "PT/In Absentia (Remastered)"},
            {"id": "a2", "url": "http://x/a2", "title": "Gravity Eyelids",
             "artist": "Porcupine Tree", "album": "In Absentia",
             "bit_depth": 24, "sample_rate": 44100,
             "album_key": "PT/In Absentia (Deluxe - Remastered)"},
        ]
        db.upsert_tracks("uuid:localfs-x", rows)
        self.assertEqual(db.track_count("uuid:localfs-x"), 2)

    def test_same_tags_same_folder_still_collide(self):
        self._make_old_db(0)
        db = LibraryDB(db_file=self._path)
        rows = [
            {"id": "b1", "url": "http://x/b1", "title": "Song",
             "artist": "A", "album": "Al", "bit_depth": 16,
             "sample_rate": 44100, "album_key": "A/Al"},
            {"id": "b2", "url": "http://x/b2", "title": "Song",
             "artist": "A", "album": "Al", "bit_depth": 16,
             "sample_rate": 44100, "album_key": "A/Al"},
        ]
        db.upsert_tracks("uuid:localfs-x", rows)
        self.assertEqual(db.track_count("uuid:localfs-x"), 1)

    def test_upnp_empty_album_key_semantics_unchanged(self):
        """UPnP rows have album_key='' — same-tag same-quality rows still
        collide exactly as before the widening. (AssetUPnP-style URLs so
        bit_depth/sample_rate parse; NULL bd/sr rows never collided even
        pre-widening — NULLs are distinct in UNIQUE.)"""
        self._make_old_db(0)
        db = LibraryDB(db_file=self._path)
        rows = [
            {"id": "c1", "url": "http://u:26125/c2/b16/f44100/d-1-coA.flac",
             "title": "Song", "artist": "A", "album": "Al"},
            {"id": "c2", "url": "http://u:26125/c2/b16/f44100/d-2-coB.flac",
             "title": "Song", "artist": "A", "album": "Al"},
        ]
        db.upsert_tracks("uuid:upnp-server", rows)
        self.assertEqual(db.track_count("uuid:upnp-server"), 1)

    def test_fts_search_works_after_migration(self):
        self._make_old_db(3)
        db = LibraryDB(db_file=self._path)
        hits = db.search("uuid:localfs-x", "T1")
        self.assertEqual(len(hits["tracks"]), 1)


class TestAllAlbumsPagination(unittest.TestCase):
    """all_albums(order/limit/offset) — the SQL pagination behind Subsonic
    getAlbumList2 (Amperfy pages the whole library)."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"
        # 4 albums: Delta/Cream, Alpha/Zed, Charlie/Mike, Bravo/Aaron —
        # deliberately album-name order != artist order.
        seed = [("Cream", "Delta"), ("Zed", "Alpha"),
                ("Mike", "Charlie"), ("Aaron", "Bravo")]
        with self.db._pool.write() as conn:
            for i, (ar, al) in enumerate(seed):
                conn.execute(
                    "INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
                    "VALUES (?,?,?,?,?,?)",
                    (self.udn, f"o{i}", f"http://t/{i}", f"T{i}", ar, al))

    def tearDown(self):
        os.unlink(self._path)

    def test_default_orders_by_album_name(self):
        names = [a["album"] for a in self.db.all_albums(self.udn)]
        self.assertEqual(names, ["Alpha", "Bravo", "Charlie", "Delta"])

    def test_order_by_artist(self):
        names = [a["album"]
                 for a in self.db.all_albums(self.udn, order="artist")]
        # Aaron(Bravo), Cream(Delta), Mike(Charlie), Zed(Alpha)
        self.assertEqual(names, ["Bravo", "Delta", "Charlie", "Alpha"])

    def test_limit_offset_paginates(self):
        p1 = [a["album"] for a in
              self.db.all_albums(self.udn, limit=2, offset=0)]
        p2 = [a["album"] for a in
              self.db.all_albums(self.udn, limit=2, offset=2)]
        self.assertEqual(p1, ["Alpha", "Bravo"])
        self.assertEqual(p2, ["Charlie", "Delta"])
        self.assertTrue(set(p1).isdisjoint(p2))

    def test_limit_none_returns_all(self):
        self.assertEqual(len(self.db.all_albums(self.udn)), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
