#!/usr/bin/env python3
"""Tests for tools/cutover_copy_userdata.py — over throw-away SQLite DBs."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cutover_copy_userdata as cc

_SCHEMA = """
CREATE TABLE album_art(artist TEXT, album TEXT, art_url TEXT, source TEXT,
                       updated_at INTEGER, PRIMARY KEY(artist, album));
CREATE TABLE radio_favourites(station_uuid TEXT PRIMARY KEY, name TEXT,
                       stream_url TEXT, sort_order INTEGER);
CREATE TABLE play_counts(url TEXT PRIMARY KEY, count INTEGER, last_played INTEGER);
CREATE TABLE lyrics(url TEXT PRIMARY KEY, plain TEXT, synced TEXT, source TEXT,
                    fetched_at INTEGER);
-- deliberately different column ORDER than 1.x to exercise name-intersection
CREATE TABLE metadata_overrides(url TEXT PRIMARY KEY, artist TEXT, album TEXT,
                       title TEXT, genre TEXT, year INTEGER, updated_at INTEGER,
                       source TEXT);
CREATE TABLE playlists(id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE album_favourites(artist TEXT, album TEXT, album_key TEXT,
                       added_at INTEGER, PRIMARY KEY(artist, album, album_key));
"""


def _mkdb(path):
    c = sqlite3.connect(path)
    c.executescript(_SCHEMA)
    c.commit()
    c.close()


class TestCutoverCopy(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.src = os.path.join(self.d, "src.db")
        self.dst = os.path.join(self.d, "dst.db")
        _mkdb(self.src)
        _mkdb(self.dst)

    def _s(self, sql, args=()):
        c = sqlite3.connect(self.src); c.execute(sql, args); c.commit(); c.close()

    def _t(self, sql, args=()):
        c = sqlite3.connect(self.dst); c.execute(sql, args); c.commit(); c.close()

    def _q(self, db, sql, args=()):
        c = sqlite3.connect(db); r = c.execute(sql, args).fetchall(); c.close(); return r

    def _apply(self, **kw):
        return cc.run(self.src, self.dst, apply=True, backup=False, **kw)

    def test_album_art_real_cover_wins_notfound_fills(self):
        # src: real MB cover for (A,Al1); notfound for (A,Al2)
        self._s("INSERT INTO album_art VALUES('A','Al1','http://cover1','musicbrainz',1)")
        self._s("INSERT INTO album_art VALUES('A','Al2','','notfound',1)")
        # dst: (A,Al1) was notfound (should be REPLACED by src cover);
        #      (A,Al3) already has a cover that src's notfound must NOT clobber
        self._t("INSERT INTO album_art VALUES('A','Al1','','notfound',1)")
        self._t("INSERT INTO album_art VALUES('A','Al3','http://cover3','sibling',1)")
        self._s("INSERT INTO album_art VALUES('A','Al3','','notfound',1)")  # src notfound for Al3
        self._apply()
        got = {(a, al): (url, src) for a, al, url, src in
               self._q(self.dst, "SELECT artist,album,art_url,source FROM album_art")}
        self.assertEqual(got[("A", "Al1")], ("http://cover1", "musicbrainz"))  # real wins
        self.assertEqual(got[("A", "Al2")], ("", "notfound"))                  # filled
        self.assertEqual(got[("A", "Al3")], ("http://cover3", "sibling"))      # cover kept

    def test_additive_tables_merge(self):
        self._s("INSERT INTO radio_favourites VALUES('u1','S1','http://s1',0)")
        self._t("INSERT INTO radio_favourites VALUES('u2','S2','http://s2',0)")
        self._s("INSERT INTO play_counts VALUES('http://x:8200/a',5,1)")
        self._s("INSERT INTO lyrics VALUES('http://x:8200/a','la','sa','lrclib',1)")
        self._s("INSERT INTO metadata_overrides VALUES('http://x:8200/a','Ar','Al','Ti','G',1999,1,'manual')")
        self._apply()
        self.assertEqual(len(self._q(self.dst, "SELECT 1 FROM radio_favourites")), 2)
        self.assertEqual(self._q(self.dst, "SELECT count FROM play_counts")[0][0], 5)
        self.assertEqual(self._q(self.dst, "SELECT source FROM lyrics")[0][0], "lrclib")
        # name-intersection handles the different column order
        self.assertEqual(self._q(self.dst, "SELECT year,source FROM metadata_overrides")[0],
                         (1999, "manual"))

    def test_excluded_tables_untouched(self):
        self._s("INSERT INTO playlists VALUES('p1','SrcList')")
        self._s("INSERT INTO album_favourites VALUES('A','Al','k',1)")
        self._t("INSERT INTO playlists VALUES('pl_fresh','DstList')")
        self._apply()
        self.assertEqual(self._q(self.dst, "SELECT id,name FROM playlists"),
                         [("pl_fresh", "DstList")])          # not overwritten/added
        self.assertEqual(self._q(self.dst, "SELECT COUNT(*) FROM album_favourites")[0][0], 0)

    def test_dry_run_no_mutation(self):
        self._s("INSERT INTO play_counts VALUES('http://x/a',9,1)")
        cc.run(self.src, self.dst, apply=False, backup=False)
        self.assertEqual(self._q(self.dst, "SELECT COUNT(*) FROM play_counts")[0][0], 0)

    def test_url_rewrite(self):
        self._s("INSERT INTO play_counts VALUES('http://x:8201/a',3,1)")
        self._apply(rewrite=("8201", "8200"))
        self.assertEqual(self._q(self.dst, "SELECT url FROM play_counts")[0][0],
                         "http://x:8200/a")

    def test_idempotent(self):
        self._s("INSERT INTO play_counts VALUES('http://x/a',1,1)")
        self._apply(); self._apply()
        self.assertEqual(self._q(self.dst, "SELECT COUNT(*) FROM play_counts")[0][0], 1)

    def test_spec_excludes_are_disjoint(self):
        self.assertFalse(set(cc.COPY_SPEC) & set(cc.EXCLUDE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
