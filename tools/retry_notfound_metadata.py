#!/usr/bin/env python3
"""
retry_notfound_metadata.py — clean up bogus 'notfound' rows so the
AcoustID worker can re-try them.

Built 2026-05-25 after an AcoustID HTTP 503 outage during the first
24k-track pass left ~10 transient lookup failures cached as permanent
'notfound' rows. The fix in `dlna_acoustid._lookup` now raises
`AcoustIDTransientError` on 5xx + network errors so they leave the URL
bare instead. But existing bogus rows from the old code need manual
cleanup — that's this tool.

The tool can't tell from the DB alone which `notfound` rows are bogus
(no per-row error code), so it offers two precision levels:

  1. `--since TIMESTAMP` — delete only notfound rows newer than this.
     Use when you know roughly when the outage was (e.g. log-scrape the
     `HTTP 5xx` warnings in `acoustid-firstpass.log` to find the window).
  2. `--all` — delete every notfound row. Re-runs every miss, including
     legitimate no-matches. Wastes ~1.5s × N seconds but is fully
     correct: legitimate misses get re-cached as notfound automatically.

Default: report stats only (no deletions). Also scans
`acoustid-firstpass.log` if present in CWD and reports the count of
`HTTP 5xx` lines as a sanity check.

Usage:
    # Report what's in metadata_overrides (no deletions):
    python3 tools/retry_notfound_metadata.py

    # Delete every notfound row, re-process all misses on next worker run:
    python3 tools/retry_notfound_metadata.py --all

    # Delete only rows updated after a known 503-outage timestamp:
    python3 tools/retry_notfound_metadata.py --since '2026-05-25 14:30:00'

    # See what would be deleted, without acting:
    python3 tools/retry_notfound_metadata.py --all --dry-run
"""
import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, Optional

# Locate library.db next to this tool's parent directory (matches the
# layout: dlna-gateway/library.db + dlna-gateway/tools/this-script.py).
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "library.db"
_DEFAULT_LOG = Path("acoustid-firstpass.log")   # relative to CWD


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"library.db not found at {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _report_stats(conn: sqlite3.Connection) -> None:
    """Print a breakdown of metadata_overrides by source + age."""
    cur = conn.execute("""
        SELECT source, COUNT(*) AS n,
               MIN(updated_at) AS oldest,
               MAX(updated_at) AS newest
          FROM metadata_overrides
         GROUP BY source
         ORDER BY n DESC
    """)
    print("metadata_overrides breakdown:")
    print(f"  {'source':>11s}  {'count':>6s}  {'oldest':<20s}  {'newest':<20s}")
    for r in cur.fetchall():
        print(f"  {r['source']:>11s}  {r['n']:>6,d}  "
              f"{(r['oldest'] or ''):<20s}  {(r['newest'] or ''):<20s}")
    print()


def _scan_log_for_5xx(log_path: Path) -> dict:
    """Best-effort scan of `acoustid-firstpass.log` for `HTTP 5xx` lines.
    Returns {count: int, sample: [up to 5 lines]} or empty if log
    absent. Used to give the user a sanity-check on the size of the
    transient-caused poison."""
    if not log_path.exists():
        return {}
    pattern = re.compile(r"HTTP [5]\d\d ")  # HTTP 5xx
    matches = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if pattern.search(line):
                    matches.append(line.strip())
    except OSError as e:
        print(f"WARNING: could not read {log_path}: {e}", file=sys.stderr)
        return {}
    return {"count": len(matches), "sample": matches[:5]}


def _print_log_summary(log_summary: dict, log_path: Path) -> None:
    if not log_summary:
        print(f"(no log scan: {log_path} not present in CWD)")
        return
    n = log_summary["count"]
    if n == 0:
        print(f"Log scan ({log_path}): no HTTP 5xx lines found.")
        return
    print(f"Log scan ({log_path}): {n} HTTP 5xx warning(s) found.")
    if log_summary["sample"]:
        print("  First few:")
        for line in log_summary["sample"]:
            # Truncate verbose lines so the report stays readable.
            print(f"    {line[:140]}")
    print()


def _select_notfound(conn: sqlite3.Connection,
                     since: Optional[str] = None) -> list:
    if since:
        q = """SELECT url, updated_at FROM metadata_overrides
                WHERE source='notfound' AND updated_at >= ?
                ORDER BY updated_at"""
        return conn.execute(q, (since,)).fetchall()
    q = """SELECT url, updated_at FROM metadata_overrides
            WHERE source='notfound'
            ORDER BY updated_at"""
    return conn.execute(q).fetchall()


def _delete_notfound(conn: sqlite3.Connection,
                     since: Optional[str] = None) -> int:
    if since:
        cur = conn.execute(
            "DELETE FROM metadata_overrides "
            " WHERE source='notfound' AND updated_at >= ?", (since,))
    else:
        cur = conn.execute(
            "DELETE FROM metadata_overrides WHERE source='notfound'")
    conn.commit()
    return cur.rowcount or 0


def main(argv: Iterable[str] = None) -> int:
    p = argparse.ArgumentParser(
        description="Clean up bogus 'notfound' rows from metadata_overrides "
                    "so the AcoustID worker can re-try them.")
    p.add_argument("--db", default=str(_DEFAULT_DB),
                   help="Path to library.db (default: %(default)s)")
    p.add_argument("--log", default=str(_DEFAULT_LOG),
                   help="Path to acoustid-firstpass.log for HTTP 5xx "
                        "summary (default: %(default)s in CWD)")
    p.add_argument("--all", action="store_true",
                   help="Delete EVERY 'notfound' row. Worker will re-run "
                        "all of them; legitimate misses get re-cached.")
    p.add_argument("--since", metavar="TIMESTAMP",
                   help="Delete only 'notfound' rows updated at or after "
                        "this SQLite-format timestamp "
                        "(e.g. '2026-05-25 14:30:00')")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be deleted without acting")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip the confirmation prompt")
    args = p.parse_args(argv)

    if args.all and args.since:
        print("ERROR: --all and --since are mutually exclusive",
              file=sys.stderr)
        return 2

    db_path  = Path(args.db).expanduser().resolve()
    log_path = Path(args.log).expanduser().resolve()
    try:
        conn = _connect(db_path)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"DB:  {db_path}")
    print(f"Log: {log_path}")
    print()

    try:
        _report_stats(conn)
        _print_log_summary(_scan_log_for_5xx(log_path), log_path)

        if not (args.all or args.since):
            print("No action requested (use --all or --since to delete).")
            print("Common workflow when an outage was detected:")
            print(f"  1. Eyeball the log scan above to estimate scale of damage.")
            print(f"  2. If small (<50): use --since with the outage window's "
                  "earliest timestamp.")
            print(f"  3. If unclear or widespread: use --all (re-processes "
                  "every miss; legitimate ones get re-cached).")
            return 0

        candidates = _select_notfound(conn, since=args.since)
        n = len(candidates)
        scope = f"after {args.since}" if args.since else "(all)"
        print(f"Would delete {n:,} 'notfound' row(s) {scope}.")
        if candidates and (args.dry_run or not args.yes):
            print("First 10:")
            for r in candidates[:10]:
                print(f"  [{r['updated_at']}]  {r['url'][:90]}")
            print()

        if args.dry_run:
            print("Dry-run; no deletions executed.")
            return 0
        if n == 0:
            print("Nothing to delete.")
            return 0
        if not args.yes:
            try:
                ans = input(f"Proceed with deleting {n:,} row(s)? [y/N] "
                            ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans != "y":
                print("Aborted.")
                return 1

        deleted = _delete_notfound(conn, since=args.since)
        print(f"\nDeleted {deleted:,} 'notfound' row(s).")
        print(f"These URLs will be re-processed on the next "
              f"`ACOUSTID_FETCHER.run_once()` invocation.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
