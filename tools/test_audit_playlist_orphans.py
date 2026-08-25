#!/usr/bin/env python3
"""
tools/test_audit_playlist_orphans.py — tests for the playlist-orphan audit.

Run standalone:
    python3 -m unittest tools.test_audit_playlist_orphans -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tools.audit_playlist_orphans import (  # noqa: E402
    _norm, apply_plan, build_index, find_orphans, plan,
)

_SCHEMA = """
CREATE TABLE tracks (url TEXT, title TEXT, artist TEXT, album TEXT, art TEXT);
CREATE TABLE playlists (id TEXT PRIMARY KEY, name TEXT);
CREATE TABLE playlist_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pl_id TEXT, url TEXT, title TEXT, artist TEXT, album TEXT,
    art TEXT, added_at TEXT DEFAULT '',
    UNIQUE(pl_id, url));
"""


def _db(tracks=(), pls=(), rows=()):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.executemany("INSERT INTO tracks VALUES (?,?,?,?,?)", tracks)
    c.executemany("INSERT INTO playlists VALUES (?,?)", pls)
    c.executemany(
        "INSERT INTO playlist_tracks (pl_id,url,title,artist,album,art) "
        "VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    return c, path


class TestNorm(unittest.TestCase):
    def test_folds_diacritics_case_and_smart_quotes(self):
        self.assertEqual(_norm("Pérez  Prado"), "perez prado")
        self.assertEqual(_norm("Luna’s Tuna"), "luna's tuna")
        self.assertEqual(_norm("  A  B  "), "a b")

    def test_empty_and_none(self):
        self.assertEqual(_norm(None), "")
        self.assertEqual(_norm(""), "")


class TestFindOrphans(unittest.TestCase):
    def test_only_rows_with_no_matching_track(self):
        conn, _ = _db(
            tracks=[("http://h/live", "T", "A", "Al", "art")],
            pls=[("p1", "Mix")],
            rows=[("p1", "http://h/live", "T", "A", "Al", ""),
                  ("p1", "http://h/dead", "T2", "A", "Al", "")],
        )
        orphans = find_orphans(conn)
        self.assertEqual([o["url"] for o in orphans], ["http://h/dead"])
        self.assertEqual(orphans[0]["pl_name"], "Mix")

    def test_clean_library_has_none(self):
        conn, _ = _db(
            tracks=[("http://h/a", "T", "A", "Al", "")],
            pls=[("p1", "Mix")],
            rows=[("p1", "http://h/a", "T", "A", "Al", "")],
        )
        self.assertEqual(find_orphans(conn), [])


class TestPlan(unittest.TestCase):
    """plan() is pure so the report you read and the mutation that runs are
    the same decision."""

    def _plan(self, tracks, rows):
        conn, _ = _db(tracks=tracks, pls=[("p1", "Mix")], rows=rows)
        return conn, plan(find_orphans(conn), *build_index(conn))

    def test_strong_match_on_artist_album_title(self):
        _, p = self._plan(
            [("http://h/new", "Watermelon Man", "Various", "Latin CD1", "A")],
            [("p1", "http://old:8201/x", "Watermelon Man", "Various",
              "Latin CD1", "")])
        self.assertEqual(p[0]["action"], "relink")
        self.assertEqual(p[0]["how"], "strong")
        self.assertEqual(p[0]["new_url"], "http://h/new")

    def test_song_level_match_when_the_album_differs(self):
        _, p = self._plan(
            [("http://h/new", "Donna", "10cc", "Greatest Hits", "")],
            [("p1", "http://old/x", "Donna", "10cc", "Original LP", "")])
        self.assertEqual(p[0]["how"], "song")

    def test_match_survives_a_retag_of_punctuation_and_case(self):
        _, p = self._plan(
            [("http://h/new", "Luna’s Tuna", "Caravan", "Business", "")],
            [("p1", "http://old/x", "LUNA'S TUNA", "caravan", "business", "")])
        self.assertEqual(p[0]["action"], "relink")

    def test_no_match_is_reported_not_guessed(self):
        _, p = self._plan(
            [("http://h/new", "Something Else", "Other", "Al", "")],
            [("p1", "http://old/x", "L'Heptade disc 2", "Harmonium", "X", "")])
        self.assertEqual(p[0]["action"], "unmatched")
        self.assertEqual(p[0]["new_url"], "")

    def test_a_track_with_no_artist_or_title_is_never_a_match_target(self):
        """Blank metadata would collide every orphan onto one row."""
        _, p = self._plan(
            [("http://h/junk", "", "", "", "")],
            [("p1", "http://old/x", "", "", "", "")])
        self.assertEqual(p[0]["action"], "unmatched")


class TestApply(unittest.TestCase):
    def test_relink_rewrites_url_and_fills_missing_art(self):
        conn, _ = _db(
            tracks=[("http://h/new", "T", "A", "Al", "http://h/art.jpg")],
            pls=[("p1", "Mix")],
            rows=[("p1", "http://old/x", "T", "A", "Al", "")])
        stats = apply_plan(conn, plan(find_orphans(conn),
                                      *build_index(conn)), False)
        self.assertEqual(stats["relinked"], 1)
        row = conn.execute("SELECT url, art FROM playlist_tracks").fetchone()
        self.assertEqual(row["url"], "http://h/new")
        self.assertEqual(row["art"], "http://h/art.jpg")

    def test_existing_art_is_not_overwritten(self):
        conn, _ = _db(
            tracks=[("http://h/new", "T", "A", "Al", "http://h/new.jpg")],
            pls=[("p1", "Mix")],
            rows=[("p1", "http://old/x", "T", "A", "Al", "http://h/keep.jpg")])
        apply_plan(conn, plan(find_orphans(conn), *build_index(conn)), False)
        self.assertEqual(
            conn.execute("SELECT art FROM playlist_tracks").fetchone()["art"],
            "http://h/keep.jpg")

    def test_relink_onto_a_track_already_in_the_playlist_removes_the_dupe(self):
        """UNIQUE(pl_id, url) would reject the UPDATE; drop the stale row."""
        conn, _ = _db(
            tracks=[("http://h/new", "T", "A", "Al", "")],
            pls=[("p1", "Mix")],
            rows=[("p1", "http://h/new", "T", "A", "Al", ""),
                  ("p1", "http://old/x", "T", "A", "Al", "")])
        stats = apply_plan(conn, plan(find_orphans(conn),
                                      *build_index(conn)), False)
        self.assertEqual(stats["dup_removed"], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0],
            1)

    def test_unmatched_is_kept_by_default(self):
        conn, _ = _db(tracks=[], pls=[("p1", "Mix")],
                      rows=[("p1", "http://old/x", "T", "A", "Al", "")])
        stats = apply_plan(conn, plan(find_orphans(conn),
                                      *build_index(conn)), False)
        self.assertEqual(stats["unmatched_kept"], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0],
            1)

    def test_unmatched_is_removed_only_when_asked(self):
        conn, _ = _db(tracks=[], pls=[("p1", "Mix")],
                      rows=[("p1", "http://old/x", "T", "A", "Al", "")])
        stats = apply_plan(conn, plan(find_orphans(conn),
                                      *build_index(conn)), True)
        self.assertEqual(stats["unmatched_removed"], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0],
            0)

    def test_idempotent(self):
        conn, _ = _db(
            tracks=[("http://h/new", "T", "A", "Al", "")],
            pls=[("p1", "Mix")],
            rows=[("p1", "http://old/x", "T", "A", "Al", "")])
        apply_plan(conn, plan(find_orphans(conn), *build_index(conn)), False)
        self.assertEqual(find_orphans(conn), [])
        stats = apply_plan(conn, plan(find_orphans(conn),
                                      *build_index(conn)), False)
        self.assertEqual(stats["relinked"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
