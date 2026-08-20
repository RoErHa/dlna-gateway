#!/usr/bin/env python3
"""Tests for tools/relink_overrides_to_localfs.py — throw-away temp DB,
no network, no live gateway."""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.relink_overrides_to_localfs import (
    _FANOUT_CAP, apply_plan, plan_relink)

UDN = "uuid:localfs-testtesttest"
BASE = "http://192.168.1.125:8200/localfs/stream/"
OLD = "http://192.168.1.125:8201/localfs/stream/"
ASSET = "http://192.168.1.125:26125/content/c2/b16/f44100/"


class Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = sqlite3.connect(self._p)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                udn TEXT, obj_id TEXT, url TEXT,
                title TEXT, artist TEXT, album TEXT, year INTEGER);
            CREATE TABLE metadata_overrides (
                url TEXT PRIMARY KEY,
                artist TEXT, album TEXT, title TEXT, genre TEXT,
                year INTEGER,
                updated_at TEXT DEFAULT (datetime('now')),
                source TEXT NOT NULL DEFAULT 'manual');
        """)

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def track(self, obj_id, artist, title, album="Album"):
        url = BASE + obj_id
        self.conn.execute(
            "INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
            "VALUES (?,?,?,?,?,?)", (UDN, obj_id, url, title, artist, album))
        return url

    def override(self, url, *, artist=None, album=None, title=None,
                 genre=None, year=None, source="manual"):
        self.conn.execute(
            "INSERT INTO metadata_overrides (url, artist, album, title, "
            "genre, year, source) VALUES (?,?,?,?,?,?,?)",
            (url, artist, album, title, genre, year, source))

    def ov_row(self, url):
        r = self.conn.execute(
            "SELECT * FROM metadata_overrides WHERE url=?", (url,)).fetchone()
        return dict(r) if r else None


class TestPortHeal(Base):

    def test_heal_repoints_full_row_by_exact_id(self):
        new = self.track("abc123", "Artist", "Song")
        self.override(OLD + "abc123", artist="Edited Artist",
                      title="Edited Title", year=1971)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["healed"], 1)
        apply_plan(self.conn, plan, True, False)
        row = self.ov_row(new)
        self.assertEqual(row["artist"], "Edited Artist")
        self.assertEqual(row["title"], "Edited Title")
        self.assertEqual(row["year"], 1971)
        self.assertIsNone(self.ov_row(OLD + "abc123"))

    def test_heal_skips_when_target_has_a_row(self):
        new = self.track("abc123", "Artist", "Song")
        self.override(new, year=1999)               # live row wins
        self.override(OLD + "abc123", year=1971)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["healed"], 0)
        self.assertEqual(plan["stats"]["heal_taken"], 1)
        apply_plan(self.conn, plan, True, False)
        self.assertEqual(self.ov_row(new)["year"], 1999)
        self.assertIsNotNone(self.ov_row(OLD + "abc123"))  # kept


class TestYearTransplant(Base):

    def test_unique_match_inserts_year_only_row(self):
        new = self.track("t1", "Bob Dylan", "Hurricane")
        orphan = ASSET + "d-1-co1.flac"
        self.override(orphan, artist="Bob Dylan", album="Old Album",
                      title="Hurricane", year=1976)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["transplanted"], 1)
        self.assertEqual(plan["stats"]["targets_insert"], 1)
        apply_plan(self.conn, plan, True, False)
        row = self.ov_row(new)
        self.assertEqual(row["year"], 1976)
        self.assertEqual(row["source"], "manual")
        # Fields deliberately NOT carried — they'd re-mask beets tags.
        self.assertIsNone(row["artist"])
        self.assertIsNone(row["album"])
        self.assertIsNone(row["title"])
        self.assertIsNone(self.ov_row(orphan))  # orphan consumed

    def test_multi_match_fans_out_to_all_tracks(self):
        u1 = self.track("t1", "Queen", "Bohemian Rhapsody", "A Night…")
        u2 = self.track("t2", "Queen", "Bohemian Rhapsody", "Greatest Hits")
        self.override(ASSET + "d-2.flac", artist="Queen",
                      title="Bohemian Rhapsody", year=1975)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["targets_insert"], 2)
        apply_plan(self.conn, plan, True, False)
        self.assertEqual(self.ov_row(u1)["year"], 1975)
        self.assertEqual(self.ov_row(u2)["year"], 1975)

    def test_fills_existing_row_with_null_year(self):
        new = self.track("t1", "Artist", "Song")
        self.override(new, genre="Rock", year=None)   # live row, no year
        self.override(ASSET + "d-3.flac", artist="Artist", title="Song",
                      year=1968)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["targets_fill"], 1)
        apply_plan(self.conn, plan, True, False)
        row = self.ov_row(new)
        self.assertEqual(row["year"], 1968)
        self.assertEqual(row["genre"], "Rock")        # untouched

    def test_never_overwrites_existing_year(self):
        new = self.track("t1", "Artist", "Song")
        self.override(new, year=1970)
        self.override(ASSET + "d-4.flac", artist="Artist", title="Song",
                      year=1980)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["targets_had_year"], 1)
        self.assertEqual(plan["stats"]["targets_insert"], 0)
        apply_plan(self.conn, plan, True, False)
        self.assertEqual(self.ov_row(new)["year"], 1970)
        # Orphan still consumed — the knowledge already exists on target.
        self.assertIsNone(self.ov_row(ASSET + "d-4.flac"))

    def test_norm_matches_curly_apostrophe_and_diacritics(self):
        new = self.track("t1", "Beyoncé", "Don’t Hurt Yourself")
        self.override(ASSET + "d-5.flac", artist="Beyonce",
                      title="Don't  hurt yourself", year=2016)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["targets_insert"], 1)
        apply_plan(self.conn, plan, True, False)
        self.assertEqual(self.ov_row(new)["year"], 2016)

    def test_fanout_cap_skips_junk_keys(self):
        for i in range(_FANOUT_CAP + 1):
            self.track(f"t{i}", "Unknown", "Track")
        self.override(ASSET + "d-6.flac", artist="Unknown", title="Track",
                      year=1990)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["fanout_capped"], 1)
        self.assertEqual(plan["stats"]["targets_insert"], 0)

    def test_missing_key_or_bogus_year_kept_as_no_key(self):
        self.track("t1", "Artist", "Song")
        self.override(ASSET + "d-7.flac", year=1970)              # no key
        self.override(ASSET + "d-8.flac", artist="Artist",
                      title="Song", year=3)                       # bogus year
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["no_key"], 2)
        apply_plan(self.conn, plan, True, False)
        self.assertIsNotNone(self.ov_row(ASSET + "d-7.flac"))     # kept

    def test_no_match_kept_unless_prune_unmatched(self):
        self.override(ASSET + "d-9.flac", artist="Gone Artist",
                      title="Gone Song", year=1970)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["no_match"], 1)
        apply_plan(self.conn, plan, True, False)
        self.assertIsNotNone(self.ov_row(ASSET + "d-9.flac"))
        plan = plan_relink(self.conn)
        apply_plan(self.conn, plan, True, True)     # prune_unmatched
        self.assertIsNone(self.ov_row(ASSET + "d-9.flac"))


class TestNotfoundAndSafety(Base):

    def test_orphan_notfound_pruned_by_default_flag(self):
        self.override(ASSET + "d-10.flac", source="notfound")
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["notfound_pruned"], 1)
        apply_plan(self.conn, plan, True, False)
        self.assertIsNone(self.ov_row(ASSET + "d-10.flac"))

    def test_orphan_notfound_kept_when_flag_off(self):
        self.override(ASSET + "d-11.flac", source="notfound")
        plan = plan_relink(self.conn)
        apply_plan(self.conn, plan, False, False)
        self.assertIsNotNone(self.ov_row(ASSET + "d-11.flac"))

    def test_live_rows_never_touched_by_plan(self):
        new = self.track("t1", "Artist", "Song")
        self.override(new, artist="Live Edit", year=1960)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["orphans"], 0)
        apply_plan(self.conn, plan, True, True)
        row = self.ov_row(new)
        self.assertEqual(row["artist"], "Live Edit")
        self.assertEqual(row["year"], 1960)

    def test_plan_is_pure_no_mutation(self):
        self.track("t1", "Artist", "Song")
        self.override(ASSET + "d-12.flac", artist="Artist", title="Song",
                      year=1970)
        before = self.conn.execute(
            "SELECT COUNT(*) FROM metadata_overrides").fetchone()[0]
        plan_relink(self.conn)
        after = self.conn.execute(
            "SELECT COUNT(*) FROM metadata_overrides").fetchone()[0]
        self.assertEqual(before, after)

    def test_idempotent_second_run_noop(self):
        self.track("t1", "Artist", "Song")
        self.override(ASSET + "d-13.flac", artist="Artist", title="Song",
                      year=1970)
        apply_plan(self.conn, plan_relink(self.conn), True, False)
        plan2 = plan_relink(self.conn)
        s = plan2["stats"]
        self.assertEqual(s["orphans"], 0)
        self.assertEqual(s["transplanted"], 0)

    def test_two_orphans_same_key_first_wins(self):
        new = self.track("t1", "Artist", "Song")
        self.override(ASSET + "d-14a.flac", artist="Artist", title="Song",
                      year=1970)
        self.override(ASSET + "d-14b.flac", artist="Artist", title="Song",
                      year=1971)
        plan = plan_relink(self.conn)
        self.assertEqual(plan["stats"]["targets_insert"], 1)
        apply_plan(self.conn, plan, True, False)
        self.assertIn(self.ov_row(new)["year"], (1970, 1971))
        # Both orphans consumed either way.
        self.assertIsNone(self.ov_row(ASSET + "d-14a.flac"))
        self.assertIsNone(self.ov_row(ASSET + "d-14b.flac"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
