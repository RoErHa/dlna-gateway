#!/usr/bin/env python3
"""
tools/localfs_scan.py — Phase 2 driver for the LocalFsProvider.

Walks a music root through `dlna_providers.localfs.LocalFsProvider`
and populates `library.db` under the provider's synthesised UDN.
Lets you compare the LocalFs view against the AssetUPnP view without
having to wire LocalFs into the running gateway.

The migration plan's P2 done-when criterion is:

> the new index matches Asset's view — same album/track counts,
> art present, oddities logged. Pure read, zero risk, runs alongside
> Asset.

This tool is the manual proof:

    # Run an incremental scan (only changed/new files re-tagged):
    python3 tools/localfs_scan.py --root /Volumes/SAMDATA/Music

    # Force a full re-scan, ignoring the mtime/size cache:
    python3 tools/localfs_scan.py --root /Volumes/SAMDATA/Music --force

    # Show track counts for every UDN currently in library.db
    # (matches what AssetUPnP indexed against what LocalFs indexed):
    python3 tools/localfs_scan.py --compare

The scan output is logged on the same stdlib `logging` config the
gateway uses; pass `-v` to crank the level to DEBUG.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

# Reach the project root from tools/
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dlna_config import DB_FILE        # noqa: E402
from dlna_library import LibraryDB     # noqa: E402
from dlna_providers.localfs import LocalFsProvider  # noqa: E402


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s")


def _compare_view(db_path: Path):
    """Print track / album counts per UDN, side-by-side."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT udn,
               COUNT(*)                   AS tracks,
               COUNT(DISTINCT album)      AS albums,
               COUNT(DISTINCT artist)     AS artists
          FROM tracks
         WHERE udn != ''
         GROUP BY udn
         ORDER BY tracks DESC
    """).fetchall()
    conn.close()
    if not rows:
        print("library.db has no tracks yet.")
        return
    print(f"{'UDN':<54} {'tracks':>8} {'albums':>8} {'artists':>8}")
    print("-" * 84)
    for r in rows:
        print(f"{r['udn']:<54} {r['tracks']:>8,} "
              f"{r['albums']:>8,} {r['artists']:>8,}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Run a LocalFsProvider scan against a music root.")
    p.add_argument("--root", default=os.environ.get(
        "LOCALFS_MUSIC_ROOT", "/Volumes/SAMDATA/Music"),
        help="Music root to scan (default: %(default)s)")
    p.add_argument("--force", action="store_true",
                   help="Ignore the (mtime, size) cache and re-tag "
                        "every file. Use after schema changes.")
    p.add_argument("--compare", action="store_true",
                   help="Skip scanning — just print tracks/albums per "
                        "UDN currently in library.db.")
    p.add_argument("--db", default=str(DB_FILE),
                   help="library.db path (default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)

    if args.compare:
        _compare_view(Path(args.db))
        return 0

    root = Path(args.root).expanduser()
    if not root.exists():
        print(f"ERROR: root not found: {root}", file=sys.stderr)
        print("  Hint: is the volume mounted? On the user's setup, "
              "SAMDATA needs `~/bin/unlock-samdata.sh` after a boot.",
              file=sys.stderr)
        return 2

    db = LibraryDB(db_file=args.db)
    provider = LocalFsProvider(db, root)
    print(f"LocalFsProvider udn={provider.udn}")
    print(f"  root: {provider.root}")
    print(f"  db  : {args.db}")
    print()

    if not provider.probe():
        print(f"ERROR: probe failed — root not accessible: {root}",
              file=sys.stderr)
        return 3

    try:
        stats = provider.rescan(force=args.force)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 4

    print()
    print(f"scanned:    {stats['scanned']:>8,}")
    print(f"new:        {stats['new']:>8,}")
    print(f"changed:    {stats['changed']:>8,}")
    print(f"unchanged:  {stats['unchanged']:>8,}")
    print(f"removed:    {stats['removed']:>8,}")
    print(f"malformed:  {stats['malformed']:>8,}")
    print(f"elapsed:    {stats['elapsed_sec']:>8.2f}s")
    print()
    print(f"tracks in library.db for this UDN: "
          f"{db.track_count(provider.udn):,}")
    print()
    print("Compare against AssetUPnP via `--compare`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
