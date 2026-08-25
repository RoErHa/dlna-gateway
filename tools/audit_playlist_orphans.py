#!/usr/bin/env python3
"""
tools/audit_playlist_orphans.py — find (and relink) playlist rows that point
at a track the index no longer has.

WHY THIS EXISTS (2026-08-25). `playlist_tracks` is deliberately independent of
`tracks` — that is what lets a playlist survive `clear(udn)` and a rebuild.
The cost of that independence is that nothing notices when a row goes stale.
A LocalFs track id is `sha1(rel_path)`, so *renaming a folder* — or splitting a
whole-album file into per-track files — silently orphans every playlist row
that referenced it. The files are still on disk under new ids; the playlist
still holds the old ones.

From the sofa that looks like the player "skipping songs at random": the
gateway resolves the dead id to a 404, and the browser reports an unplayable
file. (The relay no longer relays that 404 as if it were audio — see
dlna_asgi_media._audio_relay_response — but the row is still dead until it is
repaired, which is what this tool is for.)

MATCHING, strongest first — the same ladder the 2.0 migration tools used:

  1. (artist, album, title)   strong
  2. (artist, title)          song-level; the album name legitimately differs
                              between a compilation and the original release

Both sides are normalised (diacritics stripped, smart quotes folded, case and
whitespace collapsed) so a retag does not break the match.

A row that matches NOTHING is reported and, with --apply, removed only if
--remove-unmatched is given. Default is to KEEP it: an unmatched row is
usually music that is still on disk under a name this tool cannot guess, and
a playlist entry the owner can see and fix by hand beats one that vanished.

Rows that would collide on UNIQUE(pl_id, url) after a relink (the same track
already in that playlist) are removed as duplicates.

DRY-RUN BY DEFAULT. --apply backs up library.db first unless --no-backup.

Usage:
    python3 tools/audit_playlist_orphans.py                  # report
    python3 tools/audit_playlist_orphans.py --apply          # relink
    python3 tools/audit_playlist_orphans.py --apply --remove-unmatched
    python3 tools/audit_playlist_orphans.py --db /path/to/library.db
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
import unicodedata

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "library.db")


def _norm(s: str | None) -> str:
    """Diacritic-strip + smart-quote→ASCII + lower + whitespace-collapse.
    Mirrors dlna_library._norm_title so a retagged file still matches."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s))
    s = "".join(c for c in s if not unicodedata.combining(c))
    for a, b in (("’", "'"), ("‘", "'"),
                 ("“", '"'), ("”", '"'),
                 ("–", "-"), ("—", "-")):
        s = s.replace(a, b)
    return " ".join(s.lower().split())


def find_orphans(conn) -> list[dict]:
    """Playlist rows whose url has no matching `tracks` row."""
    cur = conn.execute("""
        SELECT pt.id, pt.pl_id, p.name AS pl_name, pt.url,
               pt.title, pt.artist, pt.album
          FROM playlist_tracks pt
          JOIN playlists p ON p.id = pt.pl_id
     LEFT JOIN tracks t ON t.url = pt.url
         WHERE t.url IS NULL
      ORDER BY p.name, pt.added_at, pt.id
    """)
    return [dict(r) for r in cur.fetchall()]


def build_index(conn) -> tuple[dict, dict]:
    """`tracks` keyed by (artist, album, title) and by (artist, title)."""
    strong: dict[tuple, dict] = {}
    song: dict[tuple, dict] = {}
    for r in conn.execute("SELECT url, title, artist, album, art FROM tracks"):
        row = dict(r)
        a, al, ti = _norm(row["artist"]), _norm(row["album"]), _norm(row["title"])
        if not (a and ti):
            continue
        strong.setdefault((a, al, ti), row)
        song.setdefault((a, ti), row)
    return strong, song


def plan(orphans: list[dict], strong: dict, song: dict) -> list[dict]:
    """Decide what happens to each orphan. Pure — the whole point is that the
    report you read and the mutation that runs come from the same function."""
    out = []
    for o in orphans:
        a, al, ti = _norm(o["artist"]), _norm(o["album"]), _norm(o["title"])
        hit = strong.get((a, al, ti))
        how = "strong"
        if hit is None:
            hit = song.get((a, ti))
            how = "song"
        if hit is None:
            out.append({**o, "action": "unmatched", "how": "", "new_url": ""})
        else:
            out.append({**o, "action": "relink", "how": how,
                        "new_url": hit["url"], "new_art": hit.get("art") or ""})
    return out


def apply_plan(conn, planned: list[dict], remove_unmatched: bool) -> dict:
    stats = {"relinked": 0, "dup_removed": 0, "unmatched_removed": 0,
             "unmatched_kept": 0}
    for row in planned:
        if row["action"] == "relink":
            dup = conn.execute(
                "SELECT 1 FROM playlist_tracks WHERE pl_id=? AND url=? "
                "AND id<>?", (row["pl_id"], row["new_url"], row["id"])
            ).fetchone()
            if dup:
                conn.execute("DELETE FROM playlist_tracks WHERE id=?",
                             (row["id"],))
                stats["dup_removed"] += 1
                continue
            # Fill a blank art from the matched track; never overwrite one the
            # playlist already carries (it may be a cover the owner chose).
            conn.execute(
                "UPDATE playlist_tracks SET url=?, art=COALESCE(NULLIF(art,''),?) "
                "WHERE id=?", (row["new_url"], row.get("new_art", ""), row["id"]))
            stats["relinked"] += 1
        elif remove_unmatched:
            conn.execute("DELETE FROM playlist_tracks WHERE id=?", (row["id"],))
            stats["unmatched_removed"] += 1
        else:
            stats["unmatched_kept"] += 1
    conn.commit()
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=_DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="mutate the DB (default: report only)")
    ap.add_argument("--remove-unmatched", action="store_true",
                    help="also delete rows that match nothing (default: keep)")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("-y", "--yes", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"✗ no such database: {args.db}", file=sys.stderr)
        return 2
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    orphans = find_orphans(conn)
    total = conn.execute("SELECT COUNT(*) FROM playlist_tracks").fetchone()[0]
    if not orphans:
        print(f"✓ no orphaned playlist rows ({total} rows checked)")
        return 0

    strong, song = build_index(conn)
    planned = plan(orphans, strong, song)

    cur_pl = None
    for row in planned:
        if row["pl_name"] != cur_pl:
            cur_pl = row["pl_name"]
            print(f"\n▸ {cur_pl}")
        mark = {"strong": "→ relink (artist+album+title)",
                "song":   "→ relink (artist+title)"}.get(row["how"],
                                                         "✗ NO MATCH")
        print(f"    {row['artist']} — {row['title']}")
        print(f"      {mark}")
        print(f"      old {row['url']}")
        if row["new_url"]:
            print(f"      new {row['new_url']}")

    n_rel = sum(1 for r in planned if r["action"] == "relink")
    n_un = len(planned) - n_rel
    print(f"\n{len(orphans)} orphaned row(s) of {total}: "
          f"{n_rel} relinkable, {n_un} unmatched")

    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to relink.")
        if n_un:
            print("Unmatched rows are KEPT unless --remove-unmatched is given.")
        return 0

    if not args.yes:
        try:
            if input("\nApply these changes? [y/N] ").strip().lower() != "y":
                print("aborted"); return 1
        except EOFError:
            print("aborted (no tty)"); return 1

    if not args.no_backup:
        bak = f"{args.db}.bak-plorphans-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(args.db, bak)
        print(f"backup → {bak}")

    stats = apply_plan(conn, planned, args.remove_unmatched)
    print(f"✓ relinked {stats['relinked']}, "
          f"removed {stats['dup_removed']} duplicate, "
          f"removed {stats['unmatched_removed']} unmatched, "
          f"kept {stats['unmatched_kept']} unmatched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
