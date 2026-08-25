#!/usr/bin/env python3
"""
tools/artist_from_folder.py — restore a missing artist tag from what the
FOLDER already knows.

The companion to `tag_from_filename.py`. That one reads a loose file's own
name; this one reads its neighbourhood, which is stronger evidence: a
folder is an album, so its other tracks — or the name on the tin — usually
say who performed it. `Mira Calvo (1996) Caminhos [FLAC]` is not a
mystery, and neither is a folder whose 40 tagged tracks all say Stormwind.

The decision itself lives in `dlna_artist_infer.infer_artist`, SHARED with
the gateway's "- Unknown Artists -" sweep, so the two can never disagree
about which tracks a person still has to do by hand. Read that module for
the evidence order and for why a compilation named after itself is refused.

What it will not do, same contract as `tag_from_filename.py`:

  * never moves, renames or deletes a file — only tags are written;
  * never overwrites an artist that is already there;
  * writes only into files the DB says have NO artist, and re-reads each
    file before writing so a stale index cannot cause a bad write.

DRY-RUN BY DEFAULT. `--apply` writes tags in place; the gateway picks them
up on its next rescan.

Usage:
    python3 tools/artist_from_folder.py                 # preview
    python3 tools/artist_from_folder.py --apply
    python3 tools/artist_from_folder.py --udn <udn> -v
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# Deliberately NOT `import dlna_library`: importing it constructs
# `DB = LibraryDB()` at module scope, which runs every pending migration
# against the live database as a side effect of a read-only preview.
from dlna_artist_infer import infer_artist        # noqa: E402

DB_FILE = os.path.join(PROJECT, "library.db")


def _connect(db_file):
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn


def pick_udn(conn, override: str = "") -> str:
    """The music source — the localfs udn owning the most tracks.
    Audiobooks are a separate udn and are left alone: a chapter with no
    artist tag is ordinary there (the author lives in `book_meta`)."""
    if override:
        return override
    rows = conn.execute(
        "SELECT udn, COUNT(*) n FROM tracks WHERE udn LIKE 'uuid:localfs-%' "
        "GROUP BY udn ORDER BY n DESC").fetchall()
    return rows[0]["udn"] if rows else ""


def plan(conn, udn: str) -> tuple[list, list]:
    """`(writes, unattributable)` — touches nothing."""
    rows = conn.execute(
        "SELECT url, title, album_key, file_path FROM tracks "
        " WHERE udn=? AND COALESCE(artist,'')='' AND COALESCE(file_path,'')<>'' "
        " ORDER BY file_path COLLATE NOCASE", (udn,)).fetchall()

    sibs: dict[str, list] = {}
    for key in {r["album_key"] for r in rows}:
        sibs[key] = [x["artist"] for x in conn.execute(
            "SELECT DISTINCT artist FROM tracks "
            " WHERE udn=? AND album_key=? AND COALESCE(artist,'')<>''",
            (udn, key)).fetchall()]

    writes, unattributable = [], []
    for r in rows:
        artist = infer_artist(r["album_key"], sibs.get(r["album_key"]))
        (writes if artist else unattributable).append(
            {"path": r["file_path"], "artist": artist,
             "album_key": r["album_key"], "title": r["title"]})
    return writes, unattributable


def write_artist(path: str, artist: str) -> str:
    """'written' | 'has_artist' | 'missing' | 'unsupported' | 'failed'.

    ⚠️ There is NO clever fallback here, and that is deliberate. A first
    version caught the easy interface refusing WAV ("not a Frame
    instance") and fell back to `ID3(path).save(path)`. For a RIFF
    container that PREPENDS a standalone ID3v2 tag, so the file starts
    with `ID3` instead of `RIFF` and stops being a WAV at all — it
    destroyed 15 real albums' worth of files before the next scan
    reported them as `malformed`. They were recoverable only because
    prepending leaves the original bytes untouched further in.

    A tag is a convenience; the audio is the product. If a container will
    not take a tag through the interface mutagen offers for it, the
    correct action is to say so and leave the file alone."""
    if not os.path.isfile(path):
        return "missing"
    try:
        from mutagen import File as MFile
        f = MFile(path, easy=True)
        if f is None:
            return "unsupported"
        # Re-read rather than trusting the index: if the file gained an
        # artist since the last scan, that tag is newer than our inference
        # and must win.
        if str((f.get("artist") or [""])[0]).strip():
            return "has_artist"
        try:
            f["artist"] = artist
        except Exception:                             # noqa: BLE001
            return "unsupported"
        f.save()
        return "written"
    except Exception as e:                            # noqa: BLE001 - reported
        print(f"  ! {os.path.basename(path)}: {e}")
        return "failed"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DB_FILE)
    ap.add_argument("--udn", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"✗ no such database: {args.db}", file=sys.stderr)
        return 2

    conn = _connect(args.db)
    udn = pick_udn(conn, args.udn)
    if not udn:
        print("✗ no localfs source in the database", file=sys.stderr)
        return 2

    writes, unattributable = plan(conn, udn)
    print(f"\nsource {udn}")
    print(f"  artist recoverable from the folder   {len(writes)}")
    print(f"  genuinely unknown (hand work)        {len(unattributable)}")

    by_folder: dict[tuple, int] = {}
    for w in writes:
        by_folder[(w["album_key"], w["artist"])] = \
            by_folder.get((w["album_key"], w["artist"]), 0) + 1
    if by_folder:
        print("\n  count  artist                        ← folder")
        for (key, artist), n in sorted(by_folder.items(), key=lambda x: -x[1]):
            print(f"  {n:>5}  {artist[:28]:28}  ← {key[:44]}")

    if args.verbose and unattributable:
        print("\n  left for the '- Unknown Artists -' playlist:")
        seen = {}
        for u in unattributable:
            seen[u["album_key"]] = seen.get(u["album_key"], 0) + 1
        for key, n in sorted(seen.items(), key=lambda x: -x[1]):
            print(f"  {n:>5}  {key[:60]}")

    if not writes:
        print("\n✓ nothing to do")
        return 0
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    tally: dict[str, int] = {}
    for w in writes:
        r = write_artist(w["path"], w["artist"])
        tally[r] = tally.get(r, 0) + 1
    print(f"\n✓ {tally}")
    print("  next: rescan so the new tags become visible")
    return 0 if tally.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
