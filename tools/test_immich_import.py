"""Unit tests for tools/immich_import.py — throw-away temp dirs, no
Immich volume, no ffprobe (min-seconds off in tests unless faked)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import immich_import                                     # noqa: E402
from immich_import import (                              # noqa: E402
    Cache, file_hash, iter_source_videos, unique_dest, main)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.source = root / "immich"
        self.dest = root / "gwmovies"
        (self.source / "library" / "admin").mkdir(parents=True)
        (self.source / "upload").mkdir(parents=True)
        (self.source / "encoded-video").mkdir(parents=True)
        self.dest.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def put(self, relpath, content):
        p = self.source / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        return p

    def run_tool(self, *extra):
        return main(["--source", str(self.source), "--dest", str(self.dest),
                     *extra])


class TestDiscovery(_Base):
    def test_videos_found_in_library_and_upload_only(self):
        self.put("library/admin/a.MOV", b"a")
        self.put("upload/b.mp4", b"b")
        self.put("encoded-video/c.mp4", b"c")       # transcode — never
        self.put("library/admin/photo.HEIC", b"p")  # not a video
        found = {p.name for p in iter_source_videos(self.source)}
        self.assertEqual(found, {"a.MOV", "b.mp4"})

    def test_hidden_dirs_skipped(self):
        self.put("library/.hidden/x.mp4", b"x")
        self.assertEqual(list(iter_source_videos(self.source)), [])

    def test_empty_subdirs_walks_source_root(self):
        # --subdirs '' → a plain folder tree (e.g. the PHOTOS-ALL external
        # library), not Immich's library/upload storage layout
        self.put("2019/trip/a.MOV", b"a")
        self.put("b.mp4", b"b")
        found = {p.name for p in iter_source_videos(self.source,
                                                    subdirs=("",))}
        self.assertEqual(found, {"a.MOV", "b.mp4"})

    def test_subdirs_flag_import(self):
        self.put("2019/a.mp4", b"AAAA")
        self.run_tool("--subdirs", "", "--apply")
        self.assertEqual([p.name for p in self.dest.glob("*.mp4")],
                         ["a.mp4"])


class TestImport(_Base):
    def test_dry_run_copies_nothing(self):
        self.put("upload/a.mp4", b"AAAA")
        self.run_tool()
        self.assertEqual(list(self.dest.glob("*.mp4")), [])

    def test_apply_copies_new(self):
        self.put("upload/a.mp4", b"AAAA")
        self.run_tool("--apply")
        self.assertEqual((self.dest / "a.mp4").read_bytes(), b"AAAA")

    def test_content_already_in_dest_not_copied_even_renamed(self):
        (self.dest / "old-name.mp4").write_bytes(b"SAME")
        self.put("upload/new-name.mp4", b"SAME")
        self.run_tool("--apply")
        self.assertFalse((self.dest / "new-name.mp4").exists())

    def test_rerun_is_incremental_noop(self):
        self.put("upload/a.mp4", b"AAAA")
        self.run_tool("--apply")
        mtime = (self.dest / "a.mp4").stat().st_mtime
        self.run_tool("--apply")
        self.assertEqual((self.dest / "a.mp4").stat().st_mtime, mtime)
        self.assertEqual(len(list(self.dest.glob("a*.mp4"))), 1)

    def test_name_collision_different_content_gets_suffix(self):
        (self.dest / "a.mp4").write_bytes(b"OLD")
        self.put("upload/a.mp4", b"NEW")
        self.run_tool("--apply")
        self.assertEqual((self.dest / "a (2).mp4").read_bytes(), b"NEW")
        self.assertEqual((self.dest / "a.mp4").read_bytes(), b"OLD")

    def test_min_seconds_skips_short_clips(self):
        self.put("upload/clip.mov", b"SHORT")
        immich_import.video_duration = lambda p: 2.5
        try:
            self.run_tool("--apply", "--min-seconds", "10")
        finally:
            del immich_import.video_duration  # restore module attr
            import importlib
            importlib.reload(immich_import)
        self.assertFalse((self.dest / "clip.mov").exists())

    def test_limit_stops_early(self):
        for i in range(4):
            self.put(f"upload/v{i}.mp4", f"V{i}".encode())
        self.run_tool("--apply", "--limit", "2")
        copied = list(self.dest.glob("v*.mp4"))
        self.assertEqual(len(copied), 2)


class TestCache(_Base):
    def test_dest_registration_incremental(self):
        f = self.dest / "x.mp4"
        f.write_bytes(b"X")
        c = Cache(self.dest / ".immich-import.db")
        h1 = c.register_dest(f)
        # second call must come from the (path,size,mtime) cache
        orig = immich_import.file_hash
        immich_import.file_hash = lambda p: (_ for _ in ()).throw(
            AssertionError("re-hashed a cached dest file"))
        try:
            h2 = c.register_dest(f)
        finally:
            immich_import.file_hash = orig
        self.assertEqual(h1, h2)

    def test_unique_dest(self):
        (self.dest / "a.mp4").write_bytes(b"1")
        (self.dest / "a (2).mp4").write_bytes(b"2")
        self.assertEqual(unique_dest(self.dest, "a.mp4").name, "a (3).mp4")

    def test_file_hash_stable(self):
        p = self.dest / "h.mp4"
        p.write_bytes(b"CONTENT")
        self.assertEqual(file_hash(p), file_hash(p))


if __name__ == "__main__":
    unittest.main()


class TestRetryShort(_Base):
    def test_retry_short_reevaluates_only_short(self):
        self.put("upload/clip.mov", b"SHORTCLIP")
        self.put("upload/long.mp4", b"LONGVIDEO")
        # first run: 10s cutoff marks clip.mov short, imports long.mp4
        import importlib
        importlib.reload(immich_import)
        immich_import.video_duration = lambda p: (
            2.5 if "clip" in str(p) else 60.0)
        try:
            main2 = immich_import.main
            main2(["--source", str(self.source), "--dest", str(self.dest),
                   "--apply", "--min-seconds", "10"])
            self.assertFalse((self.dest / "clip.mov").exists())
            # second run at 6s WITHOUT --retry-short: clip stays skipped
            immich_import.video_duration = lambda p: 8.0
            main2(["--source", str(self.source), "--dest", str(self.dest),
                   "--apply", "--min-seconds", "6"])
            self.assertFalse((self.dest / "clip.mov").exists())
            # with --retry-short: the 8s clip now imports; long.mp4 is NOT
            # re-copied (it was 'imported', not 'short')
            main2(["--source", str(self.source), "--dest", str(self.dest),
                   "--apply", "--min-seconds", "6", "--retry-short"])
            self.assertTrue((self.dest / "clip.mov").exists())
            self.assertEqual(len(list(self.dest.glob("long*.mp4"))), 1)
        finally:
            importlib.reload(immich_import)
