"""Unit tests for tools/immich_import.py — throw-away temp dirs, no
Immich volume, no ffprobe (min-seconds off in tests unless faked)."""
import contextlib
import io
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
        self._env = os.environ.get("LOCALFS_VIDEO_LINK_ROOTS")
        os.environ["LOCALFS_VIDEO_LINK_ROOTS"] = str(self.source)

    def tearDown(self):
        if self._env is None:
            os.environ.pop("LOCALFS_VIDEO_LINK_ROOTS", None)
        else:
            os.environ["LOCALFS_VIDEO_LINK_ROOTS"] = self._env
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

    def test_deleted_dest_content_stays_skipped_by_default(self):
        # "seen once, never re-copy": deleting a clip from GWMovies must
        # not make the next run bring it back
        (self.dest / "old.mp4").write_bytes(b"GONE")
        self.run_tool("--apply")                 # registers old.mp4
        (self.dest / "old.mp4").unlink()
        self.put("upload/copy.mp4", b"GONE")
        self.run_tool("--apply")
        self.assertFalse((self.dest / "copy.mp4").exists())

    def test_reimport_deleted_brings_content_back(self):
        (self.dest / "old.mp4").write_bytes(b"GONE")
        self.put("upload/copy.mp4", b"GONE")
        self.run_tool("--apply")                 # dup → skipped, recorded
        self.assertFalse((self.dest / "copy.mp4").exists())
        (self.dest / "old.mp4").unlink()
        self.run_tool("--apply", "--reimport-deleted")
        self.assertEqual((self.dest / "copy.mp4").read_bytes(), b"GONE")

    def test_reimport_deleted_does_not_recopy_present_content(self):
        (self.dest / "keep.mp4").write_bytes(b"KEEP")
        self.put("upload/twin.mp4", b"KEEP")
        self.run_tool("--apply", "--reimport-deleted")
        self.assertFalse((self.dest / "twin.mp4").exists())

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


class TestLinksInsteadOfCopies(_Base):
    def test_a_new_video_becomes_a_link_to_the_original(self):
        src = self.put("library/admin/a.mov", b"VIDEO")
        self.assertEqual(self.run_tool("--apply"), 0)
        out = self.dest / "a.mov"
        self.assertTrue(out.is_symlink())
        self.assertEqual(Path(os.readlink(out)), src.resolve())
        self.assertEqual(out.read_bytes(), b"VIDEO")

    def test_preview_creates_nothing(self):
        self.put("upload/a.mov", b"VIDEO")
        self.run_tool()
        self.assertFalse(os.path.lexists(self.dest / "a.mov"))

    def test_a_rerun_links_nothing_new(self):
        self.put("upload/a.mov", b"VIDEO")
        self.run_tool("--apply")
        self.run_tool("--apply")
        self.assertEqual(len(list(self.dest.glob("*.mov"))), 1)

    def test_an_existing_real_copy_is_not_linked_again(self):
        (self.dest / "mine.mov").write_bytes(b"VIDEO")
        self.put("upload/a.mov", b"VIDEO")
        self.run_tool("--apply")
        self.assertFalse(os.path.lexists(self.dest / "a.mov"))
        self.assertFalse((self.dest / "mine.mov").is_symlink())

    def test_the_same_video_twice_in_the_source_is_linked_once(self):
        self.put("library/a.mov", b"VIDEO")
        self.put("upload/b.mov", b"VIDEO")
        self.run_tool("--apply")
        self.assertEqual(len(list(self.dest.glob("*.mov"))), 1)

    def test_an_existing_link_counts_as_the_video_it_leads_to(self):
        src = self.put("library/a.mov", b"VIDEO")
        os.symlink(src, self.dest / "older-name.mov")
        self.put("upload/b.mov", b"VIDEO")
        self.run_tool("--apply")
        self.assertFalse(os.path.lexists(self.dest / "a.mov"))
        self.assertFalse(os.path.lexists(self.dest / "b.mov"))

    def test_deleting_a_link_keeps_the_video_gone(self):
        self.put("upload/a.mov", b"VIDEO")
        self.run_tool("--apply")
        (self.dest / "a.mov").unlink()
        self.run_tool("--apply")
        self.assertFalse(os.path.lexists(self.dest / "a.mov"))

    def test_a_folder_the_file_server_would_refuse_stops_the_run(self):
        os.environ["LOCALFS_VIDEO_LINK_ROOTS"] = "/somewhere/else"
        self.put("upload/a.mov", b"VIDEO")
        self.assertEqual(self.run_tool("--apply"), 2)
        self.assertFalse(os.path.lexists(self.dest / "a.mov"))

    def test_a_folder_starting_with_a_listed_name_is_not_listed(self):
        os.environ["LOCALFS_VIDEO_LINK_ROOTS"] = str(self.source) + "-other"
        self.put("upload/a.mov", b"VIDEO")
        self.assertEqual(self.run_tool("--apply"), 2)

    def test_a_missing_library_stops_the_run_before_any_repair(self):
        src = self.put("upload/a.mov", b"VIDEO")
        self.run_tool("--apply")
        os.environ["LOCALFS_VIDEO_LINK_ROOTS"] = str(self.source)
        moved = self.source.with_name("away")
        self.source.rename(moved)
        self.assertEqual(self.run_tool("--apply"), 2)
        self.assertTrue((self.dest / "a.mov").is_symlink())
        self.assertFalse(src.exists())

    def test_default_sources_prefer_external_libraries(self):
        names = [Path(r).name for r, _ in immich_import.DEFAULT_SOURCES]
        self.assertEqual(names, ["PHOTOS-ALL", "PHOTOS-Mar", "IMMICH-UPLOAD"])

    def test_clips_under_the_default_cutoff_are_skipped(self):
        self.put("upload/live.mov", b"CLIP")
        self.put("upload/real.mov", b"REAL")
        real = immich_import.video_duration
        immich_import.video_duration = lambda p: 2.7 if "live" in str(p) else 60.0
        try:
            self.run_tool("--apply")
        finally:
            immich_import.video_duration = real
        self.assertFalse(os.path.lexists(self.dest / "live.mov"))
        self.assertTrue((self.dest / "real.mov").is_symlink())


class TestMovedFilesAreRepointed(_Base):
    def test_the_same_link_follows_a_file_immich_moved(self):
        src = self.put("library/2024/a.mov", b"VIDEO")
        self.run_tool("--apply")
        new = self.source / "library/2018/a.mov"
        new.parent.mkdir(parents=True)
        src.rename(new)
        self.assertFalse((self.dest / "a.mov").exists())
        self.run_tool("--apply")
        self.assertEqual(Path(os.readlink(self.dest / "a.mov")), new.resolve())
        self.assertEqual((self.dest / "a.mov").read_bytes(), b"VIDEO")
        self.assertEqual(len(list(self.dest.glob("*.mov"))), 1)

    def test_preview_reports_but_does_not_repoint(self):
        src = self.put("library/2024/a.mov", b"VIDEO")
        self.run_tool("--apply")
        before = os.readlink(self.dest / "a.mov")
        src.rename(self.source / "library/a.mov")
        self.run_tool()
        self.assertEqual(os.readlink(self.dest / "a.mov"), before)

    def test_a_link_repointed_to_a_copy_already_known_as_duplicate(self):
        src = self.put("library/a.mov", b"VIDEO")
        twin = self.put("upload/twin.mov", b"VIDEO")
        self.run_tool("--apply")
        src.unlink()
        self.run_tool("--apply")
        self.assertEqual(Path(os.readlink(self.dest / "a.mov")), twin.resolve())

    def test_a_file_really_gone_leaves_the_link_and_says_so(self):
        src = self.put("library/a.mov", b"VIDEO")
        self.run_tool("--apply")
        src.unlink()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.run_tool("--apply")
        self.assertIn("lead nowhere", buf.getvalue())
        self.assertTrue((self.dest / "a.mov").is_symlink())

    def test_a_broken_link_keeps_its_name_from_a_new_video(self):
        src = self.put("library/a.mov", b"OLD")
        self.run_tool("--apply")
        src.unlink()
        self.put("upload/a.mov", b"DIFFERENT")
        self.run_tool("--apply")
        self.assertEqual((self.dest / "a (2).mov").read_bytes(), b"DIFFERENT")


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
