#!/usr/bin/env python3
"""
test_lineup.py — "who was in the band when this song was recorded?"

Step 3. Measured on the live MusicBrainz API before this was written:
per-RECORDING performer credits exist for only ~17% of this library's
tracks (release-level ~22%) — Pink Floyd's 'Shine On You Crazy Diamond'
has none at all. But band MEMBERSHIP with instruments and date ranges is
near-universal for groups:

    Syd Barrett     guitar, lead vocals, original    1965 .. 1968-04
    Richard Wright  keyboard, lead vocals, original  1965 .. 1981
    Roger Waters    bass guitar, lead vocals         1965 .. 1985
    Nick Mason      drums (drum set), percussion     1965 .. now
    David Gilmour   guitar, lead vocals, slide       1968-02-18 .. now
    Richard Wright  keyboard, lead vocals            1987 .. 2008-09-15

Note Wright appears TWICE — two separate stints. Intersecting those
ranges with a recording's year is what makes a per-song line-up
possible at all, and it is why this is a real function rather than a
list lookup.

It is INFERENCE, not fact, and the UI must say so — the caller labels
tier-1 recording credits "credited on this recording" and this
"line-up in <year>".
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_lineup import lineup_at                               # noqa: E402

PINK_FLOYD = [
    {"name": "Syd Barrett", "instruments": "guitar, lead vocals",
     "begin": "1965", "end": "1968-04"},
    {"name": "Richard Wright", "instruments": "keyboard, lead vocals",
     "begin": "1965", "end": "1981"},
    {"name": "Roger Waters", "instruments": "bass guitar, lead vocals",
     "begin": "1965", "end": "1985"},
    {"name": "Nick Mason", "instruments": "drums (drum set), percussion",
     "begin": "1965", "end": ""},
    {"name": "David Gilmour", "instruments": "guitar, lead vocals",
     "begin": "1968-02-18", "end": ""},
    {"name": "Richard Wright", "instruments": "keyboard, lead vocals",
     "begin": "1987", "end": "2008-09-15"},
]


def _names(rows):
    return [r["name"] for r in rows]


class TestLineupAt(unittest.TestCase):
    def test_wish_you_were_here_1975(self):
        """Barrett long gone, Gilmour in, Wright in his first stint."""
        got = _names(lineup_at(PINK_FLOYD, 1975))
        self.assertEqual(set(got),
                         {"Richard Wright", "Roger Waters",
                          "Nick Mason", "David Gilmour"})
        self.assertNotIn("Syd Barrett", got)

    def test_piper_at_the_gates_of_dawn_1967(self):
        """Barrett in, Gilmour not yet — he joined in Feb 1968."""
        got = _names(lineup_at(PINK_FLOYD, 1967))
        self.assertIn("Syd Barrett", got)
        self.assertNotIn("David Gilmour", got)

    def test_the_division_bell_1994_uses_the_second_stint(self):
        """Wright rejoined in 1987 after leaving in 1981. A naive
        first-match lookup reports him absent in 1994."""
        got = _names(lineup_at(PINK_FLOYD, 1994))
        self.assertIn("Richard Wright", got)
        self.assertNotIn("Roger Waters", got)

    def test_the_wall_1979_is_between_wrights_stints_but_inside_the_first(self):
        got = _names(lineup_at(PINK_FLOYD, 1979))
        self.assertIn("Richard Wright", got)

    def test_1984_falls_in_neither_wright_stint(self):
        got = _names(lineup_at(PINK_FLOYD, 1984))
        self.assertNotIn("Richard Wright", got)
        self.assertIn("Roger Waters", got)

    def test_an_open_ended_member_is_still_current(self):
        self.assertIn("Nick Mason", _names(lineup_at(PINK_FLOYD, 2020)))

    def test_nobody_is_returned_for_a_year_before_the_band_existed(self):
        self.assertEqual(lineup_at(PINK_FLOYD, 1960), [])

    def test_no_year_returns_nothing(self):
        """⚠ The whole claim is 'who was in the band THEN'. With no
        year there is no claim to make, and returning the full roster
        would silently present a different, wrong statement."""
        self.assertEqual(lineup_at(PINK_FLOYD, None), [])
        self.assertEqual(lineup_at(PINK_FLOYD, 0), [])

    def test_a_member_with_no_dates_at_all_is_always_included(self):
        """MB often has the membership but not the range. Dropping them
        would make a band look emptier than it was; including them is
        the same honesty level as the rest of this inference."""
        band = [{"name": "Someone", "instruments": "bass",
                 "begin": "", "end": ""}]
        self.assertEqual(_names(lineup_at(band, 1975)), ["Someone"])

    def test_duplicate_stints_do_not_duplicate_the_person(self):
        """Wright is two rows; in a year covered by one of them he must
        appear once, not twice."""
        got = _names(lineup_at(PINK_FLOYD, 1975))
        self.assertEqual(len(got), len(set(got)))

    def test_instruments_are_carried_through(self):
        row = next(r for r in lineup_at(PINK_FLOYD, 1975)
                   if r["name"] == "Nick Mason")
        self.assertIn("drums", row["instruments"])

    def test_a_full_iso_date_is_respected_not_just_the_year(self):
        """Gilmour joined 1968-02-18; a 1968 recording is ambiguous and
        we include him, but 1967 must not."""
        self.assertIn("David Gilmour", _names(lineup_at(PINK_FLOYD, 1968)))
        self.assertNotIn("David Gilmour", _names(lineup_at(PINK_FLOYD, 1967)))

    def test_empty_roster_is_empty(self):
        self.assertEqual(lineup_at([], 1975), [])
        self.assertEqual(lineup_at(None, 1975), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
