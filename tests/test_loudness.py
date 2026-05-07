#!/usr/bin/env python3
"""
tests/test_loudness.py — LoudnessScanner unit tests against a temp SQLite.

Focus: the data invariants — what bare_tracks returns, how ffmpeg output
parses into true-peak (dBTP), how gain_db is computed against the
-1 dBTP target, and what survives a clear(udn). All ffmpeg subprocess
calls are mocked so the suite stays under 1 second.

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
from dlna_loudness import (LoudnessScanner, TARGET_PEAK_DBTP,
                           _parse_ebur128, _parse_true_peak)


# Real ffmpeg output (-af ebur128=peak=true with -framelog=quiet) —
# captured from a 30-second test track. We parse both the "Integrated
# loudness" and "True peak" blocks; peak drives gain, lufs is
# informational.
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

  True peak:
    Peak:       -0.4 dBFS

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


class TestTruePeakParser(unittest.TestCase):

    def test_real_ebur128_true_peak_block(self):
        # The True peak block is in dBFS (dBTP-equivalent).
        self.assertEqual(_parse_true_peak(_REAL_EBUR128_OUTPUT), -0.4)

    def test_zero_peak(self):
        out = "  True peak:\n    Peak:        0.0 dBFS\n"
        self.assertEqual(_parse_true_peak(out), 0.0)

    def test_positive_peak_intersample(self):
        # Inter-sample peaks can exceed 0 dBFS (lossy codecs / loud masters).
        out = "  True peak:\n    Peak:       +0.6 dBFS\n"
        self.assertEqual(_parse_true_peak(out), 0.6)

    def test_missing_block_returns_none(self):
        # ebur128 was called without peak=true — only the LUFS block exists.
        only_lufs = "  Integrated loudness:\n    I:         -16.4 LUFS\n"
        self.assertIsNone(_parse_true_peak(only_lufs))

    def test_garbled_returns_none(self):
        for bad in ("", "no useful data", "True peak: nope",
                    "Peak: NaN dBFS"):
            with self.subTest(bad=bad):
                self.assertIsNone(_parse_true_peak(bad))


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
            conn.execute("INSERT INTO track_loudness (url, lufs, peak_db, gain_db, scanned_at) "
                         "VALUES (?,?,?,?,?)", ("http://t/0", -16.0, -0.5, -0.5, now))
            conn.execute("INSERT INTO track_loudness (url, lufs, peak_db, gain_db, scanned_at) "
                         "VALUES (?,?,?,?,?)", ("http://t/1", -20.0, -3.0, +2.0, now))
        bare = self.scanner.bare_tracks()
        urls = [b[0] for b in bare]
        self.assertNotIn("http://t/0", urls)
        self.assertNotIn("http://t/1", urls)
        self.assertEqual(len(bare), 5)

    def test_negative_cache_blocks_rescan(self):
        # Failed scans must persist a row (with NULL peak_db) so we don't
        # retry every restart — same convention as album_art.source='notfound'.
        with self.db._pool.write() as conn:
            conn.execute("INSERT INTO track_loudness (url, lufs, peak_db, gain_db, scanned_at) "
                         "VALUES (?,?,?,?,?)", ("http://t/0", None, None, 0.0, int(time.time())))
        bare = self.scanner.bare_tracks()
        urls = [b[0] for b in bare]
        self.assertNotIn("http://t/0", urls)

    def test_clear_does_not_delete_loudness(self):
        # Survives clear(udn) — same invariant as album_art and play_counts.
        with self.db._pool.write() as conn:
            conn.execute("INSERT INTO track_loudness (url, lufs, peak_db, gain_db, scanned_at) "
                         "VALUES (?,?,?,?,?)", ("http://t/0", -14.0, -2.5, +1.5, int(time.time())))
        self.db.clear(self.udn)
        with self.db._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM track_loudness").fetchone()["n"]
        self.assertEqual(n, 1, "track_loudness rows must survive clear(udn)")

    def test_run_once_writes_peak_and_gain(self):
        # Mock _analyze to return a known (lufs, peak_db) for each URL.
        per_url = {
            "http://t/0": (-10.0,  0.0),   # peak at ceiling → gain -1 dB
            "http://t/1": (-18.0, -1.0),   # peak at target → gain 0 dB
            "http://t/2": (-25.0, -3.0),   # quiet → gain +2 dB (clamped)
            "http://t/3": (-14.0, -0.5),   # gain -0.5 dB
            "http://t/4": (-22.0, -1.8),   # gain +0.8 dB
            "http://t/5": (-16.0, -2.0),   # gain +1.0 dB
            "http://t/6": (-20.0, -2.5),   # gain +1.5 dB
        }

        def fake_analyze(audio_src):
            return per_url[audio_src]

        with patch.object(LoudnessScanner, "_analyze", side_effect=fake_analyze):
            self.scanner.run_once()

        with self.db._pool.read() as conn:
            rows = {r["url"]: r for r in
                    conn.execute("SELECT * FROM track_loudness").fetchall()}
        self.assertEqual(len(rows), 7)
        # gain_db == TARGET_PEAK_DBTP - peak_db (clamped ±2)
        self.assertAlmostEqual(rows["http://t/0"]["gain_db"],
                               TARGET_PEAK_DBTP - 0.0, places=2)
        self.assertAlmostEqual(rows["http://t/1"]["gain_db"], 0.0, places=2)
        # -25 LUFS, peak -3 dBFS → desired +2; ±2 clamp keeps it at +2
        self.assertAlmostEqual(rows["http://t/2"]["gain_db"], 2.0, places=2)
        # peak_db AND lufs both persisted
        self.assertEqual(rows["http://t/3"]["peak_db"], -0.5)
        self.assertEqual(rows["http://t/3"]["lufs"], -14.0)

    def test_run_once_records_failure_as_negative_cache(self):
        # _analyze returns (None, None) for a broken track → row written
        # with peak_db=NULL, gain_db=0.0 so we don't retry forever.
        def fake_analyze(audio_src):
            if audio_src == "http://t/0":
                return (None, None)
            return (-18.0, -1.0)  # rest at-target

        with patch.object(LoudnessScanner, "_analyze", side_effect=fake_analyze):
            self.scanner.run_once()

        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT * FROM track_loudness WHERE url=?",
                ("http://t/0",)).fetchone()
        self.assertIsNotNone(row, "failed scans must still leave a sticky row")
        self.assertIsNone(row["peak_db"], "failed scan must store peak_db=NULL")
        self.assertEqual(row["gain_db"], 0.0)

    def test_gain_db_clamped(self):
        # An extreme silence-rip (peak -60 dBFS) would naively want +59 dB.
        # ±2 dB clamp prevents the renderer volume from blowing up.
        with patch.object(LoudnessScanner, "_analyze",
                          return_value=(-70.0, -60.0)):
            self.scanner.run_once()
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT gain_db FROM track_loudness LIMIT 1").fetchone()
        self.assertLessEqual(row["gain_db"], 2.0)
        self.assertGreaterEqual(row["gain_db"], -2.0)

    def test_trigger_idempotent_while_running(self):
        # Block one run forever, then call trigger() again — should be a no-op,
        # not start a second thread.
        block = [True]

        def slow_analyze(path):
            while block[0]:
                time.sleep(0.01)
            return (-18.0, -1.0)

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

    def test_ffmpeg_disappears_midrun_does_not_poison_cache(self):
        """Regression guard (2026-05-05): if ffmpeg vanishes mid-scan
        (Homebrew updating /opt/homebrew/bin/ffmpeg leaves the symlink
        target briefly missing), the scan must bail WITHOUT caching the
        in-flight track as a sticky negative. Otherwise a few seconds
        of brew activity costs a permanent partial-poisoned cache."""
        # Two tracks succeed; the third raises FileNotFoundError as if
        # ffmpeg disappeared partway.
        call_count = {"n": 0}
        def fake_analyze(audio_src):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return (-18.0, -1.0)
            raise FileNotFoundError("[Errno 2] No such file or directory: 'ffmpeg'")

        with patch.object(LoudnessScanner, "_analyze", side_effect=fake_analyze):
            self.scanner.run_once()

        with self.db._pool.read() as conn:
            ok   = conn.execute("SELECT COUNT(*) AS n FROM track_loudness "
                                "WHERE peak_db IS NOT NULL").fetchone()["n"]
            fail = conn.execute("SELECT COUNT(*) AS n FROM track_loudness "
                                "WHERE peak_db IS NULL").fetchone()["n"]
        self.assertEqual(ok, 2, "the two pre-failure tracks should be cached")
        self.assertEqual(fail, 0,
                         "ffmpeg-vanish must NOT cache anything as failed — "
                         "next trigger will re-try the remaining tracks")

    def test_subprocess_lenient_utf8_decoding(self):
        """Regression for the 2026-05-07 stuck-scanner bug: a track
        whose ffmpeg metadata banner contained a non-UTF-8 byte (Latin-1
        é, 0xe9) crashed `subprocess.run(..., text=True)` mid-scan, the
        thread died, and every restart hit the same track first and
        re-crashed. Fix: errors='replace' on the subprocess decode.

        Verify by patching subprocess.run to behave like real ffmpeg
        when the source has non-UTF-8 metadata (it actually emits the
        bytes through stderr and the framework decodes them); a
        successful scan must produce a valid lufs+peak row, not raise.
        """
        import dlna_loudness
        # Real-world ffmpeg stderr fragment with a Latin-1 'é' (0xe9)
        # in a track title, and a clean ebur128 summary at the tail.
        # We construct it as the *decoded* string the way subprocess.run
        # with errors="replace" would produce — the bug was that strict
        # decoding never even got here, raising before _parse_ebur128.
        replaced_stderr = (
            "[mp3 @ 0x...] title  : Caf�\n"   # U+FFFD substitution
            "  Integrated loudness:\n"
            "    I:         -16.4 LUFS\n"
            "  True peak:\n"
            "    Peak:       -1.2 dBFS\n"
        )

        class FakeProc:
            stderr = replaced_stderr

        # Confirm _parse_* still works on the replaced output.
        self.assertEqual(dlna_loudness._parse_ebur128(replaced_stderr), -16.4)
        self.assertEqual(dlna_loudness._parse_true_peak(replaced_stderr), -1.2)

        # And confirm _analyze rides through subprocess.run cleanly.
        with patch("subprocess.run", return_value=FakeProc()):
            lufs, peak = self.scanner._analyze("http://x/bad-meta.mp3")
        self.assertEqual(lufs, -16.4)
        self.assertEqual(peak, -1.2)

    def test_start_initial_scan_fires_after_delay(self):
        with patch.object(LoudnessScanner, "_analyze",
                          return_value=(-18.0, -1.0)):
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
            conn.execute("INSERT INTO track_loudness (url, lufs, peak_db, gain_db, scanned_at) "
                         "VALUES (?,?,?,?,?)", ("http://x.flac", -16.0, +1.0, -2.0, 0))

    def tearDown(self):
        os.unlink(self._path)

    def test_returns_gain_for_known_url(self):
        self.assertAlmostEqual(self.db.gain_db_for_url("http://x.flac"), -2.0)

    def test_returns_zero_for_unknown_url(self):
        # Don't fail-fast for missing tracks — just no gain applied.
        self.assertEqual(self.db.gain_db_for_url("http://unknown"), 0.0)


if __name__ == "__main__":
    unittest.main()
