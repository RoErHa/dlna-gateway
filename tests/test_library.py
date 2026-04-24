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


if __name__ == "__main__":
    unittest.main(verbosity=2)
