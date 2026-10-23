#!/usr/bin/env python3
"""
tools/regen_lock.py — regenerate the committed `requirements.lock` from
the current virtualenv.

`requirements.lock` is the EXACT runtime dependency set (direct deps from
`requirements.txt` plus their full transitive closure, all ==-pinned). It
is a committed artifact and does NOT auto-update, so it drifts the moment
anyone pip-installs an upgrade. Same contract as `tools/regen_schema.py`:
run this after ANY dependency change; `tests/test_lock_sync.py` fails the
suite when the lock no longer matches what is installed.

Why a lock at all: `requirements.txt` uses loose `>=` ranges on purpose
(every dep is optional and the gateway degrades when one is missing), but
that is a spec, not a reproducible install. fastapi + hypercorn own the
whole 2.0 TLS/HTTP-2 edge; without a pinned set there is no known-good
state to roll back to when an upstream release breaks a fresh setup.

    python3 tools/regen_lock.py            # rewrite requirements.lock (+ dev lock)
    python3 tools/regen_lock.py --check    # exit 1 if stale (no write)

`requirements-dev.lock` (added 2026-10-23) does the same for the test
tooling: `-r requirements.lock`, then the DEV-ONLY part of the closure of
requirements-dev.txt. Without it only the dev tools' DIRECT deps were
pinned, so a fresh clone got whatever transitive versions were newest that
day (found when an upgrade moved certifi, a selenium/requests dep that no
tracked file recorded). It is written only when every dev root is
installed — a runtime-only venv must not overwrite it with a truncated set.
"""
from __future__ import annotations

import argparse
import datetime
import importlib.metadata as md
import os
import platform
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_PATH = os.path.join(PROJECT, "requirements.lock")
REQS_PATH = os.path.join(PROJECT, "requirements.txt")
DEV_REQS_PATH = os.path.join(PROJECT, "requirements-dev.txt")
DEV_LOCK_PATH = os.path.join(PROJECT, "requirements-dev.lock")


def _norm(name: str) -> str:
    return name.lower().replace("_", "-")


def direct_deps(path: str = REQS_PATH) -> list[str]:
    """The un-pinned package names from a requirements file — the roots of
    the closure. Comment lines (which is most of that file) are skipped."""
    out = []
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split("[")[0]
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", ";", " "):
            name = name.split(sep)[0]
        if name:
            out.append(name.strip())
    return out


def resolve(roots: list[str]) -> dict[str, str]:
    """Transitive closure of `roots`, name → installed version.

    Requirements guarded by an `extra ==` marker are optional extras we do
    not install, so they are skipped. Packages that resolve to nothing
    (exceptiongroup / taskgroup / tomli — required by anyio+hypercorn only
    on Python < 3.11) are simply absent on this interpreter and are left
    out rather than pinned to a version we cannot observe."""
    seen: dict[str, str] = {}

    def walk(name: str) -> None:
        key = _norm(name)
        if key in seen:
            return
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            try:
                dist = md.distribution(key)
            except md.PackageNotFoundError:
                return
        seen[key] = dist.version
        for req in (dist.requires or []):
            if ";" in req and "extra" in req.split(";", 1)[1]:
                continue
            base = req.split(";")[0].split("[")[0]
            for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", " "):
                base = base.split(sep)[0]
            if base.strip():
                walk(base.strip())

    for r in roots:
        walk(r)
    return seen


def generate() -> str:
    roots = direct_deps()
    pins = resolve(roots)
    direct = {_norm(r) for r in roots}
    py = platform.python_version()
    header = f"""# requirements.lock — EXACT runtime dependency set.
#
# GENERATED ARTIFACT — do not hand-edit. Regenerate after any dependency
# change:  python3 tools/regen_lock.py
# Generated {datetime.date.today()} from the working .venv
# (Python {py}, {platform.system()}/{platform.machine()}) by resolving the
# transitive closure of the direct deps in requirements.txt.
#
# WHY THIS FILE EXISTS. requirements.txt intentionally uses loose `>=`
# ranges: every dep there is optional and the gateway degrades gracefully
# when one is missing. That is the right SPEC, but it is not a reproducible
# INSTALL — an unbounded `fastapi>=…` silently accepts a future breaking
# release, and fastapi + hypercorn own the entire 2.0 TLS/HTTP-2 edge.
# Without a lock there is no known-good set to roll back to when a fresh
# `./setup.sh` picks up a bad upstream release.
#
# USE: reproducible / production install
#     .venv/bin/pip install -r requirements.lock
#   deliberately testing newer upstreams:
#     .venv/bin/pip install -r requirements.txt
#
# NOT INCLUDED: dev-only tools (ruff, pytest, playwright, selenium,
# Appium-Python-Client) — see requirements-dev.txt. Also absent are
# exceptiongroup / taskgroup / tomli, which anyio+hypercorn require only
# on Python < 3.11.
"""
    body = "\n".join(
        f"{k}=={pins[k]}" + ("        # direct" if k in direct else "")
        for k in sorted(pins))
    return header + "\n" + body + "\n"


def dev_roots_installed() -> list[str]:
    """Dev roots NOT installed here — non-empty means: do not write the dev lock."""
    missing = []
    for name in direct_deps(DEV_REQS_PATH):
        try:
            md.distribution(name)
        except md.PackageNotFoundError:
            missing.append(name)
    return missing


def generate_dev() -> str:
    """The dev lock: the runtime lock by reference, then every package the
    dev roots pull in that the runtime closure does not already pin."""
    runtime = resolve(direct_deps())
    roots = direct_deps(DEV_REQS_PATH)
    pins = {k: v for k, v in resolve(roots).items() if k not in runtime}
    direct = {_norm(r) for r in roots}
    header = f"""# requirements-dev.lock — EXACT test/lint tooling set.
#
# GENERATED ARTIFACT — do not hand-edit. Regenerate after any dependency
# change:  python3 tools/regen_lock.py
# Generated {datetime.date.today()} from the working .venv
# (Python {platform.python_version()}, {platform.system()}/{platform.machine()}).
#
# requirements-dev.txt is the SPEC (the tools, pinned); this is the INSTALL:
# the runtime lock plus the full transitive closure of those tools, so a
# fresh clone tests with exactly what passed here.
#
#     .venv/bin/pip install -r requirements-dev.lock
#     .venv/bin/playwright install chromium webkit
"""
    body = "\n".join(
        f"{k}=={pins[k]}" + ("        # direct" if k in direct else "")
        for k in sorted(pins))
    return header + "\n-r requirements.lock\n" + body + "\n"


def pins_of(text: str) -> list[str]:
    """The pins only — header dates/interpreter legitimately differ run to
    run, and the dev lock's `-r` include is structure, not a pin."""
    return sorted(ln.split("#")[0].strip()
                  for ln in text.splitlines()
                  if ln.strip() and not ln.lstrip().startswith(("#", "-r")))


def _read(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _report_stale(label: str, fresh: str, current: str) -> None:
    print(f"{label} is STALE — run: python3 tools/regen_lock.py", file=sys.stderr)
    for p in sorted(set(pins_of(fresh)) - set(pins_of(current))):
        print(f"  + {p}", file=sys.stderr)
    for p in sorted(set(pins_of(current)) - set(pins_of(fresh))):
        print(f"  - {p}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if requirements.lock is stale; write nothing")
    args = ap.parse_args()

    targets = [("requirements.lock", LOCK_PATH, generate())]
    missing = dev_roots_installed()
    if missing:
        print(f"dev tools not installed ({', '.join(missing)}) — "
              "requirements-dev.lock left as it is", file=sys.stderr)
    else:
        targets.append(("requirements-dev.lock", DEV_LOCK_PATH, generate_dev()))

    if args.check:
        stale = 0
        for label, path, fresh in targets:
            current = _read(path)
            if pins_of(fresh) == pins_of(current):
                print(f"{label} is up to date")
            else:
                _report_stale(label, fresh, current)
                stale += 1
        return 1 if stale else 0

    for _label, path, fresh in targets:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(fresh)
        print(f"wrote {path} ({len(pins_of(fresh))} pinned packages)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
