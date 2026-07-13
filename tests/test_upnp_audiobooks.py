#!/usr/bin/env python3
"""
tests/test_upnp_audiobooks.py — the 📖 Audiobooks tree in the gateway's
DLNA MediaServer (P5) + the ffprobe chapter parser + the enriched
continue-listening payload.

Tree contract:
  * root "0" gains "📖 Audiobooks" ONLY when the audiobooks source
    exists and has authors
  * "abooks"      → author containers  abauthor:<b64>
  * "abauthor:*"  → book containers    abbook:<b64(artist\\0album\\0key)>
                    with the OpenLibrary series in the title when known
  * "abbook:*"    → chapter items (musicTrack, /localfs/stream res)
  * garbled ids   → empty container, never a 500

Run standalone:  python3 -m unittest tests.test_upnp_audiobooks -v
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import api_upnp
import dlna_localfs_wiring
from dlna_library import LibraryDB

AB_UDN = "uuid:localfs-abtest"

BOOKS = [
    # (artist, album, album_key, title, url)
    ("Iain M Banks", "The Player of Games", "Culture/02 - Player",
     "Chapter 1", "http://gw:8200/localfs/stream/b1c1"),
    ("Iain M Banks", "The Player of Games", "Culture/02 - Player",
     "Chapter 2", "http://gw:8200/localfs/stream/b1c2"),
    ("Ursula K. Le Guin", "A Wizard of Earthsea", "UKL/Wizard",
     "Part 1", "http://gw:8200/localfs/stream/b2c1"),
]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        with self.db._pool.write() as conn:
            for i, (ar, al, key, ti, url) in enumerate(BOOKS):
                conn.execute(
                    "INSERT INTO tracks(udn, obj_id, url, title, artist, "
                    "album, album_key, duration, art, mime, file_path) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (AB_UDN, f"ab{i}", url, ti, ar, al, key,
                     "1:10:00", "", "audio/mp4", f"/b/{i}.m4b"))
            # A music-udn track that must NOT appear in the tree.
            conn.execute(
                "INSERT INTO tracks(udn, obj_id, url, title, artist, album) "
                "VALUES ('uuid:localfs-music','m1','http://m/1','Song','A','Al')")
        self._p1 = patch.object(api_upnp, "DB", self.db)
        self._p1.start()
        self._p2 = patch.object(dlna_localfs_wiring, "AUDIOBOOKS_UDN", AB_UDN)
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def browse(self, oid, flag="BrowseDirectChildren", start=0, count=0):
        return api_upnp._gw_browse(oid, flag, start, count)


class TestAudiobooksTree(_Base):

    def test_root_lists_audiobooks_container(self):
        xml, _, _ = self.browse("0")
        self.assertIn("\U0001F4D6 Audiobooks", xml)
        self.assertIn('id="abooks"', xml)

    def test_root_hides_audiobooks_when_source_absent(self):
        with patch.object(dlna_localfs_wiring, "AUDIOBOOKS_UDN", ""):
            xml, _, _ = self.browse("0")
        self.assertNotIn("Audiobooks", xml)

    def test_abooks_lists_authors(self):
        xml, n, total = self.browse("abooks")
        self.assertEqual(total, 2)
        self.assertIn("Iain M Banks", xml)
        self.assertIn("Ursula K. Le Guin", xml)
        self.assertIn("abauthor:", xml)
        self.assertNotIn("Song", xml)   # music udn never leaks in

    def test_author_lists_books(self):
        aid = "abauthor:" + api_upnp._b64e("Iain M Banks")
        xml, n, total = self.browse(aid)
        self.assertEqual(total, 1)
        self.assertIn("The Player of Games", xml)
        self.assertIn("abbook:", xml)

    def test_book_series_overlay_in_title(self):
        self.db.book_meta_set("Culture/02 - Player",
                              author="Iain M. Banks",
                              title="The Player of Games",
                              series="Culture", series_seq=2.0)
        aid = "abauthor:" + api_upnp._b64e("Iain M Banks")
        xml, _, _ = self.browse(aid)
        self.assertIn("\U0001F4DA Culture #2", xml)

    def test_book_lists_chapters(self):
        bid = api_upnp._encode_ab_book_id(
            "Iain M Banks", "The Player of Games", "Culture/02 - Player")
        xml, n, total = self.browse(bid)
        self.assertEqual(total, 2)
        self.assertIn("Chapter 1", xml)
        self.assertIn("Chapter 2", xml)
        self.assertIn("localfs/stream/b1c1", xml)
        self.assertIn("object.item.audioItem.musicTrack", xml)

    def test_garbled_book_id_empty_never_500(self):
        xml, n, total = self.browse("abbook:!!!not-base64!!!")
        self.assertEqual((n, total), (0, 0))

    def test_pagination(self):
        xml, n, total = self.browse("abooks", start=1, count=1)
        self.assertEqual((n, total), (1, 2))


class TestChapterParsing(unittest.TestCase):

    def test_parse_chapters_shape(self):
        from dlna_ffmpeg import parse_chapters
        data = {"chapters": [
            {"start_time": "0.000", "end_time": "1800.5",
             "tags": {"title": "Opening"}},
            {"start_time": "1800.5", "end_time": "3600.0", "tags": {}},
        ]}
        out = parse_chapters(data)
        self.assertEqual(out[0], {"start": 0.0, "end": 1800.5,
                                  "title": "Opening"})
        self.assertEqual(out[1]["title"], "Chapter 2")   # untitled fallback

    def test_parse_chapters_empty_and_junk(self):
        from dlna_ffmpeg import parse_chapters
        self.assertEqual(parse_chapters({}), [])
        self.assertEqual(parse_chapters(
            {"chapters": [{"start_time": "junk"}]}), [])

    def test_chapters_payload_paths(self):
        import api_playback
        with patch.object(api_playback.DB, "track_by_url",
                          return_value=None):
            self.assertEqual(api_playback.chapters_payload(
                {"url": "http://x/nope"})[0], 404)
        self.assertEqual(api_playback.chapters_payload({})[0], 400)
        # Track exists but the file is gone → empty chapters, not an error.
        with patch.object(api_playback.DB, "track_by_url",
                          return_value={"file_path": "/no/such.m4b"}):
            code, body = api_playback.chapters_payload({"url": "http://x/a"})
        self.assertEqual((code, body), (200, {"chapters": []}))


class TestEnrichedPositions(unittest.TestCase):

    def test_positions_payload_carries_track_fields(self):
        import api_playback
        fd, p = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = LibraryDB(db_file=p)
        try:
            with db._pool.write() as conn:
                conn.execute(
                    "INSERT INTO tracks(udn, obj_id, url, title, artist, "
                    "album, album_key, art) VALUES "
                    "('uuid:localfs-ab','c7','http://x/ch7','Chapter 7',"
                    "'Author','The Book','A/Book','http://x/cover.jpg')")
            db.position_set("A/Book", "http://x/ch7", 754.3, 1820.0)
            db.position_set("Orphan/Book", "http://x/gone", 10)
            orig = api_playback.DB
            api_playback.DB = db
            try:
                code, body = api_playback.positions_list_payload({})
            finally:
                api_playback.DB = orig
            self.assertEqual(code, 200)
            rows = {r["album_key"]: r for r in body["positions"]}
            self.assertEqual(rows["A/Book"]["book"], "The Book")
            self.assertEqual(rows["A/Book"]["author"], "Author")
            self.assertEqual(rows["A/Book"]["chapter_title"], "Chapter 7")
            self.assertEqual(rows["Orphan/Book"]["book"], "")  # still listed
        finally:
            db._pool.close()
            os.unlink(p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
