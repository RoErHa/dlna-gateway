#!/usr/bin/env python3
"""
test_artist_api.py — GET /api/artist_info, the panel's one request.

The endpoint's job is to hand the PWA something it can render without
deciding anything: the line-up is already resolved for the track's
year, and already LABELLED as fact or inference so the UI cannot
accidentally present a guess as a credit.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio                                                  # noqa: E402
import json                                                      # noqa: E402

import dlna_asgi_artist                                          # noqa: E402
from dlna_library import LibraryDB                               # noqa: E402

UDN = "uuid:localfs-test"


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self._orig = dlna_asgi_artist._st.DB
        dlna_asgi_artist._st.DB = self.db

    def tearDown(self):
        dlna_asgi_artist._st.DB = self._orig
        os.unlink(self._path)

    def _get(self, **kw):
        """Call the route directly — the house pattern in
        tests/test_asgi.py, which keeps httpx out of the dev deps.
        Returns (status, body-dict)."""
        r = asyncio.run(dlna_asgi_artist.artist_info(**kw))
        if hasattr(r, "status_code"):
            return r.status_code, json.loads(bytes(r.body))
        return 200, r

    def _seed_floyd(self):
        self.db.artist_meta_set("Pink Floyd", mbid="pf", source="search")
        self.db.artist_info_set("Pink Floyd", {
            "mb_type": "Group", "born": "1965", "died": "2014",
            "birth_place": "London", "country": "GB",
            "genres": "progressive rock, psychedelic rock",
            "bio": "Pink Floyd are an English rock band.",
            "bio_url": "https://en.wikipedia.org/wiki/Pink_Floyd",
            "top_tracks": "Money, Time, Wish You Were Here"})
        self.db.artist_members_set("Pink Floyd", [
            {"name": "Syd Barrett", "instruments": "guitar",
             "begin": "1965", "end": "1968-04"},
            {"name": "David Gilmour", "instruments": "guitar, lead vocals",
             "begin": "1968-02-18", "end": ""},
            {"name": "Richard Wright", "instruments": "keyboard",
             "begin": "1965", "end": "1981"},
            {"name": "Richard Wright", "instruments": "keyboard",
             "begin": "1987", "end": "2008-09-15"}])


class TestArtistInfo(_Base):
    def test_unknown_artist_is_404_not_an_empty_panel(self):
        self.assertEqual(self._get(artist="Nobody")[0], 404)

    def test_missing_artist_param_is_400(self):
        self.assertEqual(self._get(artist="")[0], 400)

    def test_core_facts_are_returned(self):
        self._seed_floyd()
        _, d = self._get(artist="Pink Floyd")
        self.assertEqual(d["mb_type"], "Group")
        self.assertEqual(d["born"], "1965")
        self.assertEqual(d["birth_place"], "London")

    def test_lists_arrive_as_lists_not_comma_strings(self):
        """They are stored comma-joined; making the PWA re-split them
        would put the same parsing in two places."""
        self._seed_floyd()
        _, d = self._get(artist="Pink Floyd")
        self.assertEqual(d["genres"][0], "progressive rock")
        self.assertIn("Money", d["top_tracks"])

    def test_lineup_is_resolved_for_the_supplied_year(self):
        self._seed_floyd()
        _, d = self._get(artist="Pink Floyd", year="1975")
        names = [m["name"] for m in d["lineup"]]
        self.assertIn("David Gilmour", names)
        self.assertIn("Richard Wright", names)
        self.assertNotIn("Syd Barrett", names)
        self.assertEqual(d["lineup_year"], 1975)

    def test_lineup_is_labelled_as_inference(self):
        """The UI must not be able to present this as a credit."""
        self._seed_floyd()
        _, d = self._get(artist="Pink Floyd", year="1975")
        self.assertEqual(d["lineup_tier"], "inferred")

    def test_no_year_yields_no_lineup_claim(self):
        """Same contract as dlna_lineup: with no year there is nothing
        to assert, so the panel shows no line-up rather than a roster."""
        self._seed_floyd()
        _, d = self._get(artist="Pink Floyd")
        self.assertEqual(d["lineup"], [])
        self.assertIsNone(d["lineup_year"])

    def test_track_count_comes_from_the_library(self):
        self._seed_floyd()
        self.db.upsert_tracks(UDN, [{
            "id": "1", "url": "http://x/1", "title": "Money",
            "artist": "Pink Floyd", "album": "DSOTM",
            "duration": "0:06:00", "mime": "audio/flac"}])
        _, d = self._get(artist="Pink Floyd")
        self.assertEqual(d["track_count"], 1)

    def test_an_artist_with_an_mbid_but_no_facts_yet_is_404(self):
        """The sweep has resolved the id but not fetched the facts —
        an empty panel is worse than no button."""
        self.db.artist_meta_set("Elbow", mbid="eb", source="search")
        self.assertEqual(self._get(artist="Elbow")[0], 404)

    def test_a_bad_year_degrades_instead_of_failing(self):
        self._seed_floyd()
        st, d = self._get(artist="Pink Floyd", year="banana")
        self.assertEqual(st, 200)
        self.assertEqual(d["lineup"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
