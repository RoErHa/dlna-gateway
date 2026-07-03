#!/usr/bin/env python3
"""
tests/test_indexer.py — Indexer FTS self-heal regression tests.

Covers `_run_with_fts_heal(body_fn, *args)` — the wrapper that catches
the recurring "database disk image is malformed" failure (FTS5
shadow-table corruption that `PRAGMA integrity_check` does not catch),
calls `LibraryDB.repair_fts()`, and retries the body once.

This is a pure unit test of the retry logic: the body callable and the
library are MagicMocks. The repair_fts() data behaviour is covered in
test_library.py::TestRepairFts.

Run standalone:
    python3 -m unittest tests.test_indexer -v
"""
import os
import sqlite3
import sys
import unittest
from unittest.mock import MagicMock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_indexer import Indexer


def _make_indexer():
    lib = MagicMock()
    # The heal logic moved to LibraryDB (2026-07-03); the Indexer wrapper
    # delegates to it. Bind the REAL implementation onto the mock library
    # (repair_fts stays a MagicMock) so these scenarios still exercise the
    # actual heal-and-retry semantics end-to-end through the Indexer.
    import types
    from dlna_library import LibraryDB
    lib.run_with_fts_heal = types.MethodType(
        LibraryDB.run_with_fts_heal, lib)
    return Indexer(lib), lib


class TestRunWithFtsHeal(unittest.TestCase):

    def test_success_first_try_no_heal(self):
        ix, lib = _make_indexer()
        body = MagicMock(return_value="ok")
        result = ix._run_with_fts_heal(body, "arg1", kw="val")
        self.assertEqual(result, "ok")
        body.assert_called_once_with("arg1", kw="val")
        lib.repair_fts.assert_not_called()

    def test_malformed_then_success_heals_and_retries(self):
        ix, lib = _make_indexer()
        body = MagicMock(side_effect=[
            sqlite3.DatabaseError("database disk image is malformed"),
            "ok",
        ])
        result = ix._run_with_fts_heal(body)
        self.assertEqual(result, "ok")
        self.assertEqual(body.call_count, 2)
        lib.repair_fts.assert_called_once()

    def test_malformed_twice_propagates(self):
        """If the body still raises malformed AFTER the repair+retry,
        give up and let the error bubble — don't loop forever."""
        ix, lib = _make_indexer()
        body = MagicMock(side_effect=[
            sqlite3.DatabaseError("database disk image is malformed"),
            sqlite3.DatabaseError("database disk image is malformed"),
        ])
        with self.assertRaises(sqlite3.DatabaseError):
            ix._run_with_fts_heal(body)
        self.assertEqual(body.call_count, 2)
        # repair_fts called once and only once (no second heal attempt).
        lib.repair_fts.assert_called_once()

    def test_non_malformed_db_error_propagates_immediately(self):
        ix, lib = _make_indexer()
        body = MagicMock(side_effect=sqlite3.DatabaseError("disk I/O error"))
        with self.assertRaises(sqlite3.DatabaseError):
            ix._run_with_fts_heal(body)
        # No retry, no heal — only FTS-malformed triggers repair.
        body.assert_called_once()
        lib.repair_fts.assert_not_called()

    def test_non_db_error_propagates_immediately(self):
        ix, lib = _make_indexer()
        body = MagicMock(side_effect=ValueError("boom"))
        with self.assertRaises(ValueError):
            ix._run_with_fts_heal(body)
        body.assert_called_once()
        lib.repair_fts.assert_not_called()

    def test_repair_failure_propagates(self):
        """If repair_fts() itself errors, surface that — the malformed
        error is no longer recoverable."""
        ix, lib = _make_indexer()
        body = MagicMock(side_effect=sqlite3.DatabaseError("malformed"))
        lib.repair_fts.side_effect = RuntimeError("could not repair")
        with self.assertRaises(RuntimeError):
            ix._run_with_fts_heal(body)
        body.assert_called_once()
        lib.repair_fts.assert_called_once()

    def test_malformed_detection_is_case_insensitive(self):
        # SQLite's exact message is lower-case; be tolerant of variants.
        ix, lib = _make_indexer()
        body = MagicMock(side_effect=[
            sqlite3.DatabaseError("Database Disk Image Is MALFORMED"),
            "ok",
        ])
        result = ix._run_with_fts_heal(body)
        self.assertEqual(result, "ok")
        lib.repair_fts.assert_called_once()


if __name__ == "__main__":
    unittest.main()
