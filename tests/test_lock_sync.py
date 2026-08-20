#!/usr/bin/env python3
"""
tests/test_lock_sync.py — fail the suite if `requirements.lock` has
drifted from what is actually installed, or if a direct dependency was
added to `requirements.txt` without being locked.

Same contract as `tests/test_schema_sync.py`: the lock is a GENERATED
artifact that does not auto-update, so it goes stale the moment anyone
pip-installs an upgrade. Fix when it fails:
    python3 tools/regen_lock.py

Run standalone:
    python3 -m unittest tests.test_lock_sync -v
"""
import os
import re
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import tools.regen_lock as regen


def _pins(text: str) -> dict:
    out = {}
    for ln in text.splitlines():
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        name, _, ver = ln.partition("==")
        out[name.strip().lower()] = ver.strip()
    return out


class TestLockSync(unittest.TestCase):

    def setUp(self):
        self.assertTrue(os.path.isfile(regen.LOCK_PATH),
                        "requirements.lock is missing")
        with open(regen.LOCK_PATH, encoding="utf-8") as f:
            self.committed = _pins(f.read())

    def test_lock_matches_installed_closure(self):
        """Every pin equals the installed version, and nothing is missing."""
        fresh = _pins(regen.generate())
        self.assertEqual(
            self.committed, fresh,
            "requirements.lock is out of date with the installed packages — "
            "regenerate it: python3 tools/regen_lock.py")

    def test_every_direct_dep_is_locked(self):
        """A dep added to requirements.txt but never locked would install
        unpinned in production — the exact gap the lock exists to close."""
        missing = [d for d in regen.direct_deps()
                   if d.lower().replace("_", "-") not in self.committed]
        self.assertEqual(missing, [],
                         f"direct deps missing from requirements.lock: {missing}")

    def test_every_pin_is_exact(self):
        """A `>=` sneaking into the lock would defeat its whole purpose."""
        with open(regen.LOCK_PATH, encoding="utf-8") as f:
            body = [ln.split("#")[0].strip() for ln in f
                    if ln.strip() and not ln.lstrip().startswith("#")]
        loose = [ln for ln in body if ln and not re.match(r"^[\w.\-]+==[^=]+$", ln)]
        self.assertEqual(loose, [], f"non-exact pins in requirements.lock: {loose}")

    def test_edge_owning_deps_have_upper_bounds(self):
        """fastapi + hypercorn own the TLS/HTTP-2 edge. An unbounded `>=`
        there lets a future major silently replace the server on a fresh
        ./setup.sh, which is not something a lock alone prevents (the spec
        file is what setup.sh installs when someone skips the lock)."""
        with open(os.path.join(PROJECT, "requirements.txt"), encoding="utf-8") as f:
            spec = [ln.strip() for ln in f
                    if ln.strip() and not ln.lstrip().startswith("#")]
        for pkg in ("fastapi", "hypercorn"):
            line = next((ln for ln in spec if ln.lower().startswith(pkg)), None)
            self.assertIsNotNone(line, f"{pkg} missing from requirements.txt")
            self.assertIn("<", line,
                          f"{pkg} has no upper bound in requirements.txt: {line!r}")


if __name__ == "__main__":
    unittest.main()
