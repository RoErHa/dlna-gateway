#!/usr/bin/env python3
"""
tools/tag_from_filename.py — give untagged strays an artist tag, from the
one piece of evidence they still carry: their own filename.

WHY THIS EXISTS
---------------
`<music-root>/Unknown Artist/Unknown Album/` is a 2009-era junk drawer: 251 loose
MP3s, most with no artist tag at all. Folder-album identity groups a
LocalFs album by its folder, so the whole drawer resolved as ONE album —
playing a Marsh & Quinn song queued Rio Verde Social Club behind it.

The browse layer now groups blank-album rows per ARTIST
(`_localfs_album_group`), which fixes every file that has an artist tag.
It cannot help the ones that have nothing. This does: it recovers the
artist from the filename, so those files rejoin the library as themselves.

WHAT IT WILL NOT DO
-------------------
  * It never moves, renames or deletes a file. Only tags are written.
  * It never overwrites an artist that is already there. A file with a
    real tag is evidence; a filename is a guess, and a guess must not
    beat evidence.
  * It writes nothing it had to invent. No separator in the name → the
    file is reported as unparseable and left exactly as it was.

Filenames here are TRUNCATED (~36 chars, extension included), so titles
arrive clipped — "Alonso Bellini & Gioia - Vivo Per". That is fine: the
artist is the part that decides which album a file lands in, and the
artist sits at the FRONT, which is the half that survived. Titles are only
written when absent, and never corrected.

DRY-RUN BY DEFAULT. `--apply` writes tags in place.

Usage:
    python3 tools/tag_from_filename.py "<folder>"
    python3 tools/tag_from_filename.py "<folder>" --apply
    python3 tools/tag_from_filename.py "<folder>" -v --limit 20
"""
from __future__ import annotations

import argparse
import os
import re
import sys

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wma", ".aiff",
              ".aif", ".wav", ".alac", ".aac"}

# A leading track number: "01 - ", "02. ", "0198 - ", "03-", "06 ".
_TRACK_NO = re.compile(r"^\s*\d{1,4}\s*[-.)]?\s+|^\s*\d{1,4}\s*-\s*")

# Separators accepted between artist and title, widest first. A BARE "-"
# with no surrounding space is deliberately absent: it would split
# "Jean-Marc Aubert" and "Blue-Eyed Soul" straight down the middle.
_SEPARATORS = (" - ", " -", "- ")

# A parenthesised artist prefix: "(Coolwave)-In My Place".
_PAREN_ARTIST = re.compile(r"^\((?P<artist>[^)]{2,40})\)\s*-\s*(?P<title>.+)$")

_JUNK_ARTISTS = {"unknown", "various", "various artists", "va", "unknown artist"}


def parse_name(stem: str) -> tuple[str, str]:
    """`stem` (a filename without extension) → `(artist, title)`.

    Returns `("", title)` when no artist can be read, and `("", "")` when
    there is nothing usable at all. Pure — every hostile shape below is a
    test rather than something discovered in a 251-file batch."""
    s = (stem or "").strip()
    if not s:
        return "", ""

    m = _PAREN_ARTIST.match(s)
    if m:
        return _clean(m.group("artist")), _clean(m.group("title"))

    # Strip a leading track number, but only when something survives it:
    # a file literally named "07" keeps its name rather than becoming "".
    stripped = _TRACK_NO.sub("", s, count=1).strip()
    body = stripped or s

    for sep in _SEPARATORS:
        if sep in body:
            artist, _, title = body.partition(sep)
            artist, title = _clean(artist), _clean(title)
            # "00 - Unknown - DJZenith_..." — the artist slot says nothing.
            if artist.lower() in _JUNK_ARTISTS:
                return "", title
            # A digits-only left side was a track number the regex missed.
            if artist.isdigit():
                return "", title
            if artist and title:
                return artist, title
            return "", (title or artist)

    return "", body


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("_", " ")).strip(" -.")


def read_tags(path: str):
    """`(artist, title)` as currently stored, or None if unreadable."""
    try:
        from mutagen import File as MFile
    except ImportError:
        print("✗ mutagen is required:  .venv/bin/pip install mutagen",
              file=sys.stderr)
        raise SystemExit(2) from None
    try:
        f = MFile(path, easy=True)
        if f is None:
            return None
        return (str((f.get("artist") or [""])[0]),
                str((f.get("title") or [""])[0]))
    except Exception as e:                       # noqa: BLE001 - reported
        print(f"  ! unreadable: {os.path.basename(path)} ({e})")
        return None


def write_tags(path: str, artist: str, title: str) -> bool:
    from mutagen import File as MFile
    try:
        f = MFile(path, easy=True)
        if f is None:
            return False
        if artist:
            f["artist"] = artist
        if title:
            f["title"] = title
        f.save()
        return True
    except Exception as e:                       # noqa: BLE001 - reported
        print(f"  ! write failed: {os.path.basename(path)} ({e})")
        return False


def plan(folder: str, *, limit: int = 0, verbose: bool = False) -> dict:
    """Decide what to write, touching nothing. Returns a report dict."""
    out = {"write": [], "has_artist": [], "unparseable": [], "unreadable": []}
    names = sorted(os.listdir(folder))
    for name in names:
        path = os.path.join(folder, name)
        stem, ext = os.path.splitext(name)
        if not os.path.isfile(path) or ext.lower() not in AUDIO_EXTS:
            continue
        if limit and len(out["write"]) >= limit:
            break
        cur = read_tags(path)
        if cur is None:
            out["unreadable"].append(name)
            continue
        cur_artist, cur_title = cur
        if cur_artist.strip():
            out["has_artist"].append((name, cur_artist))
            if verbose:
                print(f"  = keeps {cur_artist!r:28} {name}")
            continue
        artist, title = parse_name(stem)
        if not artist:
            out["unparseable"].append(name)
            continue
        out["write"].append({
            "name": name, "path": path, "artist": artist,
            "title": title if not cur_title.strip() else "",
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--apply", action="store_true",
                    help="write the tags (default: dry run)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.folder):
        print(f"✗ not a directory: {args.folder}", file=sys.stderr)
        return 2

    rep = plan(args.folder, limit=args.limit, verbose=args.verbose)
    w = rep["write"]

    print(f"\n{args.folder}")
    print(f"  would tag        {len(w)}")
    print(f"  already tagged   {len(rep['has_artist'])}  (untouched)")
    print(f"  no artist in name{len(rep['unparseable']):4}  (left alone)")
    if rep["unreadable"]:
        print(f"  unreadable       {len(rep['unreadable'])}")

    if w:
        print("\n  artist                        ← filename")
        for it in w[:40]:
            print(f"  {it['artist'][:28]:28}  ← {it['name'][:46]}")
        if len(w) > 40:
            print(f"  … and {len(w) - 40} more")

    if rep["unparseable"] and args.verbose:
        print("\n  left alone (no artist readable):")
        for n in rep["unparseable"][:40]:
            print(f"    {n}")

    if not w:
        print("\n✓ nothing to do")
        return 0
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    ok = sum(write_tags(it["path"], it["artist"], it["title"]) for it in w)
    print(f"\n✓ tagged {ok}/{len(w)} file(s)")
    print("  next: rescan the library so the new tags become visible")
    return 0 if ok == len(w) else 1


if __name__ == "__main__":
    raise SystemExit(main())
