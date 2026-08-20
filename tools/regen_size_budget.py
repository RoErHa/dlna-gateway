#!/usr/bin/env python3
"""
tools/regen_size_budget.py — (re)baseline the per-module line budget in
`tests/module_size_budget.json`.

The budget is a RATCHET, not a target. `tests/test_module_size.py` fails
when a module grows past its recorded ceiling, so size can only go down
— and when a module shrinks enough, this tool re-baselines it lower so
the gain is locked in and cannot be silently spent again.

New modules are held to `TARGET_LINES` (400) from birth; they never get
a legacy allowance. The oversized entries in the committed budget are
the pre-existing debt, each one an explicit, visible IOU rather than an
unbounded `# TODO: split this up`.

Before 2026-08-20 the only size check in the suite was a single hardcoded
`dlna_gateway.py < 350` line in run_all.py — the discipline existed, it
was just applied to exactly one file out of ~40.

    python3 tools/regen_size_budget.py           # rewrite the budget
    python3 tools/regen_size_budget.py --check   # exit 1 if stale
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

PROJECT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUDGET_PATH = PROJECT / "tests" / "module_size_budget.json"

TARGET_LINES = 400          # what a NEW module must come in under
SHRINK_SLACK = 25           # re-baseline once a module drops this far below

# Application code only. tests/ and tools/ are excluded on purpose: a test
# file legitimately grows with every behaviour it pins, and gating that
# would put a thumb on the scale against writing tests.
SKIP_TOP = {"tests", "tools", ".venv", "art_cache", "static", "docs", "build"}


def app_modules() -> dict:
    """module path (repo-relative, posix) → line count."""
    out = {}
    for p in sorted(PROJECT.rglob("*.py")):
        rel = p.relative_to(PROJECT)
        if rel.parts[0] in SKIP_TOP or rel.name.startswith("."):
            continue
        out[rel.as_posix()] = len(p.read_text(encoding="utf-8").splitlines())
    return out


def generate(previous: dict | None = None) -> dict:
    """Budget = min(previous ceiling, current size), floored at TARGET_LINES
    for anything already at or under it. A module never gets MORE headroom
    than it had, which is what makes this a ratchet."""
    prev = (previous or {}).get("budgets", {})
    sizes = app_modules()
    budgets = {}
    for mod, n in sizes.items():
        if n <= TARGET_LINES:
            budgets[mod] = TARGET_LINES
        else:
            budgets[mod] = min(prev.get(mod, n), n)
    over = {m: b for m, b in budgets.items() if b > TARGET_LINES}
    return {
        "_comment": [
            "GENERATED — do not hand-edit. Regenerate with:",
            "    python3 tools/regen_size_budget.py",
            "Per-module line ceilings enforced by tests/test_module_size.py.",
            "This is a RATCHET: a module may shrink, never grow. Modules at or",
            f"under the {TARGET_LINES}-line target all share that number; the",
            "entries above it are pre-existing debt, listed so it is visible",
            "and bounded instead of unbounded.",
        ],
        "target_lines": TARGET_LINES,
        "over_target": sorted(over, key=lambda m: -over[m]),
        "budgets": dict(sorted(budgets.items())),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the budget could be tightened; write nothing")
    args = ap.parse_args()

    previous = None
    if BUDGET_PATH.is_file():
        previous = json.loads(BUDGET_PATH.read_text(encoding="utf-8"))
    fresh = generate(previous)

    if args.check:
        if previous is None:
            print("budget file missing — run tools/regen_size_budget.py",
                  file=sys.stderr)
            return 1
        sizes = app_modules()
        slack = [(m, b, sizes[m]) for m, b in previous.get("budgets", {}).items()
                 if m in sizes and b > TARGET_LINES and sizes[m] < b - SHRINK_SLACK]
        missing = [m for m in sizes if m not in previous.get("budgets", {})]
        if not slack and not missing:
            print("module size budget is up to date")
            return 0
        for m, b, n in slack:
            print(f"  {m}: now {n} lines, budget still {b} — tighten it",
                  file=sys.stderr)
        for m in missing:
            print(f"  {m}: new module, not in the budget", file=sys.stderr)
        print("run: python3 tools/regen_size_budget.py", file=sys.stderr)
        return 1

    BUDGET_PATH.write_text(json.dumps(fresh, indent=2) + "\n", encoding="utf-8")
    n_over = len(fresh["over_target"])
    print(f"wrote {BUDGET_PATH.relative_to(PROJECT)} "
          f"({len(fresh['budgets'])} modules, {n_over} over the "
          f"{TARGET_LINES}-line target)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
