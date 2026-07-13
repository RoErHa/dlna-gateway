#!/usr/bin/env python3
"""
tests/test_env_loader.py — dlna_config._load_env_file.

Since the .env consolidation (2026-07-13) .env is THE configuration
file, so it must load reliably: via python-dotenv when present, via the
built-in fallback parser otherwise (the old optional-import silently
skipped loading — the documented ".env caveat" failure mode). These
tests exercise the FALLBACK parser directly (dotenv import blocked).
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_config import _load_env_file


class TestFallbackEnvParser(unittest.TestCase):
    """Run _load_env_file with python-dotenv unavailable."""

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".env")
        os.close(self._fd)
        self._added: list[str] = []

    def tearDown(self):
        os.unlink(self._path)
        for k in self._added:
            os.environ.pop(k, None)

    def _load(self, content: str):
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(content)
        # Track keys so tearDown cleans os.environ.
        for line in content.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                self._added.append(line.partition("=")[0].strip())
        with patch.dict(sys.modules, {"dotenv": None}):
            _load_env_file(self._path)

    def test_basic_key_value(self):
        self._load("TEST_ENVLOADER_A=/Volumes/X/Music\n")
        self.assertEqual(os.environ["TEST_ENVLOADER_A"], "/Volumes/X/Music")

    def test_comments_and_blank_lines_skipped(self):
        self._load("# comment\n\nTEST_ENVLOADER_B=1\n#TEST_ENVLOADER_C=2\n")
        self.assertEqual(os.environ["TEST_ENVLOADER_B"], "1")
        self.assertNotIn("TEST_ENVLOADER_C", os.environ)

    def test_values_with_spaces_and_equals(self):
        self._load("TEST_ENVLOADER_D=DLNA Gateway (IINA)\n"
                   "TEST_ENVLOADER_E=a=b=c\n")
        self.assertEqual(os.environ["TEST_ENVLOADER_D"],
                         "DLNA Gateway (IINA)")
        self.assertEqual(os.environ["TEST_ENVLOADER_E"], "a=b=c")

    def test_quotes_stripped(self):
        self._load("TEST_ENVLOADER_F=\"quoted value\"\n"
                   "TEST_ENVLOADER_G='single'\n")
        self.assertEqual(os.environ["TEST_ENVLOADER_F"], "quoted value")
        self.assertEqual(os.environ["TEST_ENVLOADER_G"], "single")

    def test_existing_environ_wins(self):
        os.environ["TEST_ENVLOADER_H"] = "from-shell"
        self._added.append("TEST_ENVLOADER_H")
        self._load("TEST_ENVLOADER_H=from-file\n")
        self.assertEqual(os.environ["TEST_ENVLOADER_H"], "from-shell")

    def test_missing_file_is_silent(self):
        with patch.dict(sys.modules, {"dotenv": None}):
            _load_env_file("/no/such/.env")   # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
