"""Type-ahead search: prefix matching on the last query term (2026-07-03).

`LibraryDB.search` used to wrap the whole query in ONE quoted FTS5
phrase — a partial final word matched nothing, so clients that search
as-you-type (Amperfy, the PWA search box) appeared to be missing
content: "chil" found nothing while "chillout" did (measured by
tests/subsonic_verify.py: 8/24 sampled album lookups missed on
truncated queries).

New semantics: every whitespace-separated term must match (FTS5
implicit AND), and the LAST term matches as a prefix — so each
keystroke narrows results instead of blanking them.
"""
import os
import tempfile
import unittest

from dlna_library import LibraryDB

UDN = "uuid:test"


class TestSearchPrefix(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        rows = [
            ("Sailing Home", "Piet Veerman", "Essential Classical Chillout"),
            ("Adagio", "Tomaso Albinoni", "Essential Classical Chillout"),
            ("One", "Metallica", "And Justice for All"),
            ("Chilly Winds", "The Kingston Trio", "Goin' Places"),
            ('Song "Two"', 'The "Quoted" Band', "Quotes Galore"),
        ]
        with self.db._pool.write() as conn:
            for i, (title, artist, album) in enumerate(rows):
                conn.execute(
                    "INSERT INTO tracks (udn, obj_id, url, title, artist, "
                    "album) VALUES (?,?,?,?,?,?)",
                    (UDN, f"o{i}", f"http://x/{i}", title, artist, album))

    def tearDown(self):
        os.unlink(self._path)

    def _albums(self, q):
        return {a["album"] for a in self.db.search(UDN, q)["albums"]}

    def _titles(self, q):
        return {t["title"] for t in self.db.search(UDN, q)["tracks"]}

    def test_partial_last_word_matches(self):
        self.assertIn("Essential Classical Chillout", self._albums("chil"))

    def test_multi_term_with_partial_last(self):
        self.assertIn("Essential Classical Chillout",
                      self._albums("essential chil"))

    def test_full_words_still_match(self):
        self.assertIn("Essential Classical Chillout",
                      self._albums("classical chillout"))

    def test_terms_order_independent(self):
        self.assertIn("Essential Classical Chillout",
                      self._albums("chillout essential"))

    def test_all_terms_must_match(self):
        self.assertEqual(self._albums("essential metallica"), set())

    def test_prefix_narrows_not_blanks(self):
        # every keystroke of a longer word keeps matching
        for q in ("c", "ch", "chi", "chil", "chill", "chillo", "chillout"):
            self.assertIn("Essential Classical Chillout", self._albums(q),
                          f"query {q!r} must match")

    def test_prefix_matches_multiple(self):
        # "chil" prefix-matches both Chillout and Chilly
        titles = self._titles("chil")
        self.assertIn("Chilly Winds", titles)

    def test_quotes_in_query_are_safe(self):
        # must not raise, and should find the quoted band
        res = self.db.search(UDN, 'the "quoted" band')
        self.assertTrue(any(t["artist"] == 'The "Quoted" Band'
                            for t in res["tracks"]))

    def test_whitespace_only_query_matches_nothing(self):
        res = self.db.search(UDN, "   ")
        self.assertEqual(res["tracks"], [])


if __name__ == "__main__":
    unittest.main()
