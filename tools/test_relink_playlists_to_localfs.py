#!/usr/bin/env python3
"""Tests for tools/relink_playlists_to_localfs.py over a throw-away DB."""
import os
import sys
import tempfile
import time
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
import tools.relink_playlists_to_localfs as relink

DEAD = "http://192.168.1.125:26125/content/"
LF_UDN = "uuid:localfs-x"


def _lf_url(i):  return f"http://192.168.1.125:8200/localfs/stream/lf{i}"
def _lf_art(i):  return f"http://192.168.1.125:8200/localfs/art/lf{i}"


class TestRelink(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        # LocalFs library
        self.db.upsert_tracks(LF_UDN, [
            {"id": "lf1", "url": _lf_url(1), "art": _lf_art(1),
             "title": "Song One", "artist": "Alice", "album": "Album A",
             "album_key": "Alice/Album A", "mime": "audio/flac"},
            {"id": "lf2", "url": _lf_url(2), "art": _lf_art(2),
             "title": "Café Olé", "artist": "Bob", "album": "Comp 2024",
             "album_key": "VA/Comp", "mime": "audio/flac"},
        ])

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def _add_pl_track(self, pl_id, url, artist, album, title, art=""):
        with self.db._pool.write() as c:
            c.execute(
                "INSERT OR IGNORE INTO playlists(id, name, sort_order) "
                "VALUES (?,?,0)", (pl_id, pl_id))
            c.execute(
                "INSERT INTO playlist_tracks(pl_id, url, title, artist, "
                "album, duration, art, added_at) VALUES (?,?,?,?,?,?,?,?)",
                (pl_id, url, title, artist, album, "0:03:00", art,
                 int(time.time())))

    def _conn(self):
        import sqlite3
        c = sqlite3.connect(self.tmp.name)
        c.row_factory = sqlite3.Row
        return c

    def _urls(self, pl_id):
        with self.db._pool.read() as c:
            return [r["url"] for r in c.execute(
                "SELECT url FROM playlist_tracks WHERE pl_id=?",
                (pl_id,)).fetchall()]

    def test_strong_match_relinks_url_and_art(self):
        self._add_pl_track("p1", DEAD + "x1.flac", "Alice", "Album A", "Song One")
        conn = self._conn()
        plan = relink.plan_playlist_relink(conn)
        relink.apply_plan(conn, plan, [], prune_favs=False)
        conn.close()
        self.assertEqual(self._urls("p1"), [_lf_url(1)])
        with self.db._pool.read() as c:
            art = c.execute("SELECT art FROM playlist_tracks WHERE pl_id='p1'"
                            ).fetchone()["art"]
        self.assertEqual(art, _lf_art(1))

    def test_song_match_when_album_differs(self):
        # Same artist+title, different album (curly-apostrophe + case too).
        self._add_pl_track("p1", DEAD + "x2.flac", "bob", "Greatest Hits",
                           "café olé")
        conn = self._conn()
        plan = relink.plan_playlist_relink(conn)
        self.assertEqual(plan["stats"]["song"], 1)
        relink.apply_plan(conn, plan, [], prune_favs=False)
        conn.close()
        self.assertEqual(self._urls("p1"), [_lf_url(2)])

    def test_no_match_is_removed(self):
        self._add_pl_track("p1", DEAD + "x3.flac", "Nobody", "Nothing", "Gone")
        conn = self._conn()
        plan = relink.plan_playlist_relink(conn)
        self.assertEqual(plan["stats"]["removed_nomatch"], 1)
        relink.apply_plan(conn, plan, [], prune_favs=False)
        conn.close()
        self.assertEqual(self._urls("p1"), [])

    def test_duplicate_after_relink_removed(self):
        # Two dead rows in one playlist that map to the same LocalFs track.
        self._add_pl_track("p1", DEAD + "a.flac", "Alice", "Album A", "Song One")
        self._add_pl_track("p1", DEAD + "b.flac", "Alice", "Album A", "Song One")
        conn = self._conn()
        plan = relink.plan_playlist_relink(conn)
        relink.apply_plan(conn, plan, [], prune_favs=False)
        conn.close()
        self.assertEqual(self._urls("p1"), [_lf_url(1)])  # de-duped to one

    def test_already_localfs_untouched(self):
        self._add_pl_track("p1", _lf_url(1), "Alice", "Album A", "Song One",
                           art=_lf_art(1))
        conn = self._conn()
        plan = relink.plan_playlist_relink(conn)
        conn.close()
        self.assertEqual(plan["stats"]["total"], 0)  # nothing to do

    def test_dry_run_does_not_mutate(self):
        self._add_pl_track("p1", DEAD + "x1.flac", "Alice", "Album A", "Song One")
        conn = self._conn()
        relink.plan_playlist_relink(conn)   # plan only
        conn.close()
        self.assertEqual(self._urls("p1"), [DEAD + "x1.flac"])  # unchanged

    def test_idempotent(self):
        self._add_pl_track("p1", DEAD + "x1.flac", "Alice", "Album A", "Song One")
        conn = self._conn()
        relink.apply_plan(conn, relink.plan_playlist_relink(conn), [],
                          prune_favs=False)
        # second pass
        plan2 = relink.plan_playlist_relink(conn)
        conn.close()
        self.assertEqual(plan2["stats"]["total"], 0)

    def test_album_fav_prune(self):
        self.db.album_fav_add("Alice", "Album A", album_key="Alice/Album A")  # matches
        self.db.album_fav_add("Ghost", "Gone", album_key="Ghost/Gone")        # orphan
        conn = self._conn()
        orphans = relink.plan_album_fav_prune(conn)
        self.assertEqual(orphans, [("Ghost", "Gone", "Ghost/Gone")])
        relink.apply_plan(conn, {"relink": [], "remove": []}, orphans,
                          prune_favs=True)
        conn.close()
        self.assertTrue(self.db.album_fav_is("Alice", "Album A",
                                             album_key="Alice/Album A"))
        self.assertFalse(self.db.album_fav_is("Ghost", "Gone",
                                              album_key="Ghost/Gone"))


if __name__ == "__main__":
    unittest.main()
