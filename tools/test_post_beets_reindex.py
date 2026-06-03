#!/usr/bin/env python3
"""
test_post_beets_reindex.py — unit tests for tools/post_beets_reindex.py.

Throwaway SQLite, no network. The critical invariant under test is the
manual-override safety: ONLY source='acoustid' rows are ever deleted;
manual / notfound / video_skip survive.

    python3 -m unittest tools.test_post_beets_reindex -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import post_beets_reindex as pbr  # noqa: E402


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE metadata_overrides ("
        " url TEXT PRIMARY KEY, artist TEXT, album TEXT, title TEXT,"
        " genre TEXT, year INTEGER, updated_at TEXT, source TEXT)")
    rows = [
        ("u1", "A", "Al", "T1", None, None, "t", "acoustid"),
        ("u2", "B", "Bl", "T2", None, None, "t", "acoustid"),
        ("u3", "C", "Cl", "T3", None, None, "t", "manual"),
        ("u4", None, None, None, None, None, "t", "notfound"),
        ("u5", None, None, None, None, None, "t", "video_skip"),
    ]
    conn.executemany(
        "INSERT INTO metadata_overrides VALUES (?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


class TestPostBeetsReindex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "library.db"
        _make_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _sources(self):
        conn = sqlite3.connect(str(self.db))
        out = dict(conn.execute(
            "SELECT source, COUNT(*) FROM metadata_overrides "
            "GROUP BY source").fetchall())
        conn.close()
        return out

    def test_count_acoustid(self):
        conn = pbr._connect(self.db)
        self.assertEqual(pbr._count_acoustid(conn), 2)
        conn.close()

    def test_delete_removes_only_acoustid(self):
        conn = pbr._connect(self.db)
        deleted = pbr._delete_acoustid(conn)
        conn.close()
        self.assertEqual(deleted, 2)
        srcs = self._sources()
        self.assertNotIn("acoustid", srcs)
        self.assertEqual(srcs.get("manual"), 1)       # NEVER touched
        self.assertEqual(srcs.get("notfound"), 1)
        self.assertEqual(srcs.get("video_skip"), 1)

    def test_backup_creates_copy(self):
        bak = pbr._backup_db(self.db)
        self.assertTrue(bak.exists())
        self.assertNotEqual(bak, self.db)
        self.assertEqual(bak.read_bytes(), self.db.read_bytes())

    def test_dry_run_does_not_mutate(self):
        rc = pbr.main(["--db", str(self.db), "--dry-run", "--no-reindex"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._sources().get("acoustid"), 2)

    def test_default_is_dry_run(self):
        # no --apply → preview only, no deletion
        rc = pbr.main(["--db", str(self.db), "--no-reindex"])
        self.assertEqual(rc, 0)
        self.assertEqual(self._sources().get("acoustid"), 2)

    def test_apply_clean_only_deletes(self):
        rc = pbr.main(["--db", str(self.db), "--apply", "-y",
                       "--no-reindex", "--no-backup"])
        self.assertEqual(rc, 0)
        self.assertNotIn("acoustid", self._sources())
        self.assertEqual(self._sources().get("manual"), 1)

    def test_apply_makes_backup_by_default(self):
        pbr.main(["--db", str(self.db), "--apply", "-y", "--no-reindex"])
        baks = list(self.db.parent.glob("library.db.*.bak"))
        self.assertTrue(baks, "expected a library.db backup before deletion")

    def test_no_clean_and_no_reindex_rejected(self):
        rc = pbr.main(["--db", str(self.db), "--apply",
                       "--no-clean", "--no-reindex"])
        self.assertEqual(rc, 2)

    def test_missing_db_fails_cleanly(self):
        rc = pbr.main(["--db", str(self.db.parent / "nope.db"), "--dry-run"])
        self.assertEqual(rc, 2)

    def test_apply_refuses_when_acoustid_key_set(self):
        # Guard fallback: gateway unreachable (probe → None), so the local
        # env check applies. apply+clean must abort (exit 2), rows intact.
        with mock.patch.object(pbr, "gateway_acoustid_enabled",
                               return_value=None), \
             mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": "abc123"}):
            rc = pbr.main(["--db", str(self.db), "--apply", "-y",
                           "--no-reindex", "--no-backup"])
        self.assertEqual(rc, 2)
        self.assertEqual(self._sources().get("acoustid"), 2)  # untouched

    def test_ignore_flag_overrides_key_guard(self):
        with mock.patch.object(pbr, "gateway_acoustid_enabled",
                               return_value=None), \
             mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": "abc123"}):
            rc = pbr.main(["--db", str(self.db), "--apply", "-y",
                           "--no-reindex", "--no-backup",
                           "--ignore-acoustid-key"])
        self.assertEqual(rc, 0)
        self.assertNotIn("acoustid", self._sources())

    def test_gateway_enabled_blocks_even_with_env_unset(self):
        # Authoritative: gateway reports enabled=True (e.g. key in .env that
        # the local env check can't see) → block regardless of local env.
        with mock.patch.object(pbr, "gateway_acoustid_enabled",
                               return_value=True), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ACOUSTID_API_KEY", None)
            rc = pbr.main(["--db", str(self.db), "--apply", "-y",
                           "--no-reindex", "--no-backup"])
        self.assertEqual(rc, 2)
        self.assertEqual(self._sources().get("acoustid"), 2)  # untouched

    def test_gateway_disabled_allows_even_with_env_set(self):
        # Authoritative: gateway reports enabled=False → proceed even if a
        # stale ACOUSTID_API_KEY lingers in the tool's own env.
        with mock.patch.object(pbr, "gateway_acoustid_enabled",
                               return_value=False), \
             mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": "abc123"}):
            rc = pbr.main(["--db", str(self.db), "--apply", "-y",
                           "--no-reindex", "--no-backup"])
        self.assertEqual(rc, 0)
        self.assertNotIn("acoustid", self._sources())

    def test_key_guard_skipped_when_no_clean(self):
        # --no-clean means we never touch overrides, so the key is irrelevant
        # and the guard must NOT fire. reindex is mocked so nothing external
        # (the live gateway) is touched.
        with mock.patch.dict(os.environ, {"ACOUSTID_API_KEY": "abc123"}), \
             mock.patch.object(pbr, "trigger_reindex",
                               return_value=(True, "mocked")) as m:
            rc = pbr.main(["--db", str(self.db), "--apply", "-y",
                           "--no-clean"])
        self.assertEqual(rc, 0)             # not 2 → guard did not block
        m.assert_called_once()


if __name__ == "__main__":
    unittest.main()
