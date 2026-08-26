#!/usr/bin/env python3
"""
tests/test_library_singleton.py — the shared `LibraryDB` handle is LAZY.

Until 2026-08-26 `dlna_library` ended with `DB = LibraryDB()`, so merely
IMPORTING the module — a test, a tool, a REPL in the repo dir, or anything
that reaches it transitively, which is most of the app — ran `_init_schema`
and every pending migration against the real, live `library.db`. A new
migration therefore landed the first time any test imported the module,
not at the next restart.

The contract now: import opens nothing; the first attribute access on `DB`
opens it once. The import half is checked in a SUBPROCESS on purpose — this
process has already imported half the app, so an in-process assertion about
"has it been constructed yet" would be answering for the test runner rather
than for a clean import.
"""
import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

import dlna_library  # noqa: E402


def _import_probe(modules: str) -> str:
    """Import `modules` in a clean interpreter, report whether the DB opened."""
    src = (
        f"import sys; sys.path.insert(0, {PROJECT!r})\n"
        f"import {modules}\n"
        "import dlna_library\n"
        "print('OPEN' if dlna_library._db_instance is not None else 'COLD')\n"
    )
    out = subprocess.run([sys.executable, "-c", src], capture_output=True,
                         text=True, cwd=PROJECT, timeout=120)
    if out.returncode != 0:
        raise AssertionError(f"probe failed:\n{out.stdout}\n{out.stderr}")
    return out.stdout.strip().splitlines()[-1]


class TestImportOpensNothing(unittest.TestCase):

    def test_importing_dlna_library_does_not_open_the_db(self):
        self.assertEqual(_import_probe("dlna_library"), "COLD")

    def test_importing_the_db_consumers_does_not_open_it_either(self):
        """Every one of these does `from dlna_library import DB` at module
        scope. A module-level __getattr__ would have constructed the handle
        the moment each was imported; the proxy is what keeps them cold."""
        for mod in ("api_browse", "api_playlists", "api_radio",
                    "api_playback_state", "api_subsonic_proto",
                    "api_upnp_ids", "dlna_asgi_state"):
            with self.subTest(module=mod):
                self.assertEqual(_import_probe(mod), "COLD")

    def test_importing_the_asgi_app_does_not_open_it(self):
        """The whole edge — the widest import in the tree."""
        self.assertEqual(_import_probe("dlna_asgi"), "COLD")


class _FakeDB:
    """Stands in for LibraryDB so these tests never touch the real file."""
    built = 0
    slow = False

    def __init__(self):
        if _FakeDB.slow:
            threading.Event().wait(0.05)   # widen the construction race
        type(self).built += 1
        self.marker = "fake"

    def pl_list(self):
        return ["from the fake"]


class TestLazyResolution(unittest.TestCase):

    def setUp(self):
        _FakeDB.built = 0
        _FakeDB.slow = False
        dlna_library._reset_db_singleton()
        self.addCleanup(dlna_library._reset_db_singleton)

    def test_attribute_access_opens_it(self):
        with patch.object(dlna_library, "LibraryDB", _FakeDB):
            self.assertIsNone(dlna_library._db_instance)
            self.assertEqual(dlna_library.DB.pl_list(), ["from the fake"])
            self.assertEqual(_FakeDB.built, 1)

    def test_it_is_opened_exactly_once(self):
        with patch.object(dlna_library, "LibraryDB", _FakeDB):
            first = dlna_library.DB.marker
            for _ in range(20):
                dlna_library.DB.pl_list()
            self.assertEqual(first, "fake")
            self.assertEqual(_FakeDB.built, 1)

    def test_get_db_and_the_proxy_are_the_same_handle(self):
        with patch.object(dlna_library, "LibraryDB", _FakeDB):
            self.assertIs(dlna_library.DB.pl_list.__self__,
                          dlna_library.get_db())
            self.assertEqual(_FakeDB.built, 1)

    def test_concurrent_first_access_opens_it_once(self):
        """The reason get_db double-checks under a lock: LibraryDB.__init__
        runs every pending migration, and two threads racing it would run
        them twice against one file."""
        _FakeDB.slow = True
        seen = []
        with patch.object(dlna_library, "LibraryDB", _FakeDB):
            def worker():
                seen.append(dlna_library.get_db())

            threads = [threading.Thread(target=worker) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        self.assertEqual(_FakeDB.built, 1)
        self.assertEqual(len(seen), 16)
        self.assertTrue(all(h is seen[0] for h in seen))


class TestProxySurface(unittest.TestCase):

    def setUp(self):
        _FakeDB.built = 0
        _FakeDB.slow = False
        dlna_library._reset_db_singleton()
        self.addCleanup(dlna_library._reset_db_singleton)

    def test_owned_components_hold_the_proxy_not_a_live_handle(self):
        """DEVICE_ROLES / INDEXER / ART_FETCHER are built at import and are
        handed `DB`. If any of them resolved it eagerly, import would open
        the DB again through the back door."""
        for comp, attr in ((dlna_library.DEVICE_ROLES, "_db"),
                           (dlna_library.INDEXER, "library"),
                           (dlna_library.ART_FETCHER, "_db")):
            with self.subTest(component=type(comp).__name__):
                self.assertIs(getattr(comp, attr), dlna_library.DB)

    def test_patch_object_reaches_the_real_handle(self):
        """The suite patches methods straight onto `DB`
        (`mock.patch.object(dlna_asgi.DB, "all_artists", ...)`), and mock
        setattrs on entry then delattrs on exit. A first cut refused both —
        it read as a nice guard and broke 12 existing tests. The write has
        to land on the handle, and has to be gone again afterwards."""
        with patch.object(dlna_library, "LibraryDB", _FakeDB):
            handle = dlna_library.get_db()
            with patch.object(dlna_library.DB, "pl_list",
                              return_value=["patched"]) as m:
                self.assertEqual(dlna_library.DB.pl_list(), ["patched"])
                self.assertEqual(handle.pl_list(), ["patched"])
                self.assertEqual(m.call_count, 2)
            self.assertEqual(dlna_library.DB.pl_list(), ["from the fake"])
            self.assertEqual(_FakeDB.built, 1)

    def test_repr_does_not_open_the_db(self):
        self.assertIn("not yet opened", repr(dlna_library.DB))
        self.assertIsNone(dlna_library._db_instance)


if __name__ == "__main__":
    unittest.main(verbosity=2)
