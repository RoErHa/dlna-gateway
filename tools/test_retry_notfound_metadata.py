#!/usr/bin/env python3
"""
tools/test_retry_notfound_metadata.py — tests for the cleanup script.

Builds throw-away DBs in a tempdir and verifies:
  - Reporting (no deletions) is the default.
  - `--all` deletes only source='notfound' rows, leaving acoustid /
    manual / video_skip untouched.
  - `--since TS` only deletes notfound rows newer than the timestamp.
  - `--dry-run` doesn't act.
  - Mutually-exclusive `--all` + `--since` is rejected.
  - Log-scan finds HTTP 5xx lines in a synthetic log.

Run standalone:
    python3 -m unittest tools.test_retry_notfound_metadata -v
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import retry_notfound_metadata as R  # noqa: E402


def _build_test_db(path: Path) -> None:
    """Create a minimal metadata_overrides table populated with one row
    of each source value, with deterministic updated_at timestamps."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE metadata_overrides (
            url        TEXT PRIMARY KEY,
            artist     TEXT,
            album      TEXT,
            title      TEXT,
            genre      TEXT,
            updated_at TEXT,
            source     TEXT NOT NULL DEFAULT 'manual'
        )
    """)
    rows = [
        # source,        url,                 updated_at
        ("manual",       "http://x/manual",   "2026-05-25 09:00:00"),
        ("acoustid",     "http://x/ac1",      "2026-05-25 14:00:00"),
        ("acoustid",     "http://x/ac2",      "2026-05-25 14:05:00"),
        ("notfound",     "http://x/nf-pre",   "2026-05-25 14:10:00"),
        ("notfound",     "http://x/nf-mid1",  "2026-05-25 14:35:00"),
        ("notfound",     "http://x/nf-mid2",  "2026-05-25 14:40:00"),
        ("notfound",     "http://x/nf-post",  "2026-05-25 15:30:00"),
        ("video_skip",   "http://x/vid.mp4",  "2026-05-25 14:20:00"),
    ]
    for source, url, ts in rows:
        conn.execute(
            "INSERT INTO metadata_overrides "
            "(url, source, updated_at) VALUES (?,?,?)", (url, source, ts))
    conn.commit()
    conn.close()


class TestCleanupTool(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="retry-test-"))
        self.db = self.tmp / "library.db"
        _build_test_db(self.db)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _counts_by_source(self):
        conn = sqlite3.connect(str(self.db))
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM metadata_overrides "
            "GROUP BY source").fetchall()
        conn.close()
        return {s: n for s, n in rows}

    # ── reporting (no action) ──

    def test_default_is_report_only(self):
        with patch("sys.stdout", new_callable=StringIO) as stdout:
            rc = R.main(["--db", str(self.db),
                         "--log", str(self.tmp / "no-such-log")])
        self.assertEqual(rc, 0)
        # Nothing deleted
        self.assertEqual(self._counts_by_source().get("notfound"), 4)
        # Output mentions sources and counts
        out = stdout.getvalue()
        self.assertIn("notfound", out)
        self.assertIn("acoustid", out)
        self.assertIn("video_skip", out)
        self.assertIn("No action requested", out)

    # ── --all deletes only notfound ──

    def test_all_deletes_only_notfound(self):
        with patch("sys.stdout", new_callable=StringIO):
            rc = R.main(["--db", str(self.db),
                         "--log", str(self.tmp / "no-such-log"),
                         "--all", "-y"])
        self.assertEqual(rc, 0)
        c = self._counts_by_source()
        self.assertNotIn("notfound", c, "all notfound rows should be gone")
        self.assertEqual(c.get("manual"),     1, "manual must be untouched")
        self.assertEqual(c.get("acoustid"),   2, "acoustid must be untouched")
        self.assertEqual(c.get("video_skip"), 1, "video_skip must be untouched")

    # ── --since deletes only newer notfound ──

    def test_since_deletes_only_newer(self):
        # Outage window starts 14:30 → should delete the two mid rows
        # and the post row (14:35, 14:40, 15:30), leaving the 14:10 row.
        with patch("sys.stdout", new_callable=StringIO):
            rc = R.main(["--db", str(self.db),
                         "--log", str(self.tmp / "no-such-log"),
                         "--since", "2026-05-25 14:30:00", "-y"])
        self.assertEqual(rc, 0)
        conn = sqlite3.connect(str(self.db))
        remaining = conn.execute(
            "SELECT url FROM metadata_overrides "
            "WHERE source='notfound' ORDER BY updated_at").fetchall()
        conn.close()
        self.assertEqual([r[0] for r in remaining],
                         ["http://x/nf-pre"],
                         "only pre-outage notfound row should remain")

    # ── --dry-run never deletes ──

    def test_dry_run_does_not_delete(self):
        before = self._counts_by_source()
        with patch("sys.stdout", new_callable=StringIO):
            rc = R.main(["--db", str(self.db),
                         "--log", str(self.tmp / "no-such-log"),
                         "--all", "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._counts_by_source(), before,
                         "dry-run must not change the DB")

    # ── --all + --since rejected ──

    def test_all_and_since_mutually_exclusive(self):
        with patch("sys.stderr", new_callable=StringIO) as stderr, \
             patch("sys.stdout", new_callable=StringIO):
            rc = R.main(["--db", str(self.db), "--all",
                         "--since", "2026-05-25 14:30:00"])
        self.assertEqual(rc, 2)
        self.assertIn("mutually exclusive", stderr.getvalue())

    # ── missing DB ──

    def test_missing_db_fails_cleanly(self):
        with patch("sys.stderr", new_callable=StringIO) as stderr, \
             patch("sys.stdout", new_callable=StringIO):
            rc = R.main(["--db", str(self.tmp / "no-such.db"), "--all", "-y"])
        self.assertEqual(rc, 2)
        self.assertIn("library.db not found", stderr.getvalue())

    # ── log-scan helper ──

    def test_log_scan_counts_5xx(self):
        log = self.tmp / "ac.log"
        log.write_text(
            "AcoustIDFetcher ✓ track1 ...\n"
            "AcoustIDFetcher: HTTP 503 from AcoustID for fp[:16]=AAA\n"
            "AcoustIDFetcher: HTTP 503 from AcoustID for fp[:16]=BBB\n"
            "AcoustIDFetcher: HTTP 504 from AcoustID for fp[:16]=CCC\n"
            "AcoustIDFetcher ✓ track2 ...\n"
            "AcoustIDFetcher: HTTP 400 from AcoustID — treating as miss\n"
        )
        result = R._scan_log_for_5xx(log)
        self.assertEqual(result["count"], 3,
                         "must count exactly the three 5xx lines, "
                         "not the 4xx line")
        self.assertEqual(len(result["sample"]), 3)

    def test_log_scan_missing_log_returns_empty(self):
        self.assertEqual(R._scan_log_for_5xx(self.tmp / "no.log"), {})


if __name__ == "__main__":
    unittest.main()
