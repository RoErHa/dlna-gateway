#!/usr/bin/env python3
"""
tools/artist_meta.py — fill the artist panel's facts.

Runs after tools/artist_mbid.py has resolved artists to MusicBrainz
ids; this turns each id into the things a reader wants: who they were,
when and where, what they played, what they are best known for, and —
for a band — who was in it and when.

Per artist:
  MusicBrainz  one request (inc=artist-rels+genres+url-rels) carrying
               identity, life-span, birthplace, genres AND the band
               members with their instruments and date ranges.
  Wikipedia    the biography paragraph. Stored WITH its url — the text
               is CC BY-SA and the link is the attribution.
  Last.fm      "best known for", if LASTFM_API_KEY is set. Absent, the
               block is simply omitted; nothing else changes.

DRY-RUN by default; --apply writes. Resumable: `meta_fetched_at` means
"already done", so a second run costs only what is new.

    python3 tools/artist_meta.py                  # preview
    python3 tools/artist_meta.py --apply --limit 50
    python3 tools/artist_meta.py --apply          # the full pass

KNOWN LIMIT: Wikipedia is looked up by ARTIST NAME. MusicBrainz no
longer carries direct wikipedia links (it points at Wikidata instead),
and resolving through Wikidata would add two requests per artist to a
pass that is already an hour. A wrong title usually lands on a
disambiguation stub, which parse_wikipedia refuses outright — so the
cost of the shortcut is a missing biography, never a wrong one.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_artist_fetch import (fetch_lastfm_top, fetch_mb_artist,   # noqa: E402
                               fetch_wikipedia, parse_lastfm_top,
                               parse_mb_artist, parse_mb_members,
                               parse_wikipedia)
from dlna_config import DB_FILE                                     # noqa: E402
from dlna_library import LibraryDB                                  # noqa: E402

MB_RATE_SEC = 1.2       # MusicBrainz ToS: ~1 req/s sustained
WP_RATE_SEC = 0.3       # generous, but stay polite
RETRIES = 4


def _retrying(fn, *a):
    """MB sheds load with 503s under a long pass; that is not an answer."""
    for attempt in range(RETRIES):
        try:
            return fn(*a)
        except Exception:                                        # noqa: BLE001
            if attempt == RETRIES - 1:
                raise
            time.sleep(2.0 * (attempt + 1))
    return None


def fetch_one(artist: str, mbid: str, lastfm_key: str, verbose: bool) -> dict:
    """Everything known about one artist, as a dict ready for the DB.

    A failure in Wikipedia or Last.fm degrades that field only — the
    MusicBrainz facts are the point, and losing a biography must not
    cost us a birthplace."""
    doc = _retrying(fetch_mb_artist, mbid)
    time.sleep(MB_RATE_SEC)
    info = parse_mb_artist(doc)
    members = parse_mb_members(doc)
    bands = parse_mb_members(doc, bands=True)

    try:
        wp = parse_wikipedia(_retrying(fetch_wikipedia, artist))
        time.sleep(WP_RATE_SEC)
    except Exception as e:                                       # noqa: BLE001
        if verbose:
            print(f"         wikipedia: {type(e).__name__}")
        wp = {"bio": "", "bio_url": "", "image_url": ""}
    info.update(wp)

    if lastfm_key:
        try:
            info["top_tracks"] = ", ".join(
                parse_lastfm_top(fetch_lastfm_top(artist, lastfm_key)))
        except Exception as e:                                   # noqa: BLE001
            if verbose:
                print(f"         last.fm: {type(e).__name__}")
            info["top_tracks"] = ""
    # A person's bands are the same relation reversed — shown as
    # "Also played in" rather than as a line-up.
    info["notable"] = ", ".join(b["name"] for b in bands[:6])
    return {"info": info, "members": members}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--db", default=DB_FILE)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}")
        return 1

    db = LibraryDB(db_file=args.db)
    todo = db.artists_needing_info(limit=args.limit)
    key = os.environ.get("LASTFM_API_KEY", "").strip()

    print(f"to do    : {len(todo)} artist(s) with an mbid and no facts yet")
    print(f"last.fm  : {'key present' if key else 'NO KEY — '
                       '\"best known for\" will be omitted'}")
    if not todo:
        print("nothing to do.")
        return 0
    mins = len(todo) * (MB_RATE_SEC + WP_RATE_SEC) / 60
    print(f"estimate : ~{mins:.0f} min\n")
    if not args.apply:
        print("DRY RUN — nothing will be written. Re-run with --apply.\n")

    ok = bios = lineups = errors = 0
    t0 = time.time()
    for i, artist in enumerate(todo, 1):
        row = db.artist_meta_get(artist)
        if not row or not row.get("mbid"):
            continue
        try:
            got = fetch_one(artist, row["mbid"], key, args.verbose)
        except Exception as e:                                   # noqa: BLE001
            errors += 1
            print(f"  ERROR  {artist[:40]:40s} {type(e).__name__}: {e}")
            continue
        info, members = got["info"], got["members"]
        ok += 1
        if info.get("bio"):
            bios += 1
        if members:
            lineups += 1
        if args.verbose:
            born = info.get("born") or "?"
            print(f"  {artist[:36]:36s} {info.get('mb_type','?'):6s} "
                  f"{born:10s} members={len(members):2d} "
                  f"bio={'y' if info.get('bio') else 'n'}")
        if args.apply:
            db.artist_info_set(artist, info)
            if members:
                db.artist_members_set(artist, members)
        if i % 50 == 0:
            print(f"  … {i}/{len(todo)}  ({ok} fetched, {errors} errors)")

    print(f"\n── summary ({time.time() - t0:.0f}s) ──")
    print(f"  fetched       : {ok}")
    print(f"  with a bio    : {bios}")
    print(f"  with a line-up: {lineups}")
    print(f"  errors        : {errors}")
    if not args.apply:
        print("\n(dry run — nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
