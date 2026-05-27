#!/usr/bin/env python3
"""
tools/test_relink_orphan_overrides.py — unit tests over a throw-away DB.

Run standalone:
    python3 -m unittest tools.test_relink_orphan_overrides -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import relink_orphan_overrides as R  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            udn TEXT, obj_id TEXT, url TEXT NOT NULL,
            title TEXT, artist TEXT, album TEXT,
            bit_depth INTEGER, sample_rate INTEGER
        );
        CREATE TABLE metadata_overrides (
            url TEXT PRIMARY KEY, artist TEXT, album TEXT, title TEXT,
            genre TEXT, year INTEGER, updated_at TEXT,
            source TEXT NOT NULL DEFAULT 'manual'
        );
    """)
    return conn


def _add_track(conn, url):
    conn.execute(
        "INSERT INTO tracks (udn, obj_id, url, title) VALUES (?,?,?,?)",
        ("uuid:test", url, url, "Track"))


def _add_override(conn, url, source="acoustid"):
    conn.execute(
        "INSERT INTO metadata_overrides (url, source) VALUES (?,?)",
        (url, source))


# ── _d_id parser ──────────────────────────────────────────────────

class TestDId(unittest.TestCase):

    def test_positive(self):
        self.assertEqual(R._d_id("http://x/c2/b16/f44100/d12345-coABC.flac"),
                         "d12345")

    def test_negative(self):
        # Some d-ids are negative integers.
        self.assertEqual(R._d_id("http://x/c2/b16/f44100/d-9876543210-coABC.flac"),
                         "d-9876543210")

    def test_no_match(self):
        for u in ("http://other/foo.mp3", "", None,
                  "http://x/just-some-path"):
            with self.subTest(u=u):
                self.assertIsNone(R._d_id(u))


# ── relink logic ──────────────────────────────────────────────────

class TestRelink(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._path))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._path)

    def test_basic_co_hash_rotation(self):
        # Old URL with co-hash OLD; new URL with same d-id but co-hash NEW.
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.conn.commit()
        self.assertEqual(stats["relinked"], 1)
        # Verify override now references the new URL.
        row = self.conn.execute(
            "SELECT url FROM metadata_overrides").fetchone()
        self.assertEqual(row["url"],
                         "http://x/c2/b16/f44100/d12345-coNEW.flac")

    def test_dry_run_doesnt_mutate(self):
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=False)
        # No commit, no mutation.
        self.assertEqual(stats["relinked"], 1,
                         "dry-run still counts what WOULD relink")
        row = self.conn.execute(
            "SELECT url FROM metadata_overrides").fetchone()
        self.assertEqual(row["url"],
                         "http://x/c2/b16/f44100/d12345-coOLD.flac",
                         "dry-run must not actually write")

    def test_no_match_is_trashed_file(self):
        # Override exists, but no current track has matching d-id.
        _add_override(self.conn, "http://x/c2/b16/f44100/d999-coOLD.flac")
        # No tracks at all
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 0)
        self.assertEqual(stats["no_match"], 1)

    def test_no_d_id_in_url(self):
        # Override URL doesn't contain a d-id pattern at all.
        _add_override(self.conn, "http://other-server/foo.mp3")
        _add_track(self.conn, "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 0)
        self.assertEqual(stats["no_d"], 1)

    def test_idempotent(self):
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        s1 = R.relink(self.conn, apply=True)
        self.conn.commit()
        s2 = R.relink(self.conn, apply=True)
        self.conn.commit()
        self.assertEqual(s1["relinked"], 1)
        self.assertEqual(s2["relinked"], 0,
                         "second run finds no orphans — idempotent")

    def test_ambiguous_d_id_skipped(self):
        # Two bare tracks share the same d-id (shouldn't happen with
        # UNIQUE(udn,url) but defend against it).
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW1.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW2.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        # The orphan cannot be safely relinked to either NEW URL.
        self.assertEqual(stats["relinked"], 0)
        self.assertEqual(stats["ambiguous"], 1)

    def test_two_orphans_with_same_d_id_only_one_gets_new_url(self):
        # If two overrides happen to share a d-id (e.g. user had
        # multiple URL aliases of the same file), only ONE claims the
        # new URL — the other goes to ambiguous.
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD-A.flac")
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD-B.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 1)
        self.assertEqual(stats["ambiguous"], 1)


if __name__ == "__main__":
    unittest.main()
