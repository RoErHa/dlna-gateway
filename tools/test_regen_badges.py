#!/usr/bin/env python3
"""
tools/test_regen_badges.py — tests for the README badge regenerator.

The encoding is the whole risk: shields.io reads an unescaped `-` as its own
label/value separator and truncates a label at an unescaped `,`, so a badge
built slightly wrong renders as a broken label rather than failing loudly.
`tests_badge_value` / `lint_badge_value` / `apply_badges` are pure so the
encoding is pinned here, without running the suite.

    python3 -m unittest tools.test_regen_badges -v
"""
import os
import re
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tools import regen_badges  # noqa: E402
from tools.regen_badges import (  # noqa: E402
    _thousands, apply_badges, lint_badge_value, tests_badge_value,
)

_HDR = (
    "# DLNA Gateway\n\n"
    "[![Tests](https://img.shields.io/badge/tests-1%20checks%20%C2%B7%20"
    "2%20unit%20%C2%B7%203%20browser-brightgreen.svg)](#testing)\n"
    "[![Lint: ruff](https://img.shields.io/badge/lint-ruff%20%C2%B7%20"
    "9%20violations-red.svg)](#code-quality-gates)\n\n"
    "prose\n"
)


class TestEncoding(unittest.TestCase):
    def test_thousands_separator_is_percent_encoded(self):
        # A literal ',' truncates the shields.io label.
        self.assertEqual(_thousands(1101), "1%2C101")
        self.assertNotIn(",", _thousands(1101))

    def test_small_numbers_need_no_separator(self):
        self.assertEqual(_thousands(238), "238")

    def test_six_figure_numbers_still_encode(self):
        self.assertEqual(_thousands(1234567), "1%2C234%2C567")

    def test_tests_badge_has_exactly_one_unescaped_dash(self):
        """shields.io splits label from value on the FIRST unescaped '-'.
        More than one and the colour becomes part of the text."""
        v = tests_badge_value(191, 1101, 238)
        self.assertEqual(v.count("-"), 1, v)
        self.assertTrue(v.endswith("-brightgreen.svg"))

    def test_lint_badge_is_green_at_zero_and_red_otherwise(self):
        self.assertIn("-brightgreen.svg", lint_badge_value(0))
        self.assertIn("-red.svg", lint_badge_value(1))
        self.assertIn("-red.svg", lint_badge_value(42))

    def test_lint_badge_pluralises(self):
        self.assertIn("1%20violation-", lint_badge_value(1))
        self.assertIn("0%20violations-", lint_badge_value(0))
        self.assertIn("2%20violations-", lint_badge_value(2))

    def test_separator_is_an_encoded_middot(self):
        v = tests_badge_value(1, 2, 3)
        self.assertIn("%20%C2%B7%20", v)


class TestApply(unittest.TestCase):
    def test_rewrites_both_badges(self):
        new, changes = apply_badges(
            _HDR, {"checks": 191, "unit": 1101, "browser": 238, "ruff": 0})
        self.assertIn(tests_badge_value(191, 1101, 238), new)
        self.assertIn(lint_badge_value(0), new)
        self.assertEqual(len(changes), 2)

    def test_prose_and_links_are_untouched(self):
        new, _ = apply_badges(
            _HDR, {"checks": 1, "unit": 2, "browser": 3, "ruff": 9})
        self.assertIn("# DLNA Gateway", new)
        self.assertIn("prose", new)
        self.assertIn("](#testing)", new)
        self.assertIn("](#code-quality-gates)", new)

    def test_already_current_reports_no_change(self):
        current, _ = apply_badges(
            _HDR, {"checks": 191, "unit": 1101, "browser": 238, "ruff": 0})
        again, changes = apply_badges(
            current, {"checks": 191, "unit": 1101, "browser": 238, "ruff": 0})
        self.assertEqual(changes, [])
        self.assertEqual(current, again)

    def test_partial_counts_never_invent_a_number(self):
        """If the suite could not be read, the tests badge must be left alone
        rather than written with a guess."""
        new, _ = apply_badges(_HDR, {"browser": 238, "ruff": 0})
        self.assertIn("1%20checks", new)          # tests badge unchanged
        self.assertIn(lint_badge_value(0), new)   # lint badge updated

    def test_missing_badge_is_reported_not_silently_skipped(self):
        _, changes = apply_badges(
            "# no badges here\n",
            {"checks": 1, "unit": 2, "browser": 3, "ruff": 0})
        self.assertTrue(any(c.startswith("!") for c in changes), changes)


class TestRealReadme(unittest.TestCase):
    """A README whose badges the tool cannot find is the failure mode that
    matters — it would silently 'succeed' forever."""

    def setUp(self):
        path = os.path.join(PROJECT, "README.md")
        if not os.path.exists(path):
            self.skipTest("README.md not present")
        with open(path, encoding="utf-8") as f:
            self.text = f.read()

    def test_both_badges_are_findable(self):
        _, changes = apply_badges(
            self.text, {"checks": 1, "unit": 2, "browser": 3, "ruff": 9})
        self.assertFalse([c for c in changes if c.startswith("!")],
                         "regen_badges can no longer find a badge in README.md")

    def test_lint_badge_links_to_a_heading_that_exists(self):
        self.assertIn("](#code-quality-gates)", self.text)
        self.assertTrue(
            re.search(r"^###?\s+Code quality gates\s*$", self.text, re.M),
            "the lint badge anchors at #code-quality-gates but no such "
            "heading exists")

    def test_readme_actually_names_ruff(self):
        self.assertIn("ruff", self.text)


class TestDocClaimPatterns(unittest.TestCase):
    """The prose counts. These drifted twice in one day because nothing
    recomputed them, so the patterns that find them are worth pinning: a
    regex that silently matches NOTHING turns this gate green forever."""

    def test_finds_a_unittest_command_claim(self):
        m = regen_badges._CLAIM_CMD.search(
            "python3 -m unittest tools.test_rotate_fixes -v   # 16 tests")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "tools.test_rotate_fixes")
        self.assertEqual(m.group(2), "16")

    def test_finds_a_backticked_file_claim(self):
        m = regen_badges._CLAIM_FILE.search(
            "Guarded by `tests/test_artist_infer.py` (26) and more.")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "tests/test_artist_infer.py")
        self.assertEqual(m.group(2), "26")

    def test_finds_the_module_ratchet_claim(self):
        m = regen_badges._CLAIM_MODULES.search("grew — **89/89 today** and")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2), "89")

    def test_a_singular_test_is_still_a_claim(self):
        self.assertIsNotNone(regen_badges._CLAIM_CMD.search(
            "python3 -m unittest tools.test_x -v  # 1 test"))

    def test_prose_without_a_count_is_not_a_claim(self):
        """Most mentions carry no number and must not be flagged."""
        self.assertIsNone(regen_badges._CLAIM_CMD.search(
            "python3 -m unittest tools.test_rotate_fixes -v"))
        self.assertIsNone(regen_badges._CLAIM_FILE.search(
            "see `tests/test_artist_infer.py` for the rules"))


class TestCountingTests(unittest.TestCase):

    def test_counts_a_real_module(self):
        n = regen_badges._count_tests("tools.test_regen_badges")
        self.assertIsNotNone(n)
        self.assertGreater(n, 10)

    def test_a_missing_module_is_unavailable_not_one(self):
        """unittest substitutes a single `_FailedTest` for a module it
        cannot import. Counting that as 1 would let a deleted test file
        pass the gate by claiming "1 test"."""
        self.assertIsNone(regen_badges._count_tests("tools.no_such_module_xyz"))


class TestTheGateActuallyBites(unittest.TestCase):
    """A checker that reports nothing is indistinguishable from a clean
    tree. These assert it finds the real claims, and that they hold."""

    def test_the_repo_has_claims_to_check(self):
        with open(os.path.join(regen_badges.PROJECT, "CLAUDE.md"),
                  encoding="utf-8") as f:
            text = f.read()
        found = (len(regen_badges._CLAIM_CMD.findall(text))
                 + len(regen_badges._CLAIM_FILE.findall(text))
                 + len(regen_badges._CLAIM_MODULES.findall(text)))
        self.assertGreater(found, 5, "patterns match nothing — vacuous gate")

    def test_the_live_docs_are_currently_consistent(self):
        self.assertEqual(regen_badges.check_doc_claims(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
