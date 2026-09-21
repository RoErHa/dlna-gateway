#!/usr/bin/env python3
"""
test_artist_chronology.py — an artist's albums, oldest first.

An artist page is a career, so it reads best in the order the records
were made. That means two things: every album row carries a year, and
the list is ordered by it rather than alphabetically.

The year is the EDITION year (2026-09-21, was the effective year).
A row reports `year` — what this pressing says it is — and
`year_original`, the oldest recording on it. Both travel; the client
decides what to show.

That REPLACED "a 2011 remaster of a 1975 album belongs at 1975", which
read well and did not survive the real library: it dated the three
40th-anniversary discs of *The Piper at the Gates of Dawn* 1967, the
same as the original beside them, and only sometimes — the pull-back
needed a MusicBrainz override, so Disc 1 read 2007 while Discs 2 and 3
read 1967. See `tests/test_album_year.py` for the full case.

The ORDER follows what each row displays, because a list whose sequence
disagrees with its own labels reads as a bug. The decade facet still
uses the effective year: that one really is asking when the music was
made.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_library import LibraryDB                              # noqa: E402

UDN = "uuid:localfs-chron"


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)

    def _add(self, artist, album, year, n=2, folder=None):
        folder = folder or f"{artist}/{album}"
        self.db.upsert_tracks(UDN, [{
            "id": f"{folder}-{i}", "url": f"http://x/{folder}/{i}",
            "title": f"T{i}", "artist": artist, "album": album,
            "duration": "0:03:00", "mime": "audio/flac", "year": year,
            "file_path": f"/m/{folder}/{i}.flac", "album_key": folder}
            for i in range(n)])


class TestChronology(_Base):
    def test_albums_come_back_oldest_first(self):
        self._add("Bowie", "Heroes", 1977)
        self._add("Bowie", "Hunky Dory", 1971)
        self._add("Bowie", "Let's Dance", 1983)
        got = [(a["album"], a["year"])
               for a in self.db.artist_albums(UDN, "Bowie")]
        self.assertEqual([y for _, y in got], [1971, 1977, 1983])

    def test_every_row_carries_its_year(self):
        self._add("Bowie", "Low", 1977)
        row = self.db.artist_albums(UDN, "Bowie")[0]
        self.assertIn("year", row)
        self.assertEqual(row["year"], 1977)

    def test_the_order_follows_the_year_each_row_displays(self):
        """A remaster sorts at its own date, which is what its label
        says. Ordering it at 1972 while the row reads 2012 is the
        version of this that looks broken."""
        self._add("Bowie", "Ziggy (2012 remaster)", 2012)
        self._add("Bowie", "Aladdin Sane", 1973)
        url = "http://x/Bowie/Ziggy (2012 remaster)/0"
        self.db.metadata_override_set(url, "manual", year=1972)
        got = [a["album"] for a in self.db.artist_albums(UDN, "Bowie")]
        self.assertEqual(got, ["Aladdin Sane", "Ziggy (2012 remaster)"])

    def test_an_album_with_no_year_sorts_last(self):
        """It cannot be placed in the evolution, and guessing a position
        for it would be a claim we cannot support. Last, not first —
        first would misrepresent it as the earliest work."""
        self._add("Bowie", "Undated Bootleg", None)
        self._add("Bowie", "Heroes", 1977)
        got = [a["album"] for a in self.db.artist_albums(UDN, "Bowie")]
        self.assertEqual(got[-1], "Undated Bootleg")

    def test_same_year_albums_fall_back_to_the_name(self):
        """Stable order for a year an artist released twice in."""
        self._add("Bowie", "Low", 1977)
        self._add("Bowie", "Heroes", 1977)
        got = [a["album"] for a in self.db.artist_albums(UDN, "Bowie")]
        self.assertEqual(got, ["Heroes", "Low"])

    def test_the_own_classification_still_works(self):
        """Ordering must not disturb the records-vs-appearances split."""
        self._add("Bowie", "Hunky Dory", 1971)
        self.db.upsert_tracks(UDN, [{
            "id": f"comp-{i}", "url": f"http://x/comp/{i}", "title": f"C{i}",
            "artist": a, "album": "Big Comp", "duration": "0:03:00",
            "mime": "audio/flac", "year": 1999,
            "file_path": f"/m/comp/{i}.flac", "album_key": "Various/Big Comp"}
            for i, a in enumerate(["Bowie", "X", "Y", "Z", "W", "V"])])
        rows = self.db.artist_albums(UDN, "Bowie")
        own = {r["album"]: r["own"] for r in rows}
        self.assertTrue(own["Hunky Dory"])
        self.assertFalse(own["Big Comp"])

    def test_a_non_localfs_source_is_ordered_too(self):
        """The UPnP path uses (artist, album) grouping but the same
        promise to the reader."""
        upnp = "uuid:upnp-test"
        for album, year in [("Later", 1990), ("Earlier", 1980)]:
            self.db.upsert_tracks(upnp, [{
                "id": f"{album}", "url": f"http://u/{album}", "title": "T",
                "artist": "Someone", "album": album, "duration": "0:03:00",
                "mime": "audio/flac", "year": year}])
        got = [a["album"] for a in self.db.artist_albums(upnp, "Someone")]
        self.assertEqual(got, ["Earlier", "Later"])


class TestAlbumDateEverywhere(_Base):
    """The year was on the artist page but NOWHERE ELSE — not on the
    Albums browse, not on an open album, not in album_tracks. 96% of
    this library's albums have one, so the data was never the problem;
    three queries simply didn't select it (2026-09-21)."""

    def setUp(self):
        super().setUp()
        self._add("Bowie", "Hunky Dory", 1971, n=3)

    def test_the_albums_browse_carries_a_year(self):
        page = self.db.browse_letter(UDN, "albums", "H")
        row = [r for r in page["items"] if r["album"] == "Hunky Dory"][0]
        self.assertEqual(row["year"], 1971)

    def test_all_albums_carries_a_year(self):
        row = [a for a in self.db.all_albums(UDN)
               if a["album"] == "Hunky Dory"][0]
        self.assertEqual(row["year"], 1971)

    def test_album_tracks_carry_a_year(self):
        """So an open album can show its date from any entry point —
        favourites, a genre, a decade, a search result — without the
        caller having to carry it in."""
        tracks = self.db.album_tracks(UDN, "Bowie", "Hunky Dory",
                                      album_key="Bowie/Hunky Dory")
        self.assertTrue(tracks)
        self.assertEqual(tracks[0]["year"], 1971)

    def test_a_remaster_reports_both_of_its_dates(self):
        """The edition leads and the recording travels with it, so the
        two can be told apart without either being thrown away."""
        self._add("Bowie", "Ziggy", 2012, n=2)
        self.db.metadata_override_set("http://x/Bowie/Ziggy/0", "manual",
                                      year=1972)
        row = [a for a in self.db.all_albums(UDN)
               if a["album"] == "Ziggy"][0]
        self.assertEqual((row["year"], row["year_original"]), (2012, 1972))

    def test_an_undated_album_reports_none_not_zero(self):
        """0 would render as a year; None is what 'unknown' must be."""
        self._add("Bowie", "Undated", None, n=2)
        row = [a for a in self.db.all_albums(UDN)
               if a["album"] == "Undated"][0]
        self.assertIsNone(row["year"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
