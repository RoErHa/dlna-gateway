#!/usr/bin/env python3
"""
tools/book_language.py — report, and fix by hand, the language of each
audiobook (the PWA's 🌐 letter-bar entry).

The gateway DETECTS a book's language on every read from its titles
(`dlna_book_language`), and answers Unknown when the words do not decide
it — a book titled *Neuromancer* says nothing about its language. This
tool is how the remainder gets settled, by the two kinds of evidence the
gateway stores:

  --tags     read each book's own language TAG (ID3 TLAN, an MP4/Vorbis
             `language` field) and record it as source='tag'. Measured:
             7 of 491 books carry one, so this is a small, cheap pass.
  --set XX   record language XX as source='manual' for every book whose
             folder matches --match (case-insensitive substring of the
             album_key). `manual` always wins, over a tag and a detection.
  --clear    remove stored rows for the matching books, handing them back
             to detection.

Add --unknown-only to touch only books currently Unknown — the usual way
to settle the leftovers: `--set en --match "Top 100 Sci-Fi" --unknown-only`.

DRY-RUN by default; --apply writes. Nothing here touches a file or the
`tracks` table: the rows live in `book_language`, which survives a
rebuild-index.

    python3 tools/book_language.py                       # report + Unknown list
    python3 tools/book_language.py --tags --apply
    python3 tools/book_language.py --set en --match "Le Guin" --apply
    python3 tools/book_language.py --clear --match "Six of Crows" --apply
    python3 -m unittest tools.test_book_language -v
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlna_book_language import normalize_language  # noqa: E402


def value_to_text(value) -> str:
    """One tag value, whatever the container made of it, as text. ID3
    frames carry `.text` (a list); MP4 freeform atoms are bytes; Vorbis
    gives a list of str."""
    if hasattr(value, "text"):
        value = value.text
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value).decode("utf-8", "replace")
    return str(value or "").strip("\x00 ").strip()


def tag_language(path: str) -> str:
    """The language a file's own tags declare, as ISO 639-1, or ''."""
    try:
        import mutagen
        f = mutagen.File(path)
    except Exception as e:                                # noqa: BLE001
        print(f"  ! cannot read {path}: {e!r}", file=sys.stderr)
        return ""
    if f is None or not f.tags:
        return ""
    for key in f.tags.keys():
        k = str(key).lower()
        if k.startswith("tlan") or "language" in k:
            code = normalize_language(value_to_text(f.tags[key]))
            if code:
                return code
    return ""


def select(langs: dict, match: str, unknown_only: bool) -> list[str]:
    """album_keys whose folder contains `match` (case-insensitive) —
    every book when `match` is empty — narrowed to the Unknown ones when
    asked. Sorted, so a dry run reads in a stable order."""
    needle = (match or "").lower()
    return sorted(k for k, v in langs.items()
                  if needle in k.lower()
                  and not (unknown_only and v["language"]))


def audiobooks_udn(explicit: str) -> str:
    if explicit:
        return explicit
    import dlna_config  # noqa: F401  (loads .env)
    from dlna_localfs_wiring import audiobooks_root
    from dlna_providers.localfs_tags import _udn_for_root
    root = audiobooks_root()
    if not root:
        sys.exit("AUDIOBOOKS_ROOT is not configured — pass --udn.")
    return _udn_for_root(Path(root).expanduser())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    act = ap.add_mutually_exclusive_group()
    act.add_argument("--tags", action="store_true",
                     help="record each book's own language tag")
    act.add_argument("--set", metavar="LANG",
                     help="record LANG by hand for the matching books")
    act.add_argument("--clear", action="store_true",
                     help="drop stored rows for the matching books")
    ap.add_argument("--match", default="",
                    help="case-insensitive substring of the book folder")
    ap.add_argument("--unknown-only", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--udn", default="")
    ap.add_argument("--db", default=str(ROOT / "library.db"))
    args = ap.parse_args(argv)

    if (args.set or args.clear) and not args.match:
        ap.error("--set/--clear need --match — refusing to touch every "
                 "book at once")
    code = ""
    if args.set:
        code = normalize_language(args.set)
        if not code:
            ap.error(f"{args.set!r} names no language (try en, nl, de …)")

    if not os.path.exists(args.db):
        sys.exit(f"no database at {args.db}")
    from dlna_library import LibraryDB
    db = LibraryDB(db_file=args.db)
    udn = audiobooks_udn(args.udn)
    langs = db.book_languages(udn)
    if not langs:
        sys.exit(f"no books indexed under {udn}")

    keys = select(langs, args.match, args.unknown_only)
    verb = "Would" if not args.apply else "Did"
    changed = 0
    if args.tags:
        with db._pool.read() as conn:
            first = dict(conn.execute(
                "SELECT album_key, MIN(file_path) FROM tracks "
                "WHERE udn=? GROUP BY album_key", (udn,)).fetchall())
        for k in keys:
            if langs[k]["source"] in ("manual", "tag") or not first.get(k):
                continue
            found = tag_language(first[k])
            if found:
                print(f"  tag {found}  {k}")
                changed += bool(not args.apply
                                or db.book_language_set(k, found, "tag"))
        print(f"{verb} record {changed} tagged language(s).")
    elif args.set:
        for k in keys:
            print(f"  {langs[k]['language'] or '??'} → {code}  {k}")
            changed += bool(not args.apply or db.book_language_set(k, code))
        print(f"{verb} set {changed} book(s) to {code!r}.")
    elif args.clear:
        for k in keys:
            if langs[k]["source"] in ("manual", "tag"):
                print(f"  clear {langs[k]['source']} {langs[k]['language']}  {k}")
                changed += bool(not args.apply or db.book_language_clear(k))
        print(f"{verb} clear {changed} stored row(s).")
    else:
        tally = Counter((v["language"] or "??", v["source"])
                        for v in langs.values())
        for (lang, src), n in sorted(tally.items()):
            print(f"  {lang:3} {src:9} {n:5}")
        unknown = select(langs, args.match, True)
        print(f"\nUnknown ({len(unknown)}):")
        for k in unknown:
            print(f"  {k}")
    if (args.tags or args.set or args.clear) and not args.apply and changed:
        print("Dry run — re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
