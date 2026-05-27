#!/usr/bin/env python3
"""
relink_orphan_overrides.py — recover orphan metadata_overrides rows
after an AssetUPnP rescan rotated co-hashes.

AssetUPnP URLs are shaped `…/c2/b16/f44100/d<track-id>-co<container-hash>.ext`.
The `d-id` is content-stable across rescans; the `co-hash` is NOT —
AssetUPnP rotates it when it reorganises its container tree (which it
does on any meaningful rescan). After such a rescan, every
`metadata_overrides` row references an OLD URL the indexer no longer
sees, and the tracks table fills with new URLs that have no override.

This tool matches orphans to current bare tracks by `d-id` and
rewrites `metadata_overrides.url` to the current URL. Idempotent.

Surfaced 2026-05-27 when a duplicate-cleanup-driven AssetUPnP rescan
left 37,943 metadata_overrides rows orphaned; the d-id relink
recovered 19,149 of them (the other 18,794 were genuinely
trashed-file casualties, since pruned).

Usage:
    python3 tools/relink_orphan_overrides.py            # dry-run, default
    python3 tools/relink_orphan_overrides.py --apply    # write the relinks
    python3 tools/relink_orphan_overrides.py --db /path/to/library.db --apply
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO / "library.db"

_D_RE = re.compile(r"/(d-?\d+)-co")


def _d_id(url: str):
    """Extract the d-id portion of an AssetUPnP URL. None if not present."""
    if not url:
        return None
    m = _D_RE.search(url)
    return m.group(1) if m else None


def relink(conn: sqlite3.Connection, apply: bool) -> dict:
    """Return statistics about the relink, optionally applying it.
    Stats: {bare_tracks, orphans, relinked, no_d, no_match, ambiguous, conflict}"""
    # New URLs available, indexed by d-id. Tracks with no override yet.
    cur = conn.execute("""
        SELECT t.url FROM tracks t
         WHERE t.url != ''
           AND NOT EXISTS (SELECT 1 FROM metadata_overrides m WHERE m.url = t.url)
    """)
    new_urls = {}
    ambig_d  = set()
    bare_n   = 0
    for (url,) in cur.fetchall():
        bare_n += 1
        d = _d_id(url)
        if not d:
            continue
        if d in new_urls and new_urls[d] != url:
            ambig_d.add(d)
        new_urls[d] = url

    # Orphan overrides — URL not in tracks.
    orphans = conn.execute("""
        SELECT m.url FROM metadata_overrides m
         WHERE NOT EXISTS (SELECT 1 FROM tracks t WHERE t.url = m.url)
    """).fetchall()

    stats = {
        "bare_tracks": bare_n,
        "orphans":     len(orphans),
        "relinked":    0,
        "no_d":        0,
        "no_match":    0,
        "ambiguous":   0,
        "conflict":    0,
    }
    if not orphans:
        return stats

    # Walk orphans. Track which new_urls we've consumed so a second
    # orphan with the same d-id can't claim the same new URL.
    consumed = set()
    for (old_url,) in orphans:
        d = _d_id(old_url)
        if not d:
            stats["no_d"] += 1
            continue
        if d in ambig_d:
            stats["ambiguous"] += 1
            continue
        if d not in new_urls:
            stats["no_match"] += 1
            continue
        new_url = new_urls[d]
        if new_url in consumed:
            stats["ambiguous"] += 1
            continue
        if apply:
            try:
                conn.execute(
                    "UPDATE metadata_overrides SET url=?, "
                    "       updated_at=datetime('now') WHERE url=?",
                    (new_url, old_url))
            except sqlite3.IntegrityError:
                stats["conflict"] += 1
                continue
        consumed.add(new_url)
        stats["relinked"] += 1

    return stats


def main(argv: Iterable[str] = None) -> int:
    p = argparse.ArgumentParser(
        description="Recover orphan metadata_overrides rows after an "
                    "AssetUPnP rescan rotated co-hashes.")
    p.add_argument("--db", default=str(_DEFAULT_DB),
                   help="Path to library.db (default: %(default)s)")
    p.add_argument("--apply", action="store_true",
                   help="Actually rewrite metadata_overrides.url. "
                        "Default is dry-run.")
    args = p.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"ERROR: library.db not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    print(f"DB: {db_path}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print()
    try:
        stats = relink(conn, apply=args.apply)
        if args.apply:
            conn.commit()
    finally:
        conn.close()

    print(f"bare tracks (no override):  {stats['bare_tracks']:,}")
    print(f"orphan overrides:           {stats['orphans']:,}")
    print()
    print(f"  relinked:       {stats['relinked']:,}")
    print(f"  no d-id in URL: {stats['no_d']:,}")
    print(f"  no bare match:  {stats['no_match']:,}  "
          f"(file likely trashed / removed)")
    print(f"  ambiguous:      {stats['ambiguous']:,}")
    print(f"  conflict:       {stats['conflict']:,}")
    if not args.apply:
        print("\nDry-run — pass --apply to write changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
