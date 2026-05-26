#!/usr/bin/env python3
"""
tests/test_artist_tracks.py — tests for the artist-tracks endpoint
that backs the "Play all" button on the artist-albums view.

Run standalone:
    python3 -m unittest tests.test_artist_tracks -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB


def _add(db, udn, url, title, artist, album, *,
         bit_depth=16, sample_rate=44100):
    with db._pool.write() as conn:
        conn.execute(
            "INSERT INTO tracks (udn, obj_id, url, title, artist, album, "
            " bit_depth, sample_rate) VALUES (?,?,?,?,?,?,?,?)",
            (udn, url, url, title, artist, album, bit_depth, sample_rate))


class TestArtistTracks(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"

    def tearDown(self):
        os.unlink(self._path)

    def test_returns_all_tracks_for_artist(self):
        _add(self.db, self.udn, "u/1", "Bad",    "MJ", "Bad")
        _add(self.db, self.udn, "u/2", "Smooth", "MJ", "Bad")
        _add(self.db, self.udn, "u/3", "Beat It","MJ", "Thriller")
        _add(self.db, self.udn, "u/4", "Sweet Caroline", "Neil Diamond", "Hot August Night")
        tracks = self.db.artist_tracks(self.udn, "MJ")
        titles = sorted(t["title"] for t in tracks)
        self.assertEqual(titles, ["Bad", "Beat It", "Smooth"])

    def test_ordered_by_album_then_title(self):
        _add(self.db, self.udn, "u/1", "Smooth", "MJ", "Bad")
        _add(self.db, self.udn, "u/2", "Bad",    "MJ", "Bad")
        _add(self.db, self.udn, "u/3", "Beat It","MJ", "Thriller")
        _add(self.db, self.udn, "u/4", "Billie Jean", "MJ", "Thriller")
        tracks = self.db.artist_tracks(self.udn, "MJ")
        order = [(t["album"], t["title"]) for t in tracks]
        # Bad < Thriller alphabetically; within each, title alphabetic
        self.assertEqual(order, [
            ("Bad", "Bad"),
            ("Bad", "Smooth"),
            ("Thriller", "Beat It"),
            ("Thriller", "Billie Jean"),
        ])

    def test_dedups_lower_quality(self):
        _add(self.db, self.udn, "u/16", "Bad", "MJ", "Bad",
             bit_depth=16, sample_rate=44100)
        _add(self.db, self.udn, "u/24", "Bad", "MJ", "Bad",
             bit_depth=24, sample_rate=96000)
        _add(self.db, self.udn, "u/3",  "Smooth", "MJ", "Bad")
        tracks = self.db.artist_tracks(self.udn, "MJ")
        urls = sorted(t["url"] for t in tracks)
        self.assertEqual(urls, ["u/24", "u/3"],
                         "16-bit duplicate must be hidden when 24-bit exists")

    def test_unknown_artist_returns_empty(self):
        _add(self.db, self.udn, "u/1", "Bad", "MJ", "Bad")
        self.assertEqual(self.db.artist_tracks(self.udn, "Other Artist"), [])


if __name__ == "__main__":
    unittest.main()
