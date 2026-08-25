#!/usr/bin/env python3
"""
tools/regen_badges.py — keep README.md's badge numbers honest.

The header badges (test counts, ruff violations) are hand-written numbers in
a Markdown file, which means they drift the moment anyone adds a test — and
they had: the badge still said `1,089 unit · 231 browser` several commits
after both had moved. A badge nobody trusts is worse than no badge.

Same pattern the repo already uses for `schema.sql` (`tools/regen_schema.py`)
and the size budget (`tools/regen_size_budget.py`): the artifact is
GENERATED, and there is one command that regenerates it.

It also checks the COUNTS WRITTEN IN PROSE, which is where the drift
actually happens. The badges have this gate; `CLAUDE.md`'s "N tests" and
"N/N modules" lines had nothing, and drifted twice in a single day — both
times from edits made minutes after the badge was refreshed. A number a
human retypes is a number that goes stale.

Where the numbers come from:

    checks   `tests/run_all.py --offline`  → "ALL <n> TESTS PASSED"
    unit     the same run                  → "All unit tests pass (<n>/<n>)"
    browser  `pytest tests/frontend --collect-only -q`
    ruff     `ruff check . --output-format=concise` (violation lines)

Deliberately NOT wired into `run_all.py` as a staleness gate, unlike
`test_schema_sync.py`. That gate would have to run the suite to know the
suite's own numbers — the suite failing because it can't count itself is a
worse failure mode than a stale badge. Run this when the counts move.

DRY-RUN BY DEFAULT — prints old vs new. `--apply` rewrites README.md.

Usage:
    python3 tools/regen_badges.py                # show the drift
    python3 tools/regen_badges.py --apply        # rewrite the badges
    python3 tools/regen_badges.py --check        # exit 1 if stale
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_README = os.path.join(PROJECT, "README.md")

# shields.io renders `·` from the URL-encoded %C2%B7, and treats an unescaped
# `-` as its own label/value separator — hence the doubling in `%20%C2%B7%20`
# and the `%2C` thousands separator. Getting this wrong yields a badge that
# silently renders as a broken label, so the format lives in ONE place.
_MIDDOT = "%20%C2%B7%20"

_TESTS_RE = re.compile(
    r"(\[!\[Tests\]\(https://img\.shields\.io/badge/tests-)([^)]*?)(\))")
_LINT_RE = re.compile(
    r"(\[!\[Lint: ruff\]\(https://img\.shields\.io/badge/lint-)([^)]*?)(\))")

_DOCS = ("CLAUDE.md", "README.md")

# "python3 -m unittest tools.test_x -v   # 16 tests"
_CLAIM_CMD = re.compile(
    r"unittest\s+([\w.]+)\s+-v[^\n#]*#\s*(\d+)\s+tests?\b")
# "`tests/test_artist_infer.py` (26)"
_CLAIM_FILE = re.compile(r"`((?:tests|tools)/[\w/]+\.py)`\s*\((\d+)\)")
# "**89/89 today**" — the module ratchet
_CLAIM_MODULES = re.compile(r"\*\*(\d+)/(\d+) today\*\*")


def _count_tests(dotted: str) -> int | None:
    """How many test cases `dotted` actually defines, without running
    them. `None` when it cannot be loaded — a missing or broken module is
    reported as unavailable rather than silently counted as the single
    `_FailedTest` unittest substitutes."""
    import unittest
    try:
        suite = unittest.TestLoader().loadTestsFromName(dotted)
    except Exception:                                 # noqa: BLE001
        return None
    flat = list(_flatten(suite))
    if any(type(t).__name__ == "_FailedTest" for t in flat):
        return None
    return len(flat)


def _flatten(suite):
    import unittest
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _flatten(t)
        else:
            yield t


def _module_count() -> int | None:
    import json
    try:
        with open(os.path.join(PROJECT, "tests",
                               "module_size_budget.json")) as f:
            d = json.load(f)
    except Exception:                                 # noqa: BLE001
        return None
    return len(d.get("budgets", d))


def check_doc_claims() -> list[str]:
    """Every prose count that disagrees with reality, as readable lines.

    Read-only by design: these live inside sentences, so a regex that
    REWROTE them would eventually mangle the prose around them. Reporting
    the file, line and both numbers is enough to fix by hand, and keeps
    this tool unable to damage a document."""
    problems: list[str] = []
    if PROJECT not in sys.path:
        sys.path.insert(0, PROJECT)

    for doc in _DOCS:
        path = os.path.join(PROJECT, doc)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()

        for i, line in enumerate(lines, 1):
            for m in _CLAIM_CMD.finditer(line):
                dotted, claimed = m.group(1), int(m.group(2))
                real = _count_tests(dotted)
                if real is None:
                    problems.append(f"{doc}:{i} cannot load {dotted}")
                elif real != claimed:
                    problems.append(
                        f"{doc}:{i} {dotted} says {claimed} tests, has {real}")

            for m in _CLAIM_FILE.finditer(line):
                rel, claimed = m.group(1), int(m.group(2))
                dotted = rel[:-3].replace("/", ".")
                real = _count_tests(dotted)
                if real is None:
                    problems.append(f"{doc}:{i} cannot load {rel}")
                elif real != claimed:
                    problems.append(
                        f"{doc}:{i} {rel} says {claimed} tests, has {real}")

            for m in _CLAIM_MODULES.finditer(line):
                claimed = int(m.group(2))
                real = _module_count()
                if real is not None and real != claimed:
                    problems.append(
                        f"{doc}:{i} says {m.group(1)}/{claimed} modules, "
                        f"budget tracks {real}")
    return problems


def _run(cmd, **kw):
    return subprocess.run(cmd, cwd=PROJECT, capture_output=True,
                          text=True, **kw)


def _venv(name: str) -> str:
    """Prefer the project venv's binary — the system one may be a different
    version, and for ruff a different version means a different count."""
    p = os.path.join(PROJECT, ".venv", "bin", name)
    return p if os.path.isfile(p) else name


def collect_counts(*, skip_suite: bool = False) -> dict:
    """Gather the numbers the badges assert. Returns whatever it could get;
    a key is absent when that source could not be read, so a partial run
    never invents a number."""
    out: dict[str, int] = {}

    if not skip_suite:
        p = _run([_venv("python"), "tests/run_all.py", "--offline"])
        blob = p.stdout + p.stderr
        m = re.search(r"ALL (\d+) TESTS PASSED", blob)
        if m:
            out["checks"] = int(m.group(1))
        m = re.search(r"All unit tests pass \((\d+)/(\d+)\)", blob)
        if m:
            out["unit"] = int(m.group(2))

    p = _run([_venv("pytest"), "tests/frontend", "--collect-only", "-q"])
    m = re.search(r"(\d+) tests collected", p.stdout + p.stderr)
    if m:
        out["browser"] = int(m.group(1))

    p = _run([_venv("ruff"), "check", ".", "--output-format=concise"])
    if p.returncode in (0, 1):
        out["ruff"] = len([ln for ln in p.stdout.splitlines()
                           if re.match(r"^\S+:\d+:\d+:", ln)])
    return out


def _thousands(n: int) -> str:
    """1101 -> '1%2C101'. `,` must be percent-encoded or shields.io truncates
    the label at it."""
    return f"{n:,}".replace(",", "%2C")


def tests_badge_value(checks: int, unit: int, browser: int) -> str:
    """The encoded value half of the tests badge."""
    return (f"{_thousands(checks)}%20checks{_MIDDOT}"
            f"{_thousands(unit)}%20unit{_MIDDOT}"
            f"{_thousands(browser)}%20browser-brightgreen.svg")


def lint_badge_value(violations: int) -> str:
    colour = "brightgreen" if violations == 0 else "red"
    word = "violation" if violations == 1 else "violations"
    return f"ruff{_MIDDOT}{violations}%20{word}-{colour}.svg"


def apply_badges(text: str, counts: dict) -> tuple[str, list[str]]:
    """Rewrite the badge lines. Returns `(new_text, changes)`. Pure, so the
    diff you are shown is the edit that happens."""
    changes: list[str] = []

    if {"checks", "unit", "browser"} <= counts.keys():
        want = tests_badge_value(counts["checks"], counts["unit"],
                                 counts["browser"])

        def _sub(m):
            if m.group(2) != want:
                changes.append(f"tests: {m.group(2)}\n    ->  {want}")
            return m.group(1) + want + m.group(3)
        text, n = _TESTS_RE.subn(_sub, text)
        if not n:
            changes.append("! tests badge not found in README.md")

    if "ruff" in counts:
        want = lint_badge_value(counts["ruff"])

        def _sub(m):
            if m.group(2) != want:
                changes.append(f"lint:  {m.group(2)}\n    ->  {want}")
            return m.group(1) + want + m.group(3)
        text, n = _LINT_RE.subn(_sub, text)
        if not n:
            changes.append("! lint badge not found in README.md")

    return text, changes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--readme", default=_README)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if a badge is stale")
    ap.add_argument("--skip-suite", action="store_true",
                    help="don't run run_all.py (browser + ruff counts only)")
    args = ap.parse_args(argv)

    if not os.path.exists(args.readme):
        print(f"✗ no such file: {args.readme}", file=sys.stderr)
        return 2

    print("collecting counts (this runs the offline suite — ~40s) …")
    counts = collect_counts(skip_suite=args.skip_suite)
    if not counts:
        print("✗ could not read any counts", file=sys.stderr)
        return 2
    for k in ("checks", "unit", "browser", "ruff"):
        print(f"  {k:8} {counts.get(k, '(unavailable)')}")

    with open(args.readme, encoding="utf-8") as f:
        text = f.read()
    new_text, changes = apply_badges(text, counts)

    doc_problems = check_doc_claims()
    if doc_problems:
        print("\n  prose counts that disagree with reality:")
        for d in doc_problems:
            print(f"    {d}")

    if not changes and not doc_problems:
        print("\n✓ badges and prose counts are current")
        return 0
    if not changes:
        # Prose is never rewritten automatically — see check_doc_claims.
        print("\n✗ fix the lines above by hand"
              if args.check else
              "\nbadges are current; the prose counts above need a hand edit.")
        return 1 if args.check else 0
    print()
    for c in changes:
        print(f"  {c}")

    if args.check:
        print("\n✗ stale — run with --apply (prose lines need a hand edit)")
        return 1
    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply.")
        return 0

    with open(args.readme, "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"\n✓ {args.readme} updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
