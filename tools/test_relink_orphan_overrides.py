#!/usr/bin/env python3
"""
tools/test_relink_orphan_overrides.py — unit tests over a throw-away DB.

Run standalone:
    python3 -m unittest tools.test_relink_orphan_overrides -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import relink_orphan_overrides as R  # noqa: E402


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            udn TEXT, obj_id TEXT, url TEXT NOT NULL,
            title TEXT, artist TEXT, album TEXT,
            bit_depth INTEGER, sample_rate INTEGER
        );
        CREATE TABLE metadata_overrides (
            url TEXT PRIMARY KEY, artist TEXT, album TEXT, title TEXT,
            genre TEXT, year INTEGER, updated_at TEXT,
            source TEXT NOT NULL DEFAULT 'manual'
        );
    """)
    return conn


def _add_track(conn, url, artist="A", title="T"):
    conn.execute(
        "INSERT INTO tracks (udn, obj_id, url, title, artist) VALUES (?,?,?,?,?)",
        ("uuid:test", url, url, title, artist))


def _add_override(conn, url, source="acoustid", artist="A", title="T"):
    # The fuzzy-match guard rejects relinks when override and track
    # have disagreeing (artist, title). For the d-id round-trip tests
    # we want the relink to succeed by default, so default both sides
    # to artist='A', title='T'. Tests for the guard itself override
    # these via _set_meta.
    conn.execute(
        "INSERT INTO metadata_overrides (url, source, artist, title) "
        "VALUES (?,?,?,?)",
        (url, source, artist, title))


# ── _d_id parser ──────────────────────────────────────────────────

class TestDId(unittest.TestCase):

    def test_positive(self):
        self.assertEqual(R._d_id("http://x/c2/b16/f44100/d12345-coABC.flac"),
                         "d12345")

    def test_negative(self):
        # Some d-ids are negative integers.
        self.assertEqual(R._d_id("http://x/c2/b16/f44100/d-9876543210-coABC.flac"),
                         "d-9876543210")

    def test_no_match(self):
        for u in ("http://other/foo.mp3", "", None,
                  "http://x/just-some-path"):
            with self.subTest(u=u):
                self.assertIsNone(R._d_id(u))


# ── relink logic ──────────────────────────────────────────────────

class TestRelink(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._path))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._path)

    def test_basic_co_hash_rotation(self):
        # Old URL with co-hash OLD; new URL with same d-id but co-hash NEW.
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.conn.commit()
        self.assertEqual(stats["relinked"], 1)
        # Verify override now references the new URL.
        row = self.conn.execute(
            "SELECT url FROM metadata_overrides").fetchone()
        self.assertEqual(row["url"],
                         "http://x/c2/b16/f44100/d12345-coNEW.flac")

    def test_dry_run_doesnt_mutate(self):
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=False)
        # No commit, no mutation.
        self.assertEqual(stats["relinked"], 1,
                         "dry-run still counts what WOULD relink")
        row = self.conn.execute(
            "SELECT url FROM metadata_overrides").fetchone()
        self.assertEqual(row["url"],
                         "http://x/c2/b16/f44100/d12345-coOLD.flac",
                         "dry-run must not actually write")

    def test_no_match_is_trashed_file(self):
        # Override exists, but no current track has matching d-id.
        _add_override(self.conn, "http://x/c2/b16/f44100/d999-coOLD.flac")
        # No tracks at all
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 0)
        self.assertEqual(stats["no_match"], 1)

    def test_no_d_id_in_url(self):
        # Override URL doesn't contain a d-id pattern at all.
        _add_override(self.conn, "http://other-server/foo.mp3")
        _add_track(self.conn, "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 0)
        self.assertEqual(stats["no_d"], 1)

    def test_idempotent(self):
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        s1 = R.relink(self.conn, apply=True)
        self.conn.commit()
        s2 = R.relink(self.conn, apply=True)
        self.conn.commit()
        self.assertEqual(s1["relinked"], 1)
        self.assertEqual(s2["relinked"], 0,
                         "second run finds no orphans — idempotent")

    def test_ambiguous_d_id_skipped(self):
        # Two bare tracks share the same d-id (shouldn't happen with
        # UNIQUE(udn,url) but defend against it).
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW1.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW2.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        # The orphan cannot be safely relinked to either NEW URL.
        self.assertEqual(stats["relinked"], 0)
        self.assertEqual(stats["ambiguous"], 1)

    def test_two_orphans_with_same_d_id_only_one_gets_new_url(self):
        # If two overrides happen to share a d-id (e.g. user had
        # multiple URL aliases of the same file), only ONE claims the
        # new URL — the other goes to ambiguous.
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD-A.flac")
        _add_override(self.conn, "http://x/c2/b16/f44100/d12345-coOLD-B.flac")
        _add_track(self.conn,    "http://x/c2/b16/f44100/d12345-coNEW.flac")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        # The override / track on both sides have artist=None,
        # title='Track' (test fixtures), so the fuzzy check succeeds
        # for the first claim and the second goes ambiguous.
        self.assertEqual(stats["relinked"], 1)
        self.assertEqual(stats["ambiguous"], 1)


# ── fuzzy-match guard (regression for the d-id collision risk) ────

def _set_meta(conn, table, url, artist, title):
    conn.execute(f"UPDATE {table} SET artist=?, title=? WHERE url=?",
                 (artist, title, url))


class TestFuzzyMismatchGuard(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def test_genuine_collision_blocked(self):
        # Same d-id; two different songs (Kryptonite vs Down Poison).
        # Old override is for Kryptonite. Only Down Poison currently
        # exists as a bare track (1-bare-track case — ambiguity guard
        # alone wouldn't help).
        _add_override(self.conn, "http://x/c2/b16/f44100/d11111-coOLD.mp3")
        _set_meta(self.conn, "metadata_overrides",
                  "http://x/c2/b16/f44100/d11111-coOLD.mp3",
                  "3 Doors Down", "Kryptonite")
        _add_track(self.conn, "http://x/c2/b16/f44100/d11111-coNEW.mp3")
        _set_meta(self.conn, "tracks",
                  "http://x/c2/b16/f44100/d11111-coNEW.mp3",
                  "3 Doors Down", "Down Poison")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 0,
                         "Mismatched (artist,title) MUST block relink")
        self.assertEqual(stats["mismatch"], 1)

    def test_punctuation_only_difference_still_relinks(self):
        # 'Hard Rain's A-Gonna Fall' vs 'Hard Rain's A‐Gonna Fall'
        # (ASCII hyphen vs unicode hyphen). Same song, should relink.
        _add_override(self.conn, "http://x/c2/b16/f44100/d22222-coOLD.mp3")
        _set_meta(self.conn, "metadata_overrides",
                  "http://x/c2/b16/f44100/d22222-coOLD.mp3",
                  "Bob Dylan", "Hard Rain's A-Gonna Fall")
        _add_track(self.conn, "http://x/c2/b16/f44100/d22222-coNEW.mp3")
        _set_meta(self.conn, "tracks",
                  "http://x/c2/b16/f44100/d22222-coNEW.mp3",
                  "Bob Dylan", "Hard Rain’s A‐Gonna Fall")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 1)
        self.assertEqual(stats["mismatch"], 0)

    def test_collaboration_credit_variation_still_relinks(self):
        # "Bill Evans Trio; Stan Getz" vs "Bill Evans Trio feat. Stan Getz"
        _add_override(self.conn, "http://x/c2/b16/f44100/d33333-coOLD.mp3")
        _set_meta(self.conn, "metadata_overrides",
                  "http://x/c2/b16/f44100/d33333-coOLD.mp3",
                  "Bill Evans Trio feat. Stan Getz", "Funkallero")
        _add_track(self.conn, "http://x/c2/b16/f44100/d33333-coNEW.mp3")
        _set_meta(self.conn, "tracks",
                  "http://x/c2/b16/f44100/d33333-coNEW.mp3",
                  "Bill Evans Trio; Stan Getz", "Funkallero")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 1)

    def test_bracketed_qualifier_ignored(self):
        # Override 'Hey You' vs track 'Hey You (Live, Wembley 1990)'
        # Bracketed annotation stripped during normalisation.
        _add_override(self.conn, "http://x/c2/b16/f44100/d44444-coOLD.mp3")
        _set_meta(self.conn, "metadata_overrides",
                  "http://x/c2/b16/f44100/d44444-coOLD.mp3",
                  "Pink Floyd", "Hey You")
        _add_track(self.conn, "http://x/c2/b16/f44100/d44444-coNEW.mp3")
        _set_meta(self.conn, "tracks",
                  "http://x/c2/b16/f44100/d44444-coNEW.mp3",
                  "Pink Floyd", "Hey You (Live, Wembley 1990)")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 1)

    def test_completely_different_song_blocked(self):
        # Same d-id, but the override claims Bob Dylan / Tambourine Man
        # while the track is actually Ottawan / Crazy Music. Block.
        _add_override(self.conn, "http://x/c2/b16/f44100/d55555-coOLD.mp3")
        _set_meta(self.conn, "metadata_overrides",
                  "http://x/c2/b16/f44100/d55555-coOLD.mp3",
                  "Bob Dylan", "Mr. Tambourine Man (Album Version)")
        _add_track(self.conn, "http://x/c2/b16/f44100/d55555-coNEW.mp3")
        _set_meta(self.conn, "tracks",
                  "http://x/c2/b16/f44100/d55555-coNEW.mp3",
                  "Ottawan", "Crazy Music")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 0)
        self.assertEqual(stats["mismatch"], 1)

    def test_empty_metadata_treated_as_mismatch(self):
        # Defensive: if either side has empty artist or empty title,
        # we can't fuzzy-match, so we conservatively skip.
        _add_override(self.conn, "http://x/c2/b16/f44100/d66666-coOLD.mp3",
                      artist="", title="")
        _add_track(self.conn, "http://x/c2/b16/f44100/d66666-coNEW.mp3",
                   artist="Real Artist", title="Real Title")
        self.conn.commit()
        stats = R.relink(self.conn, apply=True)
        self.assertEqual(stats["relinked"], 0)
        self.assertEqual(stats["mismatch"], 1)


if __name__ == "__main__":
    unittest.main()
