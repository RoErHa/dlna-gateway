#!/usr/bin/env python3
"""
tests/test_badges.py — the README's badge numbers are true.

They used to be typed by hand, which is fine until somebody adds a test.
A number nobody verifies is worse than no number, because a reader
believes it. `tools/badges.py` renders the strip from the suites; this
checks the file has not drifted from them.

SCOPE: the **unit** and **browser** counts, both collected without
running anything. The **checks** count is verified by `run_all.py`
itself at the end of an --offline run — it cannot be done from here,
because the only way to know how many checks the runner performs is to
perform them, and this test running the runner would recurse into it.
"""
import os
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tools import badges  # noqa: E402


class TestBadgeRendering(unittest.TestCase):
    def test_a_hyphen_is_doubled_not_escaped(self):
        # `-` is shields.io's field separator. Percent-encoding it, or
        # leaving it single inside a label, splits the label into a
        # second colour field and the badge renders wrong.
        self.assertEqual(badges.quote("a-b"), "a--b")

    def test_the_separator_between_label_and_message_stays_single(self):
        line = [x for x in badges.render(1, 2, 3).splitlines()
                if "Quality gates" in x][0]
        self.assertIn("/quality%20gates-", line)
        self.assertNotIn("/quality%20gates--", line)

    def test_thousands_separators_survive_escaping(self):
        strip = badges.render(191, 1192, 247)
        self.assertIn("1%2C192%20unit", strip)
        self.assertIn("247%20browser", strip)
        self.assertIn("191%20checks", strip)

    def test_the_readme_carries_the_markers(self):
        self.assertTrue(badges.current_strip().startswith("<!-- badges:"))


class TestBadgeNumbersAreTrue(unittest.TestCase):
    """The point of the whole exercise."""

    def test_the_unit_count_matches_the_suite(self):
        self.assertEqual(badges.count_unit(), self._written(r"%C2%B7%20([\d%A-C]+?)%20unit"))

    def test_the_browser_count_matches_the_suite(self):
        self.assertEqual(badges.count_browser(), self._written(r"%20(\d+)%20browser"))

    @staticmethod
    def _written(pattern):
        import re
        found = re.search(pattern, badges.current_strip())
        assert found, f"the tests badge does not match {pattern}"
        return int(found.group(1).replace("%2C", ""))


if __name__ == "__main__":
    unittest.main()
