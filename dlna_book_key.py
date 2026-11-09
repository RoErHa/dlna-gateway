#!/usr/bin/env python3
"""
dlna_book_key.py — which resume slot does this audiobook file belong to?

A book's resume position is one row in `playback_positions`, and until
2026-11-09 its key was the book's FOLDER (`album_key`). That is right for
almost every book — and wrong for a folder that holds several books:
*BBC Radio 4 Hercule Poirot* is 27 separate books, one file each, so the
whole series shared ONE bookmark, pointing at whichever episode was played
last. Game of Thrones, the Dune saga and the Vonnegut and Le Guin packs
have the same shape.

The evidence that a folder holds several books is its ALBUM TAGS — but
most folders whose tags differ are ONE book split up: `Eichmann in
Jerusalem (disk 1)` … `(disk 10)`, `Prince Caspian Disc 2`, `The Master
and Margarita Part 1/2`, The BFG tagged `1`/`2`/`3`. Splitting those would
break resume across a disc change, which is worse than the bug. So a tag
is NORMALISED first — digits and disc/part/season words dropped, case,
punctuation and filler words (the/and/of…) folded — and a folder is split
only when TWO OR MORE different titles survive. A blank tag (or one too short to mean anything, like the
mojibake `ÿþS`) claims nothing and keeps the folder key.

The key of a book in a split folder is `<album_key> ▸ <normalised title>`,
so every key still STARTS with its folder: "the newest bookmark in this
folder" is a prefix query, which is what the folder's own ▶ Continue uses.

Pure on purpose — the PWA, the Naim monitor and Subsonic bookmarks must
all get the same key, or a book saved on one surface is lost on another.
"""
import re

SEP = " ▸ "

_NOISE = re.compile(
    r"\b(?:disc|disk|cd|part|pt|season|series|vol|volume|side|tape|"
    r"unabridged|abridged)\b")
# Filler words, dropped so two spellings of ONE title match (`Lion Witch &
# Wardrobe` / `The Lion, the Witch and the Wardrobe`, both in one Narnia
# folder). Not a subset test: that would merge `Dune` into `Dune Messiah`.
_FILLER = frozenset("the a an and of de het een en van".split())
_MOJIBAKE_BOMS = ("ÿþ", "þÿ", "﻿")


def norm_book_title(album: str) -> str:
    """The part of an album tag that names a BOOK, or '' when nothing
    does. Any script survives (`\\w` is Unicode-aware) — a "has A-Z" rule
    once hid every Russian composer."""
    s = (album or "").strip()
    for bom in _MOJIBAKE_BOMS:
        if s.startswith(bom):
            s = s[len(bom):]
    s = s.casefold()
    s = re.sub(r"\d+", " ", s)
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[\W_]+", " ", s)
    s = " ".join(w for w in s.split() if w not in _FILLER)
    return s if len(s) >= 3 else ""


def is_multi_book(folder_albums) -> bool:
    """Do these album tags (every file in one folder) name 2+ books?"""
    return len({norm_book_title(a) for a in folder_albums} - {""}) >= 2


def book_key(album_key: str, album: str, url: str, folder_albums) -> str:
    """The resume key for one file. `folder_albums` is the album tag of
    every file in its folder (duplicates fine). A root-level single file
    has no folder and keys by its URL, as it always has."""
    if not album_key:
        return url
    title = norm_book_title(album)
    if not title or not is_multi_book(folder_albums):
        return album_key
    return album_key + SEP + title


def folder_of(key: str) -> str:
    """The folder a resume key belongs to (the key itself if unsplit)."""
    return key.split(SEP, 1)[0]
