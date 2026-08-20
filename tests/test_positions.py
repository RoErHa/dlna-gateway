#!/usr/bin/env python3
"""
tests/test_positions.py — audiobook resume positions (P2).

Covers the `playback_positions` table contract (one row per book,
survives clear(udn) like play_counts/lyrics), the LibraryDB methods,
and the api_playback payload cores (validation + clamping).
No network, no live gateway.
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB

BOOK = "Author - Some Book (2019)"
CH7 = "http://gw:8200/localfs/stream/aaaa07"
CH8 = "http://gw:8200/localfs/stream/aaaa08"


class TestPositionsDB(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)

    def test_round_trip(self):
        self.assertTrue(self.db.position_set(BOOK, CH7, 754.3, 1820.0))
        r = self.db.position_get(BOOK)
        self.assertEqual(r["url"], CH7)
        self.assertAlmostEqual(r["position_sec"], 754.3)
        self.assertAlmostEqual(r["duration_sec"], 1820.0)
        self.assertEqual(r["finished"], 0)

    def test_one_row_per_book_upsert(self):
        self.db.position_set(BOOK, CH7, 100)
        self.db.position_set(BOOK, CH8, 5)      # moved to next chapter
        r = self.db.position_get(BOOK)
        self.assertEqual(r["url"], CH8)
        self.assertEqual(len(self.db.positions_list()), 1)

    def test_finished_set_and_cleared_by_next_save(self):
        self.db.position_set(BOOK, CH8, 1790, 1820, finished=True)
        self.assertEqual(self.db.position_get(BOOK)["finished"], 1)
        # Re-listening: a normal save clears the flag.
        self.db.position_set(BOOK, CH7, 10, 1820)
        self.assertEqual(self.db.position_get(BOOK)["finished"], 0)

    def test_negative_position_clamped_to_zero(self):
        self.assertTrue(self.db.position_set(BOOK, CH7, -3.5))
        self.assertEqual(self.db.position_get(BOOK)["position_sec"], 0.0)

    def test_bad_inputs_return_false_never_raise(self):
        self.assertFalse(self.db.position_set("", CH7, 10))
        self.assertFalse(self.db.position_set(BOOK, "", 10))
        self.assertFalse(self.db.position_set(BOOK, CH7, "not-a-number"))
        self.assertIsNone(self.db.position_get(""))
        self.assertIsNone(self.db.position_get("never-played"))

    def test_bad_duration_saved_as_null_not_rejected(self):
        self.assertTrue(self.db.position_set(BOOK, CH7, 10, "garbage"))
        self.assertIsNone(self.db.position_get(BOOK)["duration_sec"])

    def test_clear(self):
        self.db.position_set(BOOK, CH7, 10)
        self.assertTrue(self.db.position_clear(BOOK))
        self.assertIsNone(self.db.position_get(BOOK))
        self.assertFalse(self.db.position_clear(BOOK))   # already gone

    def test_list_newest_first(self):
        self.db.position_set("book-a", CH7, 10)
        # updated_at has second resolution — force distinct timestamps
        with self.db._pool.write() as c:
            c.execute("UPDATE playback_positions SET updated_at="
                      "updated_at - 100 WHERE album_key='book-a'")
        self.db.position_set("book-b", CH8, 20)
        rows = self.db.positions_list()
        self.assertEqual([r["album_key"] for r in rows],
                         ["book-b", "book-a"])

    def test_survives_clear_udn(self):
        """Same survival contract as play_counts / lyrics / album_art:
        a rebuild-index never forgets where you are in a book."""
        self.db.position_set(BOOK, CH7, 754.3)
        self.db.clear("uuid:localfs-audiobooks-test")
        r = self.db.position_get(BOOK)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["position_sec"], 754.3)


class TestPositionPayloads(unittest.TestCase):
    """api_playback payload cores, run against a throw-away DB by
    patching the module's DB reference."""

    def setUp(self):
        import api_playback
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self._mod = api_playback
        self._orig_db = api_playback.DB
        api_playback.DB = self.db

    def tearDown(self):
        self._mod.DB = self._orig_db
        os.unlink(self._path)

    def test_save_and_get(self):
        code, body = self._mod.position_save_payload(
            {"album_key": BOOK, "url": CH7,
             "position_sec": 12.5, "duration_sec": 300})
        self.assertEqual((code, body), (200, {"ok": True}))
        code, body = self._mod.position_get_payload({"album_key": BOOK})
        self.assertEqual(code, 200)
        self.assertEqual(body["position"]["url"], CH7)

    def test_save_missing_fields_400(self):
        self.assertEqual(
            self._mod.position_save_payload({"url": CH7})[0], 400)
        self.assertEqual(
            self._mod.position_save_payload({"album_key": BOOK})[0], 400)
        self.assertEqual(
            self._mod.position_save_payload("not a dict")[0], 400)

    def test_save_bad_position_400(self):
        code, _ = self._mod.position_save_payload(
            {"album_key": BOOK, "url": CH7, "position_sec": "x"})
        self.assertEqual(code, 400)

    def test_save_clamps_oversize_fields(self):
        code, _ = self._mod.position_save_payload(
            {"album_key": "k" * 2000, "url": CH7, "position_sec": 1})
        self.assertEqual(code, 200)
        row = self.db.positions_list()[0]
        self.assertEqual(len(row["album_key"]), 512)

    def test_finished_flag_round_trip(self):
        self._mod.position_save_payload(
            {"album_key": BOOK, "url": CH8, "position_sec": 1790,
             "duration_sec": 1820, "finished": True})
        _, body = self._mod.position_get_payload({"album_key": BOOK})
        self.assertEqual(body["position"]["finished"], 1)

    def test_get_missing_album_key_400(self):
        self.assertEqual(
            self._mod.position_get_payload({})[0], 400)

    def test_get_unknown_book_null(self):
        code, body = self._mod.position_get_payload({"album_key": "nope"})
        self.assertEqual(code, 200)
        self.assertIsNone(body["position"])

    def test_list_payload(self):
        self._mod.position_save_payload(
            {"album_key": BOOK, "url": CH7, "position_sec": 5})
        code, body = self._mod.positions_list_payload({})
        self.assertEqual(code, 200)
        self.assertEqual(len(body["positions"]), 1)
        code, body = self._mod.positions_list_payload({"limit": "bogus"})
        self.assertEqual(code, 200)   # bad limit falls back, never 500s


class TestBookMeta(unittest.TestCase):
    """book_meta — the OpenLibrary display overlay. manual wins,
    notfound is bookkeeping (absent from book_meta_all)."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)

    def test_round_trip(self):
        self.assertTrue(self.db.book_meta_set(
            BOOK, author="Peter F. Hamilton", title="The Reality Dysfunction",
            series="Night's Dawn", series_seq=1.0))
        m = self.db.book_meta_get(BOOK)
        self.assertEqual(m["series"], "Night's Dawn")
        self.assertEqual(m["series_seq"], 1.0)
        self.assertEqual(m["source"], "openlibrary")

    def test_manual_never_overwritten_by_tool(self):
        self.db.book_meta_set(BOOK, series="My Series", series_seq=2,
                              source="manual")
        self.assertFalse(self.db.book_meta_set(
            BOOK, series="OL Series", series_seq=9, source="openlibrary"))
        self.assertEqual(self.db.book_meta_get(BOOK)["series"], "My Series")
        # manual → manual is allowed (user re-edits)
        self.assertTrue(self.db.book_meta_set(
            BOOK, series="Edited", source="manual"))

    def test_all_excludes_notfound(self):
        self.db.book_meta_set(BOOK, series="S", series_seq=1)
        self.db.book_meta_set("other-book", source="notfound")
        books = self.db.book_meta_all()
        self.assertIn(BOOK, books)
        self.assertNotIn("other-book", books)

    def test_survives_clear_udn(self):
        self.db.book_meta_set(BOOK, series="S", series_seq=1)
        self.db.clear("uuid:localfs-any")
        self.assertIsNotNone(self.db.book_meta_get(BOOK))

    def test_payload(self):
        import api_playback
        orig = api_playback.DB
        api_playback.DB = self.db
        try:
            self.db.book_meta_set(BOOK, series="S", series_seq=1)
            code, body = api_playback.book_meta_all_payload({})
            self.assertEqual(code, 200)
            self.assertIn(BOOK, body["books"])
        finally:
            api_playback.DB = orig


class TestAudiobooksRootConfig(unittest.TestCase):
    """dlna_localfs_wiring.audiobooks_root — env beats config, '' when
    neither set (feature off)."""

    def setUp(self):
        import dlna_localfs_wiring
        self.wiring = dlna_localfs_wiring
        self._saved = os.environ.get("AUDIOBOOKS_ROOT")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("AUDIOBOOKS_ROOT", None)
        else:
            os.environ["AUDIOBOOKS_ROOT"] = self._saved

    def test_env_var_wins(self):
        os.environ["AUDIOBOOKS_ROOT"] = "/Volumes/SAMDATA-1TB/Audio_Books"
        self.assertEqual(self.wiring.audiobooks_root(),
                         "/Volumes/SAMDATA-1TB/Audio_Books")

    def test_unset_returns_empty(self):
        os.environ.pop("AUDIOBOOKS_ROOT", None)
        from unittest.mock import patch
        with patch("dlna_config.load_config", return_value={}):
            self.assertEqual(self.wiring.audiobooks_root(), "")

    def test_config_fallback(self):
        os.environ.pop("AUDIOBOOKS_ROOT", None)
        from unittest.mock import patch
        with patch("dlna_config.load_config", return_value={
                "localfs": {"audiobooks_root": "/books"}}):
            self.assertEqual(self.wiring.audiobooks_root(), "/books")


if __name__ == "__main__":
    unittest.main(verbosity=2)
