#!/usr/bin/env python3
"""
test_artist_meta.py — the artist → MBID store (step 2 keystone).

Same persistence contract as album_art / play_counts / lyrics /
book_meta: independent of `tracks`, so a rebuild-index never throws away
work that cost an hour of rate-limited MusicBrainz calls.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_library import LibraryDB                              # noqa: E402

UDN = "uuid:localfs-test"


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)


class TestRoundTrip(_Base):
    def test_set_then_get(self):
        self.db.artist_meta_set("Elbow", mbid="abc-123", source="search")
        row = self.db.artist_meta_get("Elbow")
        self.assertEqual(row["mbid"], "abc-123")
        self.assertEqual(row["source"], "search")

    def test_lookup_is_spelling_insensitive(self):
        """Stored from a tag, read back from a browse name — the two
        legitimately differ in case, article and punctuation."""
        self.db.artist_meta_set("The Bad Seeds", mbid="x1", source="tag")
        for variant in ["the bad seeds", "Bad Seeds", "The  Bad  Seeds"]:
            with self.subTest(variant=variant):
                row = self.db.artist_meta_get(variant)
                self.assertIsNotNone(row)
                self.assertEqual(row["mbid"], "x1")

    def test_unknown_artist_is_none(self):
        self.assertIsNone(self.db.artist_meta_get("Nobody At All"))

    def test_notfound_is_recorded_without_an_mbid(self):
        """A sticky negative: refusing again next run costs no request."""
        self.db.artist_meta_set("Jim Brickman, Kristy Starling",
                                mbid=None, source="notfound")
        row = self.db.artist_meta_get("Jim Brickman, Kristy Starling")
        self.assertEqual(row["source"], "notfound")
        self.assertFalse(row["mbid"])


class TestManualWins(_Base):
    def test_automation_never_overwrites_a_manual_row(self):
        """A person resolved an ambiguity by hand; the sweep must not
        undo it on the next pass. Same rule as metadata_overrides and
        book_meta."""
        self.db.artist_meta_set("Nirvana", mbid="seattle", source="manual")
        self.db.artist_meta_set("Nirvana", mbid="uk-band", source="search")
        self.assertEqual(self.db.artist_meta_get("Nirvana")["mbid"],
                         "seattle")

    def test_manual_can_correct_an_automated_row(self):
        self.db.artist_meta_set("Nirvana", mbid="uk-band", source="search")
        self.db.artist_meta_set("Nirvana", mbid="seattle", source="manual")
        self.assertEqual(self.db.artist_meta_get("Nirvana")["mbid"],
                         "seattle")

    def test_a_later_automated_pass_may_fill_a_notfound(self):
        """Sticky, but not permanent — the tool decides when to retry;
        the DB must not block the write."""
        self.db.artist_meta_set("Elbow", mbid=None, source="notfound")
        self.db.artist_meta_set("Elbow", mbid="abc", source="search")
        self.assertEqual(self.db.artist_meta_get("Elbow")["mbid"], "abc")


class TestSurvivesReindex(_Base):
    def test_clear_udn_does_not_touch_artist_meta(self):
        """THE invariant. These rows cost ~1.2s of rate-limited MB time
        each; a rebuild-index must never spend that again."""
        self.db.upsert_tracks(UDN, [{
            "id": "1", "url": "http://x/1", "title": "T", "artist": "Elbow",
            "album": "A", "duration": "0:03:00", "mime": "audio/flac"}])
        self.db.artist_meta_set("Elbow", mbid="abc", source="search")
        self.db.clear(UDN)
        self.assertEqual(self.db.track_count(UDN), 0)
        self.assertEqual(self.db.artist_meta_get("Elbow")["mbid"], "abc")


class TestWorklist(_Base):
    def _seed(self, *artists):
        self.db.upsert_tracks(UDN, [
            {"id": str(i), "url": f"http://x/{i}", "title": f"T{i}",
             "artist": a, "album": "A", "duration": "0:03:00",
             "mime": "audio/flac"}
            for i, a in enumerate(artists)])

    def test_lists_artists_with_no_row_yet(self):
        self._seed("Elbow", "Rush")
        self.assertEqual(set(self.db.artists_without_mbid(UDN)),
                         {"Elbow", "Rush"})

    def test_resolved_and_notfound_artists_drop_out(self):
        """A sticky negative must not be re-queried every run — that is
        the difference between a 5-minute resume and another full hour
        against a rate-limited API."""
        self._seed("Elbow", "Rush", "Yes")
        self.db.artist_meta_set("Elbow", mbid="abc", source="search")
        self.db.artist_meta_set("Rush", mbid=None, source="notfound")
        self.assertEqual(self.db.artists_without_mbid(UDN), ["Yes"])

    def test_placeholder_artists_are_never_queried(self):
        """'Anon' and 'Various Artists' are sentinels, not performers —
        see dlna_artist_infer. Asking MB about them wastes the budget
        and can only return a wrong answer."""
        self._seed("Elbow", "Anon", "Various Artists", "")
        self.assertEqual(self.db.artists_without_mbid(UDN), ["Elbow"])

    def test_limit_is_honoured(self):
        self._seed("A1", "A2", "A3")
        self.assertEqual(len(self.db.artists_without_mbid(UDN, limit=2)), 2)


class TestOneFilePathForArtist(_Base):
    """Phase 1 of the sweep reads the mbid the FILE already carries, so
    it needs one readable path per artist — any one will do."""

    def _seed(self, rows):
        self.db.upsert_tracks(UDN, [
            {"id": str(i), "url": f"http://x/{i}", "title": f"T{i}",
             "artist": a, "album": "A", "duration": "0:03:00",
             "mime": "audio/flac", "file_path": fp}
            for i, (a, fp) in enumerate(rows)])

    def test_returns_a_path_for_the_artist(self):
        self._seed([("Elbow", "/music/elbow/01.flac")])
        self.assertEqual(self.db.one_file_path_for_artist(UDN, "Elbow"),
                         "/music/elbow/01.flac")

    def test_spelling_variants_find_the_same_artist(self):
        self._seed([("The Bad Seeds", "/music/bs/01.flac")])
        self.assertEqual(
            self.db.one_file_path_for_artist(UDN, "the bad seeds"),
            "/music/bs/01.flac")

    def test_rows_without_a_file_path_are_skipped(self):
        """A UPnP-sourced row has no file to read — it must not mask a
        sibling row that does."""
        self._seed([("Elbow", ""), ("Elbow", "/music/elbow/02.flac")])
        self.assertEqual(self.db.one_file_path_for_artist(UDN, "Elbow"),
                         "/music/elbow/02.flac")

    def test_unknown_artist_returns_empty(self):
        self.assertEqual(self.db.one_file_path_for_artist(UDN, "Nobody"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
