#!/usr/bin/env python3
"""
test_artist_chronology.py — an artist's albums, oldest first.

An artist page is a career, so it reads best in the order the records
were made. That means two things: every album row carries a year, and
the list is ordered by it rather than alphabetically.

The year is the EFFECTIVE year — MIN of the file tag and the
MusicBrainz original — the same rule the decade browse uses
(`FacetsMixin._EFFECTIVE_YEAR`). That matters here more than anywhere:
a 2011 remaster of a 1975 album belongs at 1975, or the evolution the
ordering exists to show is wrong.
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

    def test_an_original_year_override_decides_the_position(self):
        """A remaster must sort where the RECORD belongs, not where the
        edition was pressed — otherwise the career reads out of order."""
        self._add("Bowie", "Ziggy (2012 remaster)", 2012)
        self._add("Bowie", "Aladdin Sane", 1973)
        url = "http://x/Bowie/Ziggy (2012 remaster)/0"
        self.db.metadata_override_set(url, "manual", year=1972)
        got = [a["album"] for a in self.db.artist_albums(UDN, "Bowie")]
        self.assertEqual(got[0], "Ziggy (2012 remaster)")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
