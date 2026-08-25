#!/usr/bin/env python3
"""
tests/test_artist_infer.py — when may the gateway name a performer it was
never told?

Every case here is drawn from the real library. The asymmetry the tests
encode: a blank artist asks a person, a WRONG artist files the track under
a stranger and is never questioned again. So refusing is always the safe
answer and the burden of proof sits on inferring.

Run standalone:
    python3 -m unittest tests.test_artist_infer -v
"""
import os
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_artist_infer import (
    ANON_ARTIST,
    infer_artist,
    is_a_performer_name,
    is_unattributed,
    parse_folder_artist,
)


class TestFolderName(unittest.TestCase):

    def test_bare_artist_folder(self):
        self.assertEqual(parse_folder_artist("Jean Vallier"), "Jean Vallier")

    def test_artist_album_divider(self):
        self.assertEqual(
            parse_folder_artist("Ray & Nadia Orbit Revue - 1971-01-30 - L'Olympia"),
            "Ray & Nadia Orbit Revue")

    def test_year_bracket_ends_the_artist(self):
        """The bug this was written for: stripping brackets in place left
        'Mira Calvo Caminhos ' — the album glued onto the artist."""
        self.assertEqual(
            parse_folder_artist("Mira Calvo (1996) Caminhos [CUE+WAV+FLAC] "),
            "Mira Calvo")

    def test_earliest_cut_wins(self):
        self.assertEqual(
            parse_folder_artist("RVM - Studio Discography 1983 - 2011 [FLAC]"),
            "RVM")

    def test_top_segment_not_the_leaf(self):
        """An artist folder holding album folders: the artist is on top."""
        self.assertEqual(parse_folder_artist("Stormwind/1971 - In Search of Space"),
                         "Stormwind")

    def test_dated_slug_is_a_bootleg_dir_not_a_name(self):
        self.assertEqual(parse_folder_artist("SVance2008-07-05-sbd"), "")
        self.assertEqual(
            parse_folder_artist("2024-04-04-palais-st-kilda-elvis-costello"), "")

    def test_a_real_name_with_digits_keeps_its_spaces(self):
        self.assertEqual(parse_folder_artist("Sunset Rundown 3"),
                         "Sunset Rundown 3")

    def test_collection_words_are_not_performers(self):
        for junk in ("Unknown Artist", "Various Artists", "VA", "Soundtrack",
                     "Compilation", "New Folder"):
            self.assertEqual(parse_folder_artist(junk), "", junk)

    def test_a_band_whose_name_merely_contains_a_junk_word_survives(self):
        """Matched on the WHOLE name, never as a substring."""
        self.assertEqual(parse_folder_artist("Various Comforts"),
                         "Various Comforts")
        self.assertEqual(parse_folder_artist("The Unknown"), "The Unknown")

    def test_junk_with_no_letters(self):
        self.assertEqual(parse_folder_artist("1971"), "")
        self.assertEqual(parse_folder_artist("---"), "")
        self.assertEqual(parse_folder_artist(""), "")


class TestEvidenceOrder(unittest.TestCase):

    def test_sibling_unanimity_wins(self):
        self.assertEqual(infer_artist("Some Odd Folder", ["Stormwind"] * 40),
                         "Stormwind")

    def test_uncontradicted_folder_name(self):
        self.assertEqual(infer_artist("Mira Calvo (1996) Caminhos", []),
                         "Mira Calvo")

    def test_folder_name_corroborated_by_a_sibling(self):
        """The folder spans spellings, but one of them IS the folder."""
        self.assertEqual(
            infer_artist("Jean Vallier",
                         ["jean vallier", "Jean Vallier & Edith Piaf"]),
            "Jean Vallier")

    def test_a_compilation_named_after_itself_is_refused(self):
        """'Nights On Neptune' holds 20 performers and is not a band."""
        self.assertEqual(
            infer_artist("Nights On Neptune",
                         ["Ember Hollow", "Bowie", "Stormwind", "Vex"]), "")

    def test_the_junk_drawer_is_refused(self):
        self.assertEqual(
            infer_artist("Unknown Artist/Unknown Album",
                         ["tina arena", "Coolwave", "Aerosmith"]), "")

    def test_an_explicit_VA_folder_outranks_sibling_unanimity(self):
        """A compilation can easily have exactly ONE tagged track.
        Trusting unanimity there stamps the whole comp with that name —
        this genuinely happened to 'Atlas & The Aviators'."""
        self.assertEqual(
            infer_artist("VA - 2016 - 100 Hits Pure 80s", ["Atlas & The Aviators"]),
            "")

    def test_unparseable_folder_still_allows_unanimity(self):
        """A weird folder name is not evidence AGAINST agreeing siblings."""
        self.assertEqual(infer_artist("SVance2008-07-05-sbd", ["Sam Vance"]),
                         "Sam Vance")

    def test_nothing_at_all(self):
        self.assertEqual(infer_artist("SVance2008-07-05-sbd", []), "")

    def test_blank_siblings_are_not_evidence(self):
        self.assertEqual(infer_artist("Stormwind", ["", "  ", None]), "Stormwind")


class TestAnonIsTheOnlyPlaceholder(unittest.TestCase):
    """One shared name for everything unattributable, never a per-track
    guess. A made-up name that LOOKS like a performer is worse than an
    obvious placeholder: it can collide with a real act, and it never
    invites correction."""

    def test_blank_and_anon_mean_the_same_thing(self):
        self.assertTrue(is_unattributed(""))
        self.assertTrue(is_unattributed("   "))
        self.assertTrue(is_unattributed(ANON_ARTIST))
        self.assertTrue(is_unattributed("anon"))
        self.assertFalse(is_unattributed("Stormwind"))

    def test_anon_is_never_itself_a_performer(self):
        self.assertFalse(is_a_performer_name(ANON_ARTIST))


class TestShelfLabelsAreNotPerformers(unittest.TestCase):
    """Filename parsing lifts whatever sits left of the dash, which is
    often a shelf. Each of these was written into a real file before the
    check existed."""

    def test_soundtracks_genres_and_shelves(self):
        for n in ("Some Film Soundtrack", "Beach Chillout Lounge",
                  "Classics for Meditation", "Relaxation", "relax",
                  "oldies", "Meditation", "<Unknown>"):
            self.assertFalse(is_a_performer_name(n), n)

    def test_real_acts_survive(self):
        for n in ("The Riverside Four", "A Solo Singer", "Lunacharsky",
                  "The Parish Council", "Various Comforts"):
            self.assertTrue(is_a_performer_name(n), n)

    def test_numeric_band_names_are_real_when_auditing_existing_tags(self):
        """112, 911, 999 and 98° are bands. The strict no-letters rule is
        for a freshly-parsed FILENAME, where "07" is a track number —
        applying it to existing tags erased four real artists."""
        for n in ("112", "911", "999", "98°"):
            self.assertFalse(is_a_performer_name(n), n)
            self.assertTrue(is_a_performer_name(n, allow_numeric=True), n)

    def test_various_artists_is_protected(self):
        """Not a performer, but it MEANS something — and it is the
        sentinel `_localfs_album_artist` emits for a multi-performer
        folder, so rewriting it would break album grouping."""
        self.assertTrue(is_a_performer_name("Various Artists"))
        self.assertTrue(is_a_performer_name("various artists"))

    def test_a_shelf_named_folder_does_not_become_an_artist(self):
        """The folder rule must face the same test as the filename rule,
        or it quietly reintroduces the labels the other one rejects."""
        self.assertFalse(is_a_performer_name(
            parse_folder_artist("Guitar Lounge")))


if __name__ == "__main__":
    unittest.main()
