#!/usr/bin/env python3
"""
tests/test_album_grouping.py — LocalFs folder-based album grouping
(Layer 2). The browse layer groups LocalFs albums by `album_key` (the
track's folder) instead of (artist, album), so a Various-Artists
compilation collapses into one album while distinct same-named albums
in different folders stay separate.

Run standalone:
    python3 -m unittest tests.test_album_grouping -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB

LF = "uuid:localfs-test"          # _is_localfs() → True
UPNP = "uuid:asset-1"             # legacy (artist, album) grouping


def _row(track_id, album_key, artist, album, title, file_path):
    return {
        "id": track_id, "url": f"http://h/{track_id}", "title": title,
        "artist": artist, "album": album, "album_key": album_key,
        "file_path": file_path, "mime": "audio/flac",
    }


class TestFolderGrouping(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        # A Various-Artists compilation: ONE folder, different performer
        # AND different (original) album tag per track.
        self.db.upsert_tracks(LF, [
            _row("c1", "VA/Comp", "Alice", "Her Orig LP", "Song A",
                 "/m/VA/Comp/01.flac"),
            _row("c2", "VA/Comp", "Bob", "His Orig LP", "Song B",
                 "/m/VA/Comp/02.flac"),
            _row("c3", "VA/Comp", "Cara", "Her Orig LP 2", "Song C",
                 "/m/VA/Comp/03.flac"),
        ])
        # A normal single-artist album: one folder, consistent album tag.
        self.db.upsert_tracks(LF, [
            _row("n1", "Nirvana/Nevermind", "Nirvana", "Nevermind",
                 "Breed", "/m/Nirvana/Nevermind/02.flac"),
            _row("n2", "Nirvana/Nevermind", "Nirvana", "Nevermind",
                 "Lithium", "/m/Nirvana/Nevermind/05.flac"),
        ])
        # Two DIFFERENT albums that merely share a name, in different
        # folders → must stay separate (the "Greatest Hits" trap).
        self.db.upsert_tracks(LF, [
            _row("g1", "Queen/Greatest Hits", "Queen", "Greatest Hits",
                 "Bohemian Rhapsody", "/m/Queen/Greatest Hits/01.flac"),
            _row("g2", "ABBA/Greatest Hits", "ABBA", "Greatest Hits",
                 "Mamma Mia", "/m/ABBA/Greatest Hits/01.flac"),
        ])

    def tearDown(self):
        os.unlink(self._p)

    def _albums(self):
        return {a["album_key"]: a for a in self.db.all_albums(LF)}

    def test_compilation_collapses_to_one_album(self):
        albums = self._albums()
        self.assertIn("VA/Comp", albums)
        self.assertEqual(albums["VA/Comp"]["track_count"], 3)

    def test_compilation_is_various_artists(self):
        self.assertEqual(self._albums()["VA/Comp"]["artist"], "Various Artists")

    def test_compilation_display_name_is_folder_leaf(self):
        # Tags differ per track → display name falls back to the folder leaf.
        self.assertEqual(self._albums()["VA/Comp"]["album"], "Comp")

    def test_single_artist_album_uses_consistent_tag(self):
        a = self._albums()["Nirvana/Nevermind"]
        self.assertEqual(a["album"], "Nevermind")
        self.assertEqual(a["artist"], "Nirvana")
        self.assertEqual(a["track_count"], 2)

    def test_same_name_different_folders_stay_separate(self):
        albums = self._albums()
        self.assertIn("Queen/Greatest Hits", albums)
        self.assertIn("ABBA/Greatest Hits", albums)
        self.assertEqual(albums["Queen/Greatest Hits"]["artist"], "Queen")
        self.assertEqual(albums["ABBA/Greatest Hits"]["artist"], "ABBA")

    def test_album_count_is_folder_count(self):
        # VA/Comp + Nirvana/Nevermind + Queen/GH + ABBA/GH = 4 folders
        self.assertEqual(self.db.album_count(LF), 4)

    def test_album_tracks_by_key_returns_whole_folder(self):
        tracks = self.db.album_tracks(LF, "", "", album_key="VA/Comp")
        self.assertEqual(len(tracks), 3)
        titles = {t["title"] for t in tracks}
        self.assertEqual(titles, {"Song A", "Song B", "Song C"})

    def test_album_tracks_by_key_ignores_artist_album_args(self):
        # Even with a bogus artist/album, album_key drives the result.
        tracks = self.db.album_tracks(LF, "Nobody", "Nothing",
                                      album_key="VA/Comp")
        self.assertEqual(len(tracks), 3)

    def test_artist_albums_returns_folder_for_compilation_performer(self):
        # Drilling a performer who only appears on the comp lands on the
        # whole comp folder.
        albums = self.db.artist_albums(LF, "Bob")
        keys = {a["album_key"] for a in albums}
        self.assertIn("VA/Comp", keys)
        comp = next(a for a in albums if a["album_key"] == "VA/Comp")
        self.assertEqual(comp["track_count"], 3)
        self.assertEqual(comp["artist"], "Various Artists")

    def test_browse_letter_albums_groups_by_folder(self):
        res = self.db.browse_letter(LF, "albums", "C", 0, 50)
        comp = [a for a in res["items"] if a.get("album_key") == "VA/Comp"]
        self.assertEqual(len(comp), 1)
        self.assertEqual(comp[0]["album"], "Comp")
        self.assertEqual(comp[0]["track_count"], 3)

    def test_browse_letter_albums_letter_filters_on_display_name(self):
        # "Comp" is under 'C'; "Nevermind" under 'N'.
        c = self.db.browse_letter(LF, "albums", "C", 0, 50)
        c_names = {a["album"] for a in c["items"]}
        self.assertIn("Comp", c_names)
        self.assertNotIn("Nevermind", c_names)
        n = self.db.browse_letter(LF, "albums", "N", 0, 50)
        self.assertIn("Nevermind", {a["album"] for a in n["items"]})


class TestUpnpUnaffected(unittest.TestCase):
    """A non-localfs UDN must keep the legacy (artist, album) grouping
    — album_key columns are empty for UPnP rows."""

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.db.upsert_tracks(UPNP, [
            {"id": "u1", "url": "http://a/1", "title": "X",
             "artist": "Artist", "album": "Album", "mime": "audio/flac"},
            {"id": "u2", "url": "http://a/2", "title": "Y",
             "artist": "Artist", "album": "Album", "mime": "audio/flac"},
        ])

    def tearDown(self):
        os.unlink(self._p)

    def test_albums_grouped_by_artist_album(self):
        albums = self.db.all_albums(UPNP)
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["album"], "Album")
        self.assertEqual(albums[0]["artist"], "Artist")

    def test_album_count_is_artist_album_pairs(self):
        self.assertEqual(self.db.album_count(UPNP), 1)

    def test_album_tracks_by_artist_album_still_works(self):
        tracks = self.db.album_tracks(UPNP, "Artist", "Album")
        self.assertEqual(len(tracks), 2)


class TestGenreDecadeSearchGrouping(unittest.TestCase):
    """A1 — genre / decade / search album views group LocalFs by folder."""

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        # A compilation folder: 2 performers, same genre + decade.
        self.db.upsert_tracks(LF, [
            {"id": "j1", "url": "http://h/j1", "title": "Blue Note A",
             "artist": "Miles", "album": "Orig A", "album_key": "VA/Jazz Comp",
             "genre": "Jazz", "year": 1975, "file_path": "/m/VA/Jazz Comp/01.flac",
             "mime": "audio/flac"},
            {"id": "j2", "url": "http://h/j2", "title": "Blue Note B",
             "artist": "Trane", "album": "Orig B", "album_key": "VA/Jazz Comp",
             "genre": "Jazz", "year": 1975, "file_path": "/m/VA/Jazz Comp/02.flac",
             "mime": "audio/flac"},
        ])
        # A different-genre, different-decade single-artist album.
        self.db.upsert_tracks(LF, [
            {"id": "r1", "url": "http://h/r1", "title": "Breed",
             "artist": "Nirvana", "album": "Nevermind",
             "album_key": "Nirvana/Nevermind", "genre": "Rock", "year": 1991,
             "file_path": "/m/Nirvana/Nevermind/02.flac", "mime": "audio/flac"},
        ])

    def tearDown(self):
        os.unlink(self._p)

    def test_genre_albums_groups_by_folder(self):
        rows = self.db.genre_albums(LF, "Jazz")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["album_key"], "VA/Jazz Comp")
        self.assertEqual(rows[0]["artist"], "Various Artists")
        self.assertEqual(rows[0]["track_count"], 2)

    def test_decade_albums_groups_by_folder(self):
        rows = self.db.decade_albums(LF, 1970)
        keys = {r["album_key"] for r in rows}
        self.assertEqual(keys, {"VA/Jazz Comp"})
        self.assertEqual(rows[0]["track_count"], 2)
        # The 1991 album is in the 1990 bucket, not 1970.
        self.assertNotIn("Nirvana/Nevermind", keys)

    def test_search_albums_group_by_folder(self):
        res = self.db.search(LF, "Blue Note")
        albums = res["albums"]
        comp = [a for a in albums if a.get("album_key") == "VA/Jazz Comp"]
        self.assertEqual(len(comp), 1, "compilation must appear once")
        self.assertEqual(comp[0]["track_count"], 2)
        self.assertEqual(comp[0]["artist"], "Various Artists")


class TestGenreDecadeSearchUpnp(unittest.TestCase):
    """UPnP keeps (artist, album) grouping for genre/decade/search."""

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.db.upsert_tracks(UPNP, [
            {"id": "u1", "url": "http://a/1", "title": "X", "artist": "Artist",
             "album": "Album", "genre": "Rock", "year": 1985, "mime": "audio/flac"},
            {"id": "u2", "url": "http://a/2", "title": "Y", "artist": "Artist",
             "album": "Album", "genre": "Rock", "year": 1985, "mime": "audio/flac"},
        ])

    def tearDown(self):
        os.unlink(self._p)

    def test_genre_albums_by_artist_album(self):
        rows = self.db.genre_albums(UPNP, "Rock")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["album"], "Album")
        self.assertNotIn("album_key", rows[0])

    def test_decade_albums_by_artist_album(self):
        rows = self.db.decade_albums(UPNP, 1980)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["album"], "Album")


if __name__ == "__main__":
    unittest.main()
