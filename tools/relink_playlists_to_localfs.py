#!/usr/bin/env python3
"""
⚠️  OBSOLETE ON 2.0 — KEPT FOR HISTORY, DO NOT RUN. This was the one-time
migration that repointed AssetUPnP-era playlists onto RoHaLocalFS at the 2.0
cutover; it already ran (2026-05-31). AssetUPnP is decommissioned and LocalFs is
the only backend now, so there are no dead UPnP playlist URLs left to relink —
it cannot do anything useful in the current configuration.

tools/relink_playlists_to_localfs.py — repoint playlists at LocalFs after
AssetUPnP (or any UPnP server) is decommissioned.

When the UPnP backend is switched off, every `playlist_tracks` row still
holds its now-dead `http://<host>:<port>/...` URL, so playback times out.
This rewrites each row's `url` (and `art`) to the matching RoHaLocalFS
track, matched by NORMALISED metadata:

  1. (artist, album, title)  — strong
  2. (artist, title)         — song-level (album differs: compilation vs
                               original, AcoustID-corrected album, etc.)

Rows with NO LocalFs match are REMOVED — the migration consequence: those
files simply aren't in the LocalFs library (LocalFs is a subset of what
AssetUPnP served). Rows that would collide on UNIQUE(pl_id, url) after a
relink (two source rows mapping to the same LocalFs track in one playlist)
are also removed as duplicates.

Also prunes `album_favourites` that no longer match any LocalFs album
(track_count would be 0), for the same reason.

A "LocalFs row" is any url containing `/localfs/stream/` — already-relinked
rows are left untouched, so the tool is idempotent.

DRY-RUN BY DEFAULT. Pass --apply to mutate. Always back up library.db first
(the tool refuses to run if it can't, unless --no-backup).

Usage:
    python3 tools/relink_playlists_to_localfs.py                 # dry-run
    python3 tools/relink_playlists_to_localfs.py --apply          # do it
    python3 tools/relink_playlists_to_localfs.py --apply -y       # no prompt
    python3 tools/relink_playlists_to_localfs.py --db /path/to/library.db
    python3 tools/relink_playlists_to_localfs.py --apply --no-prune-favs
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

_LOCALFS_MARK = "/localfs/stream/"


def _norm(s: Optional[str]) -> str:
    """Diacritic-strip + smart-quote→ASCII + lower + whitespace-collapse.
    Mirrors dlna_library._norm_title so AcoustID-corrected vs raw tags
    match."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = (s.replace("’", "'").replace("‘", "'")
           .replace("“", '"').replace("”", '"'))
    return re.sub(r"\s+", " ", s.lower().strip())


def _build_localfs_index(conn: sqlite3.Connection):
    """Return (by_aat, by_at): normalised-key → (url, art). First match
    wins (deterministic via ORDER BY url)."""
    by_aat: dict = {}
    by_at: dict = {}
    rows = conn.execute(
        "SELECT artist, album, title, url, art FROM tracks "
        "WHERE udn LIKE 'uuid:localfs-%' ORDER BY url").fetchall()
    for r in rows:
        a, al, t = _norm(r["artist"]), _norm(r["album"]), _norm(r["title"])
        by_aat.setdefault((a, al, t), (r["url"], r["art"]))
        by_at.setdefault((a, t), (r["url"], r["art"]))
    return by_aat, by_at


def plan_playlist_relink(conn: sqlite3.Connection) -> dict:
    """Compute relinks + removals without mutating. Returns a dict with
    `relink` [(id, new_url, new_art, kind)], `remove` [(id, reason)], and
    per-playlist / kind counters."""
    by_aat, by_at = _build_localfs_index(conn)
    rows = conn.execute(
        "SELECT id, pl_id, url, artist, album, title FROM playlist_tracks "
        "WHERE url NOT LIKE ?", (f"%{_LOCALFS_MARK}%",)).fetchall()

    relink: list = []
    remove: list = []
    # Track (pl_id, new_url) already claimed so we don't create UNIQUE
    # collisions — second claimant is removed as a duplicate.
    claimed: set = set()
    seen_localfs = {
        pid: set() for (pid,) in conn.execute(
            "SELECT DISTINCT pl_id FROM playlist_tracks").fetchall()}
    # Pre-seed with URLs already on LocalFs so a relink can't collide with
    # an existing good row.
    for r in conn.execute(
            "SELECT pl_id, url FROM playlist_tracks WHERE url LIKE ?",
            (f"%{_LOCALFS_MARK}%",)).fetchall():
        seen_localfs.setdefault(r["pl_id"], set()).add(r["url"])

    stats = {"strong": 0, "song": 0, "removed_nomatch": 0,
             "removed_dup": 0, "total": len(rows)}
    per_pl: dict = {}

    for r in rows:
        a, al, t = _norm(r["artist"]), _norm(r["album"]), _norm(r["title"])
        hit = by_aat.get((a, al, t))
        kind = "strong"
        if hit is None:
            hit = by_at.get((a, t))
            kind = "song"
        pid = r["pl_id"]
        per_pl.setdefault(pid, {"relinked": 0, "removed": 0})
        if hit is None:
            remove.append((r["id"], "no_localfs_match"))
            stats["removed_nomatch"] += 1
            per_pl[pid]["removed"] += 1
            continue
        new_url, new_art = hit
        key = (pid, new_url)
        if new_url in seen_localfs.get(pid, set()) or key in claimed:
            remove.append((r["id"], "duplicate_after_relink"))
            stats["removed_dup"] += 1
            per_pl[pid]["removed"] += 1
            continue
        claimed.add(key)
        relink.append((r["id"], new_url, new_art or "", kind))
        stats[kind] += 1
        per_pl[pid]["relinked"] += 1

    return {"relink": relink, "remove": remove, "stats": stats,
            "per_pl": per_pl}


def plan_album_fav_prune(conn: sqlite3.Connection) -> list:
    """Album favourites with no matching LocalFs tracks (album_key when
    set, else artist+album). Returns [(artist, album, album_key)]."""
    favs = conn.execute(
        "SELECT artist, album, album_key FROM album_favourites").fetchall()
    orphans = []
    for f in favs:
        if f["album_key"]:
            row = conn.execute(
                "SELECT 1 FROM tracks WHERE udn LIKE 'uuid:localfs-%' "
                "AND album_key=? LIMIT 1", (f["album_key"],)).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM tracks WHERE udn LIKE 'uuid:localfs-%' "
                "AND artist=? AND album=? LIMIT 1",
                (f["artist"], f["album"])).fetchone()
        if row is None:
            orphans.append((f["artist"], f["album"], f["album_key"]))
    return orphans


def apply_plan(conn: sqlite3.Connection, pl_plan: dict,
               fav_orphans: list, prune_favs: bool) -> None:
    with conn:  # single transaction
        for (rid, new_url, new_art, _kind) in pl_plan["relink"]:
            conn.execute(
                "UPDATE OR IGNORE playlist_tracks SET url=?, art=? WHERE id=?",
                (new_url, new_art, rid))
        # Remove explicit no-match/dup rows AND any row that still isn't on
        # LocalFs (e.g. an UPDATE that hit OR IGNORE on a late collision).
        for (rid, _reason) in pl_plan["remove"]:
            conn.execute("DELETE FROM playlist_tracks WHERE id=?", (rid,))
        conn.execute(
            "DELETE FROM playlist_tracks WHERE url NOT LIKE ?",
            (f"%{_LOCALFS_MARK}%",))
        if prune_favs:
            for (artist, album, album_key) in fav_orphans:
                conn.execute(
                    "DELETE FROM album_favourites "
                    "WHERE artist=? AND album=? AND album_key=?",
                    (artist, album, album_key))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="library.db", help="path to library.db")
    ap.add_argument("--apply", action="store_true",
                    help="actually mutate (default: dry-run)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmation prompt")
    ap.add_argument("--no-prune-favs", action="store_true",
                    help="don't prune orphan album_favourites")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the library.db backup before --apply")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: {args.db} not found", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    pl_plan = plan_playlist_relink(conn)
    fav_orphans = plan_album_fav_prune(conn)
    s = pl_plan["stats"]
    prune_favs = not args.no_prune_favs

    print(f"playlist_tracks needing relink (non-LocalFs): {s['total']}")
    print(f"  → relink strong (artist+album+title): {s['strong']}")
    print(f"  → relink song   (artist+title)      : {s['song']}")
    print(f"  → remove (no LocalFs match)         : {s['removed_nomatch']}")
    print(f"  → remove (duplicate after relink)   : {s['removed_dup']}")
    print(f"album_favourites with no LocalFs match : {len(fav_orphans)}"
          f"{'  (will prune)' if prune_favs else '  (kept; --no-prune-favs)'}")
    print("  per-playlist (relinked / removed):")
    for pid, c in sorted(pl_plan["per_pl"].items()):
        print(f"    {pid:16} {c['relinked']:5} / {c['removed']}")

    if not args.apply:
        print("\nDRY-RUN — nothing changed. Re-run with --apply to commit.")
        return 0

    if not args.yes:
        resp = input("\nApply these changes to library.db? [y/N] ").strip().lower()
        if resp != "y":
            print("aborted.")
            return 1

    if not args.no_backup:
        bak = f"{args.db}.bak-relink-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(args.db, bak)
        print(f"backup: {bak}")

    apply_plan(conn, pl_plan, fav_orphans, prune_favs)
    print(f"done: relinked {s['strong'] + s['song']}, "
          f"removed {s['removed_nomatch'] + s['removed_dup']} playlist rows"
          f"{f', pruned {len(fav_orphans)} album favourites' if prune_favs else ''}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
