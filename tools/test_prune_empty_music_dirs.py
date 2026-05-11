#!/usr/bin/env python3
"""
tests/test_prune_empty_music_dirs.py — algorithm-only tests for the
prune script. Builds throw-away directory trees in a tempdir and
asserts which dirs the walker marks for deletion.

The deletion ITSELF (osascript / rmtree) is never invoked: we exercise
_walk() in dry-run mode and inspect Stats.dirs_to_delete. Safe to run
on any machine — no Trash side-effects.

Run standalone:
    python3 -m unittest tools.test_prune_empty_music_dirs -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Allow `python3 -m unittest tools.test_prune_empty_music_dirs` from
# the repo root.
THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import prune_empty_music_dirs as P  # noqa: E402


def _touch(path: Path, content: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestPruneAlgorithm(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="prune-test-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _walk_root(self, *, limit=0, verbose=False):
        stats = P.Stats()
        for child in P._list_subdirs(self.root):
            P._walk(child, ancestor_is_album=False, exts=P.DEFAULT_EXTS,
                    stats=stats, limit=limit, verbose=verbose)
        return stats

    # ── Basic decisions ──────────────────────────────────────────

    def test_album_with_music_kept(self):
        _touch(self.root / "Artist" / "Album" / "track.flac")
        stats = self._walk_root()
        self.assertEqual(stats.dirs_to_delete, [])

    def test_no_music_anywhere_deleted(self):
        """Whole subtree has no music → top-level dir marked. The
        deletion (Trash / rm -rf) recursively removes its children;
        we don't enumerate them separately to avoid noise."""
        _touch(self.root / "Junk" / "readme.txt")
        _touch(self.root / "Junk" / "Sub" / "more.txt")
        stats = self._walk_root()
        names = {p.relative_to(self.root).as_posix()
                 for p in stats.dirs_to_delete}
        self.assertEqual(names, {"Junk"},
                         "Only the top of a music-less subtree should be "
                         "marked — children get removed by the recursive "
                         "Trash/rmtree.")

    def test_empty_dir_deleted(self):
        (self.root / "Empty").mkdir()
        stats = self._walk_root()
        self.assertIn(self.root / "Empty", stats.dirs_to_delete)

    # ── Ancestor-protection rule (core of user's clarification) ──

    def test_album_scans_subdir_kept(self):
        """Artist/Album has music → Artist/Album/scans/ (only JPEGs)
        must be PRESERVED because an ancestor has music."""
        _touch(self.root / "Artist" / "Album" / "track.flac")
        _touch(self.root / "Artist" / "Album" / "scans" / "front.jpg")
        _touch(self.root / "Artist" / "Album" / "scans" / "back.jpg")
        stats = self._walk_root()
        self.assertEqual(stats.dirs_to_delete, [],
                         "scans/ must be preserved when album has music")

    def test_deep_support_dirs_kept(self):
        _touch(self.root / "Artist" / "Album" / "CD1" / "01.flac")
        _touch(self.root / "Artist" / "Album" / "scans" / "booklet" / "p1.jpg")
        stats = self._walk_root()
        self.assertEqual(stats.dirs_to_delete, [])

    # ── Root-music-doesn't-protect rule ──────────────────────────

    def test_loose_track_at_root_does_not_protect_siblings(self):
        """Music files directly in the music root must NOT grant
        ancestor-protection to root's other subdirs."""
        _touch(self.root / "loose_track.flac")
        _touch(self.root / "Junk" / "readme.txt")
        stats = self._walk_root()
        names = {p.relative_to(self.root).as_posix()
                 for p in stats.dirs_to_delete}
        self.assertIn("Junk", names,
                      "Junk/ must still be deleted even with root-level music")

    # ── Descendants-have-music rule ──────────────────────────────

    def test_branch_with_music_at_leaf_kept(self):
        """A non-music intermediate dir whose deeper descendant has
        music must be kept."""
        _touch(self.root / "Branch" / "Sub" / "Deep" / "song.mp3")
        stats = self._walk_root()
        self.assertEqual(stats.dirs_to_delete, [])

    def test_split_branch_preserves_junk_under_album(self):
        """Same parent, two children: one music, one junk. Under the
        subtree-protection rule (Rule B), as soon as Parent's subtree
        is found to contain music, Parent becomes an "album root" and
        all its descendants — including Junk/ — are preserved.

        This is the documented trade-off: the rule cannot tell apart
        a multi-disc album's scans/ subdir (which the user wants kept)
        from a junk subdir next to a music subdir (which they might
        want deleted). The rule errs on the side of preservation;
        users with a Music-vs-Junk split should clean those manually.
        """
        _touch(self.root / "Parent" / "Music" / "song.mp3")
        _touch(self.root / "Parent" / "Junk" / "x.txt")
        stats = self._walk_root()
        names = {p.relative_to(self.root).as_posix()
                 for p in stats.dirs_to_delete}
        self.assertNotIn("Parent",        names)
        self.assertNotIn("Parent/Music",  names)
        self.assertNotIn("Parent/Junk",   names,
                         "Parent's subtree has music → Junk preserved "
                         "(documented trade-off)")

    # ── Symlinks / extensions ────────────────────────────────────

    def test_symlinks_not_followed(self):
        # Create a real music file outside root, symlink it in.
        outside = Path(tempfile.mkdtemp(prefix="prune-outside-"))
        try:
            _touch(outside / "song.flac")
            (self.root / "LinkDir").symlink_to(outside)
            stats = self._walk_root()
            # LinkDir is a symlink, so _list_subdirs(root) skips it
            # entirely (follow_symlinks=False on is_dir).
            # We expect LinkDir to NOT appear as a candidate at all.
            names = {p.name for p in stats.dirs_to_delete}
            self.assertNotIn("LinkDir", names)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_extension_match_is_case_insensitive(self):
        _touch(self.root / "Album" / "Track.FLAC")
        stats = self._walk_root()
        self.assertEqual(stats.dirs_to_delete, [])

    def test_unknown_extension_treated_as_non_music(self):
        _touch(self.root / "Vid" / "movie.mp4")  # mp4 deliberately excluded
        stats = self._walk_root()
        names = {p.name for p in stats.dirs_to_delete}
        self.assertIn("Vid", names)

    # ── Limit ────────────────────────────────────────────────────

    def test_limit_stops_walk(self):
        for i in range(20):
            _touch(self.root / f"d{i:02}" / "x.txt")
        stats = self._walk_root(limit=5)
        self.assertTrue(stats.limit_reached)
        self.assertLessEqual(stats.dirs_visited, 5)

    # ── _has_music_file ──────────────────────────────────────────

    def test_has_music_file_basic(self):
        d = self.root / "x"
        d.mkdir()
        self.assertFalse(P._has_music_file(d, P.DEFAULT_EXTS))
        _touch(d / "song.flac")
        self.assertTrue(P._has_music_file(d, P.DEFAULT_EXTS))

    def test_has_music_file_ignores_subdirs(self):
        # A music file in a SUBDIR of d shouldn't make d itself "have music".
        d = self.root / "x"
        _touch(d / "sub" / "song.flac")
        self.assertFalse(P._has_music_file(d, P.DEFAULT_EXTS))


if __name__ == "__main__":
    unittest.main()
