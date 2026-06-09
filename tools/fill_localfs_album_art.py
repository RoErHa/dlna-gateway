#!/usr/bin/env python3
"""Fill cover art for folder-keyed RoHaLocalFS albums that have NO embedded art.

RoHaLocalFS only sets ``tracks.art`` (to ``/localfs/art/<id>``) when a file has
an embedded cover. A folder album where **no** track has embedded art shows
blank everywhere — the PWA and Amperfy both resolve an album's cover by
``album_key`` → ``tracks.art`` (see ``api_subsonic._cover_art_candidates``), so
with no art on any track there's nothing to serve.

This tool finds those art-less folder albums, looks up a cover via MusicBrainz +
Cover Art Archive (reusing ``dlna_art_fetcher._mb_lookup_cover``), and on a hit
writes the ``coverartarchive.org`` URL onto **all** of the album's tracks
(``tracks.art``) so the folder-album ``getCoverArt`` + the PWA ``/art`` proxy
serve it. (``art_fetch`` follows the CAA ``front-500`` 307 redirect, so the URL
resolves end-to-end.) Misses are cached in ``album_art`` as ``source='notfound'``
(sticky) so re-runs skip them; prior hits in ``album_art`` are reused without a
new MB call. Rate-limited to MusicBrainz's 1.1 s ToS.

A DB data change (not code) — the gateway reads ``tracks.art`` per request, so
new covers show **without a restart**.

DRY-RUN by default (reports candidates + cache state, makes NO network calls and
NO writes). ``--apply`` does the MB lookups + writes, backing up ``library.db``
first.

    python3 tools/fill_localfs_album_art.py                 # preview
    python3 tools/fill_localfs_album_art.py --apply          # fetch + fill
    python3 tools/fill_localfs_album_art.py --apply --limit 5
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def artless_folder_albums(conn: sqlite3.Connection):
    """Return [(album_key, artist, album)] for folder albums where NO track has
    art, using the most common (artist, album) among each album's tracks as the
    lookup key. Albums with no usable artist+album are returned with '' fields
    (the caller skips them)."""
    artless = [r[0] for r in conn.execute(
        "SELECT album_key FROM tracks WHERE album_key!='' GROUP BY album_key "
        "HAVING MAX(CASE WHEN art!='' THEN 1 ELSE 0 END)=0").fetchall()]
    out = []
    for ak in artless:
        row = conn.execute(
            "SELECT artist, album, COUNT(*) n FROM tracks "
            "WHERE album_key=? AND artist!='' AND album!='' "
            "GROUP BY artist, album ORDER BY n DESC LIMIT 1", (ak,)).fetchone()
        if row:
            out.append((ak, row["artist"], row["album"]))
        else:
            out.append((ak, "", ""))
    return out


def cached_album_art(conn, artist, album):
    """(art_url, source) from album_art for (artist, album), or None."""
    return conn.execute(
        "SELECT art_url, source FROM album_art WHERE artist=? AND album=?",
        (artist, album)).fetchone()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="library.db", help="library.db path")
    ap.add_argument("--apply", action="store_true",
                    help="fetch covers + write them (default: dry-run preview)")
    ap.add_argument("--limit", type=int, default=0,
                    help="cap the number of albums processed (0 = all)")
    ap.add_argument("--retry-notfound", action="store_true",
                    help="re-query MusicBrainz for albums previously cached as "
                         "'notfound' (these art-less albums usually ARE that "
                         "residue; a retry catches the ones MB has since gained "
                         "or that beets re-tagged). Refreshes the cache.")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the automatic library.db backup on --apply")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"✗ no DB at {args.db}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")

    albums = artless_folder_albums(conn)
    cand = [a for a in albums if a[1] and a[2]]
    skip_meta = len(albums) - len(cand)
    if args.limit:
        cand = cand[:args.limit]

    print(f"  Fill LocalFs album art  [{'APPLY' if args.apply else 'DRY-RUN'}]")
    print(f"    art-less folder albums : {len(albums)}")
    print(f"    with usable metadata   : {len(cand)}"
          + (f"  (capped to {args.limit})" if args.limit else ""))
    print(f"    no artist/album (skip) : {skip_meta}")

    if not args.apply:
        # Preview only — no network. Show how many are already cached vs new.
        cached_hit = cached_nf = fresh = 0
        for ak, artist, album in cand:
            c = cached_album_art(conn, artist, album)
            if c and c["source"] == "notfound":
                cached_nf += 1
            elif c and c["art_url"]:
                cached_hit += 1
            else:
                fresh += 1
        will_query = fresh + (cached_nf if args.retry_notfound else 0)
        print(f"    already cached (hit)   : {cached_hit}  → would re-apply onto tracks")
        print(f"    already cached notfound: {cached_nf}  → "
              + ("RE-QUERY (--retry-notfound)" if args.retry_notfound else "skip"))
        print(f"    need a MusicBrainz call: {will_query}  "
              f"(~{int(will_query * 1.1)}s at 1.1s/req)")
        print("\n  (dry-run — re-run with --apply to fetch + fill)")
        return 0

    from dlna_art_fetcher import _mb_lookup_cover, _MB_RATE_LIMIT_SEC

    if not args.no_backup:
        bak = f"{args.db}.artfill-bak-{int(time.time())}"
        shutil.copy2(args.db, bak)
        print(f"  📦 backed up dest → {bak}")

    filled = notfound = reused = 0
    for ak, artist, album in cand:
        c = cached_album_art(conn, artist, album)
        if c and c["source"] == "notfound" and not args.retry_notfound:
            continue
        if c and c["art_url"]:
            art_url = c["art_url"]
            reused += 1
        else:
            art_url = _mb_lookup_cover(artist, album)
            # album_art.art_url is NOT NULL — a miss is stored as '' (same as
            # the AlbumArtFetcher's sticky notfound). updated_at defaults.
            conn.execute(
                "INSERT OR REPLACE INTO album_art(artist, album, art_url, source) "
                "VALUES (?,?,?,?)",
                (artist, album, art_url or "",
                 "musicbrainz" if art_url else "notfound"))
            conn.commit()
            time.sleep(_MB_RATE_LIMIT_SEC)
            if not art_url:
                notfound += 1
                continue
        # Write the cover onto the album's tracks so the folder-album
        # getCoverArt + the PWA serve it (only fill the still-empty ones).
        n = conn.execute(
            "UPDATE tracks SET art=? WHERE album_key=? AND (art='' OR art IS NULL)",
            (art_url, ak)).rowcount
        conn.commit()
        filled += 1
        print(f"  ✓ {artist} — {album}  ({n} tracks) → {art_url}")

    print(f"\n  filled={filled}  reused_cache={reused}  notfound={notfound}"
          f"  (of {len(cand)} candidates)")
    print("  Covers show without a gateway restart (tracks.art is read live).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
