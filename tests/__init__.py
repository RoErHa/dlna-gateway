"""Test package init — runs BEFORE any test module in `tests.*`, which
is the only moment early enough to do the one thing below.

**It points `DB_FILE` at a throwaway so a test run cannot migrate the
user's live `library.db`.**

`dlna_library` ends with a lazy `DB` proxy rather than a module-level
`LibraryDB()`, so merely importing it no longer opens the real index
(CLAUDE.md → "`DB` is LAZY"). That closed the import door. It did not
close this one:

    mock.patch.object(dlna_asgi.DB, "all_artists", ...)

`patch.object` must `getattr` the original in order to restore it
later, and that getattr resolves the proxy — opening the real file and
running every pending migration. The suite uses that pattern widely and
cannot stop: a first cut of the proxy that refused those writes broke 12
tests. So the defence belongs at the other end, here.

Observed on 2026-09-20: running `tests/run_all.py --offline` applied a
pending `ALTER TABLE tracks ADD COLUMN` to the live index. That one was
survivable — idempotent, no rows touched. The three UNIQUE migrations
REBUILD `tracks` from a hardcoded DDL, and one of those firing from a
test run is a different kind of afternoon.

Two details are load-bearing:

  * **`dlna_config` is mutated before `dlna_library` is imported.**
    `LibraryDB.__init__(self, db_file: str = DB_FILE)` binds that
    default at class-definition time, so patching after the import
    would be too late — and `tests/__init__.py` is the one hook that
    always runs first, under `unittest discover tests` AND under a bare
    `python -m unittest tests.test_x`.
  * **A test that wants a real DB is unaffected** — those construct
    `LibraryDB(db_file=<tempfile>)` explicitly. This only redirects the
    *accidental* default.
"""
import atexit
import os
import shutil
import sys
import tempfile

# The test modules each do this themselves, but they run after us.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlna_config                                            # noqa: E402

_TEST_DB_DIR = tempfile.mkdtemp(prefix="dlna-test-db-")
dlna_config.DB_FILE = os.path.join(_TEST_DB_DIR, "library.db")
atexit.register(shutil.rmtree, _TEST_DB_DIR, ignore_errors=True)
