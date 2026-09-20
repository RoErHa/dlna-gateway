#!/usr/bin/env python3
"""
tools/artist_mbid.py — resolve every artist to a MusicBrainz id.

The keystone of the metadata work: MusicBrainz, Wikidata, Wikipedia and
ListenBrainz all key off an artist MBID, so this runs once and unlocks
all of them. Nothing here displays anything — it fills `artist_meta`.

Two phases, cheapest first:

  1. **Tags** (no network). ~19% of this library's artists already carry
     a `musicbrainz_artistid` from a past beets run. Exact, free, and
     immune to the namesake problem that makes searching risky.
  2. **Search** (rate-limited). The rest go to MusicBrainz by name, and
     `dlna_mbid.accept_match` decides whether to believe the answer.
     Measured on a random sample of this library: **96% accepted, and
     the one refusal was correct** (a comma-joined list of three
     artists, which is deliberately never split).

DRY-RUN by default; `--apply` writes.

Resumable and cheap to re-run: a row of ANY source — including the
sticky `notfound` — means "already asked", so a second pass costs
minutes rather than another hour. `manual` rows are never touched.

    python3 tools/artist_mbid.py                    # preview
    python3 tools/artist_mbid.py --tags-only --apply  # free phase only
    python3 tools/artist_mbid.py --apply --limit 200  # bounded live pass
    python3 tools/artist_mbid.py --apply              # the full sweep
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_config import DB_FILE                                  # noqa: E402
from dlna_library import LibraryDB                               # noqa: E402
from dlna_mbid import accept_match, query_names                  # noqa: E402
# The junk-name display filter the UPnP tree already uses. Imported
# rather than re-implemented so the two can never disagree about what
# counts as an artist. It lives in api_upnp_ids (which binds the lazy
# `DB` proxy but opens nothing), so a library mixin could not import it
# without a cycle — and "is this worth a network request?" is the
# tool's question anyway, not the database's.
from api_upnp_ids import _is_junk_name                           # noqa: E402

MB_ROOT = "https://musicbrainz.org/ws/2"
RATE_SEC = 1.2          # MB allows ~1/s sustained; margin for safety
TIMEOUT = 25.0
RETRIES = 4             # measured: ~7% of calls return 503 under load

# Where each container keeps the MusicBrainz artist id. `easy=True`
# cannot be relied on here — EasyMP4 maps none of these.
_TAG_KEYS = (
    "musicbrainz_artistid",                          # Vorbis (FLAC/OGG)
    "MusicBrainz Artist Id",                         # ID3 TXXX
    "----:com.apple.iTunes:MusicBrainz Artist Id",   # MP4 freeform
)


def user_agent() -> str:
    """MusicBrainz's ToS requires a contact address; an anonymous UA
    gets throttled or blocked. Same source as dlna_art_fetcher."""
    email = os.environ.get("GATEWAY_CONTACT_EMAIL", "").strip()
    return f"DLNAGateway/1.0 ( {email} )" if email else "DLNAGateway/1.0"


def mbid_from_tagmap(tags) -> str:
    """Pull a MusicBrainz artist id out of a mutagen tag mapping.

    Pure over the mapping so the container quirks are testable without
    real files. A multi-artist file lists several ids; we take the
    first, which is the primary credit."""
    if not tags:
        return ""
    lowered = {str(k).lower(): k for k in tags.keys()}
    for want in _TAG_KEYS:
        key = lowered.get(want.lower())
        if key is None:
            continue
        val = tags[key]
        if isinstance(val, (list, tuple)):
            val = val[0] if val else ""
        if isinstance(val, bytes):                   # MP4 freeform atoms
            val = val.decode("utf-8", "replace")
        val = str(val).strip()
        if val:
            return val.split("/")[0].strip()
    return ""


def mbid_from_file(path: str) -> str:
    try:
        import mutagen
        audio = mutagen.File(path)
    except Exception:                                            # noqa: BLE001
        return ""
    if audio is None:
        return ""
    return mbid_from_tagmap(audio.tags)


def mb_search(name: str, ua: str) -> list[dict]:
    """One MusicBrainz artist search, with backoff. A 503 is MB shedding
    load, not an answer — retrying is correct; a 404 is an answer."""
    url = (f"{MB_ROOT}/artist?query="
           + urllib.parse.quote(f'artist:"{name}"') + "&fmt=json&limit=5")
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.load(r).get("artists", [])
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503) or attempt == RETRIES - 1:
                raise
        except Exception:                                        # noqa: BLE001
            if attempt == RETRIES - 1:
                raise
        time.sleep(2.0 * (attempt + 1))
    return []


def phase_tags(db, udn, todo, apply_, verbose):
    """Free phase: read the id the file already carries."""
    hits = 0
    still: list[str] = []
    for name in todo:
        path = db.one_file_path_for_artist(udn, name)
        mbid = mbid_from_file(path) if path else ""
        if mbid:
            hits += 1
            if verbose:
                print(f"  tag    {name[:44]:44s} -> {mbid[:8]}…")
            if apply_:
                db.artist_meta_set(name, mbid, "tag")
        else:
            still.append(name)
    return hits, still


def phase_search(db, udn, todo, apply_, verbose, ua):
    """Rate-limited phase: ask MusicBrainz, and believe it only when
    `accept_match` says so."""
    ok = refused = errors = 0
    for i, name in enumerate(todo, 1):
        mbid = None
        try:
            for q in query_names(name):
                cands = mb_search(q, ua)
                time.sleep(RATE_SEC)
                mbid = accept_match(q, cands)
                if mbid:
                    if verbose and q != name:
                        print(f"         (via {q!r})")
                    break
        except Exception as e:                                   # noqa: BLE001
            errors += 1
            print(f"  ERROR  {name[:44]:44s} {type(e).__name__}: {e}")
            continue
        if mbid:
            ok += 1
            if verbose:
                print(f"  search {name[:44]:44s} -> {mbid[:8]}…")
            if apply_:
                db.artist_meta_set(name, mbid, "search")
        else:
            refused += 1
            if verbose:
                print(f"  refuse {name[:44]:44s} (no confident match)")
            if apply_:
                db.artist_meta_set(name, None, "notfound")
        if i % 50 == 0:
            print(f"  … {i}/{len(todo)} searched "
                  f"({ok} matched, {refused} refused, {errors} errors)")
    return ok, refused, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="write to artist_meta (default: dry run)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N artists (0 = all)")
    ap.add_argument("--tags-only", action="store_true",
                    help="skip the network phase entirely")
    ap.add_argument("--db", default=DB_FILE)
    ap.add_argument("--udn", default="",
                    help="library to sweep (default: the biggest one)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}")
        return 1

    db = LibraryDB(db_file=args.db)
    udn = args.udn or db.primary_udn()
    if not udn:
        print("no library to sweep")
        return 1

    # Filter BEFORE limiting, so `--limit 200` means 200 real lookups
    # rather than 200 rows of which a fifth are track-number junk.
    # Measured on the first live pass: 12 of 23 searches were names like
    # "13. My Girl - The Temptations" or "07" — filename-derived rows
    # that can only ever come back refused, while still costing a
    # rate-limited request each.
    todo = [a for a in db.artists_without_mbid(udn)
            if not _is_junk_name(a)]
    skipped_junk = len(db.artists_without_mbid(udn)) - len(todo)
    if args.limit:
        todo = todo[:args.limit]
    done = len(db.artist_meta_all())
    print(f"library : {udn}")
    print(f"resolved: {done} artist(s) already in artist_meta")
    print(f"skipped : {skipped_junk} unartist-like name(s) "
          f"(track-number prefixes etc. — never queried)")
    print(f"to do   : {len(todo)} artist(s)"
          + (f" (limited to {args.limit})" if args.limit else ""))
    if not todo:
        print("nothing to do.")
        return 0
    if not args.apply:
        print("\nDRY RUN — nothing will be written. Re-run with --apply.\n")

    t0 = time.time()
    tag_hits, still = phase_tags(db, udn, todo, args.apply, args.verbose)
    print(f"\nphase 1 (tags, no network): {tag_hits} resolved free, "
          f"{len(still)} need a lookup")

    ok = refused = errors = 0
    if still and not args.tags_only:
        mins = len(still) * RATE_SEC / 60
        print(f"phase 2 (MusicBrainz): {len(still)} lookups "
              f"≈ {mins:.0f} min at {RATE_SEC}s each\n")
        ok, refused, errors = phase_search(
            db, udn, still, args.apply, args.verbose, user_agent())

    print(f"\n── summary ({time.time() - t0:.0f}s) ──")
    print(f"  from tags     : {tag_hits}")
    print(f"  from search   : {ok}")
    print(f"  refused       : {refused}   (recorded as sticky 'notfound')")
    print(f"  errors        : {errors}")
    if not args.apply:
        print("\n(dry run — nothing written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
