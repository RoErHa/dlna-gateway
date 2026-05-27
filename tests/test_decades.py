#!/usr/bin/env python3
"""
tests/test_decades.py — tests for the Decade browse category.

Verifies that `all_decades` / `decade_albums` / `decade_tracks`:
  - compute decades from the effective year (override.year COALESCE
    tracks.year — i.e. MB original year if known, file-tag year otherwise).
  - exclude tracks with no year at all (would otherwise show as decade 0).
  - apply the browse dedup_clause (16-bit hidden when 24-bit exists).

Run standalone:
    python3 -m unittest tests.test_decades -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB


def _add_track(db, udn, url, title, artist, album, *,
               year=None, override_year=None,
               bit_depth=16, sample_rate=44100):
    """Insert a track row directly (bypasses upsert_tracks to skip the
    file-path/parsing path) and optionally an acoustid override."""
    with db._pool.write() as conn:
        conn.execute(
            "INSERT INTO tracks (udn, obj_id, url, title, artist, album, "
            " bit_depth, sample_rate, year) VALUES (?,?,?,?,?,?,?,?,?)",
            (udn, url, url, title, artist, album,
             bit_depth, sample_rate, year))
    if override_year is not None:
        db.metadata_override_set(url, source="acoustid",
                                 artist=artist, album=album, title=title,
                                 year=override_year)


class TestAllDecades(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"

    def tearDown(self):
        os.unlink(self._path)

    def test_groups_by_decade(self):
        # Mix of years across decades — verify each gets its own bucket.
        _add_track(self.db, self.udn, "u/a", "T1", "A1", "AL1", year=1973)
        _add_track(self.db, self.udn, "u/b", "T2", "A2", "AL2", year=1979)
        _add_track(self.db, self.udn, "u/c", "T3", "A3", "AL3", year=1980)
        _add_track(self.db, self.udn, "u/d", "T4", "A4", "AL4", year=1999)
        _add_track(self.db, self.udn, "u/e", "T5", "A5", "AL5", year=2020)
        decades = self.db.all_decades(self.udn)
        # Decades present: 1970, 1980, 1990, 2020.
        ds = {d["decade"]: d for d in decades}
        self.assertEqual(set(ds.keys()), {1970, 1980, 1990, 2020})
        self.assertEqual(ds[1970]["track_count"], 2)
        self.assertEqual(ds[1980]["track_count"], 1)
        self.assertEqual(ds[1990]["track_count"], 1)

    def test_chronological_order(self):
        _add_track(self.db, self.udn, "u/1", "T", "A", "AL", year=2010)
        _add_track(self.db, self.udn, "u/2", "T", "A", "AL2", year=1980)
        _add_track(self.db, self.udn, "u/3", "T", "A", "AL3", year=1990)
        decades = [d["decade"] for d in self.db.all_decades(self.udn)]
        self.assertEqual(decades, [1980, 1990, 2010])

    def test_null_year_tracks_excluded(self):
        # Tracks with no year shouldn't appear under "decade 0".
        _add_track(self.db, self.udn, "u/a", "T", "A", "AL", year=None)
        _add_track(self.db, self.udn, "u/b", "T2", "A", "AL", year=1990)
        decades = self.db.all_decades(self.udn)
        ds = {d["decade"]: d for d in decades}
        self.assertNotIn(0, ds, "NULL year must not bucket as decade 0")
        self.assertEqual(set(ds.keys()), {1990})

    def test_override_year_preferred_over_tracks_year(self):
        # tracks.year = 2001 (edition), override.year = 1987 (original).
        # MIN(2001,1987)=1987 → decade should be 1980, not 2000.
        _add_track(self.db, self.udn, "u/a", "Bad", "MJ", "Bad",
                   year=2001, override_year=1987)
        decades = self.db.all_decades(self.udn)
        self.assertEqual({d["decade"] for d in decades}, {1980})

    def test_tracks_year_used_when_no_override(self):
        # No override → fall back to tracks.year for the decade.
        _add_track(self.db, self.udn, "u/a", "T", "A", "AL", year=2015)
        decades = self.db.all_decades(self.udn)
        self.assertEqual({d["decade"] for d in decades}, {2010})

    def test_tracks_year_wins_when_mb_year_is_later(self):
        # File-tag year is 1972 (original album); AcoustID matched a
        # later anniversary-edition recording (override=2021). Decade
        # must be 1970, NOT 2020 — the later mb_year is the "edition",
        # the file tag is the "original" in this direction.
        # Regression for the Demons-and-Wizards-in-2020s bug.
        _add_track(self.db, self.udn, "u/a", "Circle of Hands",
                   "Uriah Heep", "Demons and Wizards",
                   year=1972, override_year=2021)
        decades = self.db.all_decades(self.udn)
        self.assertEqual({d["decade"] for d in decades}, {1970})

    def test_decade_albums_uses_min_of_years(self):
        # decade_albums must also bucket by MIN — not just all_decades.
        _add_track(self.db, self.udn, "u/a", "T", "A", "AL",
                   year=1972, override_year=2021)
        self.assertEqual(
            [a["album"] for a in self.db.decade_albums(self.udn, 1970)],
            ["AL"])
        self.assertEqual(self.db.decade_albums(self.udn, 2020), [])

    def test_decade_tracks_uses_min_of_years(self):
        # decade_tracks must also bucket by MIN.
        _add_track(self.db, self.udn, "u/a", "Same Title", "A", "AL",
                   year=1972, override_year=2021)
        titles_70s = [t["title"] for t in self.db.decade_tracks(self.udn, 1970)]
        titles_20s = [t["title"] for t in self.db.decade_tracks(self.udn, 2020)]
        self.assertEqual(titles_70s, ["Same Title"])
        self.assertEqual(titles_20s, [])


class TestDecadeAlbumsAndTracks(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"
        # Album X (1987 original) — two tracks.
        _add_track(self.db, self.udn, "u/x1", "Bad", "MJ", "Bad", year=1987)
        _add_track(self.db, self.udn, "u/x2", "Smooth Criminal", "MJ", "Bad",
                   year=1987)
        # Album Y (2001 re-release of 1987 original).
        _add_track(self.db, self.udn, "u/y1", "Liberian Girl", "MJ",
                   "Bad 25", year=2001, override_year=1987)
        # Different decade entirely.
        _add_track(self.db, self.udn, "u/z1", "Thriller", "MJ", "Thriller",
                   year=1982)

    def tearDown(self):
        os.unlink(self._path)

    def test_decade_albums_filters_to_decade(self):
        albums = self.db.decade_albums(self.udn, 1980)
        names = sorted(a["album"] for a in albums)
        # 1980s decade should contain: "Bad" (1987), "Bad 25" (override=1987),
        # "Thriller" (1982). Three albums.
        self.assertEqual(names, ["Bad", "Bad 25", "Thriller"])

    def test_decade_albums_track_counts(self):
        albums = {a["album"]: a for a in self.db.decade_albums(self.udn, 1980)}
        self.assertEqual(albums["Bad"]["track_count"], 2)
        self.assertEqual(albums["Bad 25"]["track_count"], 1)
        self.assertEqual(albums["Thriller"]["track_count"], 1)

    def test_decade_tracks_all_titles(self):
        tracks = self.db.decade_tracks(self.udn, 1980)
        titles = sorted(t["title"] for t in tracks)
        self.assertEqual(titles, ["Bad", "Liberian Girl", "Smooth Criminal",
                                  "Thriller"])

    def test_empty_decade(self):
        self.assertEqual(self.db.decade_albums(self.udn, 1990), [])
        self.assertEqual(self.db.decade_tracks(self.udn, 1990), [])


class TestDedupApplied(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"
        # Same (artist, album, title) in 16-bit and 24-bit. Same year.
        _add_track(self.db, self.udn, "u/16", "T", "A", "AL",
                   year=1987, bit_depth=16, sample_rate=44100)
        _add_track(self.db, self.udn, "u/24", "T", "A", "AL",
                   year=1987, bit_depth=24, sample_rate=96000)

    def tearDown(self):
        os.unlink(self._path)

    def test_decade_tracks_dedups_lower_quality(self):
        # The 16-bit duplicate must be hidden.
        tracks = self.db.decade_tracks(self.udn, 1980)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["url"], "u/24",
                         "must return the 24-bit version")

    def test_decade_albums_track_count_is_deduped(self):
        albums = self.db.decade_albums(self.udn, 1980)
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["track_count"], 1,
                         "browse-visible track count must be 1, not 2")

    def test_all_decades_count_is_deduped(self):
        decades = self.db.all_decades(self.udn)
        self.assertEqual(len(decades), 1)
        self.assertEqual(decades[0]["track_count"], 1)


if __name__ == "__main__":
    unittest.main()
