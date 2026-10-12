#!/usr/bin/env python3
"""tools/test_book_language.py — the language tool's pure helpers and its
dry-run/apply contract. No real library, no audio files.

    python3 -m unittest tools.test_book_language -v
"""
import contextlib
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools import book_language as tool
from dlna_library import LibraryDB

UDN = "uuid:localfs-tool-test"


class TestHelpers(unittest.TestCase):

    def test_value_to_text(self):
        class Frame:                       # an ID3 frame carries .text
            text = ["eng"]
        self.assertEqual(tool.value_to_text(Frame()), "eng")
        self.assertEqual(tool.value_to_text([b"English\x00"]), "English")
        self.assertEqual(tool.value_to_text(["nld"]), "nld")
        self.assertEqual(tool.value_to_text([]), "")

    def test_select(self):
        langs = {"Top 100/Dune": {"language": ""},
                 "Top 100/Hyperion": {"language": "en"},
                 "Gene Wolfe/Lake": {"language": ""}}
        self.assertEqual(tool.select(langs, "top 100", False),
                         ["Top 100/Dune", "Top 100/Hyperion"])
        self.assertEqual(tool.select(langs, "top 100", True), ["Top 100/Dune"])
        self.assertEqual(tool.select(langs, "", True),
                         ["Gene Wolfe/Lake", "Top 100/Dune"])


class TestMain(unittest.TestCase):

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = LibraryDB(db_file=self.path)
        self.db.upsert_tracks(UDN, [{
            "id": "d1", "url": "http://x/dune/1", "title": "01",
            "artist": "Frank Herbert", "album": "Dune", "duration": "1:00:00",
            "mime": "audio/mpeg", "file_path": "/b/Dune/1.mp3",
            "album_key": "Top 100/Dune"}])

    def tearDown(self):
        os.unlink(self.path)

    def _run(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            tool.main([*args, "--db", self.path, "--udn", UDN])
        return out.getvalue()

    def _stored(self):
        return self.db.book_languages(UDN)["Top 100/Dune"]

    def test_set_is_a_dry_run_by_default(self):
        self.assertIn("Would set 1", self._run("--set", "en", "--match", "dune"))
        self.assertEqual(self._stored()["source"], "detected")

    def test_set_apply_writes_manual(self):
        self._run("--set", "English", "--match", "dune", "--apply")
        self.assertEqual(self._stored(), {"language": "en", "source": "manual"})

    def test_clear_apply(self):
        self._run("--set", "en", "--match", "dune", "--apply")
        self._run("--clear", "--match", "dune", "--apply")
        self.assertEqual(self._stored()["source"], "detected")

    def test_set_needs_match_and_a_real_language(self):
        for argv in (["--set", "en"], ["--set", "XXX", "--match", "dune"]):
            with self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                self._run(*argv)


if __name__ == "__main__":
    unittest.main()
