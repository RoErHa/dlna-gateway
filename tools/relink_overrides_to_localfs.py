#!/usr/bin/env python3
"""
tools/relink_overrides_to_localfs.py — recover ORPHANED metadata_overrides
after the AssetUPnP decommission.

The 2026-07-12 completeness audit found 10,859 of 10,890 manual override
rows keyed on dead URLs: 10,810 on AssetUPnP `:26125` URLs (the
improve_song_years / correct_year_drift original-year corrections, merged
with old AcoustID-era fields) and 49 on the pre-cutover `:8201` LocalFs
port. All of them have been INERT since the decommission — the overrides
COALESCE pass and the /api/track_meta year display match by exact URL.

Two recovery modes, deliberately different:

1. **Port-heal** (`…:<oldport>/localfs/stream/<id>` orphans): the track id
   in the URL is the stable LocalFs obj_id, so this is an EXACT same-file
   match — the whole row is repointed at the track's current URL, fields
   intact.

2. **Year transplant** (all other manual orphans): the row's artist/title
   are matched (normalised) against current LocalFs tracks and ONLY THE
   YEAR is transplanted onto every matching track's URL — as a fresh
   year-only `source='manual'` row, or by filling an existing row whose
   year is NULL. The orphan's artist/album/title/genre fields are
   **deliberately dropped**: they are mostly AcoustID-era values that the
   improve_song_years merge carried along, and relinking them would re-lay
   stale metadata over beets' file tags on the next rescan (the exact
   masking problem post_beets_reindex.py exists to fight). The year is
   safe — it is display-only (never COALESCEd into `tracks`) and is a
   per-recording fact, which is also why a multi-match fans out to ALL
   matching tracks (same semantic as improve_song_years). Consequence:
   any genuine pre-decommission hand edit to artist/title text is not
   recovered — indistinguishable from the merged AcoustID fields.

Never overwrites: an existing override row with a non-NULL year always
wins. Transplanted orphans are deleted (the information has moved);
unmatched manual orphans are kept (inert, harmless) unless
--prune-unmatched. Orphaned `notfound` rows are pruned by default — they
are sticky-negative markers for URLs that can never come back
(--no-prune-notfound to keep them).

Idempotent: a second run finds no transplantable orphans and no-ops.

DRY-RUN BY DEFAULT. Pass --apply to mutate. Backs up library.db first
unless --no-backup.

Usage:
    python3 tools/relink_overrides_to_localfs.py            # dry-run
    python3 tools/relink_overrides_to_localfs.py --apply    # backup + commit
    python3 tools/relink_overrides_to_localfs.py --apply -y
    python3 tools/relink_overrides_to_localfs.py --db /path/to/library.db
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time
import unicodedata
from typing import Optional

_LOCALFS_STREAM_RE = re.compile(r"/localfs/stream/([0-9a-f]+)$")

# A (artist, title) key matching more than this many tracks is almost
# certainly junk metadata ("track", ""), not a popular song — skip it.
_FANOUT_CAP = 25

_YEAR_MIN, _YEAR_MAX = 1000, 2100


def _norm(s: Optional[str]) -> str:
    """Diacritic-strip + smart-quote→ASCII + lower + whitespace-collapse.
    Mirrors dlna_library._norm_title so AcoustID-corrected vs beets-raw
    tags match."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = (s.replace("’", "'").replace("‘", "'")
           .replace("“", '"').replace("”", '"'))
    return re.sub(r"\s+", " ", s.lower().strip())


def _build_track_indexes(conn: sqlite3.Connection):
    """Return (by_obj_id url map, by_at key → [urls]) over LocalFs tracks."""
    by_id: dict = {}
    by_at: dict = {}
    for r in conn.execute(
            "SELECT obj_id, url, artist, title FROM tracks "
            "WHERE udn LIKE 'uuid:localfs-%' ORDER BY url"):
        if r["obj_id"]:
            by_id.setdefault(r["obj_id"], r["url"])
        key = (_norm(r["artist"]), _norm(r["title"]))
        if key[0] and key[1]:
            by_at.setdefault(key, []).append(r["url"])
    return by_id, by_at


def plan_relink(conn: sqlite3.Connection) -> dict:
    """Compute everything without mutating. Returns
    {heal: [(old_url, new_url)], heal_taken: [old_url],
     transplants: [(orphan_url, year, [target_urls])],
     fill: [(target_url, year)], insert: [(target_url, year)],
     prune_notfound: [url], unmatched: [url],
     stats: {...}}."""
    by_id, by_at = _build_track_indexes(conn)

    existing = {r["url"]: r["year"] for r in conn.execute(
        "SELECT url, year FROM metadata_overrides")}

    orphans = conn.execute(
        "SELECT url, artist, album, title, year, source "
        "FROM metadata_overrides "
        "WHERE url NOT IN (SELECT url FROM tracks)").fetchall()

    heal: list = []
    heal_taken: list = []
    transplants: list = []
    fill: list = []
    insert: list = []
    prune_notfound: list = []
    unmatched: list = []
    stats = {"orphans": len(orphans), "healed": 0, "heal_taken": 0,
             "transplanted": 0, "targets_insert": 0, "targets_fill": 0,
             "targets_had_year": 0, "fanout_capped": 0,
             "no_key": 0, "no_match": 0, "notfound_pruned": 0}
    # Targets claimed within this plan so two orphans can't both insert
    # for the same URL (first wins; PK would reject the second anyway).
    claimed: set = set()

    for o in orphans:
        if o["source"] == "notfound":
            prune_notfound.append(o["url"])
            stats["notfound_pruned"] += 1
            continue

        # Mode 1 — port-heal: old LocalFs URL, exact same-file id match.
        m = _LOCALFS_STREAM_RE.search(o["url"])
        if m and m.group(1) in by_id:
            new_url = by_id[m.group(1)]
            if new_url in existing or new_url in claimed:
                heal_taken.append(o["url"])
                stats["heal_taken"] += 1
            else:
                claimed.add(new_url)
                heal.append((o["url"], new_url))
                stats["healed"] += 1
            continue

        # Mode 2 — year transplant.
        year = o["year"]
        if year is None or not (_YEAR_MIN <= year <= _YEAR_MAX):
            unmatched.append(o["url"])
            stats["no_key"] += 1
            continue
        key = (_norm(o["artist"]), _norm(o["title"]))
        if not key[0] or not key[1]:
            unmatched.append(o["url"])
            stats["no_key"] += 1
            continue
        targets = by_at.get(key, [])
        if not targets:
            unmatched.append(o["url"])
            stats["no_match"] += 1
            continue
        if len(targets) > _FANOUT_CAP:
            unmatched.append(o["url"])
            stats["fanout_capped"] += 1
            continue

        applied = []
        for t in targets:
            if t in claimed:
                continue
            if t in existing:
                if existing[t] is None:
                    claimed.add(t)
                    fill.append((t, year))
                    stats["targets_fill"] += 1
                    applied.append(t)
                else:
                    stats["targets_had_year"] += 1
            else:
                claimed.add(t)
                insert.append((t, year))
                stats["targets_insert"] += 1
                applied.append(t)
        # The orphan is consumed if its year LANDED anywhere, or if every
        # target already had a year (the knowledge exists — row is spent).
        transplants.append((o["url"], year, applied))
        stats["transplanted"] += 1

    return {"heal": heal, "heal_taken": heal_taken,
            "transplants": transplants, "fill": fill, "insert": insert,
            "prune_notfound": prune_notfound, "unmatched": unmatched,
            "stats": stats}


def apply_plan(conn: sqlite3.Connection, plan: dict,
               prune_notfound: bool, prune_unmatched: bool) -> None:
    with conn:  # single transaction
        for (old_url, new_url) in plan["heal"]:
            conn.execute(
                "UPDATE OR IGNORE metadata_overrides "
                "SET url=?, updated_at=datetime('now') WHERE url=?",
                (new_url, old_url))
        for (url, year) in plan["fill"]:
            conn.execute(
                "UPDATE metadata_overrides "
                "SET year=?, updated_at=datetime('now') WHERE url=?",
                (year, url))
        for (url, year) in plan["insert"]:
            conn.execute(
                "INSERT OR IGNORE INTO metadata_overrides "
                "(url, year, source, updated_at) "
                "VALUES (?, ?, 'manual', datetime('now'))",
                (url, year))
        # Delete consumed orphans — their year now lives on live URLs.
        for (orphan_url, _year, _applied) in plan["transplants"]:
            conn.execute("DELETE FROM metadata_overrides WHERE url=?",
                         (orphan_url,))
        if prune_notfound:
            for url in plan["prune_notfound"]:
                conn.execute("DELETE FROM metadata_overrides WHERE url=?",
                             (url,))
        if prune_unmatched:
            for url in plan["unmatched"]:
                conn.execute("DELETE FROM metadata_overrides WHERE url=?",
                             (url,))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="library.db", help="path to library.db")
    ap.add_argument("--apply", action="store_true",
                    help="actually mutate (default: dry-run)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmation prompt")
    ap.add_argument("--no-prune-notfound", action="store_true",
                    help="keep orphaned notfound rows (default: prune)")
    ap.add_argument("--prune-unmatched", action="store_true",
                    help="also delete manual orphans with no LocalFs match "
                         "(default: keep — they are inert)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the library.db backup before --apply")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: {args.db} not found", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    plan = plan_relink(conn)
    s = plan["stats"]
    prune_nf = not args.no_prune_notfound

    print(f"orphaned metadata_overrides rows          : {s['orphans']}")
    print(f"  port-heal (exact LocalFs id, full row)  : {s['healed']}"
          f"  (+{s['heal_taken']} target already has a row — skipped)")
    print(f"  year transplants (orphans consumed)     : {s['transplanted']}")
    print(f"    → new year-only rows inserted         : {s['targets_insert']}")
    print(f"    → existing rows year-filled           : {s['targets_fill']}")
    print(f"    → targets skipped (already have year) : {s['targets_had_year']}")
    print(f"  unmatched (kept{', use --prune-unmatched to drop' if not args.prune_unmatched else ' → WILL PRUNE'}):")
    print(f"    no usable year or artist/title key    : {s['no_key']}")
    print(f"    no LocalFs (artist,title) match       : {s['no_match']}")
    print(f"    fan-out over {_FANOUT_CAP} tracks (junk key)      : {s['fanout_capped']}")
    print(f"  orphaned notfound rows                  : {s['notfound_pruned']}"
          f"  ({'will prune' if prune_nf else 'kept; --no-prune-notfound'})")

    if not args.apply:
        print("\nDRY-RUN — nothing changed. Re-run with --apply to commit.")
        return 0

    if not args.yes:
        resp = input("\nApply these changes to library.db? [y/N] ").strip().lower()
        if resp != "y":
            print("aborted.")
            return 1

    if not args.no_backup:
        bak = f"{args.db}.bak-ovrelink-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(args.db, bak)
        print(f"backup: {bak}")

    apply_plan(conn, plan, prune_nf, args.prune_unmatched)
    msg = (f"done: healed {s['healed']}, transplanted {s['transplanted']} "
           f"orphans onto {s['targets_insert'] + s['targets_fill']} live URLs")
    if prune_nf:
        msg += f", pruned {len(plan['prune_notfound'])} notfound rows"
    if args.prune_unmatched:
        msg += f", pruned {len(plan['unmatched'])} unmatched orphans"
    print(msg + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
