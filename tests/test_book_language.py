#!/usr/bin/env python3
"""
tests/test_book_language.py — the 🌐 audiobooks-by-language facet.

Three layers: the pure detector (`dlna_book_language`), the stored /
detected resolution in `LanguagesMixin`, and the two routes. The
detector cases are the ones the real library taught: Dan Simmons'
*Hyperion* read as Dutch because "dan" is a Dutch word, and a Dutch book
by "Betje Wolff en Aagje Deken" lost its evidence when every name token
was discarded.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from starlette.requests import Request

import dlna_asgi_books
import dlna_localfs_wiring
from dlna_book_language import detect_language, normalize_language
from dlna_library import LibraryDB

UDN = "uuid:localfs-books-test"


class TestDetect(unittest.TestCase):

    def test_dutch_chapter_titles(self):
        self.assertEqual(detect_language(
            ["Hoofdstuk 1", "Hoofdstuk 2", "Het eerste deel van het boek"]),
            "nl")

    def test_english_title(self):
        self.assertEqual(detect_language(
            ["Children of Time", "Chapter 1", "Chapter 2"]), "en")

    def test_a_capitalised_name_is_not_evidence(self):
        # "Black" and "Day" are English words; here they are a person.
        texts = ["De ridder - John Black Day", "01"]
        self.assertEqual(detect_language(texts), "")          # a tie
        self.assertEqual(detect_language(texts, ["John Black Day"]), "nl")

    def test_dan_is_not_a_listed_word(self):
        # Dutch for "then", and the first name on Hyperion's every tag.
        from dlna_book_language import _WORDS
        self.assertNotIn("dan", _WORDS["nl"])

    def test_lowercase_connector_in_a_name_still_counts(self):
        self.assertEqual(detect_language(
            ["Historie van mejuffrouw Sara Burgerhart", "001 - 005"],
            ["Betje Wolff en Aagje Deken"]), "nl")

    def test_a_repeated_string_counts_once(self):
        # One English album tag on fifty chapters is still one tag.
        texts = ["The Book of the King"] * 50 + ["Hoofdstuk een",
                                                 "Hoofdstuk twee",
                                                 "Hoofdstuk drie"]
        self.assertNotEqual(detect_language(texts), "en")

    def test_title_with_no_function_words_is_unknown(self):
        self.assertEqual(detect_language(["Neuromancer", "01", "02"]), "")

    def test_a_close_call_is_unknown(self):
        self.assertEqual(detect_language(["the het of van"]), "")

    def test_underscored_folder_names_are_split(self):
        self.assertEqual(detect_language(
            ["ali_baba_en_de_veertig_roovers_librivox"]), "nl")

    def test_the_word_lists_share_nothing(self):
        from dlna_book_language import _WORDS
        self.assertFalse(_WORDS["nl"] & _WORDS["en"])


class TestNormalize(unittest.TestCase):

    def test_tag_spellings(self):
        for v, want in [("eng", "en"), ("English", "en"), ("dut", "nl"),
                        ("nld", "nl"), ("Nederlands", "nl"), ("en-GB", "en"),
                        ("NL", "nl"), ("ger", "de"), ("sv", "sv")]:
            self.assertEqual(normalize_language(v), want, v)

    def test_unknown_markers_stay_unknown(self):
        for v in ("", None, "XXX", "und", "zxx", "mul", "klingon"):
            self.assertEqual(normalize_language(v), "", v)


class _DB(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self._book("libravox NL/max_havelaar", "Max Havelaar", "Multatuli",
                   ["Hoofdstuk 1", "Hoofdstuk 2", "Het einde van het boek"])
        self._book("Chronicles/Children of Time", "Children of Time",
                   "Adrian Tchaikovsky", ["Chapter 1", "Chapter 2"])
        self._book("Neuromancer", "Neuromancer", "William Gibson",
                   ["01", "02"])

    def tearDown(self):
        os.unlink(self._path)

    def _book(self, key, album, artist, titles):
        self.db.upsert_tracks(UDN, [{
            "id": f"{key}-{i}", "url": f"http://x/{key}/{i}", "title": t,
            "artist": artist, "album": album, "duration": "0:30:00",
            "mime": "audio/mpeg", "file_path": f"/b/{key}/{i}.mp3",
            "album_key": key} for i, t in enumerate(titles)])


class TestResolution(_DB):

    def test_detected(self):
        langs = self.db.book_languages(UDN)
        self.assertEqual(langs["libravox NL/max_havelaar"],
                         {"language": "nl", "source": "detected"})
        self.assertEqual(langs["Neuromancer"]["language"], "")

    def test_manual_beats_detection_and_tag_never_beats_manual(self):
        self.assertTrue(self.db.book_language_set("Neuromancer", "English"))
        self.assertFalse(self.db.book_language_set("Neuromancer", "nl", "tag"))
        self.assertEqual(self.db.book_languages(UDN)["Neuromancer"],
                         {"language": "en", "source": "manual"})

    def test_tag_beats_detection(self):
        self.assertTrue(self.db.book_language_set(
            "Chronicles/Children of Time", "dut", "tag"))
        self.assertEqual(
            self.db.book_languages(UDN)["Chronicles/Children of Time"],
            {"language": "nl", "source": "tag"})

    def test_junk_is_refused(self):
        self.assertFalse(self.db.book_language_set("Neuromancer", "XXX"))
        self.assertFalse(self.db.book_language_set("", "en"))
        self.assertFalse(self.db.book_language_set("Neuromancer", "en",
                                                   "detected"))

    def test_clear_hands_back_to_detection(self):
        self.db.book_language_set("Neuromancer", "en")
        self.assertTrue(self.db.book_language_clear("Neuromancer"))
        self.assertEqual(self.db.book_languages(UDN)["Neuromancer"]["source"],
                         "detected")

    def test_survives_clear_udn(self):
        self.db.book_language_set("Neuromancer", "en")
        self.db.clear(UDN)
        with self.db._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM book_language").fetchone()[0]
        self.assertEqual(n, 1)

    def test_one_book_from_a_chapter_url(self):
        self.db.book_language_set("Neuromancer", "en")
        self.assertEqual(self.db.book_language_of("http://x/Neuromancer/0", UDN),
                         {"album_key": "Neuromancer", "language": "en",
                          "source": "manual", "detected": ""})

    def test_a_track_of_another_library_is_not_a_book(self):
        self.assertIsNone(self.db.book_language_of(
            "http://x/Neuromancer/0", "uuid:localfs-music"))
        self.assertIsNone(self.db.book_language_of("http://nowhere", UDN))

    def test_facets_put_unknown_last(self):
        self.assertEqual(self.db.book_language_facets(UDN),
                         [{"language": "en", "books": 1},
                          {"language": "nl", "books": 1},
                          {"language": "", "books": 1}])

    def test_albums_of_a_language_have_the_browse_shape(self):
        [book] = self.db.book_language_albums(UDN, "nl")
        self.assertEqual(book["album"], "Max Havelaar")
        self.assertEqual(book["album_key"], "libravox NL/max_havelaar")
        self.assertEqual(book["track_count"], 3)
        self.assertEqual(book["language_source"], "detected")
        self.assertEqual([b["album"] for b in
                          self.db.book_language_albums(UDN, "")],
                         ["Neuromancer"])


def _request(query: str, body: bytes | None = None) -> Request:
    scope = {"type": "http", "method": "POST" if body is not None else "GET",
             "path": "/", "query_string": query.encode(), "headers": []}
    if body is None:
        return Request(scope)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request(scope, receive)


class TestRoutes(_DB):

    def setUp(self):
        super().setUp()
        self._orig = dlna_asgi_books._st.DB
        self._orig_udn = dlna_localfs_wiring.AUDIOBOOKS_UDN
        dlna_asgi_books._st.DB = self.db
        dlna_localfs_wiring.AUDIOBOOKS_UDN = UDN

    def tearDown(self):
        dlna_asgi_books._st.DB = self._orig
        dlna_localfs_wiring.AUDIOBOOKS_UDN = self._orig_udn
        super().tearDown()

    def _call(self, route, query, body=None):
        resp = asyncio.run(route(_request(query, body)))
        return resp.status_code, json.loads(resp.body)

    def test_languages(self):
        code, body = self._call(dlna_asgi_books.book_languages_route,
                                f"udn={UDN}")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["languages"]), 3)

    def test_empty_language_means_unknown(self):
        code, body = self._call(dlna_asgi_books.book_language_albums_route,
                                f"udn={UDN}&language=")
        self.assertEqual(code, 200)
        self.assertEqual([a["album"] for a in body["albums"]], ["Neuromancer"])

    def test_missing_params_are_400(self):
        self.assertEqual(self._call(dlna_asgi_books.book_languages_route,
                                    "")[0], 400)
        self.assertEqual(self._call(dlna_asgi_books.book_language_albums_route,
                                    f"udn={UDN}")[0], 400)

    def test_edit_form_reads_one_book(self):
        code, body = self._call(dlna_asgi_books.book_language_get_route,
                                "url=http://x/Neuromancer/0")
        self.assertEqual((code, body["book"], body["language"]),
                         (200, True, ""))
        _, body = self._call(dlna_asgi_books.book_language_get_route,
                             "url=http://elsewhere")
        self.assertEqual(body, {"book": False})

    def _post(self, payload):
        return self._call(dlna_asgi_books.book_language_set_route, "",
                          json.dumps(payload).encode())

    def test_set_by_hand_then_back_to_auto(self):
        code, body = self._post({"url": "http://x/Neuromancer/0",
                                 "language": "English"})
        self.assertEqual((code, body["language"], body["source"]),
                         (200, "en", "manual"))
        code, body = self._post({"url": "http://x/Neuromancer/0",
                                 "language": ""})
        self.assertEqual((code, body["language"], body["source"]),
                         (200, "", "detected"))

    def test_set_refuses_junk_and_non_books(self):
        self.assertEqual(self._post({"url": "http://x/Neuromancer/0",
                                     "language": "klingon"})[0], 400)
        self.assertEqual(self._post({"url": "http://elsewhere",
                                     "language": "en"})[0], 400)
        self.assertEqual(self._post({"url": "http://x/Neuromancer/0"})[0], 400)


if __name__ == "__main__":
    unittest.main()
