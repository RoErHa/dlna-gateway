#!/usr/bin/env python3
"""
tests/test_loudness.py — LoudnessScanner unit tests against a temp SQLite.

Focus: the data invariants — what bare_tracks returns, how ffmpeg output
parses into LUFS, how gain_db is computed against the -18 LUFS target,
and what survives a clear(udn). All ffmpeg subprocess calls are mocked
so the suite stays under 1 second.

Run standalone:
    python3 -m unittest tests.test_loudness -v
"""
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
from dlna_loudness import LoudnessScanner, TARGET_LUFS, _parse_ebur128


# Real ffmpeg output (-af ebur128 with -framelog=quiet) — captured from
# a 30-second test track. The "Integrated loudness" block at the tail
# is the only thing we parse.
_REAL_EBUR128_OUTPUT = """\
[Parsed_ebur128_0 @ 0x600000d8c000] Summary:

  Integrated loudness:
    I:         -16.4 LUFS
    Threshold: -26.4 LUFS

  Loudness range:
    LRA:         5.6 LU
    Threshold: -36.4 LUFS
    LRA low:   -19.5 LUFS
    LRA high:  -13.9 LUFS

[out#0/null @ 0x600000d8c180] video:0KiB audio:5kB subtitle:0kB other streams:0kB
"""


# ── Pure-function parser tests ────────────────────────────────────

class TestParser(unittest.TestCase):

    def test_real_ebur128_output(self):
        lufs = _parse_ebur128(_REAL_EBUR128_OUTPUT)
        self.assertEqual(lufs, -16.4)

    def test_negative_with_decimal(self):
        out = "  Integrated loudness:\n    I:         -23.7 LUFS\n"
        self.assertEqual(_parse_ebur128(out), -23.7)

    def test_positive_loudness(self):
        # Theoretically possible (very loud master)
        out = "  Integrated loudness:\n    I:           1.2 LUFS\n"
        self.assertEqual(_parse_ebur128(out), 1.2)

    def test_garbled_output_returns_none(self):
        for bad in ("", "no useful data", "Integrated loudness: nope",
                    "I: NaN LUFS", "ffmpeg crashed"):
            with self.subTest(bad=bad):
                self.assertIsNone(_parse_ebur128(bad))


# ── DB-level scanner tests ────────────────────────────────────────

class TestLoudnessScanner(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"
        self.scanner = LoudnessScanner(self.db)
        # 7 tracks — every track has a URL (file_path is irrelevant since
        # AssetUPnP-style servers never populate it; ffmpeg accepts the
        # HTTP URL directly).
        with self.db._pool.write() as conn:
            for i in range(7):
                conn.execute(
                    "INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
                    "VALUES (?,?,?,?,?,?)",
                    (self.udn, f"obj{i}", f"http://t/{i}", f"T{i}", "A",
                     "Album"))

    def tearDown(self):
        try:
            self.scanner.stop()
        finally:
            os.unlink(self._path)

    def test_bare_tracks_returns_all_unscanned(self):
        bare = self.scanner.bare_tracks()
        self.assertEqual(len(bare), 7)
        # Each entry is a (url,) tuple
        for b in bare:
            self.assertEqual(len(b), 1)
            self.assertTrue(b[0].startswith("http://t/"))

    def test_bare_tracks_excludes_already_scanned(self):
        # Mark two as already analysed
        with self.db._pool.write() as conn:
            now = int(time.time())
            conn.execute("INSERT INTO track_loudness (url, lufs, gain_db, scanned_at) "
                         "VALUES (?,?,?,?)", ("http://t/0", -16.0, -2.0, now))
            conn.execute("INSERT INTO track_loudness (url, lufs, gain_db, scanned_at) "
                         "VALUES (?,?,?,?)", ("http://t/1", -20.0, 2.0, now))
        bare = self.scanner.bare_tracks()
        urls = [b[0] for b in bare]
        self.assertNotIn("http://t/0", urls)
        self.assertNotIn("http://t/1", urls)
        self.assertEqual(len(bare), 5)

    def test_negative_cache_blocks_rescan(self):
        # Failed scans must persist a row (with NULL lufs) so we don't retry
        # every restart — same convention as album_art.source='notfound'.
        with self.db._pool.write() as conn:
            conn.execute("INSERT INTO track_loudness (url, lufs, gain_db, scanned_at) "
                         "VALUES (?,?,?,?)", ("http://t/0", None, 0.0, int(time.time())))
        bare = self.scanner.bare_tracks()
        urls = [b[0] for b in bare]
        self.assertNotIn("http://t/0", urls)

    def test_clear_does_not_delete_loudness(self):
        # Survives clear(udn) — same invariant as album_art and play_counts.
        with self.db._pool.write() as conn:
            conn.execute("INSERT INTO track_loudness (url, lufs, gain_db, scanned_at) "
                         "VALUES (?,?,?,?)", ("http://t/0", -14.0, -4.0, int(time.time())))
        self.db.clear(self.udn)
        with self.db._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM track_loudness").fetchone()["n"]
        self.assertEqual(n, 1, "track_loudness rows must survive clear(udn)")

    def test_run_once_writes_lufs_and_gain(self):
        # Mock _analyze to return a known LUFS for each track URL.
        per_url = {
            "http://t/0": -10.0,   # loud
            "http://t/1": -18.0,   # at target
            "http://t/2": -25.0,   # quiet
            "http://t/3": -14.0,   # mildly loud
            "http://t/4": -22.0,   # mildly quiet
            "http://t/5": -16.0,   # near target
            "http://t/6": -20.0,   # slightly quiet
        }

        def fake_analyze(audio_src):
            return per_url[audio_src]

        with patch.object(LoudnessScanner, "_analyze", side_effect=fake_analyze):
            self.scanner.run_once()

        with self.db._pool.read() as conn:
            rows = {r["url"]: r for r in
                    conn.execute("SELECT * FROM track_loudness").fetchall()}
        self.assertEqual(len(rows), 7)
        # gain_db == TARGET_LUFS - lufs
        self.assertAlmostEqual(rows["http://t/0"]["gain_db"],
                               TARGET_LUFS - (-10.0), places=2)
        self.assertAlmostEqual(rows["http://t/1"]["gain_db"], 0.0, places=2)
        self.assertAlmostEqual(rows["http://t/2"]["gain_db"],
                               TARGET_LUFS - (-25.0), places=2)
        # And the LUFS value itself was persisted
        self.assertEqual(rows["http://t/3"]["lufs"], -14.0)

    def test_run_once_records_failure_as_negative_cache(self):
        # _analyze returns None for a broken track → row written with lufs=NULL,
        # gain_db=0.0 so we don't retry forever.
        def fake_analyze(audio_src):
            if audio_src == "http://t/0":
                return None
            return -18.0  # rest at-target

        with patch.object(LoudnessScanner, "_analyze", side_effect=fake_analyze):
            self.scanner.run_once()

        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT * FROM track_loudness WHERE url=?",
                ("http://t/0",)).fetchone()
        self.assertIsNotNone(row, "failed scans must still leave a sticky row")
        self.assertIsNone(row["lufs"], "failed scan must store lufs=NULL")
        self.assertEqual(row["gain_db"], 0.0)

    def test_gain_db_clamped(self):
        # Tracks at extreme loudness (e.g. silence at -70 LUFS) must not
        # produce absurd gain values like +52 dB. Clamp ±20 dB.
        with patch.object(LoudnessScanner, "_analyze", return_value=-70.0):
            self.scanner.run_once()
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT gain_db FROM track_loudness LIMIT 1").fetchone()
        self.assertLessEqual(row["gain_db"], 20.0)
        self.assertGreaterEqual(row["gain_db"], -20.0)

    def test_trigger_idempotent_while_running(self):
        # Block one run forever, then call trigger() again — should be a no-op,
        # not start a second thread.
        block = [True]

        def slow_analyze(path):
            while block[0]:
                time.sleep(0.01)
            return -18.0

        with patch.object(LoudnessScanner, "_analyze", side_effect=slow_analyze):
            self.scanner.trigger()
            time.sleep(0.1)
            first_thread = self.scanner._thread
            self.scanner.trigger()  # should be no-op
            self.assertIs(self.scanner._thread, first_thread,
                          "second trigger() must not spawn a second thread")
            block[0] = False  # let it finish
            self.scanner.stop()

    def test_ffmpeg_missing_does_not_poison_cache(self):
        """Regression guard for the launchd PATH bug (2026-05-05): when
        ffmpeg can't be found, run_once must bail BEFORE iterating —
        otherwise every track gets sticky-cached as failed and the scan
        permanently can't recover, even after ffmpeg is installed."""
        import dlna_loudness
        # Simulate ffmpeg not being on PATH
        with patch.object(dlna_loudness, "_FFMPEG_PATH", None):
            stats = self.scanner.run_once()
        self.assertEqual(stats["total"], 0,
                         "scanner must not iterate when ffmpeg is missing")
        with self.db._pool.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM track_loudness").fetchone()["n"]
        self.assertEqual(n, 0,
                         "no rows should be written when ffmpeg is missing")

    def test_start_initial_scan_fires_after_delay(self):
        with patch.object(LoudnessScanner, "_analyze", return_value=-18.0):
            self.scanner.start_initial_scan(delay=0.05)
            time.sleep(0.5)
            self.scanner.stop()
        with self.db._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM track_loudness").fetchone()["n"]
        self.assertEqual(n, 7)


# ── gain_db_for_url query helper ─────────────────────────────────

class TestGainDbForUrl(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        with self.db._pool.write() as conn:
            conn.execute("INSERT INTO track_loudness (url, lufs, gain_db, scanned_at) "
                         "VALUES (?,?,?,?)", ("http://x.flac", -16.0, -2.0, 0))

    def tearDown(self):
        os.unlink(self._path)

    def test_returns_gain_for_known_url(self):
        self.assertAlmostEqual(self.db.gain_db_for_url("http://x.flac"), -2.0)

    def test_returns_zero_for_unknown_url(self):
        # Don't fail-fast for missing tracks — just no gain applied.
        self.assertEqual(self.db.gain_db_for_url("http://unknown"), 0.0)


if __name__ == "__main__":
    unittest.main()
