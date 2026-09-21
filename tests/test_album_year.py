#!/usr/bin/env python3
"""
test_album_year.py — an album is dated by its OWN edition (2026-09-21).

An album row used to report `MIN(_EFFECTIVE_YEAR)` — the oldest
recording it contains, after each track had already been pulled back to
its MusicBrainz original. Two consequences, both measured on the real
library before this changed:

  * The three 40th-anniversary discs of *The Piper at the Gates of Dawn*
    were dated 1967, the same as the original beside them — and only
    SOMETIMES, because the pull-back depends on whether
    `improve_song_years` happened to have written an override for those
    tracks. Disc 1 read 2007 while Discs 2 and 3 read 1967. Same record,
    same tags, three different answers.
  * `VA - 100 Greatest Jazz Icons (2020)` was dated **1946** and
    `Now #1s - 70 Years of the Official Singles Chart (2022)` **1952**:
    for a compilation, the oldest track is not the album's date by any
    reading.

842 of 2,249 albums showed a year that was not their own; 671 were out
by three years or more.

So an album now reports two facts and asserts neither over the other:

  `year`          the EDITION — what this pressing says it is
  `year_original` the oldest recording on it, when known

MAX, not MIN, decides the edition: every mixed-tag-year folder in the
reference library is a compilation whose tracks carry their own original
years, and whose compilation year is the newest of them (and matches the
year in the folder name). For an ordinary album every track shares one
year, so MIN and MAX agree and the choice is invisible.

**The decade facet is deliberately NOT changed** and is pinned here.
"Which decade is this music from" is a different question from "which
pressing is this", and it wants the recording: a 2007 reissue of a 1967
record belongs in the sixties.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_library import LibraryDB                              # noqa: E402

UDN = "uuid:localfs-year"


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)

    def _add(self, artist, album, folder, tracks):
        """tracks: [(title, tag_year, mb_year|None), …]"""
        rows = []
        for i, (title, tag, _mb) in enumerate(tracks):
            rows.append({
                "id": f"{folder}-{i}", "url": f"http://x/{folder}/{i}",
                "title": title, "artist": artist, "album": album,
                "duration": "0:03:00", "mime": "audio/flac", "year": tag,
                "file_path": f"/m/{folder}/{i}.flac", "album_key": folder})
        self.db.upsert_tracks(UDN, rows)
        for i, (_t, _tag, mb) in enumerate(tracks):
            if mb:
                self.db.metadata_override_set(
                    f"http://x/{folder}/{i}", "manual", year=mb,
                    update_tracks=False)

    def _albums(self):
        return {a["album"]: a for a in self.db.all_albums(UDN)}


class TestEditionYearLeads(_Base):
    def test_a_remaster_is_dated_by_its_own_pressing(self):
        self._add("Pink Floyd", "The Piper at the Gates of Dawn",
                  "PF/Piper 40th 2007",
                  [("Astronomy Domine", 2007, 1967),
                   ("Lucifer Sam", 2007, 1967)])
        a = self._albums()["The Piper at the Gates of Dawn"]
        self.assertEqual(a["year"], 2007)

    def test_the_original_recording_year_travels_with_it(self):
        self._add("Pink Floyd", "The Piper at the Gates of Dawn",
                  "PF/Piper 40th 2007",
                  [("Astronomy Domine", 2007, 1967)])
        a = self._albums()["The Piper at the Gates of Dawn"]
        self.assertEqual(a["year_original"], 1967)

    def test_an_untouched_album_reports_the_same_year_twice(self):
        """No remaster, nothing to disambiguate — the display rule that
        decides whether to SHOW the original lives in one place in the
        client; the server just reports both facts."""
        self._add("Bowie", "Hunky Dory", "Bowie/Hunky Dory 1971",
                  [("Changes", 1971, 1971)])
        a = self._albums()["Hunky Dory"]
        self.assertEqual((a["year"], a["year_original"]), (1971, 1971))

    def test_editions_of_one_record_stay_distinguishable(self):
        """The reported bug: four Piper folders, and the anniversary
        discs were indistinguishable from the original by date."""
        self._add("Pink Floyd", "The Piper at the Gates of Dawn",
                  "PF/Piper 1967", [("Astronomy Domine", 1967, None)])
        for d in (1, 2, 3):
            self._add("Pink Floyd", f"Piper 40th Disc {d}",
                      f"PF/Piper 40th Disc {d} 2007",
                      [("Astronomy Domine", 2007, 1967)])
        years = sorted(a["year"] for a in self.db.all_albums(UDN))
        self.assertEqual(years, [1967, 2007, 2007, 2007])

    def test_a_partial_override_cannot_change_the_edition(self):
        """Disc 1 read 2007 and Discs 2-3 read 1967 purely because the
        year sweep had reached some tracks and not others. The edition
        year must not depend on that."""
        self._add("Pink Floyd", "Piper 40th A", "PF/40th A",
                  [("One", 2007, None), ("Two", 2007, None)])
        self._add("Pink Floyd", "Piper 40th B", "PF/40th B",
                  [("One", 2007, 1967), ("Two", 2007, None)])
        got = self._albums()
        self.assertEqual(got["Piper 40th A"]["year"],
                         got["Piper 40th B"]["year"])


class TestCompilations(_Base):
    def test_a_compilation_is_dated_by_the_compilation(self):
        """Tracks carry their own original years; the album does not."""
        self._add("Various Artists", "100 Greatest Jazz Icons",
                  "VA/Jazz Icons 2020",
                  [("Round Midnight", 1946, None),
                   ("So What", 1959, None),
                   ("Sleeve note", 2020, None)])
        a = self._albums()["100 Greatest Jazz Icons"]
        self.assertEqual(a["year"], 2020)

    def test_and_still_reports_its_oldest_recording(self):
        self._add("Various Artists", "100 Greatest Jazz Icons",
                  "VA/Jazz Icons 2020",
                  [("Round Midnight", 1946, None),
                   ("Sleeve note", 2020, None)])
        a = self._albums()["100 Greatest Jazz Icons"]
        self.assertEqual(a["year_original"], 1946)


class TestEverySurfaceAgrees(_Base):
    def _seed(self):
        self._add("Pink Floyd", "Piper 40th", "PF/Piper 40th 2007",
                  [("Astronomy Domine", 2007, 1967)])

    def test_artist_albums(self):
        self._seed()
        a = self.db.artist_albums(UDN, "Pink Floyd")[0]
        self.assertEqual((a["year"], a["year_original"]), (2007, 1967))

    def test_browse_letter(self):
        self._seed()
        rows = self.db.browse_letter(UDN, "albums", "P")["items"]
        a = [r for r in rows if r["album"] == "Piper 40th"][0]
        self.assertEqual((a["year"], a["year_original"]), (2007, 1967))

    def test_album_tracks_carries_the_edition_year_per_row(self):
        """The album header derives its date from the tracks it just
        fetched, so it works from favourites, a genre, a decade or a
        search result — none of which hand it an album row."""
        self._seed()
        t = self.db.album_tracks(UDN, "Pink Floyd", "Piper 40th",
                                 album_key="PF/Piper 40th 2007")[0]
        self.assertEqual(t["year_edition"], 2007)
        self.assertEqual(t["year"], 1967)   # the track's own effective year

    def test_chronology_is_ordered_by_what_it_displays(self):
        """A list whose order disagrees with its own labels reads as a
        bug, so the sort follows the edition year too."""
        self._add("Pink Floyd", "Piper", "PF/Piper 1967",
                  [("A", 1967, None)])
        self._add("Pink Floyd", "Piper 40th", "PF/Piper 40th 2007",
                  [("A", 2007, 1967)])
        self._add("Pink Floyd", "Animals", "PF/Animals 1977",
                  [("A", 1977, None)])
        got = [(a["album"], a["year"])
               for a in self.db.artist_albums(UDN, "Pink Floyd")]
        self.assertEqual([y for _, y in got], [1967, 1977, 2007])


class TestDecadesAreDeliberatelyUnchanged(_Base):
    def test_a_reissue_still_belongs_to_the_decade_it_was_recorded_in(self):
        """'Which decade is this music from' is not 'which pressing is
        this'. The facet keeps the recording year on purpose."""
        self._add("Pink Floyd", "Piper 40th", "PF/Piper 40th 2007",
                  [("Astronomy Domine", 2007, 1967)])
        decades = {d["decade"]: d for d in self.db.all_decades(UDN)}
        self.assertIn(1960, decades)
        self.assertNotIn(2000, decades)


if __name__ == "__main__":
    unittest.main(verbosity=2)
