#!/usr/bin/env python3
"""
tools/cutover_copy_userdata.py — copy 1.x user-data into the 2.x DB at cutover.

At cutover the 2.x ASGI gateway takes over 1.x's identity (same ports + UDN), so
the only thing that needs carrying is the user-data 2.x hasn't accumulated. Per
the cutover decision (project_v2_side_by_side):

  COPY:    album_art · radio_favourites · play_counts · lyrics · metadata_overrides
  EXCLUDE: playlists / playlist_tracks (= the ⭐ track favourites) · album_favourites
           — playlists/favourites are started FRESH on 2.x by choice.

Keying notes:
  • album_art is (artist, album)-keyed → backend-independent, copies cleanly.
    Policy: a real cover from 1.x WINS (INSERT OR REPLACE for source≠notfound) so
    2.x's bare/notfound albums gain 1.x's MusicBrainz covers; 1.x 'notfound' rows
    only FILL where 2.x has nothing (INSERT OR IGNORE) — never overwrite a cover.
  • radio_favourites is station_uuid-keyed → backend-independent.
  • play_counts / lyrics / metadata_overrides are URL-keyed. The LocalFs URL is
    `http://<lan-ip>:<port>/localfs/stream/<sha1(rel_path)>`. Because the smooth
    cutover gives 2.x the SAME host:port (8200) and the same files hash the same,
    the URLs line up — provided 2.x has rescanned on the adopted port first (see
    the cutover sequence in the README). `--rewrite-localfs-base OLD:NEW` rewrites
    the host:port in copied URLs if you ever need to bridge a port difference.
  • metadata_overrides: 1.x carries only 'manual' (year corrections / edits) +
    'notfound' (no acoustid rows — already cleared), so the whole table is safe to
    copy without re-masking beets (Option A).

DRY-RUN by default; --apply backs up the destination DB first, then commits.
Idempotent (additive INSERT OR IGNORE / REPLACE), so re-running is safe.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

DEFAULT_SRC = "/Users/ronhamersma/dlna-gateway/library.db"   # 1.x daily driver
DEFAULT_DST = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "library.db")               # this worktree (2.x)

# table → copy policy
COPY_SPEC = {
    "album_art":          "art",      # real-cover-wins, notfound-fill
    "radio_favourites":   "ignore",   # additive (station_uuid PK)
    "play_counts":        "ignore",   # additive (url PK)
    "lyrics":             "ignore",   # additive (url PK)
    "metadata_overrides": "ignore",   # additive (url PK); 1.x has no acoustid rows
}
# Never touched — playlists + track favourites + album favourites start fresh.
EXCLUDE = ("playlists", "playlist_tracks", "album_favourites")

_URL_COLS = ("url",)   # columns to rewrite when --rewrite-localfs-base is given


def _cols(conn: sqlite3.Connection, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _rewrite_url(val, old: str, new: str):
    if isinstance(val, str) and old in val:
        return val.replace(old, new)
    return val


def copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str,
               policy: str, *, apply: bool, rewrite=None) -> dict:
    """Copy one table src→dst under `policy`. Returns a stats dict. When
    apply=False, computes would-insert counts without writing."""
    scols = _cols(src, table)
    dcols = _cols(dst, table)
    if not scols or not dcols:
        return {"table": table, "skipped": "missing in src or dst"}
    cols = [c for c in scols if c in dcols]          # intersection, by NAME
    rows = [dict(zip(scols, r, strict=True)) for r in
            src.execute(f"SELECT {','.join(scols)} FROM {table}")]
    if rewrite:
        old, new = rewrite
        for row in rows:
            for c in _URL_COLS:
                if c in row:
                    row[c] = _rewrite_url(row[c], old, new)

    placeholders = ",".join("?" for _ in cols)
    collist = ",".join(cols)
    inserted = replaced = 0

    def _insert(verb: str, subset):
        nonlocal inserted, replaced
        sql = f"INSERT OR {verb} INTO {table} ({collist}) VALUES ({placeholders})"
        for row in subset:
            vals = [row[c] for c in cols]
            if apply:
                cur = dst.execute(sql, vals)
                if verb == "IGNORE":
                    inserted += cur.rowcount or 0
                else:
                    replaced += 1
            else:
                # dry-run: count rows whose PK isn't already present (approx for
                # IGNORE); REPLACE always writes so count them all.
                inserted += 1 if verb == "IGNORE" else 0
                replaced += 1 if verb == "REPLACE" else 0

    if policy == "art":
        real = [r for r in rows if r.get("source") != "notfound"]
        notf = [r for r in rows if r.get("source") == "notfound"]
        _insert("REPLACE", real)     # 1.x's real covers win
        _insert("IGNORE", notf)      # notfound only fills gaps
    else:
        _insert("IGNORE", rows)

    return {"table": table, "src_rows": len(rows),
            "inserted": inserted, "replaced": replaced}


def run(src_path: str, dst_path: str, *, apply: bool, backup: bool,
        rewrite=None) -> list:
    if not os.path.exists(src_path):
        print(f"✗  source DB not found: {src_path}", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(dst_path):
        print(f"✗  dest DB not found: {dst_path}", file=sys.stderr)
        sys.exit(1)

    if apply and backup:
        bak = f"{dst_path}.cutover-bak-{int(time.time())}"
        shutil.copy2(dst_path, bak)
        print(f"📦  backed up dest → {bak}")

    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    stats = []
    try:
        for table, policy in COPY_SPEC.items():
            assert table not in EXCLUDE
            stats.append(copy_table(src, dst, table, policy,
                                    apply=apply, rewrite=rewrite))
        if apply:
            dst.commit()
    finally:
        src.close()
        dst.close()
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default=DEFAULT_SRC, help="1.x library.db")
    ap.add_argument("--dest", default=DEFAULT_DST, help="2.x library.db")
    ap.add_argument("--apply", action="store_true",
                    help="commit the copy (default: dry-run)")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the dest backup on --apply")
    ap.add_argument("--rewrite-localfs-base", default="",
                    help="rewrite host:port in copied URLs, e.g. 8201:8200")
    args = ap.parse_args()

    rewrite = None
    if args.rewrite_localfs_base:
        old, new = args.rewrite_localfs_base.split(":", 1)
        rewrite = (old, new)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"\n  Cutover user-data copy [{mode}]")
    print(f"    source (1.x): {args.source}")
    print(f"    dest   (2.x): {args.dest}")
    print(f"    copy: {', '.join(COPY_SPEC)}")
    print(f"    exclude (fresh on 2.x): {', '.join(EXCLUDE)}\n")

    stats = run(args.source, args.dest, apply=args.apply,
                backup=not args.no_backup, rewrite=rewrite)
    for s in stats:
        if s.get("skipped"):
            print(f"  {s['table']:20} skipped ({s['skipped']})")
        else:
            print(f"  {s['table']:20} src={s['src_rows']:6}  "
                  f"insert={s['inserted']:6}  replace={s['replaced']:6}")
    if not args.apply:
        print("\n  (dry-run — re-run with --apply to commit)")


if __name__ == "__main__":
    main()
