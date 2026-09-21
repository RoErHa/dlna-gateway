#!/usr/bin/env python3
"""
tools/track_credits.py — composer/lyricist from MusicBrainz for the
tracks whose FILES carry none (step 4).

**Why this is affordable.** The obvious route — search each track, then
its recording, then its work — is three rate-limited requests per track,
about 16 hours across this library. Instead this browses each ARTIST's
works: MusicBrainz returns up to 100 at a time, each already carrying
its composer/lyricist relations. One request per 100 songs, roughly
1.5 hours. That is a direct payoff from having resolved artists to MBIDs
first (tools/artist_mbid.py) — it is what the keystone was for.

Results go to `track_credits`, NOT `tracks.composer`: a rescan is
blank-safe, but `clear(udn)` DELETEs `tracks` and a rebuild-index would
throw away the whole night. **The file tag always wins on read** — this
fills gaps, it does not correct the library.

DRY-RUN by default; `--apply` writes. Resumable: a row of any source,
including the sticky `notfound`, means "already asked".

    python3 tools/track_credits.py                   # preview
    python3 tools/track_credits.py --apply --limit 20  # a bounded pass
    python3 tools/track_credits.py --apply             # the full sweep
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_artist_fetch import fetch_mb_works, parse_mb_works       # noqa: E402
from dlna_config import DB_FILE                                    # noqa: E402
from dlna_library import LibraryDB                                 # noqa: E402
from dlna_library_sql import _norm_title                           # noqa: E402

MB_RATE_SEC = 1.2
RETRIES = 4
PAGE = 100
MAX_PAGES = 12          # 1,200 works is far past any real artist here


def _retrying(fn, *a):
    for attempt in range(RETRIES):
        try:
            return fn(*a)
        except Exception:                                        # noqa: BLE001
            if attempt == RETRIES - 1:
                raise
            time.sleep(2.0 * (attempt + 1))
    return None


def works_for(mbid: str, verbose: bool = False) -> dict:
    """Every work MusicBrainz relates to this artist, keyed by
    normalised title. Paginated; stops at the reported total.

    NOTE the browse is deliberately broad — it returns works the artist
    is related to in any way, which can include a member's work
    elsewhere (Brian May's 'Driven by You' appears under Pink Floyd).
    That is harmless because the caller only ever looks up titles of
    tracks it already holds BY THAT ARTIST, so a stray work simply never
    matches."""
    out: dict[str, dict] = {}
    offset = 0
    for _ in range(MAX_PAGES):
        doc = _retrying(fetch_mb_works, mbid, offset, PAGE)
        time.sleep(MB_RATE_SEC)
        page = parse_mb_works(doc)
        for k, v in page.items():
            out.setdefault(k, v)
        total = (doc or {}).get("work-count") or 0
        offset += PAGE
        if offset >= total:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N ARTISTS (0 = all)")
    ap.add_argument("--db", default=DB_FILE)
    ap.add_argument("--udn", default="")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}")
        return 1

    db = LibraryDB(db_file=args.db)
    udn = args.udn or db.primary_udn()
    todo = db.tracks_needing_credits(udn)

    # Group by artist: one artist's whole catalogue costs ~1 request.
    by_artist: dict[str, list] = collections.defaultdict(list)
    for t in todo:
        by_artist[t["artist"]].append(t)

    # Only artists we have an mbid for — the browse needs one, and an
    # artist without one was already refused for a good reason.
    artists = []
    for name in sorted(by_artist, key=lambda a: -len(by_artist[a])):
        row = db.artist_meta_get(name)
        if row and row.get("mbid"):
            artists.append(name)
    if args.limit:
        artists = artists[:args.limit]

    covered = sum(len(by_artist[a]) for a in artists)
    print(f"tracks without a composer : {len(todo):,}")
    print(f"artists with an mbid      : {len(artists):,} "
          f"(covering {covered:,} of those tracks)")
    print(f"estimate                  : ~{len(artists) * 1.6 * MB_RATE_SEC / 60:.0f} min")
    if not artists:
        print("nothing to do.")
        return 0
    if not args.apply:
        print("\nDRY RUN — nothing will be written. Re-run with --apply.\n")

    matched = unmatched = errors = 0
    t0 = time.time()
    for i, name in enumerate(artists, 1):
        row = db.artist_meta_get(name)
        try:
            works = works_for(row["mbid"], args.verbose)
        except Exception as e:                                   # noqa: BLE001
            errors += 1
            print(f"  ERROR  {name[:38]:38s} {type(e).__name__}: {e}")
            continue
        hit = miss = 0
        for t in by_artist[name]:
            w = works.get(_norm_title(t["title"]))
            if w:
                hit += 1
                if args.apply:
                    db.track_credits_set(t["url"], w["composer"],
                                         w["lyricist"], w["work_mbid"],
                                         "musicbrainz")
            else:
                miss += 1
                # Sticky negative: without it a second pass re-asks for
                # every unmatched track and costs another whole night.
                if args.apply:
                    db.track_credits_set(t["url"], None, None, "", "notfound")
        matched += hit
        unmatched += miss
        if args.verbose:
            print(f"  {name[:34]:34s} works={len(works):4d} "
                  f"matched={hit:3d}/{hit + miss:3d}")
        if i % 25 == 0:
            print(f"  … {i}/{len(artists)} artists  "
                  f"({matched:,} matched, {unmatched:,} unmatched, "
                  f"{errors} errors)")

    total = matched + unmatched
    print(f"\n── summary ({time.time() - t0:.0f}s) ──")
    print(f"  matched   : {matched:,}"
          + (f"  ({100 * matched / total:.0f}%)" if total else ""))
    print(f"  unmatched : {unmatched:,}   (sticky 'notfound')")
    print(f"  errors    : {errors}")
    if not args.apply:
        print("\n(dry run — nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
