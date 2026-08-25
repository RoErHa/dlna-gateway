#!/usr/bin/env python3
"""
tools/test_rotate_fixes.py — tests for the FIXES.md rolling window.

The risk in this tool is deleting the wrong half of a file, so `split_entries`
and `rotate` are pure and tested directly.

    python3 -m unittest tools.test_rotate_fixes -v
"""
import os
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tools.rotate_fixes import rotate, split_entries  # noqa: E402

PREAMBLE = "# Fixes\n\nA rolling log.\n\n---\n\n"


def _doc(*entries):
    out = PREAMBLE
    for sha, date, title, body in entries:
        out += f"## {sha} — {date} — {title}\n\n{body}\n\n"
    return out


class TestSplit(unittest.TestCase):
    def test_preamble_is_everything_before_the_first_entry(self):
        pre, entries = split_entries(_doc(("aaa1111", "20260825", "T", "body")))
        self.assertEqual(pre, PREAMBLE)
        self.assertEqual(len(entries), 1)

    def test_fields_are_parsed(self):
        _, e = split_entries(_doc(("aaa1111", "20260825", "The stop", "why")))
        self.assertEqual(e[0]["sha"], "aaa1111")
        self.assertEqual(e[0]["date"], "20260825")
        self.assertEqual(e[0]["title"], "The stop")
        self.assertIn("why", e[0]["body"])

    def test_body_includes_its_own_heading_so_rejoin_is_lossless(self):
        doc = _doc(("aaa1111", "20260825", "A", "x"),
                   ("bbb2222", "20260810", "B", "y"))
        pre, entries = split_entries(doc)
        self.assertEqual(pre + "".join(e["body"] for e in entries), doc)

    def test_h3_and_lower_headings_inside_an_entry_are_not_entries(self):
        """An entry's own ### subsections must not split it."""
        doc = _doc(("aaa1111", "20260825", "A",
                    "### 1. part one\ntext\n\n### 2. part two\ntext"))
        _, entries = split_entries(doc)
        self.assertEqual(len(entries), 1)
        self.assertIn("part two", entries[0]["body"])

    def test_a_plain_h2_without_a_date_is_not_an_entry(self):
        """Prose in the preamble that happens to use ## must not be rotated."""
        doc = "# Fixes\n\n## How to use this\n\ntext\n\n"
        pre, entries = split_entries(doc)
        self.assertEqual(entries, [])
        self.assertEqual(pre, doc)

    def test_hyphen_separator_parses_too(self):
        doc = "# Fixes\n\n## abc1234 - 20260825 - Title\n\nbody\n"
        _, entries = split_entries(doc)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["date"], "20260825")

    def test_heading_without_a_title_still_parses(self):
        doc = "# Fixes\n\n## abc1234 — 20260825\n\nbody\n"
        _, entries = split_entries(doc)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "")


class TestRotate(unittest.TestCase):
    def _five(self):
        return _doc(*[(f"sha{i}", f"2026082{i}", f"T{i}", f"body{i}")
                      for i in range(5)])

    def test_keeps_the_newest_n_in_file_order(self):
        new, dropped = rotate(self._five(), 3)
        _, kept = split_entries(new)
        self.assertEqual([e["sha"] for e in kept], ["sha0", "sha1", "sha2"])
        self.assertEqual([e["sha"] for e in dropped], ["sha3", "sha4"])

    def test_dropped_content_is_gone_kept_content_is_intact(self):
        new, _ = rotate(self._five(), 2)
        self.assertIn("body1", new)
        self.assertNotIn("body2", new)
        self.assertNotIn("body4", new)

    def test_preamble_survives_untouched(self):
        new, _ = rotate(self._five(), 1)
        self.assertTrue(new.startswith(PREAMBLE))

    def test_under_the_limit_is_a_no_op_byte_for_byte(self):
        doc = _doc(("a", "20260825", "T", "b"))
        new, dropped = rotate(doc, 3)
        self.assertEqual(new, doc)
        self.assertEqual(dropped, [])

    def test_exactly_at_the_limit_is_a_no_op(self):
        doc = _doc(*[(f"s{i}", "20260825", "T", "b") for i in range(3)])
        new, dropped = rotate(doc, 3)
        self.assertEqual(new, doc)
        self.assertEqual(dropped, [])

    def test_idempotent(self):
        once, _ = rotate(self._five(), 3)
        twice, dropped = rotate(once, 3)
        self.assertEqual(once, twice)
        self.assertEqual(dropped, [])

    def test_no_entries_at_all_is_left_alone(self):
        doc = "# Fixes\n\nnothing here yet.\n"
        new, dropped = rotate(doc, 3)
        self.assertEqual(new, doc)
        self.assertEqual(dropped, [])

    def test_result_ends_with_exactly_one_newline(self):
        new, _ = rotate(self._five(), 3)
        self.assertTrue(new.endswith("\n"))
        self.assertFalse(new.endswith("\n\n"))

    def test_the_real_file_parses(self):
        """A shipped FIXES.md the tool cannot read is worse than no tool."""
        path = os.path.join(PROJECT, "FIXES.md")
        if not os.path.exists(path):
            self.skipTest("FIXES.md not present")
        with open(path, encoding="utf-8") as f:
            _, entries = split_entries(f.read())
        self.assertGreaterEqual(len(entries), 1,
                                "FIXES.md has no parseable `## <sha> — <date>` entry")
        self.assertLessEqual(len(entries), 3,
                             "FIXES.md holds more than three entries — rotate it")


if __name__ == "__main__":
    unittest.main(verbosity=2)
