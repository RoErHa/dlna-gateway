#!/usr/bin/env python3
"""
tests/test_dedup.py — tests for the option-7 schema change.

Three concerns:
  1. `_parse_audio_params` extracts (bit_depth, sample_rate) from
     AssetUPnP-style URLs (`/b16/f44100/`).
  2. `_migrate_widen_tracks_unique` rebuilds an old-schema `tracks`
     table with the widened UNIQUE and backfills the new columns
     from parsed URLs; idempotent.
  3. Browse-view query methods (album_tracks, search, all_albums,
     artist_albums, all_artists, genre_*) hide lower-quality
     duplicates via `_dedup_clause`.

Run standalone:
    python3 -m unittest tests.test_dedup -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import (LibraryDB, _parse_audio_params, _dedup_clause)


# Real AssetUPnP URLs from the production library captured 2026-05-25.
_URL_16BIT = ("http://192.168.1.125:26125/content/c2/b16/f44100/"
              "d-6034886842893489109-co45798ECD833FFD6F.flac")
_URL_24BIT = ("http://192.168.1.125:26125/content/c2/b24/f96000/"
              "d3571337766393215143-coC7284C5E.flac")


# ── _parse_audio_params ───────────────────────────────────────────

class TestParseAudioParams(unittest.TestCase):

    def test_16bit_44100(self):
        self.assertEqual(_parse_audio_params(_URL_16BIT), (16, 44100))

    def test_24bit_96000(self):
        self.assertEqual(_parse_audio_params(_URL_24BIT), (24, 96000))

    def test_24bit_192000(self):
        url = "http://x/c2/b24/f192000/d-abc.flac"
        self.assertEqual(_parse_audio_params(url), (24, 192000))

    def test_32bit_dsd(self):
        # DSD streams via AssetUPnP have unusual bit-depth markers
        # too — make sure the regex doesn't hard-code 16/24.
        url = "http://x/c2/b32/f352800/d-abc.dsf"
        self.assertEqual(_parse_audio_params(url), (32, 352800))

    def test_no_pattern_returns_none(self):
        for url in (
            "http://x/foo.mp3",
            "http://other-server/audio/track.flac",
            "file:///Users/me/Music/song.m4a",
            "http://x/b16f44100/missing-slashes.flac",   # no slashes
            "http://x/x16/f44100/wrong-prefix.flac",     # no 'b'
        ):
            with self.subTest(url=url):
                self.assertEqual(_parse_audio_params(url), (None, None))

    def test_empty_or_none_returns_none(self):
        self.assertEqual(_parse_audio_params(""),   (None, None))
        self.assertEqual(_parse_audio_params(None), (None, None))


# ── _migrate_widen_tracks_unique ──────────────────────────────────

def _build_old_schema_db(path: str) -> None:
    """Construct a tracks table using the pre-2026-05-25 schema (no
    bit_depth/sample_rate, narrow UNIQUE)."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tracks (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            udn      TEXT NOT NULL,
            obj_id   TEXT,
            url      TEXT NOT NULL,
            title    TEXT,
            artist   TEXT,
            album    TEXT,
            duration TEXT,
            art      TEXT,
            mime     TEXT,
            genre    TEXT DEFAULT '',
            file_path TEXT DEFAULT '',
            UNIQUE(udn, artist, album, title)
        );
        CREATE VIRTUAL TABLE tracks_fts USING fts5(
            title, artist, album,
            content=tracks, content_rowid=id,
            tokenize='unicode61 remove_diacritics 1'
        );
        CREATE TRIGGER tracks_ai AFTER INSERT ON tracks BEGIN
            INSERT INTO tracks_fts(rowid, title, artist, album)
            VALUES (new.id, new.title, new.artist, new.album);
        END;
        CREATE TRIGGER tracks_ad AFTER DELETE ON tracks BEGIN
            INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
            VALUES ('delete', old.id, old.title, old.artist, old.album);
        END;
    """)
    # Insert representative rows: a 16/24-bit pair (will only fit ONE in
    # the old schema because of the narrow UNIQUE — the OLD schema doesn't
    # support having both), one unique track, one non-AssetUPnP URL.
    conn.execute("INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
                 "VALUES (?,?,?,?,?,?)",
                 ("uuid:test", "obj1", _URL_16BIT, "Track One", "A", "Album X"))
    conn.execute("INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
                 "VALUES (?,?,?,?,?,?)",
                 ("uuid:test", "obj3", "http://x/random.mp3",
                  "Track Three", "A", "Album X"))
    conn.execute("INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
                 "VALUES (?,?,?,?,?,?)",
                 ("uuid:test", "obj4", "http://x/y/z.flac",
                  "Track Four", "B", "Album Y"))
    conn.commit()
    conn.close()


class TestMigration(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)

    def tearDown(self):
        os.unlink(self._path)

    def test_migration_runs_on_old_schema_db(self):
        _build_old_schema_db(self._path)
        # Constructing LibraryDB triggers _init_schema → migration.
        db = LibraryDB(db_file=self._path)
        # Verify new schema: bit_depth/sample_rate columns present,
        # widened UNIQUE.
        with db._pool.read() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
            self.assertIn("bit_depth",   cols)
            self.assertIn("sample_rate", cols)
            sql = conn.execute("SELECT sql FROM sqlite_master "
                               "WHERE name='tracks'").fetchone()[0]
            self.assertIn("bit_depth, sample_rate", sql)

    def test_migration_backfills_from_url(self):
        _build_old_schema_db(self._path)
        db = LibraryDB(db_file=self._path)
        with db._pool.read() as conn:
            row16 = conn.execute(
                "SELECT bit_depth, sample_rate FROM tracks WHERE url=?",
                (_URL_16BIT,)).fetchone()
            row_random = conn.execute(
                "SELECT bit_depth, sample_rate FROM tracks WHERE url=?",
                ("http://x/random.mp3",)).fetchone()
        self.assertEqual((row16["bit_depth"], row16["sample_rate"]),
                         (16, 44100), "AssetUPnP URL must backfill")
        self.assertEqual((row_random["bit_depth"], row_random["sample_rate"]),
                         (None, None),
                         "non-AssetUPnP URL must leave NULL")

    def test_migration_preserves_all_rows(self):
        _build_old_schema_db(self._path)
        db = LibraryDB(db_file=self._path)
        with db._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        self.assertEqual(n, 3, "all 3 old rows must survive migration")

    def test_migration_rebuilds_fts(self):
        _build_old_schema_db(self._path)
        db = LibraryDB(db_file=self._path)
        # FTS5 search should still work after migration.
        result = db.search("uuid:test", "Track One")
        self.assertEqual(len(result["tracks"]), 1)
        self.assertEqual(result["tracks"][0]["title"], "Track One")

    def test_migration_idempotent(self):
        _build_old_schema_db(self._path)
        # First construct runs the migration. Second construct should
        # see "bit_depth" in the schema and skip.
        db1 = LibraryDB(db_file=self._path)
        del db1
        # Reading without re-migration must keep the same data
        db2 = LibraryDB(db_file=self._path)
        with db2._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        self.assertEqual(n, 3)

    def test_fresh_db_uses_new_schema_without_migration(self):
        # No pre-existing tracks table — _init_schema's CREATE TABLE
        # IF NOT EXISTS uses the new schema directly. Migration is
        # a no-op.
        db = LibraryDB(db_file=self._path)
        with db._pool.read() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
        self.assertIn("bit_depth",   cols)
        self.assertIn("sample_rate", cols)


# ── Browse-side dedup filter ──────────────────────────────────────

class TestBrowseDedup(unittest.TestCase):
    """Insert a 16-bit + 24-bit version of the same (artist, album,
    title), assert browse views show only the 24-bit version."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db  = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"
        with self.db._pool.write() as conn:
            # 16-bit and 24-bit copies of "Track A" on "Album X" by "Artist".
            conn.execute(
                "INSERT INTO tracks "
                "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.udn, "a16", _URL_16BIT, "Track A", "Artist", "Album X",
                 16, 44100))
            conn.execute(
                "INSERT INTO tracks "
                "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.udn, "a24", _URL_24BIT, "Track A", "Artist", "Album X",
                 24, 96000))
            # And a unique 16-bit track to verify non-duplicates still appear.
            conn.execute(
                "INSERT INTO tracks "
                "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.udn, "b16",
                 "http://192.168.1.125:26125/content/c2/b16/f44100/dbbb.flac",
                 "Track B", "Artist", "Album X", 16, 44100))

    def tearDown(self):
        os.unlink(self._path)

    def test_album_tracks_hides_lower_quality(self):
        tracks = self.db.album_tracks(self.udn, "Artist", "Album X")
        # Should be 2: Track A (24-bit only) + Track B (only 16-bit exists)
        self.assertEqual(len(tracks), 2)
        track_a = [t for t in tracks if t["title"] == "Track A"][0]
        self.assertEqual(track_a["url"], _URL_24BIT,
                         "browse must return 24-bit Track A, not 16-bit")

    def test_search_tracks_hide_lower_quality(self):
        result = self.db.search(self.udn, "Track A")
        track_urls = {t["url"] for t in result["tracks"]}
        self.assertNotIn(_URL_16BIT, track_urls,
                         "search must not return the 16-bit Track A")
        self.assertIn(_URL_24BIT, track_urls)

    def test_all_albums_track_count_is_deduped(self):
        albums = self.db.all_albums(self.udn)
        self.assertEqual(len(albums), 1)
        # 3 raw rows → 2 browse-visible: Track A (24-bit), Track B (16-bit)
        self.assertEqual(albums[0]["track_count"], 2,
                         "track_count must reflect deduped browse-visible rows")

    def test_artist_albums_track_count_is_deduped(self):
        albums = self.db.artist_albums(self.udn, "Artist")
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["track_count"], 2)

    def test_all_artists_track_count_is_deduped(self):
        artists = self.db.all_artists(self.udn)
        self.assertEqual(len(artists), 1)
        self.assertEqual(artists[0]["track_count"], 2)

    def test_higher_sample_rate_wins_within_same_bitdepth(self):
        # Add a 16-bit/96kHz "Track C" + 16-bit/44.1kHz "Track C". The
        # 96kHz one should win — _dedup_clause uses sample_rate as
        # tiebreak within equal bit_depth.
        with self.db._pool.write() as conn:
            conn.execute(
                "INSERT INTO tracks "
                "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.udn, "c16-44",
                 "http://192.168.1.125:26125/content/c2/b16/f44100/dccc.flac",
                 "Track C", "Artist", "Album X", 16, 44100))
            conn.execute(
                "INSERT INTO tracks "
                "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.udn, "c16-96",
                 "http://192.168.1.125:26125/content/c2/b16/f96000/dccc.flac",
                 "Track C", "Artist", "Album X", 16, 96000))
        tracks = self.db.album_tracks(self.udn, "Artist", "Album X")
        track_c_urls = [t["url"] for t in tracks if t["title"] == "Track C"]
        self.assertEqual(len(track_c_urls), 1)
        self.assertIn("f96000", track_c_urls[0],
                      "must keep the higher-sample-rate Track C")

    def test_null_quality_loses_to_any_non_null(self):
        # NULL bit_depth/sample_rate is treated as 0 — any non-NULL wins.
        with self.db._pool.write() as conn:
            conn.execute(
                "INSERT INTO tracks "
                "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.udn, "d-null", "http://x/null.flac",
                 "Track D", "Artist", "Album X", None, None))
            conn.execute(
                "INSERT INTO tracks "
                "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (self.udn, "d-16", "http://x/16bit.flac",
                 "Track D", "Artist", "Album X", 16, 44100))
        tracks = self.db.album_tracks(self.udn, "Artist", "Album X")
        track_d_urls = [t["url"] for t in tracks if t["title"] == "Track D"]
        self.assertEqual(len(track_d_urls), 1)
        self.assertEqual(track_d_urls[0], "http://x/16bit.flac",
                         "non-NULL row must win over NULL")

    def test_two_null_tracks_both_survive(self):
        # Two non-AssetUPnP tracks with NULL params and the same
        # (artist, album, title) — they shouldn't dedup against each
        # other (NULL = NULL = 0; neither is strictly greater).
        with self.db._pool.write() as conn:
            conn.execute(
                "INSERT INTO tracks "
                "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("uuid:other", "e1", "http://other/e1.mp3",
                 "Track E", "Artist", "Album E", None, None))
            conn.execute(
                "INSERT INTO tracks "
                "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("uuid:other", "e2", "http://other/e2.mp3",
                 "Track E", "Artist", "Album E", None, None))
        # These would have collided on the OLD narrow UNIQUE, but with
        # the new wide UNIQUE and NULL-treated-as-distinct, both insert.
        # Dedup: COALESCE(NULL,0) == COALESCE(NULL,0) → neither is
        # strictly greater → both survive in browse.
        tracks = self.db.album_tracks("uuid:other", "Artist", "Album E")
        self.assertEqual(len(tracks), 2,
                         "two NULL-quality rows must not dedup against "
                         "each other")


# ── Indexer integration: upsert fills the new columns ─────────────

class TestUniqueUrlMigration(unittest.TestCase):
    """The follow-up migration to `_migrate_widen_tracks_unique`:
    enforce UNIQUE(udn, url) so the indexer can't keep accumulating
    same-URL phantom dupes from raw-vs-corrected metadata."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)

    def tearDown(self):
        os.unlink(self._path)

    def _build_widened_schema_with_url_dupes(self):
        """Manually construct the widened-UNIQUE schema (post-widen
        but pre-url-unique) and seed same-URL dupes. We bypass
        LibraryDB so the dedup migration doesn't run yet."""
        conn = sqlite3.connect(self._path)
        conn.executescript("""
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
                UNIQUE(udn, artist, album, title, bit_depth, sample_rate)
            );
            CREATE VIRTUAL TABLE tracks_fts USING fts5(
                title, artist, album, content=tracks, content_rowid=id,
                tokenize='unicode61 remove_diacritics 1');
            CREATE TRIGGER tracks_ai AFTER INSERT ON tracks BEGIN
                INSERT INTO tracks_fts(rowid, title, artist, album)
                VALUES (new.id, new.title, new.artist, new.album);
            END;
            CREATE TRIGGER tracks_ad AFTER DELETE ON tracks BEGIN
                INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                VALUES ('delete', old.id, old.title, old.artist, old.album);
            END;
        """)
        # URL X has TWO rows: a corrected one (lower id) and a raw one.
        conn.execute("INSERT INTO tracks "
                     "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     ("uuid:test", "ok1", "http://x/sameurl.flac",
                      "Empire (Live From XFM)", "Kasabian", "Empire",
                      16, 44100))
        conn.execute("INSERT INTO tracks "
                     "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     ("uuid:test", "ok2", "http://x/sameurl.flac",
                      "Empire", "Kasabian", "Empire", 16, 44100))
        # URL Y has only one row — must survive.
        conn.execute("INSERT INTO tracks "
                     "(udn,obj_id,url,title,artist,album,bit_depth,sample_rate) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     ("uuid:test", "ok3", "http://x/uniq.flac",
                      "Solo", "Artist", "Album", 16, 44100))
        conn.commit()
        conn.close()

    def test_dedup_keeps_min_id_per_udn_url(self):
        self._build_widened_schema_with_url_dupes()
        db = LibraryDB(db_file=self._path)
        with db._pool.read() as conn:
            rows = conn.execute(
                "SELECT id, url, title FROM tracks ORDER BY id"
            ).fetchall()
        self.assertEqual(len(rows), 2,
                         "duplicate URL must collapse to one row")
        urls = {r["url"] for r in rows}
        self.assertEqual(urls, {"http://x/sameurl.flac", "http://x/uniq.flac"})
        # The corrected metadata row (lower id) survives.
        same_url_row = [r for r in rows
                        if r["url"] == "http://x/sameurl.flac"][0]
        self.assertEqual(same_url_row["title"], "Empire (Live From XFM)",
                         "the lower-id (corrected) row must be kept")

    def test_unique_url_index_created(self):
        self._build_widened_schema_with_url_dupes()
        db = LibraryDB(db_file=self._path)
        with db._pool.read() as conn:
            idx = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_tracks_udn_url'"
            ).fetchone()
        self.assertIsNotNone(idx, "UNIQUE(udn, url) index must exist")

    def test_subsequent_insert_with_same_url_skipped(self):
        self._build_widened_schema_with_url_dupes()
        db = LibraryDB(db_file=self._path)
        # Re-insert via upsert_tracks — should INSERT OR IGNORE on (udn,url).
        n = db.upsert_tracks("uuid:test", [
            {"id": "new", "url": "http://x/sameurl.flac",
             "title": "Different Title", "artist": "X", "album": "Y"}])
        with db._pool.read() as conn:
            n_rows = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE url=?",
                ("http://x/sameurl.flac",)).fetchone()[0]
        self.assertEqual(n_rows, 1,
                         "INSERT OR IGNORE on (udn,url) must dedupe "
                         "subsequent inserts with same URL")

    def test_migration_idempotent(self):
        self._build_widened_schema_with_url_dupes()
        db1 = LibraryDB(db_file=self._path)
        del db1
        db2 = LibraryDB(db_file=self._path)
        with db2._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        self.assertEqual(n, 2,
                         "second-pass migration must not re-delete anything")

    def test_fresh_db_has_unique_url_index(self):
        # No pre-existing schema → CREATE TABLE in _init_schema runs and
        # already includes the index.
        db = LibraryDB(db_file=self._path)
        with db._pool.read() as conn:
            idx = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_tracks_udn_url'"
            ).fetchone()
        self.assertIsNotNone(idx)


class TestUpsertFillsAudioParams(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db  = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"

    def tearDown(self):
        os.unlink(self._path)

    def test_upsert_parses_assetupnp_url(self):
        n = self.db.upsert_tracks(self.udn, [
            {"id": "1", "url": _URL_24BIT, "title": "T", "artist": "A",
             "album": "Al"}])
        self.assertGreater(n, 0)
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT bit_depth, sample_rate FROM tracks WHERE url=?",
                (_URL_24BIT,)).fetchone()
        self.assertEqual((row["bit_depth"], row["sample_rate"]), (24, 96000))

    def test_upsert_leaves_non_assetupnp_null(self):
        self.db.upsert_tracks(self.udn, [
            {"id": "1", "url": "http://foreign/track.mp3",
             "title": "T", "artist": "A", "album": "Al"}])
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT bit_depth, sample_rate FROM tracks "
                "WHERE url='http://foreign/track.mp3'").fetchone()
        self.assertIsNone(row["bit_depth"])
        self.assertIsNone(row["sample_rate"])

    def test_upsert_16_and_24_coexist(self):
        # The crucial test: with the widened UNIQUE, INSERT OR IGNORE
        # accepts both rows. (Before the fix, the second would silently
        # vanish into the OR IGNORE.) 16-bit and 24-bit copies have
        # DIFFERENT d-ids in AssetUPnP, so the d-id+title dedup added
        # 2026-05-28 doesn't interfere.
        self.db.upsert_tracks(self.udn, [
            {"id": "1", "url": _URL_16BIT, "title": "Same", "artist": "A",
             "album": "Same"},
            {"id": "2", "url": _URL_24BIT, "title": "Same", "artist": "A",
             "album": "Same"}])
        with self.db._pool.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM tracks "
                "WHERE artist='A' AND album='Same' AND title='Same'"
            ).fetchone()[0]
        self.assertEqual(n, 2, "16-bit + 24-bit pair must both survive INSERT")


# ── upsert_tracks (d-id + title) virtual-album dedup ──────────────

class TestUpsertVirtualAlbumDedup(unittest.TestCase):
    """AssetUPnP exposes the SAME physical file under multiple browse
    paths (real album + 'Music From the OC: Mix 5' compilation, etc.)
    with different co-hash but the SAME d-id. upsert_tracks dedups
    these on (d_id, lower(title)) so the index doesn't double-count
    them. Confirmed 2026-05-28: HTTP HEAD on alias URLs returns
    byte-identical Content-Length on every sample."""

    _BASE = "http://srv/content/c2/b16/f44100"

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db  = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"

    def tearDown(self):
        os.unlink(self._path)

    def _alias(self, d_id: str, co_hash: str) -> str:
        return f"{self._BASE}/{d_id}-co{co_hash}.flac"

    def _count(self):
        with self.db._pool.read() as conn:
            return conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    def test_two_aliases_in_same_batch_collapse_to_one(self):
        # Same file (d-12345) indexed twice under different album names.
        self.db.upsert_tracks(self.udn, [
            {"id": "a", "url": self._alias("d12345", "AAA"),
             "title": "Reason Is Treason", "artist": "Kasabian",
             "album": "Kasabian"},
            {"id": "b", "url": self._alias("d12345", "BBB"),
             "title": "Reason Is Treason", "artist": "Kasabian",
             "album": "Music From the OC: Mix 5"},
        ])
        self.assertEqual(self._count(), 1,
                         "Same d-id + same title → must collapse to ONE row")

    def test_second_batch_alias_skipped(self):
        # First crawl adds the file. Second crawl tries to add an alias
        # — must be ignored (existing row check, not just within-batch).
        self.db.upsert_tracks(self.udn, [
            {"id": "a", "url": self._alias("d12345", "AAA"),
             "title": "Reason Is Treason", "artist": "Kasabian",
             "album": "Kasabian"},
        ])
        self.db.upsert_tracks(self.udn, [
            {"id": "b", "url": self._alias("d12345", "BBB"),
             "title": "Reason Is Treason", "artist": "Kasabian",
             "album": "Music From the OC: Mix 5"},
        ])
        self.assertEqual(self._count(), 1)

    def test_same_d_id_different_title_BOTH_survive(self):
        # The Kryptonite/Down Poison case: same d-id (AssetUPnP collides
        # within the same album) but the titles ARE different. Both
        # rows MUST be preserved.
        self.db.upsert_tracks(self.udn, [
            {"id": "a", "url": self._alias("d99999", "AAA"),
             "title": "Kryptonite",  "artist": "3 Doors Down",
             "album": "The Better Life"},
            {"id": "b", "url": self._alias("d99999", "BBB"),
             "title": "Down Poison", "artist": "3 Doors Down",
             "album": "The Better Life"},
        ])
        self.assertEqual(self._count(), 2,
                         "Same d-id BUT different title MUST keep both rows")

    def test_title_normalisation_case_insensitive(self):
        # 'Reason Is Treason' vs 'reason is treason' should collapse
        # — AssetUPnP sometimes exposes the same file with case-only
        # title variations.
        self.db.upsert_tracks(self.udn, [
            {"id": "a", "url": self._alias("d12345", "AAA"),
             "title": "Reason Is Treason", "artist": "Kasabian",
             "album": "Kasabian"},
            {"id": "b", "url": self._alias("d12345", "BBB"),
             "title": "reason is treason", "artist": "Kasabian",
             "album": "Music From the OC: Mix 5"},
        ])
        self.assertEqual(self._count(), 1)

    def test_non_assetupnp_url_not_deduped_by_d_id(self):
        # URLs without a d-id pattern fall through to the wide UNIQUE
        # constraint and are NOT touched by d-id dedup. With NULL
        # bit_depth/sample_rate (no AssetUPnP /b/f pattern), SQLite
        # treats each row as distinct in UNIQUE — so two foreign URLs
        # with the same metadata survive as 2 rows. The dedup fix
        # added 2026-05-28 explicitly does NOT alter this.
        self.db.upsert_tracks(self.udn, [
            {"id": "a", "url": "http://foreign/track1.mp3",
             "title": "T", "artist": "A", "album": "AL"},
            {"id": "b", "url": "http://foreign/track2.mp3",
             "title": "T", "artist": "A", "album": "AL"},
        ])
        self.assertEqual(self._count(), 2,
                         "Non-AssetUPnP URLs should survive — d-id dedup "
                         "must not affect them")

    def test_within_batch_first_url_wins(self):
        # Verify the FIRST URL in the batch is kept; the alias is
        # discarded. Order in the input list matters.
        self.db.upsert_tracks(self.udn, [
            {"id": "first",  "url": self._alias("d12345", "AAA"),
             "title": "T", "artist": "A", "album": "Real"},
            {"id": "second", "url": self._alias("d12345", "BBB"),
             "title": "T", "artist": "A", "album": "Compilation"},
        ])
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT album FROM tracks WHERE artist='A' AND title='T'"
            ).fetchone()
        self.assertEqual(row["album"], "Real")


# ── _dedup_clause smoke: SQL syntax is valid ──────────────────────

class TestDedupClause(unittest.TestCase):

    def test_returns_valid_sql_fragment(self):
        frag = _dedup_clause("t")
        self.assertIn("NOT EXISTS", frag)
        self.assertIn("_hq.udn",    frag)
        self.assertIn("t.udn",      frag)

    def test_uses_supplied_alias(self):
        frag = _dedup_clause("outer_x")
        self.assertIn("outer_x.udn", frag)
        # No leakage from default alias
        self.assertNotIn("t.udn", frag)


if __name__ == "__main__":
    unittest.main()
