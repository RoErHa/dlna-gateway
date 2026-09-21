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


class TestArtistInfo(_Base):
    """The step-3 display columns and the band line-up."""

    def setUp(self):
        super().setUp()
        self.db.artist_meta_set("Pink Floyd", mbid="pf", source="search")

    def test_info_round_trip(self):
        self.db.artist_info_set("Pink Floyd", {
            "mb_type": "Group", "born": "1965", "died": "2014",
            "birth_place": "London", "country": "GB",
            "genres": "progressive rock, psychedelic rock",
            "bio": "Pink Floyd are an English rock band…",
            "bio_url": "https://en.wikipedia.org/wiki/Pink_Floyd"})
        row = self.db.artist_info_get("Pink Floyd")
        self.assertEqual(row["mb_type"], "Group")
        self.assertEqual(row["born"], "1965")
        self.assertEqual(row["mbid"], "pf")          # untouched
        self.assertTrue(row["meta_fetched_at"])

    def test_info_never_clobbers_the_resolved_mbid_or_source(self):
        """artist_meta.source describes how the MBID was resolved, not
        where the biography came from. Writing info must not rewrite
        it, or a 'manual' resolution silently becomes 'search'."""
        self.db.artist_meta_set("Pink Floyd", mbid="pf", source="manual")
        self.db.artist_info_set("Pink Floyd", {"mb_type": "Group"})
        row = self.db.artist_info_get("Pink Floyd")
        self.assertEqual(row["source"], "manual")
        self.assertEqual(row["mbid"], "pf")

    def test_members_round_trip_with_both_stints(self):
        """Richard Wright twice — begin_date is in the primary key, so
        1965-1981 and 1987-2008 are separate rows."""
        self.db.artist_members_set("Pink Floyd", [
            {"name": "Richard Wright", "instruments": "keyboard",
             "begin": "1965", "end": "1981", "mbid": "rw"},
            {"name": "Richard Wright", "instruments": "keyboard",
             "begin": "1987", "end": "2008-09-15", "mbid": "rw"},
            {"name": "Nick Mason", "instruments": "drums",
             "begin": "1965", "end": "", "mbid": "nm"}])
        got = self.db.artist_members_get("Pink Floyd")
        self.assertEqual(len(got), 3)
        wright = [m for m in got if m["member_name"] == "Richard Wright"]
        self.assertEqual({w["begin_date"] for w in wright}, {"1965", "1987"})

    def test_members_are_REPLACED_not_merged(self):
        """A re-fetch is a sync: a member MusicBrainz has since removed
        must disappear, or the line-up only ever grows."""
        self.db.artist_members_set("Pink Floyd", [
            {"name": "Syd Barrett", "instruments": "guitar",
             "begin": "1965", "end": "1968"}])
        self.db.artist_members_set("Pink Floyd", [
            {"name": "Nick Mason", "instruments": "drums",
             "begin": "1965", "end": ""}])
        got = [m["member_name"] for m in self.db.artist_members_get("Pink Floyd")]
        self.assertEqual(got, ["Nick Mason"])

    def test_members_survive_clear_udn(self):
        self.db.upsert_tracks(UDN, [{
            "id": "1", "url": "http://x/1", "title": "T",
            "artist": "Pink Floyd", "album": "A", "duration": "0:03:00",
            "mime": "audio/flac"}])
        self.db.artist_members_set("Pink Floyd", [
            {"name": "Nick Mason", "instruments": "drums",
             "begin": "1965", "end": ""}])
        self.db.clear(UDN)
        self.assertEqual(len(self.db.artist_members_get("Pink Floyd")), 1)

    def test_an_unknown_artist_has_no_info_and_no_members(self):
        self.assertIsNone(self.db.artist_info_get("Nobody"))
        self.assertEqual(self.db.artist_members_get("Nobody"), [])

    def test_worklist_is_artists_with_an_mbid_and_no_info_yet(self):
        self.db.artist_meta_set("Elbow", mbid="eb", source="search")
        self.db.artist_meta_set("Nobody", mbid=None, source="notfound")
        todo = self.db.artists_needing_info()
        self.assertIn("Pink Floyd", todo)
        self.assertIn("Elbow", todo)
        self.assertNotIn("Nobody", todo)     # no mbid — nothing to fetch with

    def test_worklist_drops_artists_already_fetched(self):
        """Sticky like every other cache here: a second run resumes."""
        self.db.artist_meta_set("Elbow", mbid="eb", source="search")
        self.db.artist_info_set("Pink Floyd", {"mb_type": "Group"})
        self.assertEqual(self.db.artists_needing_info(), ["Elbow"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
