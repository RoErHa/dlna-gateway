"""Unit tests for tools/compilation_playlists.py — pure helpers over a
throw-away DB; never touches the live library or the network."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)
sys.path.insert(0, HERE)

from dlna_library import LibraryDB                       # noqa: E402
from compilation_playlists import (                      # noqa: E402
    find_candidates, split_existing, compilation_tracks, create_playlist)

UDN = "uuid:test"


def _track(album, artist, title, folder):
    slug = f"{album}-{folder}-{title}".replace(" ", "_")
    return (UDN, f"http://x/{slug}", title, artist, album, folder,
            f"/m/{folder}/{title}.flac")


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)

    def seed(self, rows):
        with self.db._pool.write() as conn:
            conn.executemany(
                "INSERT INTO tracks (udn, url, title, artist, album, "
                "album_key, file_path) VALUES (?,?,?,?,?,?,?)", rows)

    def seed_compilation(self, album, n, folder_prefix="f"):
        """n tracks, each by its own artist in its own folder."""
        self.seed([_track(album, f"Artist {i}", f"Song {i}",
                          f"{folder_prefix}{i}") for i in range(n)])


class TestCandidates(_Base):
    def test_scattered_compilation_found(self):
        self.seed_compilation("2 meter sessies", 8)
        c = find_candidates(self.db, UDN)
        self.assertEqual([x["album"] for x in c], ["2 meter sessies"])
        self.assertEqual(c[0]["n"], 8)
        self.assertEqual(c[0]["artists"], 8)

    def test_min_tracks_boundary(self):
        self.seed_compilation("Four Tracks", 4)
        self.seed_compilation("Five Tracks", 5)
        albums = {x["album"] for x in find_candidates(self.db, UDN)}
        self.assertEqual(albums, {"Five Tracks"})
        albums4 = {x["album"] for x in
                   find_candidates(self.db, UDN, min_tracks=4)}
        self.assertEqual(albums4, {"Four Tracks", "Five Tracks"})

    def test_single_artist_album_excluded(self):
        # Supertramp "Paris": one artist, many tracks → NOT a compilation
        self.seed([_track("Paris", "Supertramp", f"Song {i}", f"p{i}")
                   for i in range(13)])
        self.assertEqual(find_candidates(self.db, UDN), [])

    def test_generic_title_collision_excluded(self):
        # Two different artists' whole albums both tagged "Greatest Hits":
        # each folder holds a coherent >=5-track chunk → excluded.
        self.seed([_track("Greatest Hits", "Queen", f"Q{i}", "queen-gh")
                   for i in range(10)])
        self.seed([_track("Greatest Hits", "Eagles", f"E{i}", "eagles-gh")
                   for i in range(10)])
        self.assertEqual(find_candidates(self.db, UDN), [])

    def test_max_per_folder_boundary(self):
        # 4 tracks in one folder + 4 scattered → max_per_folder=4 < 5: kept
        self.seed([_track("Mixed Comp", f"A{i}", f"S{i}", "shared")
                   for i in range(4)])
        self.seed([_track("Mixed Comp", f"B{i}", f"T{i}", f"solo{i}")
                   for i in range(4)])
        self.assertEqual(
            [x["album"] for x in find_candidates(self.db, UDN)],
            ["Mixed Comp"])
        # tighten the ceiling → excluded
        self.assertEqual(
            find_candidates(self.db, UDN, max_per_folder=4), [])

    def test_other_udn_ignored(self):
        self.seed([("uuid:other", f"http://y/{i}", f"S{i}", f"A{i}",
                    "Foreign Comp", f"g{i}", f"/m/g{i}/s.flac")
                   for i in range(8)])
        self.assertEqual(find_candidates(self.db, UDN), [])


class TestExistingSkip(_Base):
    def test_existing_playlist_skipped_case_insensitive(self):
        self.seed_compilation("Billboard Top 100", 8)
        self.seed_compilation("Fresh Comp", 6)
        self.db.pl_create("billboard top 100")
        new, skipped = split_existing(
            self.db, find_candidates(self.db, UDN))
        self.assertEqual([c["album"] for c in new], ["Fresh Comp"])
        self.assertEqual([c["album"] for c in skipped],
                         ["Billboard Top 100"])


class TestCreate(_Base):
    def test_create_adds_all_tracks_in_artist_title_order(self):
        self.seed([
            _track("Comp", "Zebra", "Alpha", "f1"),
            _track("Comp", "Aardvark", "Zulu", "f2"),
            _track("Comp", "Aardvark", "Alpha", "f3"),
            _track("Comp", "Mid", "Mid", "f4"),
            _track("Comp", "Beta", "Beta", "f5"),
        ])
        pid, added = create_playlist(self.db, UDN, "Comp")
        self.assertEqual(added, 5)
        got = [(t["artist"], t["title"])
               for t in self.db.pl_get(pid)["tracks"]]
        self.assertEqual(got, [("Aardvark", "Alpha"), ("Aardvark", "Zulu"),
                               ("Beta", "Beta"), ("Mid", "Mid"),
                               ("Zebra", "Alpha")])

    def test_compilation_tracks_shape(self):
        self.seed_compilation("Comp", 5)
        ts = compilation_tracks(self.db, UDN, "Comp")
        self.assertEqual(len(ts), 5)
        for t in ts:
            self.assertIn("url", t)
            self.assertIn("title", t)

    def test_second_run_skips_created(self):
        self.seed_compilation("Comp", 6)
        create_playlist(self.db, UDN, "Comp")
        new, skipped = split_existing(
            self.db, find_candidates(self.db, UDN))
        self.assertEqual(new, [])
        self.assertEqual([c["album"] for c in skipped], ["Comp"])


if __name__ == "__main__":
    unittest.main()
