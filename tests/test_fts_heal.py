"""FTS5 auto-heal on mass writes (2026-07-03).

The recurring tracks_fts shadow-table corruption ("database disk image is
malformed") has struck 6× since Apr 2026. The heal-and-retry wrapper
existed only on the Indexer's UPnP crawl path; the 5th and 6th occurrences
were tripped by the LocalFs clear+rebuild flow — a mass DELETE fired the
FTS delete triggers straight into the corrupt index with no heal.

These tests pin the shared `LibraryDB.run_with_fts_heal` wrapper and prove
the mass-write entry points (`clear`, `upsert_tracks`) route through it.
"""
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from dlna_library import LibraryDB

MALFORMED = sqlite3.DatabaseError("database disk image is malformed")


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)


class TestRunWithFtsHeal(_Base):
    def test_malformed_once_repairs_and_retries(self):
        calls = []

        def body():
            calls.append(1)
            if len(calls) == 1:
                raise MALFORMED
            return "ok"

        with mock.patch.object(self.db, "repair_fts") as rep:
            self.assertEqual(self.db.run_with_fts_heal(body), "ok")
        rep.assert_called_once()
        self.assertEqual(len(calls), 2)

    def test_other_errors_reraised_without_repair(self):
        def body():
            raise ValueError("nope")

        with mock.patch.object(self.db, "repair_fts") as rep:
            with self.assertRaises(ValueError):
                self.db.run_with_fts_heal(body)
        rep.assert_not_called()

    def test_malformed_twice_reraised_after_one_repair(self):
        def body():
            raise MALFORMED

        with mock.patch.object(self.db, "repair_fts") as rep:
            with self.assertRaises(sqlite3.DatabaseError):
                self.db.run_with_fts_heal(body)
        rep.assert_called_once()

    def test_args_passed_through(self):
        self.assertEqual(
            self.db.run_with_fts_heal(lambda a, b=0: a + b, 2, b=3), 5)


class TestMassWritePathsAreHealed(_Base):
    """clear() and upsert_tracks() must route through run_with_fts_heal —
    a malformed error inside them is repaired and the operation retried."""

    def _rows(self, n=3):
        return [{"url": f"http://x/{i}", "title": f"t{i}",
                 "artist": "A", "album": "B"} for i in range(n)]

    def test_upsert_routes_through_heal(self):
        with mock.patch.object(self.db, "run_with_fts_heal",
                               wraps=self.db.run_with_fts_heal) as heal:
            self.db.upsert_tracks("uuid:t", self._rows())
        heal.assert_called_once()

    def test_clear_routes_through_heal(self):
        self.db.upsert_tracks("uuid:t", self._rows())
        with mock.patch.object(self.db, "run_with_fts_heal",
                               wraps=self.db.run_with_fts_heal) as heal:
            self.db.clear("uuid:t")
        heal.assert_called_once()

    def test_clear_survives_one_malformed(self):
        """First DELETE raises malformed → repair_fts (real) runs → the
        retry succeeds and the rows are gone."""
        self.db.upsert_tracks("uuid:t", self._rows())
        real_write = self.db._pool.write
        state = {"failed": False}

        class _FailingCtx:
            def __enter__(ctx):
                ctx._inner = real_write()
                conn = ctx._inner.__enter__()
                if not state["failed"]:
                    state["failed"] = True
                    ctx._inner.__exit__(None, None, None)
                    raise MALFORMED
                return conn

            def __exit__(ctx, *a):
                return ctx._inner.__exit__(*a)

        with mock.patch.object(self.db._pool, "write", _FailingCtx):
            self.db.clear("uuid:t")
        with self.db._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        self.assertEqual(n, 0)

    def test_upsert_survives_one_malformed(self):
        real_write = self.db._pool.write
        state = {"failed": False}

        class _FailingCtx:
            def __enter__(ctx):
                ctx._inner = real_write()
                conn = ctx._inner.__enter__()
                if not state["failed"]:
                    state["failed"] = True
                    ctx._inner.__exit__(None, None, None)
                    raise MALFORMED
                return conn

            def __exit__(ctx, *a):
                return ctx._inner.__exit__(*a)

        with mock.patch.object(self.db._pool, "write", _FailingCtx):
            self.db.upsert_tracks("uuid:t", self._rows())
        with self.db._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        self.assertEqual(n, 3)


class TestIndexerDelegates(_Base):
    def test_indexer_wrapper_uses_library_heal(self):
        """Indexer._run_with_fts_heal delegates to LibraryDB so there is
        exactly one heal implementation."""
        from dlna_indexer import Indexer
        idx = Indexer(self.db)
        with mock.patch.object(self.db, "run_with_fts_heal",
                               wraps=self.db.run_with_fts_heal) as heal:
            out = idx._run_with_fts_heal(lambda: "done")
        self.assertEqual(out, "done")
        heal.assert_called_once()


if __name__ == "__main__":
    unittest.main()
