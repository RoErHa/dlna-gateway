#!/usr/bin/env python3
"""
tools/test_find_duplicate_audio.py — unit tests over throw-away temp DB
+ tempdir for the duplicate-audio scanner.

The tool's HTTP HEAD is the one piece we don't exercise here — it
needs a real running AssetUPnP. We test the pure / DB / disk-walk
pieces; the HEAD step is independently obvious code-wise.

Run standalone:
    python3 -m unittest tools.test_find_duplicate_audio -v
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import find_duplicate_audio as F  # noqa: E402


def _touch(path: Path, size: int) -> None:
    """Create a file of exact size (zero-filled)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        if size > 0:
            f.seek(size - 1)
            f.write(b"\x00")


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


def _add_track(conn, url, artist, album, title, *,
               bit_depth=16, sample_rate=44100):
    conn.execute(
        "INSERT INTO tracks (udn, obj_id, url, title, artist, album, "
        " bit_depth, sample_rate) VALUES (?,?,?,?,?,?,?,?)",
        ("uuid:test", url, url, title, artist, album,
         bit_depth, sample_rate))


def _add_override(conn, url, artist, album, title, source="acoustid"):
    conn.execute(
        "INSERT INTO metadata_overrides (url, artist, album, title, source) "
        "VALUES (?,?,?,?,?)",
        (url, artist, album, title, source))


# ── find_duplicate_groups ─────────────────────────────────────────

class TestFindGroups(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._path))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._path)

    def test_groups_same_artist_album_title(self):
        # Three files all identified as Tom Petty / Damn Torpedoes / Refugee.
        for u in ("u/a", "u/b", "u/c"):
            _add_track(self.conn, u, "Tom Petty", "Damn Torpedoes", "Refugee")
            _add_override(self.conn, u, "Tom Petty", "Damn Torpedoes", "Refugee")
        # One unrelated file.
        _add_track(self.conn, "u/x", "Other", "Other", "Other")
        _add_override(self.conn, "u/x", "Other", "Other", "Other")
        self.conn.commit()
        groups = F.find_duplicate_groups(self.conn)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]["members"]), 3)

    def test_single_member_excluded(self):
        # Unique recording — never a duplicate.
        _add_track(self.conn, "u/a", "A", "B", "C")
        _add_override(self.conn, "u/a", "A", "B", "C")
        self.conn.commit()
        self.assertEqual(F.find_duplicate_groups(self.conn), [])

    def test_manual_source_excluded(self):
        # Manual edits aren't trusted for dedup decisions.
        for u in ("u/a", "u/b"):
            _add_track(self.conn, u, "A", "B", "C")
            _add_override(self.conn, u, "A", "B", "C", source="manual")
        self.conn.commit()
        self.assertEqual(F.find_duplicate_groups(self.conn), [])

    def test_null_metadata_excluded(self):
        # If artist/album/title NULL in override, the file can't be
        # confidently identified as part of any group.
        for u in ("u/a", "u/b"):
            _add_track(self.conn, u, "raw", "raw", "raw")
            _add_override(self.conn, u, None, None, None, source="notfound")
        self.conn.commit()
        self.assertEqual(F.find_duplicate_groups(self.conn), [])


# ── rank_groups ───────────────────────────────────────────────────

class TestRanking(unittest.TestCase):

    def test_higher_bit_depth_wins(self):
        # Members must have `path` set — rank_groups dedupes by path
        # now (URL aliases collapse to one file).
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/16", "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 10_000_000, "path": Path("/m/16.flac")},
                 {"url": "u/24", "bit_depth": 24, "sample_rate": 44100,
                  "file_size": 5_000_000, "path": Path("/m/24.flac")},
             ]}
        F.rank_groups([g])
        self.assertEqual(g["winner"]["url"], "u/24",
                         "24-bit wins regardless of file size")
        self.assertEqual([m["url"] for m in g["losers"]], ["u/16"])

    def test_higher_sample_rate_wins_within_bit_depth(self):
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/44", "bit_depth": 24, "sample_rate": 44100,
                  "file_size": 5_000_000, "path": Path("/m/44.flac")},
                 {"url": "u/96", "bit_depth": 24, "sample_rate": 96000,
                  "file_size": 4_000_000, "path": Path("/m/96.flac")},
             ]}
        F.rank_groups([g])
        self.assertEqual(g["winner"]["url"], "u/96")

    def test_larger_file_wins_within_quality(self):
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/sm", "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 5_000_000, "path": Path("/m/sm.mp3")},
                 {"url": "u/lg", "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 9_000_000, "path": Path("/m/lg.flac")},
             ]}
        F.rank_groups([g])
        self.assertEqual(g["winner"]["url"], "u/lg")

    def test_alpha_tiebreaker(self):
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/zzz", "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 1000, "path": Path("/m/zzz.flac")},
                 {"url": "u/aaa", "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 1000, "path": Path("/m/aaa.flac")},
             ]}
        F.rank_groups([g])
        self.assertEqual(g["winner"]["url"], "u/aaa")

    def test_null_quality_counts_as_zero(self):
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/null",  "bit_depth": None, "sample_rate": None,
                  "file_size": 9_000_000, "path": Path("/m/null.flac")},
                 {"url": "u/16",    "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 1_000_000, "path": Path("/m/16.flac")},
             ]}
        F.rank_groups([g])
        self.assertEqual(g["winner"]["url"], "u/16",
                         "any non-NULL quality beats NULL")

    def test_url_aliases_collapse_to_one(self):
        """Critical safety test: AssetUPnP serves the same file via
        multiple URLs (Artist / Album / Genre browse paths). All those
        URLs end up in the same duplicate group, but they're the SAME
        physical file. Without path-dedup, we'd incorrectly mark the
        winner's file as both KEEP and TRASH — the trash step would
        kill the only copy.

        rank_groups must collapse URL aliases pointing to the same path
        into ONE representative."""
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 # Three URLs all served from the same file on disk.
                 {"url": "u/via-artist",  "bit_depth": 16,
                  "sample_rate": 44100, "file_size": 5_000_000,
                  "path": Path("/m/the-one-file.mp3")},
                 {"url": "u/via-album",   "bit_depth": 16,
                  "sample_rate": 44100, "file_size": 5_000_000,
                  "path": Path("/m/the-one-file.mp3")},
                 {"url": "u/via-genre",   "bit_depth": 16,
                  "sample_rate": 44100, "file_size": 5_000_000,
                  "path": Path("/m/the-one-file.mp3")},
             ]}
        F.rank_groups([g])
        # All three URLs collapse to ONE entry — no losers, no trash.
        self.assertIsNotNone(g["winner"])
        self.assertEqual(g["losers"], [],
                         "URL-aliased members must NOT become losers")
        self.assertEqual(sorted(g["winner"]["_aliased_urls"]),
                         ["u/via-album", "u/via-artist", "u/via-genre"])

    def test_aliased_winner_plus_real_loser(self):
        # Winner served via 2 URL aliases (same file). Plus one REAL
        # duplicate file at lower quality.
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/24-a", "bit_depth": 24, "sample_rate": 96000,
                  "file_size": 9_000_000, "path": Path("/m/24.flac")},
                 {"url": "u/24-b", "bit_depth": 24, "sample_rate": 96000,
                  "file_size": 9_000_000, "path": Path("/m/24.flac")},
                 {"url": "u/16",   "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 5_000_000, "path": Path("/m/16.mp3")},
             ]}
        F.rank_groups([g])
        self.assertEqual(str(g["winner"]["path"]), "/m/24.flac")
        self.assertEqual(len(g["losers"]), 1,
                         "only ONE physical loser despite 3 member URLs")
        self.assertEqual(str(g["losers"][0]["path"]), "/m/16.mp3")
        self.assertEqual(sorted(g["winner"]["_aliased_urls"]),
                         ["u/24-a", "u/24-b"])

    def test_unresolved_members_segregated(self):
        # Members without path go into `unresolved`, not losers.
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/24", "bit_depth": 24, "sample_rate": 96000,
                  "file_size": 9_000_000, "path": Path("/m/24.flac")},
                 {"url": "u/?",  "bit_depth": 16, "sample_rate": 44100,
                  "file_size": None, "path": None},
             ]}
        F.rank_groups([g])
        self.assertEqual(g["winner"]["url"], "u/24")
        self.assertEqual(g["losers"], [])
        self.assertEqual(len(g["unresolved"]), 1)
        self.assertEqual(g["unresolved"][0]["url"], "u/?")

    # ── Size-tolerance filter ──

    def test_size_ratio_within_tolerance_stays_loser(self):
        # winner 10 MB, loser 9 MB → ratio 0.9 ≥ 0.3 default → TRASH.
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/big",   "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 10_000_000, "path": Path("/m/big.mp3")},
                 {"url": "u/close", "bit_depth": 16, "sample_rate": 44100,
                  "file_size":  9_000_000, "path": Path("/m/close.mp3")},
             ]}
        F.rank_groups([g])
        self.assertEqual(len(g["losers"]), 1)
        self.assertEqual(g["review"], [])

    def test_size_ratio_below_tolerance_goes_to_review(self):
        # winner 30 MB, loser 1 MB → ratio 0.033 < 0.3 → REVIEW
        # Likely AcoustID false match.
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/big",   "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 30_000_000, "path": Path("/m/big.flac")},
                 {"url": "u/tiny",  "bit_depth": 16, "sample_rate": 44100,
                  "file_size":  1_000_000, "path": Path("/m/tiny.mp3")},
             ]}
        F.rank_groups([g])
        self.assertEqual(g["losers"], [],
                         "tiny loser must NOT be a trash candidate")
        self.assertEqual(len(g["review"]), 1)
        self.assertAlmostEqual(g["review"][0]["_size_ratio"], 0.033, places=2)

    def test_tolerance_zero_disables_filter(self):
        # With tolerance=0, even a 1% loser goes to TRASH.
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/big", "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 100_000_000, "path": Path("/m/big.flac")},
                 {"url": "u/tiny","bit_depth": 16, "sample_rate": 44100,
                  "file_size":     500_000, "path": Path("/m/tiny.mp3")},
             ]}
        F.rank_groups([g], size_tolerance=0)
        self.assertEqual(len(g["losers"]), 1)
        self.assertEqual(g["review"], [])

    def test_tolerance_custom_threshold(self):
        # Tolerance 0.5 — ratio 0.4 goes to review.
        g = {"artist": "A", "album": "B", "title": "C",
             "members": [
                 {"url": "u/w", "bit_depth": 16, "sample_rate": 44100,
                  "file_size": 10_000_000, "path": Path("/m/w.flac")},
                 {"url": "u/l", "bit_depth": 16, "sample_rate": 44100,
                  "file_size":  4_000_000, "path": Path("/m/l.mp3")},
             ]}
        F.rank_groups([g], size_tolerance=0.5)
        self.assertEqual(g["losers"], [])
        self.assertEqual(len(g["review"]), 1)
        # Same data with tolerance 0.3 → loser.
        g2 = {"artist": "A", "album": "B", "title": "C",
              "members": [
                  {"url": "u/w", "bit_depth": 16, "sample_rate": 44100,
                   "file_size": 10_000_000, "path": Path("/m/w.flac")},
                  {"url": "u/l", "bit_depth": 16, "sample_rate": 44100,
                   "file_size":  4_000_000, "path": Path("/m/l.mp3")},
              ]}
        F.rank_groups([g2], size_tolerance=0.3)
        self.assertEqual(len(g2["losers"]), 1)
        self.assertEqual(g2["review"], [])


# ── build_disk_size_index ─────────────────────────────────────────

class TestDiskWalk(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dup-disk-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_indexes_by_size(self):
        _touch(self.root / "a.flac", 1000)
        _touch(self.root / "b.flac", 2000)
        _touch(self.root / "c.flac", 1000)
        idx = F.build_disk_size_index(self.root)
        self.assertEqual(sorted(p.name for p in idx[1000]), ["a.flac", "c.flac"])
        self.assertEqual([p.name for p in idx[2000]], ["b.flac"])

    def test_skips_non_audio(self):
        _touch(self.root / "song.flac", 1000)
        _touch(self.root / "readme.txt", 1000)
        _touch(self.root / "cover.jpg", 5000)
        idx = F.build_disk_size_index(self.root)
        # Only the .flac registers
        self.assertEqual(idx[1000], [self.root / "song.flac"])
        self.assertEqual(idx[5000], [])

    def test_zero_size_skipped(self):
        # Pathological zero-byte file shouldn't pollute the index.
        _touch(self.root / "empty.flac", 0)
        _touch(self.root / "real.flac", 100)
        idx = F.build_disk_size_index(self.root)
        self.assertEqual(idx[0], [])
        self.assertEqual(idx[100], [self.root / "real.flac"])

    def test_symlinks_not_followed(self):
        outside = Path(tempfile.mkdtemp(prefix="dup-outside-"))
        try:
            _touch(outside / "ghost.flac", 1234)
            (self.root / "linkdir").symlink_to(outside)
            idx = F.build_disk_size_index(self.root)
            self.assertEqual(idx[1234], [])
        finally:
            shutil.rmtree(outside, ignore_errors=True)


# ── resolve_paths ─────────────────────────────────────────────────

class TestResolvePaths(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dup-resolve-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_unique_size_match(self):
        # Order: resolve_paths first, then rank_groups (rank consumes path).
        _touch(self.root / "winner.flac", 9_000_000)
        _touch(self.root / "loser.flac",  5_000_000)
        idx = F.build_disk_size_index(self.root)
        groups = [{
            "artist": "A", "album": "B", "title": "C",
            "members": [
                {"url": "u/w", "bit_depth": 24, "sample_rate": 96000,
                 "file_size": 9_000_000},
                {"url": "u/l", "bit_depth": 16, "sample_rate": 44100,
                 "file_size": 5_000_000},
            ],
        }]
        stats = F.resolve_paths(groups, idx)
        F.rank_groups(groups)
        self.assertEqual(stats["resolved"], 2)
        self.assertEqual(groups[0]["winner"]["path"].name, "winner.flac")
        self.assertEqual(groups[0]["losers"][0]["path"].name, "loser.flac")

    def test_ambiguous_size_skipped(self):
        _touch(self.root / "a.flac", 1000)
        _touch(self.root / "b.flac", 1000)
        idx = F.build_disk_size_index(self.root)
        groups = [{
            "artist": "A", "album": "B", "title": "C",
            "members": [
                {"url": "u/1", "bit_depth": 24, "sample_rate": 96000,
                 "file_size": 1000},
                {"url": "u/2", "bit_depth": 16, "sample_rate": 44100,
                 "file_size": 1000},
            ],
        }]
        stats = F.resolve_paths(groups, idx)
        F.rank_groups(groups)
        self.assertEqual(stats["ambiguous"], 2)
        # Both members unresolved → no winner, no losers, everything in
        # unresolved. (No action possible.)
        self.assertIsNone(groups[0]["winner"])
        self.assertEqual(groups[0]["losers"], [])
        self.assertEqual(len(groups[0]["unresolved"]), 2)

    def test_missing_size_reported(self):
        groups = [{
            "artist": "A", "album": "B", "title": "C",
            "members": [
                {"url": "u/x", "bit_depth": 24, "sample_rate": 96000,
                 "file_size": None},
                {"url": "u/y", "bit_depth": 16, "sample_rate": 44100,
                 "file_size": None},
            ],
        }]
        stats = F.resolve_paths(groups, {})
        F.rank_groups(groups)
        self.assertEqual(stats["missing"], 2)
        self.assertIsNone(groups[0]["winner"])
        self.assertEqual(len(groups[0]["unresolved"]), 2)


# ── Report shape ──────────────────────────────────────────────────

class TestReport(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dup-report-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_report_has_keep_and_trash(self):
        groups = [{
            "artist": "Tom Petty", "album": "Damn Torpedoes", "title": "Refugee",
            "members": [
                {"url": "u/24", "bit_depth": 24, "sample_rate": 96000,
                 "file_size": 9_000_000, "path": Path("/m/24.flac")},
                {"url": "u/16", "bit_depth": 16, "sample_rate": 44100,
                 "file_size": 5_000_000, "path": Path("/m/16.flac")},
            ],
        }]
        F.rank_groups(groups)
        out = self.tmp / "report.txt"
        counts = F.write_report(groups, out)
        self.assertEqual(counts, {"trash": 1, "review": 0})
        text = out.read_text(encoding="utf-8")
        self.assertIn("KEEP", text)
        self.assertIn("TRASH", text)
        self.assertIn("Tom Petty", text)
        self.assertIn("/m/24.flac", text)
        self.assertIn("/m/16.flac", text)


if __name__ == "__main__":
    unittest.main()
