#!/usr/bin/env python3
"""
tests/test_winners_only.py — tests for the dedup-winners-only worker
optimization + the sibling-propagate SQL pass.

Run standalone:
    python3 -m unittest tests.test_winners_only -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB


def _add(db, udn, url, title, artist, album, *, bit_depth=16, sample_rate=44100):
    with db._pool.write() as conn:
        conn.execute(
            "INSERT INTO tracks (udn, obj_id, url, title, artist, album, "
            " bit_depth, sample_rate) VALUES (?,?,?,?,?,?,?,?)",
            (udn, url, url, title, artist, album, bit_depth, sample_rate))


class TestBareTracksWinnersOnly(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"

    def tearDown(self):
        os.unlink(self._path)

    def test_winners_only_skips_lower_quality(self):
        # 16-bit + 24-bit copies of the same track. winners_only should
        # return ONLY the 24-bit URL.
        _add(self.db, self.udn, "u/16", "T", "A", "AL",
             bit_depth=16, sample_rate=44100)
        _add(self.db, self.udn, "u/24", "T", "A", "AL",
             bit_depth=24, sample_rate=96000)
        bare = self.db.bare_metadata_tracks(winners_only=True)
        urls = [r[0] for r in bare]
        self.assertEqual(urls, ["u/24"],
                         "winners_only=True must return only 24-bit URL")

    def test_winners_only_keeps_unique_tracks(self):
        # Tracks with no duplicate sibling all survive.
        _add(self.db, self.udn, "u/a", "Track A", "A", "AL")
        _add(self.db, self.udn, "u/b", "Track B", "A", "AL")
        _add(self.db, self.udn, "u/c", "Track C", "A", "AL")
        bare = self.db.bare_metadata_tracks(winners_only=True)
        urls = sorted(r[0] for r in bare)
        self.assertEqual(urls, ["u/a", "u/b", "u/c"])

    def test_default_returns_all_bare(self):
        # Backwards-compat: default is winners_only=False → all rows.
        _add(self.db, self.udn, "u/16", "T", "A", "AL",
             bit_depth=16, sample_rate=44100)
        _add(self.db, self.udn, "u/24", "T", "A", "AL",
             bit_depth=24, sample_rate=96000)
        bare = self.db.bare_metadata_tracks()
        self.assertEqual(sorted(r[0] for r in bare), ["u/16", "u/24"])

    def test_already_processed_excluded_either_way(self):
        # Already-overridden tracks are never bare, regardless of winners_only.
        _add(self.db, self.udn, "u/a", "T", "A", "AL")
        self.db.metadata_override_set("u/a", source="acoustid",
                                      artist="A", album="AL", title="T")
        self.assertEqual(self.db.bare_metadata_tracks(), [])
        self.assertEqual(self.db.bare_metadata_tracks(winners_only=True), [])


class TestPropagateOverridesToSiblings(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"

    def tearDown(self):
        os.unlink(self._path)

    def test_propagates_to_lower_quality(self):
        # 24-bit winner has an override; 16-bit sibling does not.
        _add(self.db, self.udn, "u/24", "T", "A", "AL",
             bit_depth=24, sample_rate=96000)
        _add(self.db, self.udn, "u/16", "T", "A", "AL",
             bit_depth=16, sample_rate=44100)
        self.db.metadata_override_set("u/24", source="acoustid",
                                      artist="Real Artist", album="Real Album",
                                      title="Real Title", year=1987,
                                      update_tracks=False)
        n = self.db.propagate_overrides_to_siblings()
        self.assertEqual(n, 1)
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT artist, album, title, year, source "
                "FROM metadata_overrides WHERE url='u/16'"
            ).fetchone()
        self.assertEqual(row["artist"], "Real Artist")
        self.assertEqual(row["album"],  "Real Album")
        self.assertEqual(row["title"],  "Real Title")
        self.assertEqual(row["year"],   1987)
        # source carries over from the winner — keeps it skipped on
        # next bare_metadata_tracks call.
        self.assertEqual(row["source"], "acoustid")

    def test_no_propagate_when_winner_has_no_override(self):
        # Neither has an override yet — propagate is a no-op.
        _add(self.db, self.udn, "u/24", "T", "A", "AL",
             bit_depth=24, sample_rate=96000)
        _add(self.db, self.udn, "u/16", "T", "A", "AL",
             bit_depth=16, sample_rate=44100)
        n = self.db.propagate_overrides_to_siblings()
        self.assertEqual(n, 0)
        with self.db._pool.read() as conn:
            n_overrides = conn.execute(
                "SELECT COUNT(*) FROM metadata_overrides"
            ).fetchone()[0]
        self.assertEqual(n_overrides, 0)

    def test_no_overwrite_existing_sibling_override(self):
        # If the lower-quality sibling ALREADY has an override (manual
        # edit, prior backfill, etc.), the propagate must NOT touch it.
        _add(self.db, self.udn, "u/24", "T", "A", "AL",
             bit_depth=24, sample_rate=96000)
        _add(self.db, self.udn, "u/16", "T", "A", "AL",
             bit_depth=16, sample_rate=44100)
        self.db.metadata_override_set("u/24", source="acoustid",
                                      artist="Winner Artist",
                                      album="W", title="W", year=1987)
        self.db.metadata_override_set("u/16", source="manual",
                                      artist="User Edit Artist",
                                      album="U", title="U", year=2001)
        n = self.db.propagate_overrides_to_siblings()
        self.assertEqual(n, 0,
                         "INSERT OR IGNORE must not overwrite existing override")
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT artist, year, source FROM metadata_overrides "
                "WHERE url='u/16'").fetchone()
        self.assertEqual(row["artist"], "User Edit Artist")
        self.assertEqual(row["year"],   2001)
        self.assertEqual(row["source"], "manual")

    def test_propagates_to_multiple_lower_siblings(self):
        # 24-bit winner + 16-bit-44.1 + 16-bit-96 — both lower siblings
        # should get the override.
        _add(self.db, self.udn, "u/24-96",  "T", "A", "AL",
             bit_depth=24, sample_rate=96000)
        _add(self.db, self.udn, "u/16-44",  "T", "A", "AL",
             bit_depth=16, sample_rate=44100)
        _add(self.db, self.udn, "u/16-96",  "T", "A", "AL",
             bit_depth=16, sample_rate=96000)
        self.db.metadata_override_set("u/24-96", source="acoustid",
                                      artist="A", album="AL", title="T",
                                      year=1987)
        n = self.db.propagate_overrides_to_siblings()
        self.assertEqual(n, 2)

    def test_no_cross_artist_propagation(self):
        # Two completely unrelated tracks must not be linked.
        _add(self.db, self.udn, "u/x", "Same Title", "Artist X", "Album X")
        _add(self.db, self.udn, "u/y", "Same Title", "Artist Y", "Album Y",
             bit_depth=24, sample_rate=96000)
        self.db.metadata_override_set("u/y", source="acoustid",
                                      artist="Real Y", album="Real Y AL",
                                      title="Real T", year=2000)
        n = self.db.propagate_overrides_to_siblings()
        self.assertEqual(n, 0, "different (artist, album) must not cross-propagate")

    def test_idempotent(self):
        # Running twice doesn't double-propagate.
        _add(self.db, self.udn, "u/24", "T", "A", "AL",
             bit_depth=24, sample_rate=96000)
        _add(self.db, self.udn, "u/16", "T", "A", "AL",
             bit_depth=16, sample_rate=44100)
        self.db.metadata_override_set("u/24", source="acoustid",
                                      artist="A", album="AL", title="T",
                                      year=1987)
        n1 = self.db.propagate_overrides_to_siblings()
        n2 = self.db.propagate_overrides_to_siblings()
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)

    def test_notfound_propagates_too(self):
        # If the winner is 'notfound' (no AcoustID match), the sibling
        # should also become notfound — same recording, same outcome.
        _add(self.db, self.udn, "u/24", "T", "A", "AL",
             bit_depth=24, sample_rate=96000)
        _add(self.db, self.udn, "u/16", "T", "A", "AL",
             bit_depth=16, sample_rate=44100)
        self.db.metadata_override_mark_notfound("u/24")
        n = self.db.propagate_overrides_to_siblings()
        self.assertEqual(n, 1)
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT source FROM metadata_overrides WHERE url='u/16'"
            ).fetchone()
        self.assertEqual(row["source"], "notfound")


if __name__ == "__main__":
    unittest.main()
