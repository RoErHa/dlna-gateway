#!/usr/bin/env python3
"""
tests/test_unknown_artists_playlist.py — the hand-editing worklist.

A track the indexer could not attribute is not a browse problem that can
be solved in code: past what a filename will give up, only a person knows
who the performer is. So every artist-less track is swept into one
playlist instead of being guessed at.

Run standalone:
    python3 -m unittest tests.test_unknown_artists_playlist -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
from dlna_library_sql import UNKNOWN_ARTISTS_PLAYLIST as PL_NAME

LF = "uuid:localfs-test"
OTHER = "uuid:localfs-other"


# The junk drawer: a folder that can never name its own performer, so
# an untagged track in it is genuinely unknown. Using an ATTRIBUTABLE
# folder name here would make every sweep test vacuous — the sweep would
# correctly infer an artist and take nothing.
DRAWER = "Unknown Artist/Unknown Album"


def _row(tid, artist, title, album_key=DRAWER, album=""):
    return {"id": tid, "url": f"http://h/{tid}", "title": title,
            "artist": artist, "album": album, "album_key": album_key,
            "file_path": f"/m/{album_key}/{tid}.mp3", "mime": "audio/mpeg"}


class TestSweep(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.db.upsert_tracks(LF, [
            _row("t1", "", "01 - Elegy Is Dancing"),
            _row("t2", "", "Track08"),
            _row("t3", "Real Artist", "A Named Song"),
        ])

    def tearDown(self):
        os.unlink(self._p)

    def _pl(self):
        pl = [p for p in self.db.pl_list() if p["name"] == PL_NAME]
        return self.db.pl_get(pl[0]["id"]) if pl else None

    def test_untagged_tracks_are_swept_in(self):
        res = self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual(res["added"], 2)
        titles = {t["title"] for t in self._pl()["tracks"]}
        self.assertEqual(titles, {"01 - Elegy Is Dancing", "Track08"})

    def test_a_tagged_track_is_never_swept_in(self):
        self.db.sync_unknown_artist_playlist(LF)
        titles = {t["title"] for t in self._pl()["tracks"]}
        self.assertNotIn("A Named Song", titles)

    def test_running_twice_adds_nothing_new(self):
        self.db.sync_unknown_artist_playlist(LF)
        again = self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual(again["added"], 0)
        self.assertEqual(again["total"], 2)

    def test_tagging_a_track_prunes_it_from_the_worklist(self):
        """The worklist tracks OUTSTANDING work, so finishing a file
        removes it. Otherwise the list only ever grows and stops meaning
        anything."""
        self.db.sync_unknown_artist_playlist(LF)
        self.db.upsert_tracks(LF, [_row("t1", "Now Tagged",
                                        "01 - Elegy Is Dancing")])
        res = self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual(res["pruned"], 1)
        self.assertEqual({t["title"] for t in self._pl()["tracks"]},
                         {"Track08"})

    def test_the_playlist_carries_a_usable_url_for_every_row(self):
        """It exists to be played and identified by ear."""
        self.db.sync_unknown_artist_playlist(LF)
        self.assertTrue(all(t["url"] for t in self._pl()["tracks"]))

    def test_a_blank_album_is_labelled_not_left_empty(self):
        self.db.sync_unknown_artist_playlist(LF)
        self.assertTrue(all(t["album"] for t in self._pl()["tracks"]))


class TestRestraint(unittest.TestCase):
    """It edits playlists automatically, so what it must NOT touch is as
    important as what it sweeps."""

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)

    def tearDown(self):
        os.unlink(self._p)

    def test_a_fully_tagged_library_grows_no_empty_playlist(self):
        self.db.upsert_tracks(LF, [_row("t1", "Someone", "A Song")])
        res = self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual(res, {"added": 0, "pruned": 0, "total": 0})
        self.assertEqual([p for p in self.db.pl_list()
                          if p["name"] == PL_NAME], [])

    def test_another_sources_rows_are_never_pruned(self):
        """Audiobooks opt out, but a second music source must not be able
        to delete the first one's outstanding work either."""
        self.db.upsert_tracks(LF, [_row("t1", "", "Untagged")])
        self.db.sync_unknown_artist_playlist(LF)
        self.db.upsert_tracks(OTHER, [_row("o1", "Tagged", "Other Song")])
        res = self.db.sync_unknown_artist_playlist(OTHER)
        self.assertEqual(res["pruned"], 0)
        self.assertEqual(res["total"], 1)

    def test_a_hand_added_row_of_another_source_survives(self):
        self.db.upsert_tracks(LF, [_row("t1", "", "Untagged")])
        self.db.sync_unknown_artist_playlist(LF)
        pl = [p for p in self.db.pl_list() if p["name"] == PL_NAME][0]
        self.db.pl_add_track(pl["id"], {
            "url": "http://elsewhere/x", "title": "Mine", "artist": "",
            "album": "", "duration": "", "art": ""})
        self.db.sync_unknown_artist_playlist(LF)
        titles = {t["title"] for t in self.db.pl_get(pl["id"])["tracks"]}
        self.assertIn("Mine", titles)

    def test_an_orphan_row_is_left_for_the_orphan_tool(self):
        """A row pointing at no track at all is a different repair, and
        this must not quietly delete playlist history."""
        self.db.upsert_tracks(LF, [_row("t1", "", "Untagged")])
        self.db.sync_unknown_artist_playlist(LF)
        pl = [p for p in self.db.pl_list() if p["name"] == PL_NAME][0]
        self.db.pl_add_track(pl["id"], {
            "url": "http://h/gone", "title": "Dead Row", "artist": "",
            "album": "", "duration": "", "art": ""})
        self.db.sync_unknown_artist_playlist(LF)
        titles = {t["title"] for t in self.db.pl_get(pl["id"])["tracks"]}
        self.assertIn("Dead Row", titles)

    def test_survives_a_rebuild_like_every_other_playlist(self):
        """`clear(udn)` must not touch it — the whole point is that the
        work outlives a re-index."""
        self.db.upsert_tracks(LF, [_row("t1", "", "Untagged")])
        self.db.sync_unknown_artist_playlist(LF)
        pl = [p for p in self.db.pl_list() if p["name"] == PL_NAME][0]
        self.db.clear(LF)
        self.assertEqual(len(self.db.pl_get(pl["id"])["tracks"]), 1)


class TestAudiobooksOptOut(unittest.TestCase):
    """A chapter with no artist tag is ORDINARY in an audiobook library —
    the author lives in `book_meta`. Sweeping 500-odd chapters in would
    bury the music that actually needs the work."""

    def test_music_provider_sweeps_by_default(self):
        from dlna_providers.localfs import LocalFsProvider
        p = LocalFsProvider(None, tempfile.gettempdir())
        self.assertTrue(p._collect_unknown_artists)

    def test_audiobook_provider_opts_out(self):
        from dlna_providers.localfs import LocalFsProvider
        p = LocalFsProvider(None, tempfile.gettempdir(),
                            id_namespace="audiobooks",
                            collect_unknown_artists=False)
        self.assertFalse(p._collect_unknown_artists)

    def test_the_wiring_actually_opts_audiobooks_out(self):
        """The flag is worthless if the boot path forgets to pass it."""
        import inspect

        import dlna_localfs_wiring as w
        src = inspect.getsource(w)
        self.assertIn("collect_unknown_artists=False", src)


class TestOnlyTheUnattributable(unittest.TestCase):
    """The worklist is for what NOTHING can attribute. A folder that
    names its own performer is `tools/artist_from_folder.py` work, and
    sweeping it in would bury the real hand-work under it."""

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)

    def tearDown(self):
        os.unlink(self._p)

    def _titles(self):
        pl = [p for p in self.db.pl_list() if p["name"] == PL_NAME]
        if not pl:
            return set()
        return {t["title"] for t in self.db.pl_get(pl[0]["id"])["tracks"]}

    def test_a_named_folder_is_not_swept(self):
        self.db.upsert_tracks(LF, [
            _row("d1", "", "Chapter One", album_key="Mira Calvo (1996) Caminhos"),
        ])
        res = self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual(res["total"], 0)

    def test_a_folder_its_siblings_agree_on_is_not_swept(self):
        self.db.upsert_tracks(LF, [
            _row("h1", "Stormwind", "Tagged", album_key="H-slug-2008-01-01"),
            _row("h2", "", "Untagged", album_key="H-slug-2008-01-01"),
        ])
        self.assertEqual(self.db.sync_unknown_artist_playlist(LF)["total"], 0)

    def test_the_drawer_is_still_swept(self):
        self.db.upsert_tracks(LF, [
            _row("j1", "", "Untitled"),
            _row("j2", "Some Artist", "Tagged"),
            _row("j3", "Other Artist", "Also Tagged"),
        ])
        self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual(self._titles(), {"Untitled"})

    def test_a_compilation_named_after_itself_is_swept(self):
        """Many performers, folder named after the COMPILATION — nothing
        can attribute the untagged ones."""
        self.db.upsert_tracks(LF, [
            _row("c1", "Ember Hollow", "A", album_key="Nights On Neptune"),
            _row("c2", "Bowie", "B", album_key="Nights On Neptune"),
            _row("c3", "", "Mystery", album_key="Nights On Neptune"),
        ])
        self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual(self._titles(), {"Mystery"})


class TestPrunesWhatIsNoLongerOutstanding(unittest.TestCase):
    """A track leaves the worklist two ways: somebody tagged it, or
    inference improved and a tool can now do it. Pruning only the first
    stranded 25 real rows (RVM, Mira Calvo) when the sweep was
    narrowed — they were still blank, so they were never pruned, but no
    longer belonged."""

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)

    def tearDown(self):
        os.unlink(self._p)

    def _pl(self):
        pl = [p for p in self.db.pl_list() if p["name"] == PL_NAME][0]
        return self.db.pl_get(pl["id"])["tracks"]

    def test_a_track_that_became_inferable_is_pruned(self):
        # A dated bootleg slug names nobody, and nothing else in the
        # folder is tagged → genuinely unknown → swept.
        self.db.upsert_tracks(LF, [
            _row("r1", "", "Bootleg Cut", album_key="SVance2008-07-05-sbd"),
            _row("d1", "", "Drawer Track"),
        ])
        self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual({t["title"] for t in self._pl()},
                         {"Bootleg Cut", "Drawer Track"})

        # A tagged sibling arrives. Unanimity can now name the performer,
        # so this is tool work — even though the track is STILL blank.
        self.db.upsert_tracks(LF, [
            _row("r2", "Sam Vance", "Tagged", album_key="SVance2008-07-05-sbd"),
        ])
        res = self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual(res["pruned"], 1)
        self.assertEqual({t["title"] for t in self._pl()}, {"Drawer Track"})

    def test_a_still_unattributable_row_is_kept(self):
        self.db.upsert_tracks(LF, [_row("d1", "", "Drawer Track")])
        self.db.sync_unknown_artist_playlist(LF)
        res = self.db.sync_unknown_artist_playlist(LF)
        self.assertEqual(res["pruned"], 0)
        self.assertEqual(len(self._pl()), 1)


if __name__ == "__main__":
    unittest.main()
