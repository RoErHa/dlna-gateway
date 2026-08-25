#!/usr/bin/env python3
"""
tools/test_tag_from_filename.py — the filename parser, which is the whole
risk in this tool: a bad split writes a wrong artist into a real file.

Run standalone:
    python3 -m unittest tools.test_tag_from_filename -v
"""
import os
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tools.tag_from_filename import parse_name


class TestArtistTitleSplit(unittest.TestCase):
    """The shapes that actually occur in the drawer."""

    def test_plain_artist_title(self):
        self.assertEqual(parse_name("Jonah Jansen - Real Men"),
                         ("Jonah Jansen", "Real Men"))

    def test_track_number_artist_title(self):
        self.assertEqual(parse_name("01 - We Are Cartographers - Ram It Home"),
                         ("We Are Cartographers", "Ram It Home"))

    def test_long_catalogue_number_prefix(self):
        self.assertEqual(parse_name("0198 - Gordon Baxter Selection - Litt"),
                         ("Gordon Baxter Selection", "Litt"))

    def test_dotted_track_number(self):
        self.assertEqual(parse_name("02. Manuel R. Rivera - Para Bailar"),
                         ("Manuel R. Rivera", "Para Bailar"))

    def test_no_space_before_dash(self):
        self.assertEqual(parse_name("Rory Fenwick- Blues Guitar Solo"),
                         ("Rory Fenwick", "Blues Guitar Solo"))

    def test_number_glued_to_artist(self):
        self.assertEqual(parse_name("03-Blade Alive - Darling Be Home Soo"),
                         ("Blade Alive", "Darling Be Home Soo"))

    def test_parenthesised_artist(self):
        self.assertEqual(parse_name("(Coolwave)-In My Place (New Album Ve"),
                         ("Coolwave", "In My Place (New Album Ve"))

    def test_underscores_become_spaces(self):
        self.assertEqual(parse_name("Marsh & Quinn - Kiss_on_my_List"),
                         ("Marsh & Quinn", "Kiss on my List"))


class TestRefusesToGuess(unittest.TestCase):
    """Writing nothing is always available, and is the correct answer
    whenever the name carries no artist. A wrong artist is worse than a
    missing one: it files the track under someone else."""

    def test_track_number_and_title_only(self):
        self.assertEqual(parse_name("02 - Elegy Is Dancing"),
                         ("", "Elegy Is Dancing"))

    def test_bare_title(self):
        self.assertEqual(parse_name("Aeon"), ("", "Aeon"))

    def test_literal_unknown_is_not_an_artist(self):
        self.assertEqual(parse_name("00 - Unknown - DJZenith_Killing_boomb"),
                         ("", "DJZenith Killing boomb"))

    def test_various_artists_is_not_an_artist(self):
        artist, _ = parse_name("Various Artists - Some Song")
        self.assertEqual(artist, "")

    def test_hyphenated_name_is_never_split(self):
        """The reason a bare '-' is not a separator."""
        self.assertEqual(parse_name("Jean-Marc Aubert"),
                         ("", "Jean-Marc Aubert"))

    def test_digits_only_left_side_is_a_track_number(self):
        artist, _ = parse_name("07 - 12 - Something")
        self.assertNotEqual(artist, "12")

    def test_empty_and_whitespace(self):
        self.assertEqual(parse_name(""), ("", ""))
        self.assertEqual(parse_name("   "), ("", ""))

    def test_a_bare_number_keeps_its_name(self):
        """Stripping the track number must not consume the whole stem."""
        self.assertEqual(parse_name("07"), ("", "07"))


class TestNeverInventsAnArtist(unittest.TestCase):

    def test_dash_with_empty_left_side(self):
        artist, _ = parse_name(" - Just A Title")
        self.assertEqual(artist, "")

    def test_trailing_separator(self):
        artist, title = parse_name("Some Title - ")
        self.assertEqual(artist, "")
        self.assertEqual(title, "Some Title")

    def test_artist_is_stripped_of_stray_punctuation(self):
        artist, _ = parse_name("Alonso Bellini & Gioia - Vivo Per")
        self.assertEqual(artist, "Alonso Bellini & Gioia")


if __name__ == "__main__":
    unittest.main()
