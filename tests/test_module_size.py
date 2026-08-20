#!/usr/bin/env python3
"""
tests/test_module_size.py — per-module line ceilings, as a ratchet.

Until 2026-08-20 the suite's only size check was one hardcoded line in
run_all.py asserting `dlna_gateway.py < 350`. The discipline existed; it
was applied to exactly one file out of ~40, while dlna_library.py grew to
2,912 lines and `api_upnp._gw_browse` to 491 lines in a single function.

WHY A RATCHET AND NOT A FLAT LIMIT. A flat 400-line rule would fail the
suite on day one for 15 modules, so it would be switched off within a
week. Instead `tests/module_size_budget.json` records each module's
CURRENT size as its ceiling:

  * a module may shrink freely; it may never grow past its ceiling
  * a NEW module is held to the 400-line target from birth — legacy
    modules get an allowance, new ones do not
  * once a module drops well below its ceiling, the budget is stale and
    the suite says so, so the gain gets locked in rather than silently
    spent again (regenerate: `python3 tools/regen_size_budget.py`)

The 15 entries over target are pre-existing debt. Listing them makes the
debt explicit and bounded instead of an unbounded "TODO: split this up".

SCOPE: application code only. tests/ and tools/ are excluded on purpose —
a test file legitimately grows with every behaviour it pins, and gating
that would put a thumb on the scale against writing tests.

Run standalone:  python3 -m unittest tests.test_module_size -v
"""
import json
import os
import pathlib
import sys
import unittest

PROJECT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

import tools.regen_size_budget as budget


class TestModuleSize(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.spec = json.loads(budget.BUDGET_PATH.read_text(encoding="utf-8"))
        cls.budgets = cls.spec["budgets"]
        cls.sizes = budget.app_modules()

    def test_no_module_exceeds_its_budget(self):
        over = [f"{m}: {n} lines > budget {self.budgets[m]}"
                for m, n in sorted(self.sizes.items())
                if m in self.budgets and n > self.budgets[m]]
        self.assertEqual(
            over, [],
            "module(s) grew past their line budget. Split the new code out "
            "rather than raising the ceiling — the budget only ratchets "
            "DOWN:\n  " + "\n  ".join(over))

    def test_new_modules_meet_the_target(self):
        """A module absent from the budget is new, and new code has no
        legacy excuse: it must come in under the target."""
        target = self.spec["target_lines"]
        offenders = [f"{m}: {n} lines (new modules must be < {target})"
                     for m, n in sorted(self.sizes.items())
                     if m not in self.budgets and n > target]
        self.assertEqual(offenders, [], "\n  ".join(offenders))

    def test_budget_is_not_stale(self):
        """A module that shrank well below its ceiling should have the gain
        locked in, or the headroom quietly comes back."""
        slack, target = budget.SHRINK_SLACK, self.spec["target_lines"]
        stale = [f"{m}: now {self.sizes[m]}, budget still {b}"
                 for m, b in sorted(self.budgets.items())
                 if m in self.sizes and b > target
                 and self.sizes[m] < b - slack]
        self.assertEqual(
            stale, [],
            "budget can be tightened — run `python3 tools/regen_size_budget.py`"
            ":\n  " + "\n  ".join(stale))

    def test_budget_covers_every_app_module(self):
        missing = sorted(set(self.sizes) - set(self.budgets))
        self.assertEqual(
            missing, [],
            "module(s) missing from the budget — run "
            f"`python3 tools/regen_size_budget.py`: {missing}")

    def test_debt_list_matches_the_budget(self):
        """`over_target` is documentation; keep it honest."""
        target = self.spec["target_lines"]
        computed = sorted((m for m, b in self.budgets.items() if b > target),
                          key=lambda m: -self.budgets[m])
        self.assertEqual(self.spec["over_target"], computed)

    def test_the_debt_does_not_grow(self):
        """The count of over-target modules is itself a ratchet: splitting
        one file into three 500-line files would pass every check above
        while making the problem worse."""
        self.assertLessEqual(
            len(self.spec["over_target"]), 15,
            "more modules are over the 400-line target than when this gate "
            "was introduced — splitting a big file into several still-big "
            "files is not progress")

    def test_gateway_stays_slim(self):
        """The original hand-written check, preserved: dlna_gateway.py is
        the wiring module and must not reacquire logic (that is what
        dlna_localfs_wiring.py exists to absorb)."""
        self.assertLess(self.sizes["dlna_gateway.py"], 350)


if __name__ == "__main__":
    unittest.main()
