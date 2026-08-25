#!/usr/bin/env python3
"""
tools/rotate_fixes.py — keep FIXES.md to the newest N entries.

FIXES.md is a ROLLING log: the three most recent fix write-ups, newest first.
Older ones are dropped, not archived — every entry is headed by the commit
sha that carries the fix (`## <sha> — YYYYMMDD — <title>`), so a rotated-out
entry is still reachable in git history at exactly that commit. That is why
the sha is part of the heading and not decoration.

Structure this expects:

    # Fixes
    <preamble — kept verbatim>
    ---
    ## <sha> — <YYYYMMDD> — <title>     <- newest
    ...
    ## <sha> — <YYYYMMDD> — <title>     <- oldest

Entries are separated by `## ` headings at the top level. The preamble is
everything before the first one and is never touched.

DRY-RUN BY DEFAULT — prints what would go. `--apply` rewrites the file.

Usage:
    python3 tools/rotate_fixes.py                 # what would be dropped
    python3 tools/rotate_fixes.py --apply         # do it
    python3 tools/rotate_fixes.py --keep 5 --apply
    python3 tools/rotate_fixes.py --check         # exit 1 if over the limit
"""
from __future__ import annotations

import argparse
import os
import re
import sys

KEEP_DEFAULT = 3

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "FIXES.md")

# `## <sha> — <date> — <title>`. The dash may be an em-dash or a hyphen, and
# the title is optional, so a heading written slightly differently still
# rotates instead of silently being treated as prose.
_HEADING = re.compile(r"^##\s+(\S+)\s*[—-]\s*(\d{8})\b\s*[—-]?\s*(.*)$")


def split_entries(text: str) -> tuple[str, list[dict]]:
    """Return `(preamble, entries)`. Each entry is
    `{sha, date, title, body}` where `body` is the full text INCLUDING its
    heading, so joining them back is lossless.

    Pure, because the whole risk here is deleting the wrong half of a file."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if _HEADING.match(ln.rstrip("\n"))]
    if not starts:
        return text, []
    preamble = "".join(lines[:starts[0]])
    entries = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        m = _HEADING.match(lines[i].rstrip("\n"))
        entries.append({"sha": m.group(1), "date": m.group(2),
                        "title": m.group(3).strip(),
                        "body": "".join(lines[i:end])})
    return preamble, entries


def rotate(text: str, keep: int) -> tuple[str, list[dict]]:
    """Return `(new_text, dropped_entries)`. Entries are kept in the order
    they already appear — the file is authored newest-first and this does NOT
    re-sort by date, because a hand-edited ordering is intentional and a
    silent reshuffle would be a nasty surprise in a doc people read top-down."""
    preamble, entries = split_entries(text)
    if len(entries) <= keep:
        return text, []
    kept, dropped = entries[:keep], entries[keep:]
    body = "".join(e["body"] for e in kept)
    # Keep exactly one trailing newline; entry bodies already carry their own
    # blank-line separation.
    return preamble + body.rstrip("\n") + "\n", dropped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", default=_DEFAULT_PATH)
    ap.add_argument("--keep", type=int, default=KEEP_DEFAULT)
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the file (default: report only)")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the file holds more than --keep entries")
    args = ap.parse_args(argv)

    if args.keep < 1:
        print("✗ --keep must be at least 1", file=sys.stderr)
        return 2
    if not os.path.exists(args.path):
        print(f"✗ no such file: {args.path}", file=sys.stderr)
        return 2

    with open(args.path, encoding="utf-8") as f:
        text = f.read()

    _, entries = split_entries(text)
    if not entries:
        print(f"✗ no `## <sha> — <YYYYMMDD>` entries found in {args.path}",
              file=sys.stderr)
        return 2

    print(f"{args.path}: {len(entries)} entr{'y' if len(entries)==1 else 'ies'}, "
          f"keeping {args.keep}")
    for n, e in enumerate(entries):
        mark = "keep" if n < args.keep else "DROP"
        print(f"  [{mark}] {e['sha']} {e['date']} {e['title'][:60]}")

    new_text, dropped = rotate(text, args.keep)
    if args.check:
        if dropped:
            print(f"\n✗ {len(dropped)} entr{'y' if len(dropped)==1 else 'ies'} "
                  f"over the limit — run with --apply")
            return 1
        print("\n✓ within the limit")
        return 0
    if not dropped:
        print("\n✓ nothing to rotate")
        return 0
    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply.")
        print("Dropped entries stay reachable in git at the commit each names.")
        return 0

    with open(args.path, "w", encoding="utf-8") as f:
        f.write(new_text)
    print(f"\n✓ rotated — dropped {len(dropped)}: "
          + ", ".join(e["sha"] for e in dropped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
