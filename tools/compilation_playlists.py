#!/usr/bin/env python3
"""Create playlists for scattered compilation albums.

Some compilations ("2 meter sessies", "Billboard Top 100 of 1970") exist
in the library only as an album TAG shared by tracks that live in many
different folders — one per contributing artist. Folder-based album
grouping (album_key) can't reunite them, so they're invisible as albums.
This tool finds them and creates one playlist per compilation, named
after the album tag, with the tracks ordered artist → title.

What counts as a scattered compilation (defaults tuned on the real
library, 2026-07-03):

  * >= --min-tracks tracks share the exact album tag        (default 5)
  * by >= --min-artists distinct artists                    (default 3)
  * and NO single folder holds >= --max-per-folder of them  (default 5)

The artist floor keeps single-artist albums (Supertramp "Paris") out;
the per-folder ceiling keeps generic-title collisions out ("Greatest
Hits" is 20 different artists' SEPARATE albums — each folder holds a
coherent chunk, so it's excluded).

Existing playlists are never touched: a candidate whose name matches an
existing playlist (case-insensitive) is skipped, so re-running after new
rips only adds what's new. DRY-RUN by default; --apply mutates.

    python3 tools/compilation_playlists.py               # preview
    python3 tools/compilation_playlists.py --apply       # create them
    python3 tools/compilation_playlists.py --min-tracks 8
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)

CANDIDATE_SQL = """
WITH per_folder AS (
    SELECT album, album_key, artist,
           COUNT(*) OVER (PARTITION BY album, album_key) AS cnt
    FROM tracks
    WHERE album != '' AND album IS NOT NULL AND udn = :udn
)
SELECT album,
       COUNT(*)                  AS n,
       COUNT(DISTINCT artist)    AS artists,
       COUNT(DISTINCT album_key) AS folders,
       MAX(cnt)                  AS max_per_folder
FROM per_folder
GROUP BY album
HAVING n >= :min_tracks
   AND artists >= :min_artists
   AND max_per_folder < :max_per_folder
ORDER BY n DESC
"""


def find_candidates(db, udn, *, min_tracks=5, min_artists=3,
                    max_per_folder=5):
    """Scattered-compilation album tags with their stats, most tracks
    first. Pure read."""
    with db._pool.read() as conn:
        rows = conn.execute(CANDIDATE_SQL, {
            "udn": udn, "min_tracks": min_tracks,
            "min_artists": min_artists,
            "max_per_folder": max_per_folder}).fetchall()
    return [dict(r) for r in rows]


def split_existing(db, candidates):
    """Partition candidates into (new, skipped-as-existing) by
    case-insensitive playlist-name match."""
    existing = {p["name"].strip().lower() for p in db.pl_list()}
    new, skipped = [], []
    for c in candidates:
        (skipped if c["album"].strip().lower() in existing
         else new).append(c)
    return new, skipped


def compilation_tracks(db, udn, album):
    """The candidate's tracks in playlist order (artist → title)."""
    with db._pool.read() as conn:
        rows = conn.execute(
            "SELECT url, title, artist, album, duration, art FROM tracks "
            "WHERE udn=? AND album=? "
            "ORDER BY artist COLLATE NOCASE, title COLLATE NOCASE",
            (udn, album)).fetchall()
    return [dict(r) for r in rows]


def create_playlist(db, udn, album):
    """Create the playlist and add every track. Returns (pl_id, added)."""
    pid = db.pl_create(album)
    added = sum(1 for t in compilation_tracks(db, udn, album)
                if db.pl_add_track(pid, t))
    return pid, added


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Create playlists for scattered compilation albums "
                    "(dry-run by default).")
    ap.add_argument("--db", default=os.path.join(PROJECT, "library.db"))
    ap.add_argument("--apply", action="store_true",
                    help="actually create the playlists")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="preview only (the default)")
    ap.add_argument("--min-tracks", type=int, default=5,
                    help="minimum tracks sharing the album tag (default 5)")
    ap.add_argument("--min-artists", type=int, default=3,
                    help="minimum distinct artists (default 3)")
    ap.add_argument("--max-per-folder", type=int, default=5,
                    help="exclude when any one folder holds this many or "
                         "more of the tracks (default 5)")
    args = ap.parse_args(argv)
    if args.dry_run and args.apply:
        ap.error("--apply and --dry-run are mutually exclusive")
    apply = args.apply

    os.environ.setdefault("GATEWAY_NO_SERVICES", "1")
    from dlna_library import LibraryDB
    db = LibraryDB(db_file=args.db)
    udn = db.primary_udn()
    if not udn:
        print("no tracks in the library — nothing to do")
        return 0

    cands = find_candidates(db, udn, min_tracks=args.min_tracks,
                            min_artists=args.min_artists,
                            max_per_folder=args.max_per_folder)
    new, skipped = split_existing(db, cands)

    for c in skipped:
        print(f"skip (playlist exists): {c['album']}  [{c['n']} tracks]")
    if not new:
        print("no new compilations found")
        return 0

    for c in new:
        tag = "create" if apply else "would create"
        print(f"{tag}: {c['album']!r} — {c['n']} tracks, "
              f"{c['artists']} artists, {c['folders']} folders")
        if apply:
            pid, added = create_playlist(db, udn, c["album"])
            print(f"    → playlist {pid} with {added} tracks")

    if not apply:
        print(f"\nDRY-RUN: {len(new)} playlist(s) would be created — "
              f"re-run with --apply")
    else:
        print(f"\ncreated {len(new)} playlist(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
