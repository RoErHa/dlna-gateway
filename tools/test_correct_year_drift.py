#!/usr/bin/env python3
"""
tools/test_correct_year_drift.py — unit tests over a throw-away DB.

Run standalone:
    python3 -m unittest tools.test_correct_year_drift -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import correct_year_drift as C  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            udn TEXT, obj_id TEXT, url TEXT NOT NULL,
            title TEXT, artist TEXT, album TEXT, year INTEGER,
            genre TEXT
        );
        CREATE TABLE metadata_overrides (
            url TEXT PRIMARY KEY,
            artist TEXT, album TEXT, title TEXT, genre TEXT,
            year INTEGER,
            updated_at TEXT,
            source TEXT NOT NULL DEFAULT 'manual'
        );
    """)
    return conn


def _add(conn, url, artist, album, title, year=None, mb_year=None,
         mb_source='acoustid'):
    conn.execute(
        "INSERT INTO tracks (udn, obj_id, url, title, artist, album, year) "
        "VALUES ('u', ?, ?, ?, ?, ?, ?)",
        (url, url, title, artist, album, year))
    if mb_year is not None:
        conn.execute(
            "INSERT INTO metadata_overrides (url, artist, album, title, "
            " year, source) VALUES (?,?,?,?,?,?)",
            (url, artist, album, title, mb_year, mb_source))
    conn.commit()


# ─────────────────────────────────────────────────────────────────

class TestLiveClause(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def test_studio_album_not_flagged_live(self):
        _add(self.conn, "u1", "Pink Floyd", "The Wall", "Comfortably Numb",
             year=1979)
        _add(self.conn, "u2", "Pink Floyd", "Echoes Best Of",
             "Comfortably Numb", year=2001)
        cands = C.find_candidates(self.conn)
        # Echoes should be flagged; The Wall is the earliest.
        urls = [c["url"] for c in cands]
        self.assertIn("u2", urls)
        self.assertNotIn("u1", urls)

    def test_live_album_excluded(self):
        _add(self.conn, "u1", "Pink Floyd", "The Wall", "Comfortably Numb",
             year=1979)
        _add(self.conn, "u2", "Pink Floyd", "Pulse", "Comfortably Numb",
             year=1995)
        cands = C.find_candidates(self.conn)
        # 'pulse' is in LIVE_MARKERS; that row must NOT be a candidate.
        urls = [c["url"] for c in cands]
        self.assertEqual(urls, [])

    def test_unplugged_marker_catches_mtv(self):
        _add(self.conn, "u1", "Nirvana", "Nevermind", "Smells Like Teen Spirit",
             year=1991)
        _add(self.conn, "u2", "Nirvana", "MTV Unplugged in New York",
             "Smells Like Teen Spirit", year=1994)
        cands = C.find_candidates(self.conn)
        # MTV Unplugged row must be excluded as live.
        self.assertEqual([c["url"] for c in cands], [])


class TestCandidateIdentification(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def test_drift_below_threshold_not_flagged(self):
        # 2-year drift is below the 3-year threshold.
        _add(self.conn, "u1", "A", "AL1", "T", year=1979)
        _add(self.conn, "u2", "A", "AL2", "T", year=1981)
        self.assertEqual(C.find_candidates(self.conn), [])

    def test_drift_at_threshold_flagged(self):
        _add(self.conn, "u1", "A", "AL1", "T", year=1979)
        _add(self.conn, "u2", "A", "AL2", "T", year=1982)
        urls = [c["url"] for c in C.find_candidates(self.conn)]
        self.assertEqual(urls, ["u2"])

    def test_bogus_pre_1950_year_excluded_from_floor(self):
        # The 1905 row CANNOT be the earliest_plausible. The 1979 row is.
        _add(self.conn, "u_bogus", "A", "Bogus Compilation", "T", year=1905)
        _add(self.conn, "u_orig",  "A", "Studio Album",      "T", year=1979)
        _add(self.conn, "u_comp",  "A", "Greatest Hits",     "T", year=2001)
        cands = C.find_candidates(self.conn)
        # The 2001 row should be corrected to 1979, NOT 1905.
        urls = {c["url"]: c["should"] for c in cands}
        self.assertEqual(urls.get("u_comp"), 1979)
        # The 1905 row itself has eff=1905 < 1979, so it's not "later" —
        # tool would propose increasing its year. By design we still
        # flag it because eff drift in either direction matters? Actually
        # the SQL only flags rows where eff > earliest, so 1905 is
        # IGNORED (it would BE the floor but is excluded for <1950).
        # Net: tool surfaces only the 2001 row.
        self.assertNotIn("u_bogus", urls)

    def test_mb_year_min_logic(self):
        # File=2017 (reissue), MB=1979 → eff=1979 (MIN). This row should
        # NOT be a candidate because it already buckets at the earliest.
        _add(self.conn, "u1", "PF", "Wall", "Comfortably Numb",
             year=1979)
        _add(self.conn, "u2", "PF", "Reissue", "Comfortably Numb",
             year=2017, mb_year=1979, mb_source='acoustid')
        cands = C.find_candidates(self.conn)
        self.assertEqual(cands, [])

    def test_mb_year_drift_caught(self):
        # File=2001 AND MB=2001 → eff=2001. Another row has 1979.
        # The 2001 row should be a candidate.
        _add(self.conn, "u1", "PF", "Wall", "Comfortably Numb",
             year=1979)
        _add(self.conn, "u2", "PF", "Echoes Best Of", "Comfortably Numb",
             year=2001, mb_year=2001, mb_source='acoustid')
        cands = C.find_candidates(self.conn)
        urls = [c["url"] for c in cands]
        self.assertEqual(urls, ["u2"])
        self.assertEqual(cands[0]["should"], 1979)

    def test_case_insensitive_grouping(self):
        # "pink floyd" vs "Pink Floyd" must group together.
        _add(self.conn, "u1", "Pink Floyd", "Wall", "Comfortably Numb",
             year=1979)
        _add(self.conn, "u2", "pink floyd", "Echoes", "comfortably numb",
             year=2001)
        cands = C.find_candidates(self.conn)
        self.assertEqual([c["url"] for c in cands], ["u2"])

    def test_single_album_no_grouping_no_candidate(self):
        # A song that only appears on one album can never be a candidate.
        _add(self.conn, "u1", "A", "ONE", "T", year=2010)
        self.assertEqual(C.find_candidates(self.conn), [])


class TestApply(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def test_apply_creates_new_override_when_none_exists(self):
        _add(self.conn, "u1", "A", "AL1", "T", year=1979)
        _add(self.conn, "u2", "A", "AL2", "T", year=2001)
        cands = C.find_candidates(self.conn)
        n = C.apply_corrections(self.conn, cands)
        self.conn.commit()
        self.assertEqual(n, 1)
        ov = self.conn.execute(
            "SELECT year, source FROM metadata_overrides WHERE url='u2'"
        ).fetchone()
        self.assertEqual(ov["year"],   1979)
        self.assertEqual(ov["source"], "manual")

    def test_apply_merges_with_existing_acoustid_override(self):
        _add(self.conn, "u1", "A", "AL1", "T", year=1979)
        _add(self.conn, "u2", "A", "AL2", "T",
             year=2001, mb_year=2001, mb_source='acoustid')
        cands = C.find_candidates(self.conn)
        n = C.apply_corrections(self.conn, cands)
        self.conn.commit()
        self.assertEqual(n, 1)
        ov = self.conn.execute(
            "SELECT year, source, artist FROM metadata_overrides WHERE url='u2'"
        ).fetchone()
        self.assertEqual(ov["year"],   1979)
        self.assertEqual(ov["source"], "manual",
                         "tool must overwrite source='manual' even if "
                         "an acoustid row already existed")
        self.assertEqual(ov["artist"], "A",
                         "non-year fields must be preserved on update")

    def test_apply_is_idempotent(self):
        _add(self.conn, "u1", "A", "AL1", "T", year=1979)
        _add(self.conn, "u2", "A", "AL2", "T", year=2001)
        C.apply_corrections(self.conn, C.find_candidates(self.conn))
        self.conn.commit()
        # Second run: u2 now has year=1979 → drift gone → no candidates.
        self.assertEqual(C.find_candidates(self.conn), [])


if __name__ == "__main__":
    unittest.main()
