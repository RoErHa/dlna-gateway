#!/usr/bin/env python3
"""
tools/regen_schema.py — regenerate the committed `schema.sql` from a
fresh, fully-migrated `library.db`.

`schema.sql` is a committed artifact (the README points at it) but does
NOT auto-update when the DB schema changes — it has silently drifted
before (album_key across A1/A2; the dropped track_loudness table). Run
this after ANY schema change. `tests/test_schema_sync.py` fails the suite
if `schema.sql` is stale, so drift is caught automatically.

    python3 tools/regen_schema.py            # rewrite schema.sql
    python3 tools/regen_schema.py --check     # exit 1 if out of date (no write)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(PROJECT, "schema.sql")


def generate_schema() -> str:
    """The `CREATE` dump of a fresh, fully-migrated DB — identical form to
    `sqlite3 <db> .schema`. Builds a throw-away DB via LibraryDB (which
    runs every CREATE + migration) and dumps its schema."""
    if PROJECT not in sys.path:
        sys.path.insert(0, PROJECT)
    from dlna_library import LibraryDB   # noqa: E402 (path set above)

    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        LibraryDB(tmp)                    # CREATE TABLEs + all migrations
        out = subprocess.run(["sqlite3", tmp, ".schema"],
                             capture_output=True, text=True, check=True)
        return out.stdout
    finally:
        os.unlink(tmp)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if schema.sql is out of date; don't write")
    args = ap.parse_args(argv)

    fresh = generate_schema()
    current = ""
    if os.path.exists(SCHEMA_PATH):
        with open(SCHEMA_PATH) as f:
            current = f.read()

    if args.check:
        if fresh != current:
            print("schema.sql is OUT OF DATE — run: python3 tools/regen_schema.py",
                  file=sys.stderr)
            return 1
        print("schema.sql is up to date.")
        return 0

    with open(SCHEMA_PATH, "w") as f:
        f.write(fresh)
    n = fresh.count("CREATE TABLE")
    print(f"wrote {SCHEMA_PATH} ({n} tables)"
          + ("" if fresh != current else " — no change"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
