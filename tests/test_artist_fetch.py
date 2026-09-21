#!/usr/bin/env python3
"""
test_artist_fetch.py — parsing what the four artist-metadata sources
return. Pure functions over already-decoded documents: the caller does
the HTTP, these decide what the payload MEANS, which is the half worth
testing.

Every fixture below is the real response shape, captured live from the
APIs while building this.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_artist_fetch import (parse_lastfm_top, parse_mb_artist,   # noqa: E402
                               parse_mb_members, parse_wikipedia)

# Trimmed to the fields the parsers read, values verbatim from MB.
BOWIE = {
    "name": "David Bowie", "type": "Person", "gender": "Male",
    "country": "GB", "disambiguation": "English singer‐songwriter",
    "life-span": {"begin": "1947-01-08", "end": "2016-01-10", "ended": True},
    "begin-area": {"name": "Brixton"}, "area": {"name": "England"},
    "genres": [{"name": "rock", "count": 12}, {"name": "art rock", "count": 40},
               {"name": "glam rock", "count": 31}],
    "relations": [
        {"type": "member of band", "direction": "forward",
         "artist": {"name": "Tin Machine", "id": "tm"},
         "attributes": ["lead vocals"], "begin": "1988", "end": "1992"},
        {"type": "member of band", "direction": "forward",
         "artist": {"name": "The Konrads", "id": "tk"},
         "attributes": [], "begin": "", "end": ""},
        {"type": "wikidata", "direction": "forward",
         "url": {"resource": "https://www.wikidata.org/wiki/Q5383"}},
    ],
}

FLOYD = {
    "name": "Pink Floyd", "type": "Group", "gender": None, "country": "GB",
    "disambiguation": "",
    "life-span": {"begin": "1965", "end": "2014", "ended": True},
    "begin-area": {"name": "London"}, "area": {"name": "England"},
    "genres": [{"name": "progressive rock", "count": 90},
               {"name": "psychedelic rock", "count": 55}],
    "relations": [
        {"type": "member of band", "direction": "backward",
         "artist": {"name": "Richard Wright", "id": "rw"},
         "attributes": ["keyboard", "lead vocals", "original"],
         "begin": "1965", "end": "1981"},
        {"type": "member of band", "direction": "backward",
         "artist": {"name": "David Gilmour", "id": "dg"},
         "attributes": ["guitar", "lead vocals", "slide guitar"],
         "begin": "1968-02-18", "end": ""},
    ],
}


class TestParseMbArtist(unittest.TestCase):
    def test_a_person_carries_birth_and_death(self):
        a = parse_mb_artist(BOWIE)
        self.assertEqual(a["mb_type"], "Person")
        self.assertEqual(a["born"], "1947-01-08")
        self.assertEqual(a["died"], "2016-01-10")
        self.assertEqual(a["birth_place"], "Brixton")
        self.assertEqual(a["country"], "GB")

    def test_a_group_reuses_the_same_fields_for_formed_and_ended(self):
        """MB models a band's inception in `life-span` too. The UI, not
        the parser, decides whether to print 'Born' or 'Formed' — it
        reads mb_type. Keeping one pair of columns avoids a second pair
        that is NULL for half the rows."""
        a = parse_mb_artist(FLOYD)
        self.assertEqual(a["mb_type"], "Group")
        self.assertEqual(a["born"], "1965")
        self.assertEqual(a["died"], "2014")
        self.assertEqual(a["birth_place"], "London")

    def test_genres_come_back_most_used_first(self):
        """MB returns them unordered with a vote count; the panel shows
        only the first few, so the order is the whole value."""
        self.assertEqual(parse_mb_artist(BOWIE)["genres"],
                         "art rock, glam rock, rock")

    def test_disambiguation_is_carried(self):
        self.assertIn("singer", parse_mb_artist(BOWIE)["disambiguation"])

    def test_missing_blocks_do_not_raise(self):
        a = parse_mb_artist({"name": "X", "type": "Person"})
        self.assertEqual(a["born"], "")
        self.assertEqual(a["genres"], "")
        self.assertEqual(a["birth_place"], "")


class TestParseMbMembers(unittest.TestCase):
    """⚠ THE DIRECTION RULE.

    `member of band` runs BOTH ways. On a Group, direction='backward'
    names the members. On a Person, direction='forward' names the bands
    that person joined. Without the filter, David Bowie's panel lists
    Tin Machine and The Konrads as his LINE-UP — which is exactly what
    happened while fetching real data for the design."""

    def test_a_group_yields_its_members(self):
        got = parse_mb_members(FLOYD)
        self.assertEqual([m["name"] for m in got],
                         ["Richard Wright", "David Gilmour"])

    def test_a_person_yields_no_members_at_all(self):
        self.assertEqual(parse_mb_members(BOWIE), [])

    def test_a_persons_bands_are_available_separately(self):
        got = parse_mb_members(BOWIE, bands=True)
        self.assertEqual([m["name"] for m in got],
                         ["Tin Machine", "The Konrads"])

    def test_instruments_are_joined_in_the_order_given(self):
        wright = parse_mb_members(FLOYD)[0]
        self.assertEqual(wright["instruments"],
                         "keyboard, lead vocals, original")

    def test_date_range_is_carried_for_the_lineup_intersection(self):
        gilmour = parse_mb_members(FLOYD)[1]
        self.assertEqual(gilmour["begin"], "1968-02-18")
        self.assertEqual(gilmour["end"], "")

    def test_other_relation_types_are_ignored(self):
        """The same relations array carries wikidata/discogs urls."""
        self.assertTrue(all(m["name"] != "" for m in parse_mb_members(BOWIE, bands=True)))


class TestParseWikipedia(unittest.TestCase):
    DOC = {
        "description": "English musician and actor (1947–2016)",
        "extract": "David Robert Jones, known as David Bowie, was an "
                   "English singer, songwriter and actor.",
        "content_urls": {"desktop": {"page":
                         "https://en.wikipedia.org/wiki/David_Bowie"}},
        "thumbnail": {"source": "https://upload.wikimedia.org/x/bowie.jpg"},
    }

    def test_extract_and_url_are_both_returned(self):
        """bio_url is NOT optional — the text is CC BY-SA and the link
        IS the attribution, so a bio without one must not be stored."""
        w = parse_wikipedia(self.DOC)
        self.assertIn("David Robert Jones", w["bio"])
        self.assertEqual(w["bio_url"],
                         "https://en.wikipedia.org/wiki/David_Bowie")

    def test_image_is_returned(self):
        self.assertEqual(parse_wikipedia(self.DOC)["image_url"],
                         "https://upload.wikimedia.org/x/bowie.jpg")

    def test_a_bio_with_no_url_is_dropped_entirely(self):
        """Attribution is a licence condition, not a nice-to-have."""
        w = parse_wikipedia({"extract": "Some text."})
        self.assertEqual(w["bio"], "")

    def test_a_disambiguation_page_is_refused(self):
        """Wikipedia answers a bad title with a 'may refer to' stub —
        storing that as an artist biography is worse than storing
        nothing."""
        w = parse_wikipedia({
            "type": "disambiguation", "extract": "Nirvana may refer to:",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Nirvana"}}})
        self.assertEqual(w["bio"], "")

    def test_empty_document_is_safe(self):
        self.assertEqual(parse_wikipedia({})["bio"], "")
        self.assertEqual(parse_wikipedia(None)["bio"], "")


class TestParseLastfmTop(unittest.TestCase):
    DOC = {"toptracks": {"track": [
        {"name": "Ziggy Stardust", "playcount": "100", "listeners": "9"},
        {"name": "Life on Mars?", "playcount": "90", "listeners": "8"},
        {"name": "Ziggy Stardust", "playcount": "5", "listeners": "1"},
    ]}}

    def test_track_names_in_order(self):
        self.assertEqual(parse_lastfm_top(self.DOC)[:2],
                         ["Ziggy Stardust", "Life on Mars?"])

    def test_duplicates_are_collapsed(self):
        """Last.fm lists live and remastered versions separately; the
        panel wants distinct songs, not the same title twice."""
        self.assertEqual(parse_lastfm_top(self.DOC).count("Ziggy Stardust"), 1)

    def test_limit_is_honoured(self):
        self.assertEqual(len(parse_lastfm_top(self.DOC, limit=1)), 1)

    def test_a_single_track_object_is_tolerated(self):
        """Last.fm collapses a one-element array into a bare object."""
        doc = {"toptracks": {"track": {"name": "Only One"}}}
        self.assertEqual(parse_lastfm_top(doc), ["Only One"])

    def test_remaster_suffixes_are_stripped(self):
        """Live from Last.fm: 'Army Dreamers - 2018 Remaster' and
        'Babooshka - 2018 Remaster'. The panel calls this block "Best
        known for" — it wants the SONG, not which master you heard."""
        doc = {"toptracks": {"track": [
            {"name": "Army Dreamers - 2018 Remaster"},
            {"name": "Babooshka (2018 Remastered Version)"},
            {"name": "Cloudbusting - Remastered 2018"},
        ]}}
        self.assertEqual(parse_lastfm_top(doc),
                         ["Army Dreamers", "Babooshka", "Cloudbusting"])

    def test_stripping_then_collapses_the_duplicate(self):
        """The original and its remaster are one song."""
        doc = {"toptracks": {"track": [
            {"name": "Babooshka"}, {"name": "Babooshka - 2018 Remaster"}]}}
        self.assertEqual(parse_lastfm_top(doc), ["Babooshka"])

    def test_live_is_NOT_stripped(self):
        """'Live and Let Die' and 'Live Forever' are song titles, and a
        live recording is a legitimate separate entry. Only edition
        wording goes."""
        doc = {"toptracks": {"track": [
            {"name": "Live and Let Die"}, {"name": "Live Forever"}]}}
        self.assertEqual(parse_lastfm_top(doc),
                         ["Live and Let Die", "Live Forever"])

    def test_error_or_empty_document_is_safe(self):
        self.assertEqual(parse_lastfm_top({"error": 6}), [])
        self.assertEqual(parse_lastfm_top(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
