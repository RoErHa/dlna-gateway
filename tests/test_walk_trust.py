#!/usr/bin/env python3
"""
test_walk_trust.py — a rescan may only call a file GONE when the walk
actually looked (2026-10-10). See dlna_walk.py.

The pure rule first, then the same three failures driven through a real
LocalFsProvider.rescan(), because the bug was never in the rule: it was
that nothing asked.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_walk import trusted_removals                       # noqa: E402
from dlna_library import LibraryDB                           # noqa: E402
from dlna_providers.localfs import LocalFsProvider           # noqa: E402
import dlna_providers.localfs as lfs                         # noqa: E402


def _err(path):
    return OSError(5, "Input/output error", path)


class TestTheRule(unittest.TestCase):
    def test_a_clean_walk_removes_what_it_did_not_see(self):
        self.assertEqual(trusted_removals(["/m/a.flac"], [], True),
                         ["/m/a.flac"])

    def test_a_walk_that_saw_nothing_removes_nothing(self):
        self.assertEqual(trusted_removals(["/m/a.flac"], [], False), [])

    def test_only_removals_under_an_errored_folder_are_held(self):
        got = trusted_removals(["/m/Lost/x.flac", "/m/Gone/y.flac"],
                               [_err("/m/Lost")], True)
        self.assertEqual(got, ["/m/Gone/y.flac"])

    def test_the_prefix_is_a_path_component_not_a_string(self):
        """`/m/Lost` errored; `/m/Lost Tapes` did not — same trap as the
        file server's containment check (§Security posture 8)."""
        got = trusted_removals(["/m/Lost Tapes/x.flac"],
                               [_err("/m/Lost")], True)
        self.assertEqual(got, ["/m/Lost Tapes/x.flac"])

    def test_an_error_naming_no_folder_holds_everything(self):
        self.assertEqual(trusted_removals(["/m/a.flac"],
                                          [OSError("??")], True), [])


class TestTheRescanAsks(unittest.TestCase):
    def setUp(self):
        fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = LibraryDB(db_file=self._p)
        self._tmp = tempfile.TemporaryDirectory(prefix="walk-trust-")
        self.root = Path(self._tmp.name).resolve()
        self.p = LocalFsProvider(self.db, self.root)

    def tearDown(self):
        self._tmp.cleanup()
        os.unlink(self._p)

    def _write(self, rel):
        f = self.root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\0" * 16)
        return f

    def _rescan(self):
        tags = lambda path: {"title": path.stem, "artist": path.parent.name,
                             "album": "A", "duration": "0:03:00",
                             "mime": "audio/flac"}
        with patch.object(lfs, "_require_mutagen"), \
             patch.object(lfs, "_read_tags", side_effect=tags), \
             patch.object(lfs, "_extract_art_hash", return_value=None):
            return self.p.rescan()

    def test_a_folder_that_errors_mid_walk_keeps_its_tracks(self):
        self._write("Keep/a.flac")
        self._write("Lost/b.flac")
        self._rescan()
        real = os.walk
        def flaky(top, **kw):
            for d, dirs, files in real(top, **kw):
                if d.endswith("Lost"):
                    kw["onerror"](_err(d))
                    continue
                yield d, dirs, files
        with patch.object(lfs.os, "walk", flaky):
            st = self._rescan()
        self.assertEqual(st["removed"], 0)
        self.assertEqual(self.db.track_count(self.p.udn), 2)

    def test_a_walk_that_finds_no_audio_keeps_the_library(self):
        """A stale mount point with leftover folders passes probe()."""
        f = self._write("Artist/song.flac")
        self._rescan()
        f.unlink()
        (self.root / "Artist" / "cover.jpg").write_bytes(b"x")
        st = self._rescan()
        self.assertEqual(st["removed"], 0)
        self.assertEqual(self.db.track_count(self.p.udn), 1)

    def test_a_file_that_fails_stat_is_not_deleted(self):
        self._write("Artist/a.flac")
        self._write("Artist/b.flac")
        self._rescan()
        real_stat = Path.stat
        def flaky(self_, *a, **kw):
            if self_.name == "b.flac":
                raise OSError(5, "Input/output error")
            return real_stat(self_, *a, **kw)
        with patch.object(Path, "stat", flaky):
            st = self._rescan()
        self.assertEqual(st["removed"], 0)
        self.assertEqual(self.db.track_count(self.p.udn), 2)

    def test_a_genuinely_deleted_file_is_still_removed(self):
        self._write("Artist/a.flac")
        f = self._write("Artist/b.flac")
        self._rescan()
        f.unlink()
        st = self._rescan()
        self.assertEqual(st["removed"], 1)
        self.assertEqual(self.db.track_count(self.p.udn), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
