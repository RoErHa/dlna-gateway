#!/usr/bin/env python3
"""
test_track_credits.py — composer/lyricist fetched from MusicBrainz for
the tracks whose files carry none (step 4).

**Why a separate table rather than `tracks.composer`.** A rescan is
blank-safe, so a fetched credit would survive that — but `clear(udn)`
DELETEs `tracks`, and a rebuild-index would throw away hours of
rate-limited lookups. `track_credits` lives in the survives-a-rebuild
half of the schema, exactly like `lyrics`.

**The file tag wins.** These fill the ~68% of tracks that have nothing;
they never override what the file already says.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_library import LibraryDB                              # noqa: E402

UDN = "uuid:localfs-cred"
URL = "http://x/1"


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)

    def _track(self, composer="", title="Money", url=URL):
        self.db.upsert_tracks(UDN, [{
            "id": "1", "url": url, "title": title, "artist": "Pink Floyd",
            "album": "DSOTM", "duration": "0:06:00", "mime": "audio/flac",
            "composer": composer, "album_key": "PF/DSOTM"}])


class TestRoundTrip(_Base):
    def test_set_then_get(self):
        self._track()
        self.db.track_credits_set(URL, "Roger Waters", "Roger Waters",
                                  work_mbid="w1", source="musicbrainz")
        row = self.db.track_credits_get(URL)
        self.assertEqual(row["composer"], "Roger Waters")
        self.assertEqual(row["work_mbid"], "w1")

    def test_sticky_notfound_costs_nothing_next_run(self):
        self.db.track_credits_set(URL, None, None, source="notfound")
        self.assertEqual(self.db.track_credits_get(URL)["source"], "notfound")

    def test_unknown_url_is_none(self):
        self.assertIsNone(self.db.track_credits_get("http://nope"))


class TestReadPrecedence(_Base):
    def test_a_fetched_credit_fills_an_empty_file_tag(self):
        self._track(composer="")
        self.db.track_credits_set(URL, "Roger Waters", "", source="musicbrainz")
        self.assertEqual(self.db.track_meta_by_url(URL)["composer"],
                         "Roger Waters")

    def test_the_FILE_TAG_WINS_over_a_fetched_credit(self):
        """The file is what the owner actually has; MusicBrainz is
        filling gaps, not correcting the library."""
        self._track(composer="Someone On The File")
        self.db.track_credits_set(URL, "Roger Waters", "", source="musicbrainz")
        self.assertEqual(self.db.track_meta_by_url(URL)["composer"],
                         "Someone On The File")

    def test_a_fetched_credit_is_junk_filtered_like_any_other(self):
        """dlna_credits.clean_credit applies to every source."""
        self._track(composer="")
        self.db.track_credits_set(URL, "www.t.me/pmedia_music", "",
                                  source="musicbrainz")
        self.assertEqual(self.db.track_meta_by_url(URL)["composer"], "")


class TestSurvivesRebuild(_Base):
    def test_clear_udn_does_not_touch_track_credits(self):
        """THE reason this is its own table. Each row cost a
        rate-limited MusicBrainz round-trip."""
        self._track()
        self.db.track_credits_set(URL, "Roger Waters", "", source="musicbrainz")
        self.db.clear(UDN)
        self.assertEqual(self.db.track_credits_get(URL)["composer"],
                         "Roger Waters")


class TestWorklist(_Base):
    def test_lists_only_tracks_with_no_composer_and_no_row(self):
        self._track(composer="", url="http://x/a", title="A")
        self._track(composer="Has One", url="http://x/b", title="B")
        self._track(composer="", url="http://x/c", title="C")
        self.db.track_credits_set("http://x/c", None, None, source="notfound")
        todo = self.db.tracks_needing_credits(UDN)
        self.assertEqual([t["url"] for t in todo], ["http://x/a"])

    def test_worklist_rows_carry_what_the_matcher_needs(self):
        self._track(composer="", url="http://x/a", title="Money")
        t = self.db.tracks_needing_credits(UDN)[0]
        self.assertEqual(t["title"], "Money")
        self.assertEqual(t["artist"], "Pink Floyd")

    def test_limit_is_honoured(self):
        for i in range(3):
            self._track(composer="", url=f"http://x/{i}", title=f"T{i}")
        self.assertEqual(len(self.db.tracks_needing_credits(UDN, limit=2)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
