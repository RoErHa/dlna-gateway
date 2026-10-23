#!/usr/bin/env python3
"""
tests/test_versions.py — the README's version strip tells the truth.

`tools/versions.py` renders it. This gates the PINNED half — versions taken
from requirements.lock, requirements-dev.lock and the vendored hls.js, which
are the same on every clone. The PLATFORM half (python, sqlite, openssl,
node, ffmpeg) belongs to the machine and is watched by tools/upgrade.py
instead; gating it here would turn every clone with another Python red.

    python3 -m unittest tests.test_versions -v
"""
import os
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tools import versions as V  # noqa: E402


class TestPinnedVersionsAreTrue(unittest.TestCase):
    def test_readme_shows_the_locked_and_vendored_versions(self):
        expected = V.pinned_badges({**V.pinned_stack(), "python": None})
        self.assertTrue(expected, "no pinned badges rendered at all")
        strip = V.current_strip()
        stale = [b for b in expected if b not in strip]
        self.assertEqual(stale, [], "README version strip is stale — run: "
                                    ".venv/bin/python tools/versions.py --write")

    def test_every_pinned_package_was_found(self):
        missing = [k for k, v in V.pinned_stack().items() if not v]
        self.assertEqual(missing, [], f"no version found for {missing}")


class TestParsing(unittest.TestCase):
    def test_lock_pins_skip_includes_and_comments(self):
        text = "# header\n-r requirements.lock\nfastapi==0.161.1        # direct\nh2==4.4.1\n"
        self.assertEqual(V.lock_pins(text), {"fastapi": "0.161.1", "h2": "4.4.1"})

    def test_hls_version_from_both_build_styles(self):
        self.assertEqual(V.hls_version("…in hls.js version 1.7.3')"), "1.7.3")
        self.assertEqual(V.hls_version('{key:"version",get:function(){return"1.5.17"}}'),
                         "1.5.17")


class TestRendering(unittest.TestCase):
    STACK = {"python": "3.15.0", "sqlite": "3.54.1", "node": "24.22.1 · tests only",
             "ffmpeg": None, "vulns": (0, "2026-10-23")}

    def test_missing_tools_are_left_out_not_shown_blank(self):
        self.assertNotIn("ffmpeg", V.render(self.STACK))

    def test_dashes_in_a_date_are_doubled_for_shields(self):
        self.assertIn("osv.dev%202026--10--23-brightgreen", V.render(self.STACK))

    def test_a_vulnerability_turns_the_badge_red(self):
        self.assertIn("-red.svg", V.render({**self.STACK, "vulns": (2, "2026-10-23")}))

    def test_node_says_it_is_test_tooling(self):
        self.assertIn("node-24.22.1%20%C2%B7%20tests%20only", V.render(self.STACK))


if __name__ == "__main__":
    unittest.main()
