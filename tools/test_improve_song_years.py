#!/usr/bin/env python3
"""
tools/test_improve_song_years.py — unit tests for the external
year-lookup tool. Mocks MB calls; never hits the real network.

Run standalone:
    python3 -m unittest tools.test_improve_song_years -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import improve_song_years as I  # noqa: E402


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            udn TEXT, obj_id TEXT, url TEXT NOT NULL,
            title TEXT, artist TEXT, album TEXT, year INTEGER, genre TEXT
        );
        CREATE TABLE metadata_overrides (
            url TEXT PRIMARY KEY,
            artist TEXT, album TEXT, title TEXT, genre TEXT,
            year INTEGER, updated_at TEXT,
            source TEXT NOT NULL DEFAULT 'manual'
        );
    """)
    return conn


def _t(conn, url, artist, title, file_year, override_year=None,
       override_src='acoustid'):
    conn.execute("INSERT INTO tracks (udn, obj_id, url, artist, title, year) "
                 "VALUES ('u', ?, ?, ?, ?, ?)",
                 (url, url, artist, title, file_year))
    if override_year is not None:
        conn.execute("INSERT INTO metadata_overrides "
                     "(url, year, source) VALUES (?,?,?)",
                     (url, override_year, override_src))
    conn.commit()


# ── Normalisation ───────────────────────────────────────────────

class TestNorm(unittest.TestCase):

    def test_curly_apostrophe_normalised(self):
        # The bytes that bit us in the dedup fix
        self.assertEqual(I._norm("Art for Art's Sake"),
                         I._norm("Art for Art’s Sake"))

    def test_diacritic_normalised(self):
        self.assertEqual(I._norm("Une Nuit à Paris"),
                         I._norm("Une Nuit a Paris"))

    def test_case_normalised(self):
        self.assertEqual(I._norm("Hello World"),
                         I._norm("HELLO world"))

    def test_whitespace_collapsed(self):
        self.assertEqual(I._norm("  what  a   wonderful  world  "),
                         "what a wonderful world")

    def test_empty(self):
        self.assertEqual(I._norm(""), "")
        self.assertEqual(I._norm(None), "")


# ── Schema ───────────────────────────────────────────────────────

class TestSchema(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def test_ensure_schema_creates_cache_table(self):
        I._ensure_schema(self.conn)
        cols = {c[1] for c in self.conn.execute(
            "PRAGMA table_info(song_year_cache)")}
        for required in ("artist_key", "title_key", "year", "source",
                         "n_matches", "fetched_at"):
            self.assertIn(required, cols)

    def test_ensure_schema_idempotent(self):
        I._ensure_schema(self.conn)
        I._ensure_schema(self.conn)   # should not raise


# ── Candidate selection ─────────────────────────────────────────

class TestFindCandidates(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))
        I._ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def test_lists_distinct_artist_title(self):
        _t(self.conn, "u1", "Pink Floyd", "Comfortably Numb", 1979)
        _t(self.conn, "u2", "Pink Floyd", "Hey You", 1979)
        _t(self.conn, "u3", "Led Zeppelin", "Stairway to Heaven", 1971)
        groups = I.find_candidates(self.conn)
        self.assertEqual(len(groups), 3)

    def test_normalisation_collapses_duplicates(self):
        # Two rows differing only in apostrophe variant should count as one
        _t(self.conn, "u1", "10cc", "Art for Art's Sake", 2017)
        _t(self.conn, "u2", "10cc", "Art for Art’s Sake", 2018)
        groups = I.find_candidates(self.conn)
        self.assertEqual(len(groups), 1)

    def test_cached_groups_excluded(self):
        _t(self.conn, "u1", "Pink Floyd", "Hey You", 1979)
        _t(self.conn, "u2", "Led Zeppelin", "Stairway to Heaven", 1971)
        # Pre-cache the PF entry
        self.conn.execute(
            "INSERT INTO song_year_cache "
            "(artist_key, title_key, year, source, fetched_at) "
            "VALUES ('pink floyd', 'hey you', 1979, 'mb_recording', 0)")
        self.conn.commit()
        groups = I.find_candidates(self.conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0], "Led Zeppelin")

    def test_empty_artist_or_title_excluded(self):
        _t(self.conn, "u1", "",          "Hey You",  1979)
        _t(self.conn, "u2", "Pink Floyd", "",        1979)
        self.assertEqual(I.find_candidates(self.conn), [])

    def test_unknown_artist_phantom_excluded(self):
        _t(self.conn, "u1", "(Unknown Artist)", "01 - Maneater", 2017)
        _t(self.conn, "u2", "[unknown]",        "[untitled]",    2017)
        _t(self.conn, "u3", "Hall & Oates",     "Maneater",      2017)
        groups = I.find_candidates(self.conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][0], "Hall & Oates")

    def test_track_number_prefix_title_excluded(self):
        # Title starts with track-number prefix → filename-derived,
        # MB will never match. Skip.
        _t(self.conn, "u1", "Hall & Oates", "01 - Maneater", 2017)
        _t(self.conn, "u2", "Hall & Oates", "10 You're So Fine", 2017)
        _t(self.conn, "u3", "Hall & Oates", "Maneater", 2017)  # OK
        groups = I.find_candidates(self.conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0][1], "Maneater")


# ── Lookup phase (mocked MB) ────────────────────────────────────

class TestRunLookup(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))
        I._ensure_schema(self.conn)
        # Disable rate limit for tests (don't actually sleep).
        self._rl_patcher = patch.object(I, "_RATE_LIMIT_SEC", 0.0)
        self._rl_patcher.start()

    def tearDown(self):
        self._rl_patcher.stop()
        self.conn.close()
        os.unlink(self._p)

    def test_hit_writes_min_year_to_cache(self):
        with patch.object(I, "search_song_year",
                          return_value=(1967, 234)):
            stats = I.run_lookup(self.conn,
                [("Louis Armstrong", "What a Wonderful World")],
                limit=0, verbose=False)
        self.assertEqual(stats["hits"], 1)
        r = self.conn.execute(
            "SELECT year, source, n_matches FROM song_year_cache"
        ).fetchone()
        self.assertEqual(r, (1967, "mb_recording", 234))

    def test_no_match_writes_notfound(self):
        with patch.object(I, "search_song_year", return_value=(None, 0)):
            stats = I.run_lookup(self.conn,
                [("Obscure Artist", "Unknown Song")],
                limit=0, verbose=False)
        self.assertEqual(stats["notfound"], 1)
        r = self.conn.execute(
            "SELECT year, source FROM song_year_cache").fetchone()
        self.assertEqual(r, (None, "notfound"))

    def test_transient_error_leaves_uncached(self):
        with patch.object(I, "search_song_year",
                          side_effect=I.TransientError("HTTP 503")):
            stats = I.run_lookup(self.conn,
                [("Some Artist", "Some Song")],
                limit=0, verbose=False)
        self.assertEqual(stats["transient"], 1)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM song_year_cache").fetchone()[0]
        self.assertEqual(n, 0, "Transient must NOT cache — try again later")

    def test_limit_stops_lookup_early(self):
        candidates = [(f"A{i}", f"T{i}") for i in range(10)]
        with patch.object(I, "search_song_year", return_value=(2000, 1)):
            stats = I.run_lookup(self.conn, candidates,
                                 limit=3, verbose=False)
        self.assertEqual(stats["queried"], 3)


# ── Apply phase ─────────────────────────────────────────────────

class TestRunApply(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))
        I._ensure_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def _cache(self, artist, title, year, source="mb_recording"):
        self.conn.execute(
            "INSERT INTO song_year_cache "
            "(artist_key, title_key, year, source, fetched_at) "
            "VALUES (?,?,?,?,0)",
            (I._norm(artist), I._norm(title), year, source))
        self.conn.commit()

    def test_apply_writes_when_eff_later_than_cached(self):
        # Track currently effective at 2017; MB cache says 1967.
        _t(self.conn, "u1", "Louis Armstrong", "What a Wonderful World",
           file_year=2017)
        self._cache("Louis Armstrong", "What a Wonderful World", 1967)
        stats = I.run_apply(self.conn, verbose=False)
        self.assertEqual(stats["applied"], 1)
        r = self.conn.execute(
            "SELECT year, source FROM metadata_overrides WHERE url='u1'"
        ).fetchone()
        self.assertEqual(tuple(r), (1967, "manual"))

    def test_apply_skips_when_eff_already_at_or_before_cached(self):
        _t(self.conn, "u1", "Louis Armstrong", "What a Wonderful World",
           file_year=1967)
        self._cache("Louis Armstrong", "What a Wonderful World", 1967)
        stats = I.run_apply(self.conn, verbose=False)
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(stats["already_ok"], 1)

    def test_apply_skips_manual_overrides(self):
        # User has already manually set the year. Don't touch.
        _t(self.conn, "u1", "Louis Armstrong", "What a Wonderful World",
           file_year=2017, override_year=1968, override_src='manual')
        self._cache("Louis Armstrong", "What a Wonderful World", 1967)
        stats = I.run_apply(self.conn, verbose=False)
        self.assertEqual(stats["applied"], 0)
        self.assertEqual(stats["skipped_manual"], 1)
        r = self.conn.execute(
            "SELECT year, source FROM metadata_overrides WHERE url='u1'"
        ).fetchone()
        self.assertEqual(tuple(r), (1968, "manual"),
                         "User edit must survive untouched")

    def test_apply_overwrites_acoustid_override(self):
        # AcoustID set 2002 from a compilation recording entry;
        # MB-search finds the earlier 1967 original. Overwrite.
        _t(self.conn, "u1", "Louis Armstrong", "What a Wonderful World",
           file_year=2017, override_year=2002, override_src='acoustid')
        self._cache("Louis Armstrong", "What a Wonderful World", 1967)
        stats = I.run_apply(self.conn, verbose=False)
        self.assertEqual(stats["applied"], 1)
        r = self.conn.execute(
            "SELECT year, source FROM metadata_overrides WHERE url='u1'"
        ).fetchone()
        self.assertEqual(tuple(r), (1967, "manual"))

    def test_apply_skips_notfound_entries(self):
        _t(self.conn, "u1", "Obscure", "Song", file_year=2017)
        self._cache("Obscure", "Song", year=None, source='notfound')
        stats = I.run_apply(self.conn, verbose=False)
        self.assertEqual(stats["applied"], 0)
        # No metadata_overrides row created
        n = self.conn.execute(
            "SELECT COUNT(*) FROM metadata_overrides").fetchone()[0]
        self.assertEqual(n, 0)

    def test_apply_handles_apostrophe_normalisation(self):
        # Cache key has straight apostrophe; track has curly.
        # Apply must still find the match.
        _t(self.conn, "u1", "10cc", "Art for Art’s Sake",
           file_year=2017)
        self._cache("10cc", "Art for Art's Sake", 1975)
        stats = I.run_apply(self.conn, verbose=False)
        self.assertEqual(stats["applied"], 1)


# ── MB query escape ─────────────────────────────────────────────

class TestMbEscape(unittest.TestCase):

    def test_quotes_escaped(self):
        self.assertEqual(I._mb_escape('Hello "World"'),
                         'Hello \\"World\\"')

    def test_backslash_escaped(self):
        self.assertEqual(I._mb_escape("a\\b"), "a\\\\b")

    def test_plain_string_unchanged(self):
        self.assertEqual(I._mb_escape("Pink Floyd"), "Pink Floyd")


if __name__ == "__main__":
    unittest.main()
