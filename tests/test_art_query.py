#!/usr/bin/env python3
"""
test_art_query.py — what to ASK for an album cover.

3,998 albums sit in `album_art` as a sticky `notfound`. Sampling them
showed the sources were never the problem — the QUESTION was. Two
shapes, both measured on the live library:

  1. A rip-specific suffix. The tag says "Ummagumma - Studio Album";
     MusicBrainz has "Ummagumma". Asked literally, it fails forever —
     and the very same album is already present as a `musicbrainz` hit
     under its clean name.

  2. A compilation, keyed by ONE contributing performer. We ask "does
     Harry Nilsson have an album called Voices of the 70s?" — no, and
     never will. Dropping the artist and searching Cover Art Archive by
     TITLE ALONE found covers for 3 of 4 such albums.

The exact name is always tried FIRST, so nothing that works today can
regress; the looser forms are only ever fallbacks.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_art_query import art_queries, tidy_album                 # noqa: E402


class TestTidyAlbum(unittest.TestCase):
    """Real album names from the failing set."""

    def test_a_rip_suffix_is_dropped(self):
        self.assertEqual(tidy_album("Ummagumma - Studio Album"), "Ummagumma")
        self.assertEqual(tidy_album("Ummagumma - Live Album"), "Ummagumma")

    def test_scene_and_format_brackets_go(self):
        self.assertEqual(
            tidy_album("Echoes - The Best Of Pink Floyd [Disc 1] 2001 "
                       "[EAC - FLAC] (oan)"),
            "Echoes - The Best Of Pink Floyd")

    def test_a_trailing_disc_marker_goes(self):
        self.assertEqual(tidy_album("Psychedelic Soul - Disc 2"),
                         "Psychedelic Soul")
        self.assertEqual(tidy_album("Greatest Hits (Vol. 3 CD3)"),
                         "Greatest Hits")

    def test_a_real_title_is_left_alone(self):
        for name in ["The Dark Side of the Moon", "Wish You Were Here",
                     "A Momentary Lapse of Reason", "Hunky Dory"]:
            with self.subTest(name=name):
                self.assertEqual(tidy_album(name), name)

    def test_a_title_that_is_only_cruft_survives(self):
        """Never reduce a real name to nothing: an empty query searches
        for everything and caches the miss. A name made entirely of
        cruft is handed back unchanged rather than emptied."""
        self.assertTrue(tidy_album("[FLAC]"))
        self.assertTrue(tidy_album("Disc 2"))

    def test_a_genuinely_blank_name_stays_blank(self):
        """Distinct from the case above: there is nothing to preserve,
        and art_queries drops it entirely rather than asking."""
        self.assertEqual(tidy_album("   "), "")
        self.assertEqual(art_queries("Pink Floyd", "   "), [])

    def test_a_hyphen_inside_a_real_title_is_kept(self):
        """Only KNOWN edition words are stripped after a dash, not
        whatever follows one — 'Live' in a title is not an edition."""
        self.assertEqual(tidy_album("Sign o' the Times - Live in Paris"),
                         "Sign o' the Times - Live in Paris")


class TestQueryOrder(unittest.TestCase):
    def test_the_exact_name_is_always_tried_first(self):
        """Nothing that resolves today may regress."""
        q = art_queries("Pink Floyd", "Animals")
        self.assertEqual(q[0], ("Pink Floyd", "Animals"))

    def test_a_tidied_name_follows_the_exact_one(self):
        q = art_queries("Pink Floyd", "Ummagumma - Studio Album")
        self.assertEqual(q[0], ("Pink Floyd", "Ummagumma - Studio Album"))
        self.assertIn(("Pink Floyd", "Ummagumma"), q)

    def test_a_compilation_also_gets_a_TITLE_ONLY_query(self):
        """The artist is one contributing performer, so including them
        guarantees a miss."""
        q = art_queries("Harry Nilsson", "Voices of the 70s",
                        multi_artist=True)
        self.assertIn(("", "Voices of the 70s"), q)

    def test_a_single_artist_album_does_not_get_a_title_only_query(self):
        """Title-only is the loosest form and the likeliest to fetch
        the wrong sleeve; a normal album has no need of it."""
        q = art_queries("Pink Floyd", "Animals", multi_artist=False)
        self.assertNotIn(("", "Animals"), q)

    def test_no_duplicate_queries(self):
        """A clean name yields exactly one attempt — re-asking the same
        question is a wasted rate-limited request."""
        self.assertEqual(art_queries("Pink Floyd", "Animals"),
                         [("Pink Floyd", "Animals")])

    def test_a_blank_album_yields_nothing(self):
        self.assertEqual(art_queries("Pink Floyd", ""), [])
        self.assertEqual(art_queries("", ""), [])

    def test_various_artists_counts_as_a_compilation(self):
        """`_localfs_album_artist` emits that sentinel for a folder with
        several performers, so it means the same thing."""
        q = art_queries("Various Artists", "Blue Note Trip")
        self.assertIn(("", "Blue Note Trip"), q)


if __name__ == "__main__":
    unittest.main(verbosity=2)
