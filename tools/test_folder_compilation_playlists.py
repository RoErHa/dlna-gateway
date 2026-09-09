#!/usr/bin/env python3
"""Tests for tools/folder_compilation_playlists.py.

The risk in this tool is entirely in `classify` and the naming: a false
positive puts a junk playlist into a list the owner curates by hand.
Every case below is a folder shape taken from the real library, so the
traps are pinned rather than imagined.

    python3 -m unittest tools.test_folder_compilation_playlists -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)

from tools.folder_compilation_playlists import (  # noqa: E402
    classify, declares_various_artists, folder_tracks, meaningful_segment,
    names_a_collection, playlist_name, resolve_names, split_existing,
    tidy_name,
)


class TestVariousArtistsHead(unittest.TestCase):
    def test_the_spellings_folders_actually_use(self):
        for key in ("Various Artists - 70s HITS 100 Greatest (2023)",
                    "VA - 100 Greatest Jazz Icons (2020)",
                    "V.A. - Best Hits 2025 (2025 Pop) [Flac 16-44]",
                    "Various - Something",
                    "VA - 2016 - 100 Hits Pure 80s"):
            self.assertTrue(declares_various_artists(key), key)

    def test_a_band_whose_name_merely_starts_that_way_is_not_a_marker(self):
        # The head is matched whole, exactly as dlna_artist_infer does it.
        self.assertFalse(declares_various_artists("Various Comforts - Album"))
        self.assertFalse(declares_various_artists("Vandals - Live"))

    def test_the_tag_can_carry_it_when_the_folder_does_not(self):
        self.assertTrue(declares_various_artists(
            "SomeFolder", ["Various Artists"]))

    def test_it_is_read_at_any_depth(self):
        self.assertTrue(declares_various_artists(
            "VA The Very Best Of Smooth Jazz/The Very Best Of Smooth Jazz cd1"))


class TestCollectionPhrase(unittest.TestCase):
    def test_english_phrases(self):
        for key in ("801 Greatest 1970s Music Hit Singles",
                    "The Best 60s & 70s Soul Album in the World... Ever!",
                    "Christmas Music Compilation",
                    "All 60's Top Hits (2022)",
                    "Now #1s - 70 Years Of The Official Singles Chart",
                    "Hi-Res Masters: Classic Rock Essentials"):
            self.assertTrue(names_a_collection(key), key)

    def test_dutch_phrases_because_half_this_library_is_dutch(self):
        self.assertTrue(names_a_collection("Het Beste Uit De Mega Top 50"))
        self.assertTrue(names_a_collection("Folder", ["28 Vlaamse voltreffers"]))

    def test_the_tag_is_searched_not_only_the_folder(self):
        # Real folders: `Wembley`, `Grrl`, `Speed Ticket` — the tin is
        # blank, the tag reads "Best Of JMFH's Choice 2004-2007 - Grrl".
        self.assertFalse(names_a_collection("Wembley"))
        self.assertTrue(names_a_collection(
            "Wembley", ["Best Of JMFH's Choice 2004-2007 - Wembley"]))

    def test_an_ordinary_album_names_nothing(self):
        self.assertFalse(names_a_collection("Santana - Supernatural (2003)"))
        self.assertFalse(names_a_collection("Life On Mars", ["Life On Mars"]))


class TestClassifyAcceptsRealCompilations(unittest.TestCase):
    def test_the_folder_the_report_started_from(self):
        tier, _ = classify(
            "Various Artists - 70s HITS 100 Greatest Songs of the 1970s "
            "(2023) Mp3 320kbps [PMEDIA]",
            ["70s HITS - 100 Greatest Songs of the 1970s"], 67, 54)
        self.assertEqual(tier, "named")

    def test_a_tag_only_compilation(self):
        tier, _ = classify("Wembley",
                           ["Best Of JMFH's Choice 2004-2007 - Wembley"],
                           24, 24)
        self.assertEqual(tier, "named")

    def test_structural_catches_one_that_announces_nothing(self):
        # `Blue Note Trip - Sunset`, `Life On Mars`, `De Pre Historie`.
        for key, n, na in (("Blue Note Trip - Sunset", 18, 17),
                           ("Life On Mars", 23, 22),
                           ("Tantra Lounge 2", 17, 17)):
            tier, _ = classify(key, [key], n, na)
            self.assertEqual(tier, "structural", key)

    def test_chartbusters_reads_as_named_via_the_chart_phrase(self):
        # Worth pinning: it looks structural, but "Chartbusters" is a
        # collection phrase, so the named tier claims it first.
        tier, why = classify("Motown Chartbusters 3",
                             ["Motown Chartbusters 3"], 15, 12)
        self.assertEqual(tier, "named")
        self.assertIn("collection", why)

    def test_a_small_perfect_spread_still_counts(self):
        tier, _ = classify("De Pre Historie 1963 Vol. 01",
                           ["De Pre Historie 1963 Vol. 01"], 6, 6)
        self.assertEqual(tier, "structural")


class TestClassifyRefusals(unittest.TestCase):
    def test_the_junk_drawer_never_qualifies_however_many_artists(self):
        # 126 performers, 251 tracks, and not one album tag. This is the
        # single check that keeps it out; it must not depend on counts.
        tier, why = classify("Unknown Artist/Unknown Album", [], 251, 126)
        self.assertEqual(tier, "")
        self.assertIn("no album tag", why)

    def test_a_record_full_of_guests_is_not_a_compilation(self):
        for key, n, na in (("Santana - Supernatural (2003) [FLAC 24-96]", 14, 9),
                           ("Eminem - The Slim Shady LP (1999)", 20, 8),
                           ("Rod Stewart - Great American Songbook 2007", 13, 8)):
            tier, _ = classify(key, ["Album"], n, na)
            self.assertEqual(tier, "", key)

    def test_a_classical_box_is_not_a_compilation(self):
        # 6 performers over 53 tracks — the density floor is what refuses it.
        tier, _ = classify("Tchaikovsky - Complete Symphonies - Muti [2011]",
                           ["Symphonies"], 53, 6)
        self.assertEqual(tier, "")

    def test_a_normal_album_is_never_touched(self):
        tier, _ = classify("Pink Floyd - The Wall", ["The Wall"], 26, 1)
        self.assertEqual(tier, "")

    def test_a_named_folder_still_needs_the_artists(self):
        # "Greatest Hits" by ONE act is an album, not a compilation.
        tier, _ = classify("Queen - Greatest Hits", ["Greatest Hits"], 17, 1)
        self.assertEqual(tier, "")

    def test_too_few_tracks_is_not_a_compilation(self):
        tier, _ = classify("VA - Sampler", ["Sampler"], 3, 3)
        self.assertEqual(tier, "")


class TestTidyName(unittest.TestCase):
    """Folder names carry the release group's markings; these have to
    read on a Naim remote and in a CarPlay list."""

    def test_scene_and_format_brackets_are_dropped(self):
        self.assertEqual(
            tidy_name("VA - Hi-Res Masters 50 Britpop Tracks To Test Your "
                      "Speakers [24Bit-FLAC] [PMEDIA] \u2b50\ufe0f"),
            "Hi-Res Masters 50 Britpop Tracks To Test Your Speakers")

    def test_a_leading_various_artists_marker_goes(self):
        for raw in ("Various Artists - Greatest 70's Classic Hits",
                    "VA - Greatest 70's Classic Hits",
                    "V.A. - Greatest 70's Classic Hits"):
            self.assertEqual(tidy_name(raw), "Greatest 70's Classic Hits", raw)

    def test_a_year_survives_because_it_is_information(self):
        # The owner named the year as one of the marks of a compilation;
        # it also says which edition this is.
        self.assertEqual(
            tidy_name("Various Artists - 70s HITS 100 Greatest Songs of the "
                      "1970s (2023) Mp3 320kbps [PMEDIA] \u2b50\ufe0f"),
            "70s HITS 100 Greatest Songs of the 1970s (2023)")

    def test_a_disc_number_survives_because_it_separates_two_discs(self):
        self.assertEqual(tidy_name("Best Of The Celtic Circle (Disc 1)"),
                         "Best Of The Celtic Circle (Disc 1)")
        self.assertEqual(tidy_name("Greatest Hits Collection - 50's Cd1"),
                         "Greatest Hits Collection - 50's Cd1")

    def test_a_truncated_directory_name_loses_its_dangling_bracket(self):
        self.assertEqual(tidy_name("Het Beste Uit De Mega Top 50 Van '93 (Di"),
                         "Het Beste Uit De Mega Top 50 Van '93")

    def test_it_never_returns_nothing(self):
        self.assertEqual(tidy_name("[FLAC]"), "[FLAC]")
        self.assertEqual(tidy_name(""), "")

    def test_an_ordinary_title_is_left_alone(self):
        self.assertEqual(tidy_name("Blue Note Trip - Sunset"),
                         "Blue Note Trip - Sunset")


class TestNaming(unittest.TestCase):
    def test_a_unanimous_tag_is_used_verbatim_not_tidied(self):
        # A tag the tracks agree on is already a title someone chose.
        self.assertEqual(
            playlist_name("F", ["Various Artists - Live [FLAC]"]),
            "Various Artists - Live [FLAC]")

    def test_a_folder_derived_name_is_tidied(self):
        self.assertEqual(
            playlist_name("VA - Big Hits [FLAC] [PMEDIA]", ["A", "B"]),
            "Big Hits")

    def test_a_unanimous_tag_wins_over_the_folder(self):
        self.assertEqual(
            playlist_name("Wembley",
                          ["Best Of JMFH's Choice 2004-2007 - Wembley"]),
            "Best Of JMFH's Choice 2004-2007 - Wembley")

    def test_disc_wrappers_are_skipped(self):
        # The real 40-CD box nests every disc in a directory called `CD`.
        for key in ("Box/Greatest Hits Collection - 50's Cd1/CD",
                    "Box/Greatest Hits Collection - 50's Cd1/Disc 1",
                    "Box/Greatest Hits Collection - 50's Cd1/2"):
            self.assertEqual(meaningful_segment(key),
                             "Greatest Hits Collection - 50's Cd1", key)

    def test_a_folder_of_only_wrappers_still_yields_something(self):
        self.assertEqual(meaningful_segment("CD"), "CD")

    def test_forty_discs_sharing_one_tag_get_forty_distinct_names(self):
        cands = [{"album_key": f"Box/Greatest Hits Collection - 50's Cd{i}/CD",
                  "album_tags": ["Greatest Hits Collection 50s"]}
                 for i in range(1, 41)]
        resolve_names(cands)
        names = [c["name"] for c in cands]
        self.assertEqual(len(set(names)), 40)
        self.assertEqual(names[0], "Greatest Hits Collection - 50's Cd1")

    def test_an_unavoidable_collision_gets_a_suffix(self):
        cands = [{"album_key": "A/CD", "album_tags": ["Same"]},
                 {"album_key": "A/CD", "album_tags": ["Same"]}]
        resolve_names(cands)
        self.assertNotEqual(cands[0]["name"], cands[1]["name"])

    def test_an_uncontested_tag_is_left_alone(self):
        cands = [{"album_key": "X", "album_tags": ["Only One"]}]
        resolve_names(cands)
        self.assertEqual(cands[0]["name"], "Only One")


class _FakeDB:
    def __init__(self, names):
        self._names = names

    def pl_list(self):
        return [{"name": n} for n in self._names]


class TestSplitExisting(unittest.TestCase):
    def test_an_existing_playlist_is_skipped_case_insensitively(self):
        cands = [{"name": "Life On Mars"}, {"name": "Blue Note Trip"}]
        new, skipped = split_existing(_FakeDB(["  life on mars "]), cands)
        self.assertEqual([c["name"] for c in new], ["Blue Note Trip"])
        self.assertEqual([c["name"] for c in skipped], ["Life On Mars"])


class _PoolDB:
    """Minimal stand-in exposing the `_pool.read()` contract."""
    class _Pool:
        def __init__(self, conn):
            self._conn = conn

        def read(self):
            conn = self._conn

            class _Ctx:
                def __enter__(self):
                    return conn

                def __exit__(self, *a):
                    return False
            return _Ctx()

    def __init__(self, conn):
        self._pool = self._Pool(conn)


class TestTrackOrder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.conn = sqlite3.connect(os.path.join(self.tmp, "t.db"))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE tracks (udn TEXT, album_key TEXT, url TEXT, "
            "title TEXT, artist TEXT, album TEXT, duration TEXT, art TEXT, "
            "file_path TEXT)")

    def test_running_order_not_alphabetical_and_discs_stay_in_order(self):
        rows = [("CD 2/01. Zoe - Bee.mp3", "Bee", "Zoe"),
                ("CD 1/02. Al - Cat.mp3", "Cat", "Al"),
                ("CD 1/01. Moe - Ant.mp3", "Ant", "Moe")]
        for fp, title, artist in rows:
            self.conn.execute(
                "INSERT INTO tracks VALUES ('u','k',?,?,?,'Comp','','',?)",
                (f"http://x/{title}", title, artist, "/root/k/" + fp))
        got = [t["title"] for t in folder_tracks(_PoolDB(self.conn), "u", "k")]
        self.assertEqual(got, ["Ant", "Cat", "Bee"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
