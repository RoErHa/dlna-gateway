#!/usr/bin/env python3
"""
tests/test_book_key.py — one resume slot per BOOK, not per folder.

Every case below is a real folder of the owner's audiobook library
(2026-11-09). The trap runs both ways: the Poirot folder is 27 books
that shared one bookmark, while Eichmann's ten `(disk N)` tags are ONE
book that must keep one bookmark across a disc change.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_book_key import SEP, book_key, folder_of, is_multi_book, norm_book_title

LF = "uuid:localfs-books"


class TestWhatIsOneBook(unittest.TestCase):

    def test_disc_part_and_number_tags_are_one_book(self):
        for tags in (
            [f"Eichmann in Jerusalem (disk {n})" for n in range(1, 11)],
            ["Prince Caspian", "Prince Caspian Disc 2", "Prince Caspian Disc 3"],
            ["The Master and Margarita Part 1", "The Master and Margarita Part 2"],
            ["1", "2", "3"],                                   # The BFG
            [f"Bleak Expectations Season 0{n}" for n in range(1, 6)],
            ["Hitchhikers Guide To The Galaxy .Macmillan. READNFO",
             "Hitchhikers Guide To The Galaxy-.Macmillan.-READNFO"],
            ["Brandon Sanderson - Mistborn 02 - The Well of Ascension  pt 1",
             "Brandon Sanderson - Mistborn 02 - The Well of Ascension  pt 2"],
        ):
            self.assertFalse(is_multi_book(tags), tags)

    def test_two_spellings_of_one_title_are_one_book(self):
        self.assertFalse(is_multi_book(
            ["Lion Witch & Wardrobe", "The Lion, the Witch and the Wardrobe"]))
        self.assertFalse(is_multi_book(
            ["Revolting Rhymes & Dirty Beasts", "Revolting Rhymes and Dirty Beasts"]))

    def test_different_titles_are_different_books(self):
        self.assertTrue(is_multi_book(
            ["BBC Radio 4: Death on the Nile", "BBC Radio 4: Cards on the Table"]))
        self.assertTrue(is_multi_book(
            ["A Game of Thrones", "A Game of Thrones", "A Clash of Kings"]))

    def test_a_title_inside_another_is_not_the_same_book(self):
        """Why the filler-word fold is not a subset test."""
        self.assertTrue(is_multi_book(["Dune", "Dune Messiah"]))

    def test_blank_and_junk_tags_claim_nothing(self):
        self.assertFalse(is_multi_book(["Cleopatra", ""] * 3))
        self.assertFalse(is_multi_book(["ÿþS", "ÿþA", ""]))   # Classic Chandler
        self.assertEqual(norm_book_title("ÿþS"), "")

    def test_any_script_survives(self):
        self.assertEqual(norm_book_title("Мастер и Маргарита"), "мастер и маргарита")


class TestTheKey(unittest.TestCase):

    POIROT = ["BBC Radio 4: Death on the Nile", "BBC Radio 4: Cards on the Table"]

    def test_an_ordinary_book_keys_by_its_folder(self):
        tags = ["Prince Caspian", "Prince Caspian Disc 2"]
        self.assertEqual(book_key("Narnia/04", "Prince Caspian Disc 2", "u", tags),
                         "Narnia/04")

    def test_a_book_in_a_folder_of_books_keys_by_folder_and_title(self):
        k = book_key("Poirot", "BBC Radio 4: Death on the Nile", "u", self.POIROT)
        self.assertEqual(k, "Poirot" + SEP + "bbc radio death on nile")
        self.assertEqual(folder_of(k), "Poirot")

    def test_a_blank_tag_in_a_folder_of_books_keeps_the_folder(self):
        self.assertEqual(book_key("Poirot", "", "u", self.POIROT + [""]), "Poirot")

    def test_a_root_level_file_keys_by_url(self):
        self.assertEqual(book_key("", "Anything", "http://x/1", ["Anything"]),
                         "http://x/1")


def _row(tid, folder, album, path):
    return {"id": tid, "url": f"http://h/{tid}", "title": tid, "artist": "BBC",
            "album": album, "album_key": folder, "file_path": path,
            "mime": "audio/mp4"}


class TestStore(unittest.TestCase):

    def setUp(self):
        from dlna_library import LibraryDB
        fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = LibraryDB(db_file=self._p)
        self.db.upsert_tracks(LF, [
            _row("p1", "Poirot", "BBC Radio 4: Death on the Nile", "/b/Poirot/01.m4b"),
            _row("p2", "Poirot", "BBC Radio 4: Cards on the Table", "/b/Poirot/02.m4b"),
            _row("e1", "Eichmann", "Eichmann (disk 1)", "/b/Eichmann/01.mp3"),
            _row("e2", "Eichmann", "Eichmann (disk 2)", "/b/Eichmann/02.mp3"),
        ])

    def tearDown(self):
        os.unlink(self._p)

    def test_album_tracks_carries_each_files_book_key(self):
        keys = {t["url"]: t["book_key"]
                for t in self.db.album_tracks(LF, "", "", album_key="Poirot")}
        self.assertEqual(len(set(keys.values())), 2)
        self.assertTrue(all(folder_of(k) == "Poirot" for k in keys.values()))
        one = {t["book_key"]
               for t in self.db.album_tracks(LF, "", "", album_key="Eichmann")}
        self.assertEqual(one, {"Eichmann"})

    def test_book_key_of_agrees_with_album_tracks(self):
        """Subsonic keys a single track; the PWA keys from album_tracks.
        Disagreeing would lose a bookmark between CarPlay and the PWA."""
        for t in self.db.album_tracks(LF, "", "", album_key="Poirot"):
            self.assertEqual(self.db.book_key_of(self.db.track_by_url(t["url"])),
                             t["book_key"])

    def test_each_book_keeps_its_own_bookmark(self):
        k1 = self.db.book_key_of(self.db.track_by_url("http://h/p1"))
        k2 = self.db.book_key_of(self.db.track_by_url("http://h/p2"))
        self.db.position_set(k1, "http://h/p1", 600)
        self.db.position_set(k2, "http://h/p2", 30)
        self.assertEqual(self.db.position_get(k1)["position_sec"], 600)
        self.assertEqual(self.db.position_get(k2)["position_sec"], 30)

    def test_the_folder_resumes_its_newest_book(self):
        k1 = self.db.book_key_of(self.db.track_by_url("http://h/p1"))
        k2 = self.db.book_key_of(self.db.track_by_url("http://h/p2"))
        self.db.position_set(k1, "http://h/p1", 600)
        self.db.position_set(k2, "http://h/p2", 30)
        self.assertEqual(self.db.position_get("Poirot")["url"], "http://h/p2")

    def test_the_prefix_never_reaches_a_neighbouring_folder(self):
        self.db.position_set("Poirot Extra" + SEP + "x", "http://h/z", 9)
        self.assertIsNone(self.db.position_get("Poirot"))

    def test_shelf_opens_the_folder_not_the_book(self):
        import api_playback_meta
        k1 = self.db.book_key_of(self.db.track_by_url("http://h/p1"))
        self.db.position_set(k1, "http://h/p1", 600)
        with patch.object(api_playback_meta._st, "DB", self.db):
            _, body = api_playback_meta.positions_list_payload({})
        self.assertEqual(body["positions"][0]["folder"], "Poirot")


class TestSurfaces(unittest.TestCase):

    def test_monitor_saves_under_the_book_key(self):
        from dlna_player import RendererQueue
        q = RendererQueue()
        q._av_url = "http://r/AVT"
        q._tracks = [{"url": "http://h/p2", "album_key": "Poirot",
                      "book_key": "Poirot" + SEP + "cards table"}]
        q._index = 0
        q._is_book = True
        calls = {"n": 0}

        def probe(url):
            calls["n"] += 1
            if calls["n"] > 1:
                q._stop_event.set()
            return "PLAYING", ""

        with patch("dlna_avtransport.avtransport_probe_state", side_effect=probe), \
             patch("dlna_avtransport.avtransport_get_position",
                   return_value={"position": 42.0, "track_uri": "http://h/p2"}), \
             patch("dlna_library.DB") as db, \
             patch.object(q._stop_event, "wait", return_value=False):
            q._monitor()
        self.assertEqual(db.position_set.call_args[0][0],
                         "Poirot" + SEP + "cards table")

    def test_subsonic_bookmark_uses_the_book_key(self):
        import api_subsonic_extras as ex
        db = MagicMock()
        db.track_by_url.return_value = {"url": "http://h/p2", "album_key": "Poirot"}
        db.book_key_of.return_value = "Poirot" + SEP + "cards table"
        with patch.object(ex._proto, "DB", db), \
             patch.object(ex, "_track_id_decode", return_value="http://h/p2"), \
             patch.object(ex, "_ok"):
            ex._create_bookmark(MagicMock(), {"id": "tr:x", "position": "5000"})
        db.position_set.assert_called_once_with(
            "Poirot" + SEP + "cards table", "http://h/p2", 5.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
