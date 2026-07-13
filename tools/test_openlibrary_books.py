#!/usr/bin/env python3
"""Tests for tools/openlibrary_books.py — pure helpers + DB semantics.
Never touches the network (HTTP layer mocked)."""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.openlibrary_books import (
    audiobook_books, extract_series, lookup_book, parse_book_folder,
    parse_series, pick_best_doc, write_meta)


class TestParseSeries(unittest.TestCase):

    def test_name_paren_number(self):
        self.assertEqual(parse_series("Discworld (13)"), ("Discworld", 13.0))

    def test_name_paren_book_number(self):
        self.assertEqual(parse_series("The Expanse (Book 4)"),
                         ("The Expanse", 4.0))

    def test_name_hash_number(self):
        self.assertEqual(parse_series("The Expanse #4"), ("The Expanse", 4.0))

    def test_name_semicolon_number(self):
        self.assertEqual(parse_series("Night's Dawn trilogy ; 1"),
                         ("Night's Dawn trilogy", 1.0))

    def test_name_comma_book_number(self):
        self.assertEqual(parse_series("The Stormlight Archive, Book 2"),
                         ("The Stormlight Archive", 2.0))

    def test_book_n_of_name(self):
        self.assertEqual(parse_series("Book 3 of The Wheel of Time"),
                         ("The Wheel of Time", 3.0))

    def test_fractional_number(self):
        self.assertEqual(parse_series("The Expanse #4.5"),
                         ("The Expanse", 4.5))

    def test_dashes_bk_form(self):
        """Real OL data: 'The chronicles of Narnia -- bk. 6'."""
        self.assertEqual(parse_series("The chronicles of Narnia -- bk. 6"),
                         ("The chronicles of Narnia", 6.0))

    def test_bare_name_no_number(self):
        self.assertEqual(parse_series("Culture series"),
                         ("Culture series", None))

    def test_empty(self):
        self.assertEqual(parse_series(""), ("", None))


class TestExtractSeries(unittest.TestCase):

    def test_majority_vote_prefers_numbered(self):
        editions = [
            {"series": ["Night's Dawn"]},
            {"series": ["Night's Dawn (1)"]},
            {"series": ["Nights Dawn (1)"]},          # diacritic variant
            {"series": ["Some Publisher Collection"]},
        ]
        name, num = extract_series(editions)
        self.assertIn("Night", name)
        self.assertEqual(num, 1.0)

    def test_no_series_anywhere(self):
        self.assertEqual(extract_series([{}, {"series": []}]), ("", None))

    def test_most_common_number_wins(self):
        editions = [{"series": ["Saga #2"]}, {"series": ["Saga #2"]},
                    {"series": ["Saga #3"]}]
        self.assertEqual(extract_series(editions), ("Saga", 2.0))

    def test_publisher_series_rejected(self):
        """Imprint series are edition bookkeeping (real sweep data:
        'Penguin twentieth-century classics', 'SF Masterworks')."""
        editions = [{"series": ["Penguin twentieth-century classics"]},
                    {"series": ["SF Masterworks (12)"]}]
        self.assertEqual(extract_series(editions), ("", None))

    def test_tiny_junk_names_rejected(self):
        self.assertEqual(extract_series([{"series": ["v. 4"]}]), ("", None))

    def test_publisher_paren_suffix_rejected(self):
        """'Series (Publisher)' form (real sweep data: 'Historia
        (DeBolsillo)', 'Madaʻ bidyoni (Keter)') is an imprint marker."""
        editions = [{"series": ["Historia (DeBolsillo)"]},
                    {"series": ["Mada bidyoni (Keter) ; 12"]}]
        self.assertEqual(extract_series(editions), ("", None))
        # A numeric parenthetical is a series POSITION, not a publisher.
        self.assertEqual(extract_series([{"series": ["Discworld (13)"]}]),
                         ("Discworld", 13.0))

    def test_latin_script_preferred_over_translation(self):
        """A foreign edition's series name must not beat the Latin one
        (real sweep data: Armenian 'Narniayi kʻroniknerě')."""
        editions = [{"series": ["Narniayi kʻroniknerě (5)"]},
                    {"series": ["The Chronicles of Narnia (5)"]}]
        name, num = extract_series(editions)
        self.assertEqual(name, "The Chronicles of Narnia")
        self.assertEqual(num, 5.0)

    def test_foreign_script_hard_rejected_even_alone(self):
        """User rule: books are English/Dutch ONLY — a foreign series
        name is never valid, even with no alternative candidate."""
        editions = [{"series": ["Narniayi kʻroniknerě (5)"]}]
        self.assertEqual(extract_series(editions), ("", None))
        # Dutch diacritics survive the ASCII floor.
        editions = [{"series": ["Het Bureau ; 3"]}]
        self.assertEqual(extract_series(editions), ("Het Bureau", 3.0))

    def test_multi_series_string_split(self):
        """One edition string carrying several series (real sweep data:
        The Alloy of Law) — each fragment parses separately."""
        editions = [
            {"series": ["Mistborn, Era 2: Wax & Wayne (#1), "
                        "The Mistborn Saga (#4), The Cosmere #16"]},
            {"series": ["The Mistborn Saga (4)"]},
        ]
        name, num = extract_series(editions)
        self.assertEqual(name, "The Mistborn Saga")
        self.assertEqual(num, 4.0)

    def test_catalog_numbers_rejected(self):
        """A publisher catalog entry ("Frye annotated #1249", real case:
        The Doors of Perception) is bookkeeping, not a story series —
        the whole entry is dropped."""
        editions = [{"series": ["Frye annotated (1249)"]}]
        self.assertEqual(extract_series(editions), ("", None))
        # A real series alongside catalog junk still wins.
        editions.append({"series": ["Night's Dawn (1)"]})
        self.assertEqual(extract_series(editions), ("Night's Dawn", 1.0))


class TestParseBookFolder(unittest.TestCase):

    def test_author_dash_title(self):
        a, t = parse_book_folder("Aldous Huxley - The Doors Of Perception (audiobook)")
        self.assertEqual(a, "Aldous Huxley")
        self.assertEqual(t, "The Doors Of Perception")

    def test_index_title_author_year(self):
        a, t = parse_book_folder(
            "Top 100 Sci-Fi Books - 51-75/57 - The Reality Dysfunction - Peter F Hamilton - 1996")
        self.assertEqual(a, "Peter F Hamilton")
        self.assertEqual(t, "The Reality Dysfunction")

    def test_title_only(self):
        a, t = parse_book_folder("Hyperion")
        self.assertEqual(a, "")
        self.assertEqual(t, "Hyperion")

    def test_narrator_parenthetical_stripped(self):
        a, t = parse_book_folder(
            "Douglas Adams - Hitchhiker's Guide To The Galaxy (Stephen Fry and Martin Freeman)")
        self.assertEqual(a, "Douglas Adams")
        self.assertEqual(t, "Hitchhiker's Guide To The Galaxy")


class TestPickBestDoc(unittest.TestCase):

    DOCS = [
        {"key": "/works/OL1W", "title": "The Reality Dysfunction",
         "author_name": ["Peter F. Hamilton"]},
        {"key": "/works/OL2W", "title": "Reality Is Not What It Seems",
         "author_name": ["Carlo Rovelli"]},
    ]

    def test_fuzzy_title_plus_author_overlap(self):
        d = pick_best_doc(self.DOCS, "The Reality Dysfunction",
                          "Peter F Hamilton")
        self.assertEqual(d["key"], "/works/OL1W")

    def test_wrong_author_blocks_match(self):
        d = pick_best_doc(self.DOCS, "The Reality Dysfunction",
                          "Somebody Else")
        self.assertIsNone(d)

    def test_weak_title_blocked_by_floor(self):
        d = pick_best_doc(self.DOCS, "Completely Different Book", "")
        self.assertIsNone(d)

    def test_no_author_guess_matches_on_title_alone(self):
        d = pick_best_doc(self.DOCS, "the reality dysfunction", "")
        self.assertEqual(d["key"], "/works/OL1W")


class TestLookupBook(unittest.TestCase):
    """lookup_book with the HTTP layer mocked — verifies the
    found / notfound / transient trichotomy."""

    def test_found_with_series(self):
        with patch("tools.openlibrary_books.ol_search",
                   return_value=[{"key": "/works/OL1W",
                                  "title": "The Reality Dysfunction",
                                  "author_name": ["Peter F. Hamilton"]}]), \
             patch("tools.openlibrary_books.ol_editions",
                   return_value=[{"series": ["Night's Dawn (1)"]}]):
            meta = lookup_book("57 - The Reality Dysfunction - Peter F Hamilton",
                               "Peter F Hamilton", "The Reality Dysfunction")
        self.assertEqual(meta["series"], "Night's Dawn")
        self.assertEqual(meta["series_seq"], 1.0)
        self.assertEqual(meta["author"], "Peter F. Hamilton")

    def test_confident_miss_returns_empty_dict(self):
        with patch("tools.openlibrary_books.ol_search", return_value=[]):
            meta = lookup_book("Nobody - No Such Book", "Nobody",
                               "No Such Book")
        self.assertEqual(meta, {})     # cache as notfound

    def test_transport_failure_returns_none(self):
        with patch("tools.openlibrary_books.ol_search", return_value=None):
            meta = lookup_book("Nobody - No Such Book", "Nobody",
                               "No Such Book")
        self.assertIsNone(meta)        # transient — never cached

    def test_short_title_never_retries_title_only(self):
        """Author-mismatched short title must NOT fall back to a
        title-only search (real case: "Alpha" audio drama matched
        "Alphas" by Lisi Harrison). One search per guess, no retry."""
        calls = []

        def fake_search(title, author):
            calls.append((title, author))
            return [{"key": "/works/OLX", "title": "Alphas",
                     "author_name": ["Lisi Harrison"]}]

        with patch("tools.openlibrary_books.ol_search",
                   side_effect=fake_search):
            meta = lookup_book("Alpha - Audio Drama", "Mike Walker",
                               "Alpha")
        self.assertEqual(meta, {})     # confident miss, cached notfound
        self.assertTrue(all(author for _, author in calls),
                        "title-only retry fired for a short title")


class TestDbSemantics(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = sqlite3.connect(self._p)
        self.conn.executescript("""
            CREATE TABLE tracks (udn TEXT, album_key TEXT, artist TEXT,
                                 album TEXT);
            CREATE TABLE book_meta (
                album_key TEXT PRIMARY KEY, author TEXT, title TEXT,
                series TEXT, series_seq REAL, source TEXT NOT NULL,
                fetched_at INTEGER NOT NULL);
        """)

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def test_audiobook_books_majority_tags_and_root_skip(self):
        rows = [("u", "Book A", "Author X", "Title A")] * 3
        rows += [("u", "Book A", "Narrator Y", "Title A")]
        rows += [("u", "", "Loose", "Root Single")]      # root-level → skipped
        self.conn.executemany("INSERT INTO tracks VALUES (?,?,?,?)", rows)
        books = audiobook_books(self.conn, "u")
        self.assertEqual(books, [("Book A", "Author X", "Title A")])

    def test_write_meta_replace(self):
        write_meta(self.conn, "b", "A", "T", "S", 2.0, "openlibrary")
        write_meta(self.conn, "b", "A2", "T2", None, None, "openlibrary")
        r = self.conn.execute(
            "SELECT author, series FROM book_meta WHERE album_key='b'"
        ).fetchone()
        self.assertEqual(r, ("A2", None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
