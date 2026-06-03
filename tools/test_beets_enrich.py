#!/usr/bin/env python3
"""Unit tests for tools/beets_enrich.py — pure helpers only (never invokes
the external `beet` binary, never touches the network or the library).

    python3 -m unittest tools.test_beets_enrich -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import beets_enrich as be


class TestDefaultConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = be.default_config_yaml("/Volumes/SAMDATA/Music")

    def test_in_place_invariant_present(self):
        self.assertRegex(self.cfg, r"write:\s*yes")
        self.assertRegex(self.cfg, r"copy:\s*no")
        self.assertRegex(self.cfg, r"move:\s*no")

    def test_music_root_and_separate_beets_library(self):
        self.assertIn("/Volumes/SAMDATA/Music", self.cfg)
        # beets' own db, NOT the gateway's library.db
        self.assertIn(".config/beets/library.db", self.cfg)
        self.assertNotIn("dlna-gateway/library.db", self.cfg)

    def test_no_scrub_plugin(self):
        # §3: scrub strips existing tags — must never be in the ENABLED
        # plugins line (a comment warning about it is fine).
        plugins_line = next(l for l in self.cfg.splitlines()
                            if l.strip().startswith("plugins:"))
        self.assertNotIn("scrub", plugins_line)

    def test_has_chroma_and_original_year_bias(self):
        self.assertIn("chroma", self.cfg)
        self.assertRegex(self.cfg, r"original_year:\s*yes")
        # lowered from 0.90 → 0.80 (0.90 skipped this library wholesale)
        self.assertRegex(self.cfg, r"strong_rec_thresh:\s*0\.80")

    def test_library_path_parses_to_beets_own_db(self):
        lib = be.parse_library_path(self.cfg)
        self.assertTrue(lib.endswith(".config/beets/library.db"), lib)

    def test_generated_config_passes_its_own_safety_gate(self):
        ok, problems = be.verify_inplace(self.cfg)
        self.assertTrue(ok, problems)

    def test_config_does_not_force_timid(self):
        # baking timid:yes in would make --quiet ('-q') fail in beets
        self.assertRegex(self.cfg, r"timid:\s*no")
        self.assertFalse(be.config_forces_timid(self.cfg))

    def test_config_enables_musicbrainz_plugin(self):
        # beets 2.x: without it the importer has no metadata source
        self.assertTrue(be.config_has_musicbrainz_plugin(self.cfg))


class TestMusicbrainzGuards(unittest.TestCase):
    def test_plugin_detected_only_on_plugins_line(self):
        self.assertTrue(be.config_has_musicbrainz_plugin(
            "plugins: musicbrainz chroma fetchart\n"))
        self.assertFalse(be.config_has_musicbrainz_plugin(
            "plugins: chroma fetchart embedart\n"))
        # a substring elsewhere must not count
        self.assertFalse(be.config_has_musicbrainz_plugin(
            "plugins: chroma\n# musicbrainz is great\n"))

    def test_beet_python_reads_shebang(self):
        import os
        import tempfile
        d = tempfile.mkdtemp()
        script = os.path.join(d, "beet")
        with open(script, "w") as f:
            f.write(f"#!{sys.executable}\nprint('hi')\n")
        self.assertEqual(be.beet_python(script), sys.executable)

    def test_beet_python_none_without_shebang(self):
        import os
        import tempfile
        script = os.path.join(tempfile.mkdtemp(), "beet")
        with open(script, "w") as f:
            f.write("not a shebang\n")
        self.assertIsNone(be.beet_python(script))

    def test_module_importable(self):
        self.assertTrue(be.module_importable(sys.executable, "os"))
        self.assertFalse(be.module_importable(
            sys.executable, "no_such_module_xyzzy"))


class TestConfigForcesTimid(unittest.TestCase):
    def test_yes_detected(self):
        self.assertTrue(be.config_forces_timid("import:\n  timid: yes\n"))
        self.assertTrue(be.config_forces_timid("import:\n  timid: true\n"))

    def test_no_or_absent(self):
        self.assertFalse(be.config_forces_timid("import:\n  timid: no\n"))
        self.assertFalse(be.config_forces_timid("import:\n  write: yes\n"))


class TestVerifyInplace(unittest.TestCase):
    def test_good(self):
        ok, problems = be.verify_inplace(
            "import:\n  write: yes\n  copy: no\n  move: no\n")
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_copy_yes_flagged(self):
        ok, problems = be.verify_inplace(
            "import:\n  write: yes\n  copy: yes\n  move: no\n")
        self.assertFalse(ok)
        self.assertTrue(any("copy" in p for p in problems))

    def test_move_yes_flagged(self):
        ok, problems = be.verify_inplace(
            "import:\n  write: yes\n  copy: no\n  move: yes\n")
        self.assertFalse(ok)
        self.assertTrue(any("move" in p for p in problems))

    def test_write_no_flagged(self):
        ok, problems = be.verify_inplace(
            "import:\n  write: no\n  copy: no\n  move: no\n")
        self.assertFalse(ok)
        self.assertTrue(any("write" in p for p in problems))

    def test_missing_keys_are_not_assumed_safe(self):
        # absent copy/move must NOT be treated as a safe default
        ok, problems = be.verify_inplace("import:\n  write: yes\n")
        self.assertFalse(ok)
        self.assertEqual(len(problems), 2)   # copy + move

    def test_true_false_accepted_as_bools(self):
        ok, _ = be.verify_inplace(
            "import:\n  write: true\n  copy: false\n  move: false\n")
        self.assertTrue(ok)


class TestBuildImportCmd(unittest.TestCase):
    def test_default_interactive(self):
        self.assertEqual(be.build_import_cmd("beet", "/m"),
                         ["beet", "import", "/m"])

    def test_quiet_bulk(self):
        self.assertEqual(be.build_import_cmd("beet", "/m", quiet=True),
                         ["beet", "import", "-q", "/m"])

    def test_timid(self):
        self.assertEqual(be.build_import_cmd("beet", "/m", timid=True),
                         ["beet", "import", "--timid", "/m"])

    def test_revisit_noincremental(self):
        self.assertEqual(be.build_import_cmd("beet", "/album", revisit=True),
                         ["beet", "import", "-I", "/album"])

    def test_quiet_and_revisit_combined(self):
        self.assertEqual(
            be.build_import_cmd("beet", "/m", quiet=True, revisit=True),
            ["beet", "import", "-q", "-I", "/m"])


class TestPickLocalfsUdn(unittest.TestCase):
    def test_override_wins(self):
        udn, err = be.pick_localfs_udn(
            [{"udn": "uuid:localfs-1"}], override="uuid:other")
        self.assertEqual(udn, "uuid:other")
        self.assertIsNone(err)

    def test_prefers_localfs(self):
        udn, err = be.pick_localfs_udn(
            [{"udn": "uuid:upnp-x"}, {"udn": "uuid:localfs-abc"}])
        self.assertEqual(udn, "uuid:localfs-abc")
        self.assertIsNone(err)

    def test_sole_server(self):
        udn, err = be.pick_localfs_udn([{"udn": "uuid:only"}])
        self.assertEqual(udn, "uuid:only")
        self.assertIsNone(err)

    def test_ambiguous_errors(self):
        udn, err = be.pick_localfs_udn(
            [{"udn": "uuid:a"}, {"udn": "uuid:b"}])
        self.assertIsNone(udn)
        self.assertIn("multiple", err)

    def test_no_servers_errors(self):
        udn, err = be.pick_localfs_udn([])
        self.assertIsNone(udn)
        self.assertIn("no servers", err)


class TestFindBinary(unittest.TestCase):
    def test_missing_returns_none(self):
        self.assertIsNone(
            be.find_binary("definitely-not-a-real-binary-xyz", ()))

    def test_finds_python_via_fallback(self):
        # use the running interpreter as a guaranteed-existing executable
        self.assertEqual(
            be.find_binary("definitely-not-a-real-binary-xyz",
                           (sys.executable,)),
            sys.executable)


class TestImportSummary(unittest.TestCase):
    def test_counts_on_temp_libraries(self):
        import sqlite3
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "library.db")
        con = sqlite3.connect(p)
        con.execute("CREATE TABLE items (id INTEGER)")
        con.execute("CREATE TABLE albums (id INTEGER)")
        con.executemany("INSERT INTO items VALUES (?)", [(1,), (2,), (3,)])
        con.execute("INSERT INTO albums VALUES (1)")
        con.commit()
        con.close()
        self.assertEqual(be.beets_lib_counts(p), (3, 1))

    def test_counts_missing_db_is_zero(self):
        self.assertEqual(be.beets_lib_counts("/no/such/library.db"), (0, 0))

    def test_taghistory_count_from_pickle(self):
        import pickle
        import tempfile
        p = os.path.join(tempfile.mkdtemp(), "state.pickle")
        with open(p, "wb") as f:
            pickle.dump({"taghistory": {(b"/a",), (b"/b",)},
                         "tagprogress": {}}, f)
        self.assertEqual(be.taghistory_count(p), 2)

    def test_taghistory_missing_is_zero(self):
        self.assertEqual(be.taghistory_count("/no/such/state.pickle"), 0)

    def test_summary_reports_imported(self):
        s = be.format_import_summary((10, 2), (40, 5), 100, 108)
        self.assertIn("3 album(s), 30 track(s)", s)
        self.assertIn("8", s)               # dirs processed

    def test_summary_flags_did_nothing_run(self):
        # 0 imported but dirs processed → the loud "all skipped" note
        s = be.format_import_summary((0, 0), (0, 0), 0, 2008)
        self.assertIn("all 2008 skipped", s)
        self.assertIn("strong_rec_thresh", s)
        self.assertIn("--revisit", s)


if __name__ == "__main__":
    unittest.main()
