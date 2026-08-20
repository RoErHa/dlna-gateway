#!/usr/bin/env python3
"""
audit_override_mismatches.py — find metadata_overrides rows whose
(artist, title) doesn't fuzzily match the tracks row they're joined
to. Surfaces past damage from d-id-collision-driven mis-relinks (see
the conversation around 2026-05-28 for the background).

Background
==========
`metadata_overrides` is keyed by URL. The relink tool moved overrides
from old URLs to new URLs by matching AssetUPnP `d-id` segments, on
the assumption d-id was content-stable. d-id is NOT content-stable —
it collides across distinct files (same album's adjacent tracks,
re-tagged copies, even unrelated songs by hash collision). So some
overrides ended up attached to the wrong track URL, with metadata
that wildly disagrees with what the track actually is.

This tool runs after the relink-tool fix is in place. For each
override row, it computes a fuzzy similarity score between the
override's (artist, title) and the joined tracks row's
(artist, title). Rows where BOTH artist AND title score below
`_FUZZY_FLOOR` are flagged as suspect.

Default action is to print the report. `--clean` deletes the suspect
rows so the AcoustID worker can re-fingerprint those tracks correctly
on its next pass (the now-bare URLs naturally enter the worker's
`bare_metadata_tracks()` set).

`manual` overrides (user edits via the PWA modal) are NEVER touched —
the user knows best.

Usage
=====
    python3 tools/audit_override_mismatches.py             # dry-run, top 30
    python3 tools/audit_override_mismatches.py --top 0     # full list
    python3 tools/audit_override_mismatches.py --clean     # interactive
    python3 tools/audit_override_mismatches.py --clean -y  # non-interactive
"""
import argparse
import re
import sqlite3
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from collections.abc import Iterable

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO / "library.db"

# Same threshold as relink_orphan_overrides.py's fuzzy-match guard, so
# the two stay in sync — if relink would now reject a pairing, the
# audit considers the existing pairing suspect for the same reason.
_FUZZY_FLOOR = 0.55


def _norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[\(\[][^)\]]*[\)\]]", " ", s)
    s = re.sub(r"[^a-zA-Z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _score(a1, a2):
    n1, n2 = _norm(a1), _norm(a2)
    if not n1 or not n2:
        return 0.0
    if n1 == n2:
        return 1.0
    return SequenceMatcher(None, n1, n2).ratio()


def find_suspects(conn: sqlite3.Connection,
                  floor: float = _FUZZY_FLOOR) -> list[dict]:
    """All overrides whose (artist, title) fuzzily disagrees with the
    joined track. Excludes source='manual' (user edits stay)."""
    conn.row_factory = sqlite3.Row
    suspects = []
    for r in conn.execute("""
        SELECT t.url, t.artist AS t_artist, t.title AS t_title,
                      m.artist AS m_artist, m.title AS m_title,
                      m.source, m.updated_at
          FROM metadata_overrides m JOIN tracks t ON t.url = m.url
         WHERE m.source IN ('acoustid')
           AND m.artist IS NOT NULL AND m.title IS NOT NULL
           AND m.artist != '' AND m.title != ''
    """):
        ar_s = _score(r["t_artist"], r["m_artist"])
        ti_s = _score(r["t_title"], r["m_title"])
        if ar_s < floor and ti_s < floor:
            # Both artist AND title disagree → almost certainly wrong.
            d = dict(r)
            d["ar_score"] = ar_s
            d["ti_score"] = ti_s
            suspects.append(d)
    return suspects


def delete_suspects(conn: sqlite3.Connection,
                    suspects: list[dict]) -> int:
    n = 0
    cur = conn.cursor()
    for s in suspects:
        cur.execute("DELETE FROM metadata_overrides WHERE url=? "
                    "AND source='acoustid'", (s["url"],))
        n += cur.rowcount or 0
    return n


def _preview(suspects, limit):
    n = len(suspects)
    if n == 0:
        print("No suspect overrides found. ✓")
        return
    print(f"Found {n:,} suspect override row(s) "
          f"(both artist AND title score < {_FUZZY_FLOOR:.2f}).\n")
    show = suspects if limit <= 0 or n <= limit else suspects[:limit]
    for s in show:
        print(f"  TRACK : '{s['t_artist']}' / '{s['t_title']}'")
        print(f"  OVERR : '{s['m_artist']}' / '{s['m_title']}'    "
              f"src={s['source']}  scores=({s['ar_score']:.2f}, "
              f"{s['ti_score']:.2f})")
        print()
    if limit > 0 and n > limit:
        print(f"  … ({n - limit:,} more not shown — pass --top 0 for all)")


def main(argv: Iterable[str] = None) -> int:
    p = argparse.ArgumentParser(
        description="Audit metadata_overrides for likely-mis-attached "
                    "rows (past damage from d-id-collision relinks).")
    p.add_argument("--db", default=str(_DEFAULT_DB),
                   help="Path to library.db (default: %(default)s)")
    p.add_argument("--top", type=int, default=30,
                   help="Preview first N suspects (0 = all; default 30)")
    p.add_argument("--clean", action="store_true",
                   help="Delete the suspect rows. Default is dry-run.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip confirmation prompt before deleting.")
    args = p.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"ERROR: library.db not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    print(f"DB: {db_path}")
    print(f"Mode: {'CLEAN' if args.clean else 'DRY-RUN'}")
    print(f"Fuzzy floor: {_FUZZY_FLOOR}")
    print()

    suspects = find_suspects(conn)
    _preview(suspects, args.top)

    if not args.clean:
        print()
        print("Dry-run — pass --clean to delete the suspect rows. After "
              "deletion the AcoustID worker will re-fingerprint those "
              "tracks on its next pass (start_initial_scan / Indexer "
              "tail trigger).")
        conn.close()
        return 0

    if not suspects:
        conn.close()
        return 0

    if not args.yes:
        print()
        try:
            ans = input(f"Delete {len(suspects):,} suspect override "
                        f"row(s)? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            ans = "n"
        if ans not in ("y", "yes"):
            print("Aborted.")
            conn.close()
            return 1

    try:
        with conn:
            n = delete_suspects(conn, suspects)
        print(f"\nDeleted {n:,} override row(s). "
              f"They will be re-fingerprinted on the next AcoustID worker pass.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
