#!/usr/bin/env python3
"""
test_mbid_match.py — accepting (or refusing) a MusicBrainz artist match.

Step 2's keystone: every external source (MusicBrainz, Wikidata,
Wikipedia, ListenBrainz) keys off an artist MBID, so a WRONG mbid here
poisons every one of them at once — and, unlike a blank, never invites
correction. Same asymmetry as `dlna_artist_infer`: refuse rather than
guess.

Every case below comes from a measured run against this library's own
artists (random sample, not the popular head).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_mbid import accept_match, norm_artist, query_names      # noqa: E402


def _c(name, score, mbid="mb-1", dis=""):
    return {"id": mbid, "name": name, "score": score, "disambiguation": dis}


class TestNormArtist(unittest.TestCase):
    """Variants MusicBrainz legitimately spells differently from the
    file tags. Each pair was a real score-100 result this library's
    artists produced."""

    SAME = [
        ("Jr Walker & The All Stars", "Jr. Walker & The All Stars"),   # dots
        ("Dhol Foundation",           "The Dhol Foundation"),          # article
        ("Nick Cave & The Bad Seeds", "Nick Cave & the Bad Seeds"),    # case
        ("Two Steps from Hell",       "Two Steps From Hell"),          # case
        ("Guns N' Roses",             "Guns N’ Roses"),           # smart quote
        ("Bjork",                     "Björk"),                   # diacritic
        ("  Elbow  ",                 "Elbow"),                        # whitespace
    ]

    def test_equivalent_spellings_share_a_key(self):
        for a, b in self.SAME:
            with self.subTest(pair=(a, b)):
                self.assertEqual(norm_artist(a), norm_artist(b))

    DASHES = [
        # MusicBrainz uses TYPOGRAPHIC dashes where file tags use the
        # ASCII hyphen-minus — or nothing at all. Both pairs below are
        # real score-100 refusals from the first live sweep.
        ("Bachman-Turner Overdrive",              # U+002D hyphen-minus
         "Bachman–Turner Overdrive"),        # U+2013 en dash
        ("Jean Michel Jarre",                     # space
         "Jean‐Michel Jarre"),               # U+2010 hyphen
        ("Jay-Z", "Jay‑Z"),                  # U+2011 non-breaking
        ("Emerson, Lake & Palmer",
         "Emerson, Lake & Palmer"),
    ]

    def test_dash_variants_share_a_key(self):
        """The single highest-value normalisation: every hyphenated
        artist was being refused at score 100 because MB spells the
        dash differently from the tag."""
        for a, b in self.DASHES:
            with self.subTest(pair=(a, b)):
                self.assertEqual(norm_artist(a), norm_artist(b))

    def test_and_and_ampersand_are_the_same_word(self):
        """'Delaney And Bonnie' in the tags, 'Delaney & Bonnie' in MB."""
        self.assertEqual(norm_artist("Delaney And Bonnie"),
                         norm_artist("Delaney & Bonnie"))
        self.assertEqual(norm_artist("Hall & Oates"),
                         norm_artist("Hall and Oates"))

    def test_and_inside_a_word_is_not_touched(self):
        """A blind 'and'->'&' replace would maul 'Andrews' and
        'Anderson' — the substitution must be word-bounded."""
        self.assertEqual(norm_artist("The Andrews Sisters"),
                         "andrews sisters")
        self.assertNotEqual(norm_artist("Anderson"), norm_artist("&erson"))

    def test_genuinely_different_names_do_not_collide(self):
        for a, b in [("Rainbow", "Rainbows"), ("Elbow", "Elbo"),
                     ("Nirvana", "Nirvana UK"), ("Queen", "Queens")]:
            with self.subTest(pair=(a, b)):
                self.assertNotEqual(norm_artist(a), norm_artist(b))

    def test_an_article_only_name_is_not_emptied(self):
        """Dropping a leading 'The' must not erase a band CALLED 'The'."""
        self.assertNotEqual(norm_artist("The"), "")


class TestQueryNames(unittest.TestCase):
    """The ordered list of names to try. The full credit is ALWAYS
    first, so a band whose real name contains the separator wins before
    any split is considered."""

    def test_full_name_is_always_tried_first(self):
        self.assertEqual(query_names("Nick Cave & The Bad Seeds")[0],
                         "Nick Cave & The Bad Seeds")

    def test_featuring_credit_falls_back_to_the_primary(self):
        for raw, primary in [
            ("Eric Clapton feat. Willie Nelson", "Eric Clapton"),
            ("Rodney Crowell Feat. Lyle Lovett", "Rodney Crowell"),
            ("Trijntje Oosterhuis featuring Keith Jarrett",
             "Trijntje Oosterhuis"),
            ("Kylie Minogue ft. Nick Cave", "Kylie Minogue"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(query_names(raw)[-1], primary)

    def test_semicolon_list_falls_back_to_the_first(self):
        self.assertEqual(query_names("Eros Ramazzotti; Luis Fonsi")[-1],
                         "Eros Ramazzotti")

    def test_ampersand_is_a_FALLBACK_never_a_split(self):
        """⚠ `&` belongs to band names far more often than it joins two
        artists — 'Nick Cave & The Bad Seeds', 'Billy Larkin & The
        Delegates', 'Jr Walker & The All Stars' all matched whole. So
        the & form is only ever tried AFTER the full name has failed,
        which is what rescues 'Hans Zimmer & Benjamin Wallfisch' without
        risking the bands."""
        names = query_names("Hans Zimmer & Benjamin Wallfisch")
        self.assertEqual(names[0], "Hans Zimmer & Benjamin Wallfisch")
        self.assertIn("Hans Zimmer", names)

    def test_comma_is_never_split(self):
        """'Anderson, Bruford, Wakeman, Howe' IS the band's name."""
        raw = "Anderson, Bruford, Wakeman, Howe"
        self.assertEqual(query_names(raw), [raw])

    def test_a_plain_name_yields_exactly_one_query(self):
        self.assertEqual(query_names("Elbow"), ["Elbow"])


class TestAcceptMatch(unittest.TestCase):
    def test_a_clean_exact_match_is_accepted(self):
        self.assertEqual(
            accept_match("Elbow", [_c("Elbow", 100, "abc")]), "abc")

    def test_a_punctuation_variant_is_accepted(self):
        self.assertEqual(
            accept_match("Jr Walker & The All Stars",
                         [_c("Jr. Walker & The All Stars", 100, "xyz")]),
            "xyz")

    def test_a_low_score_is_refused(self):
        self.assertIsNone(accept_match("Elbow", [_c("Elbow", 70)]))

    def test_a_different_name_at_high_score_is_refused(self):
        """MB's score is TEXT relevance, not correctness — 'Elbo' can
        score 100 for 'Elbow'."""
        self.assertIsNone(accept_match("Elbow", [_c("Elbo", 100)]))

    def test_two_artists_sharing_the_name_are_refused(self):
        """The real ambiguity: several acts genuinely called 'Nirvana'.
        Picking the popular one would be a guess, and a wrong mbid is
        silently wrong forever."""
        self.assertIsNone(accept_match("Nirvana", [
            _c("Nirvana", 100, "seattle", "US grunge band"),
            _c("Nirvana", 100, "uk", "UK psychedelic band")]))

    def test_a_weaker_namesake_does_not_block_a_clear_winner(self):
        """Only a rival at >=95 counts as ambiguity."""
        self.assertEqual(accept_match("Rush", [
            _c("Rush", 100, "good", "Canadian rock trio"),
            _c("Rush", 60, "other")]), "good")

    def test_no_candidates_is_refused(self):
        self.assertIsNone(accept_match("Nobody", []))

    def test_blank_query_is_refused(self):
        self.assertIsNone(accept_match("", [_c("", 100)]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
