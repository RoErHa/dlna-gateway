#!/usr/bin/env python3
"""
dlna_artist_infer.py — can we say who performed this track without
guessing?

Two callers must answer that identically or they contradict each other:
`tools/artist_from_folder.py`, which WRITES the inferred artist into the
file, and `LibraryDB.sync_unknown_artist_playlist`, which sweeps whatever
is left into the hand-editing worklist. If the sweep were more generous
than the tool, tracks the tool could fix would sit in the worklist
forever; if it were stricter, tracks nobody can fix would vanish from it.
So the decision lives here once, pure and dependency-free.

Evidence, strongest first:

  1. **Sibling unanimity.** Every tagged track in the folder names the
     same performer → that is the performer. A folder is an album.
  2. **An uncontradicted folder name.** NOTHING in the folder is tagged,
     so there is no evidence to weigh against the name on the tin —
     `Mira Calvo (1996) Caminhos [FLAC]` → `Mira Calvo`.
  3. **A folder name a sibling confirms.** The folder spans several
     artist spellings, but one of them IS the folder name (`Jean Vallier`
     beside `jean vallier`), so the name is corroborated rather than
     assumed.

Anything else is a genuine unknown and returns "". The case that matters:
a compilation folder — many different performers, a folder named after
the COMPILATION. `Nights On Neptune` holds 20 artists and is not a band, and
`Unknown Artist/Unknown Album` is a junk drawer holding 126. Naming
either as the performer would be confidently wrong, which is worse than
blank: a blank asks a person, a wrong tag files the track under a
stranger and is never questioned again.
"""
from __future__ import annotations

import re

# Names that describe a COLLECTION rather than a performer. Matched on the
# whole parsed name, case-folded — a substring test would eat real bands
# ("Various Comforts", "The Unknown").
_NOT_A_PERFORMER = {
    "unknown", "unknown artist", "unknown artists", "various",
    "various artists", "va", "v a", "diverse", "compilation",
    "compilations", "soundtrack", "ost", "misc", "assorted", "music",
    "albums", "singles", "sampler", "hits", "greatest hits", "top 100",
    "the collection", "collection", "unsorted", "new folder", "temp",
}

# Where the artist portion of a folder name ENDS. A bracket opens the
# year/format cruft (`Mira Calvo (1996) Caminhos [FLAC] `), and
# " - " divides artist from album (`RVM - Studio Discography [FLAC]`).
# The EARLIEST of them wins, or the album name gets glued onto the artist.
# A BARE "-" is not a divider: it would halve "Jean-Marc Aubert" exactly
# as it would in a filename.
_CUTS = (" - ", "(", "[", "{")


def parse_folder_artist(album_key: str) -> str:
    """The performer a folder path claims, or "" if it claims nothing.

    Uses the TOP segment: `RVM - Studio Discography [FLAC]/RVM 1983 -
    Murmur` is an artist folder holding album folders, so the artist is
    at the top and the album is at the leaf."""
    name = (album_key or "").replace("\\", "/").split("/")[0]

    cut = min((name.index(c) for c in _CUTS if c in name), default=len(name))
    name = re.sub(r"\s+", " ", name[:cut]).strip(" -_.,").strip()

    if not name or len(name) > 60:
        return ""
    if name.casefold() in _NOT_A_PERFORMER:
        return ""
    if not re.search(r"[^\W\d_]", name):          # no letters at all
        return ""
    # A dated, spaceless slug is a bootleg directory, not a name:
    # "SVance2008-07-05-sbd", "2024-04-04-palais-st-kilda-elvis-costello".
    # A real performer with digits keeps its spaces ("Sunset Rundown 3").
    if re.search(r"\d", name) and " " not in name:
        return ""
    return name


def _declares_itself_a_compilation(album_key: str) -> bool:
    """`VA - 2016 - 100 Hits Pure 80s` says outright that it holds many
    performers. That claim outranks sibling unanimity: such a folder can
    easily have ONE tagged track, and treating that as the answer would
    stamp the whole compilation with a single artist's name."""
    name = (album_key or "").replace("\\", "/").split("/")[0]
    cut = min((name.index(c) for c in _CUTS if c in name), default=len(name))
    head = re.sub(r"\s+", " ", name[:cut]).strip(" -_.,").casefold()
    return head in _NOT_A_PERFORMER


def infer_artist(album_key: str, sibling_artists) -> str:
    """The performer for an untagged track in `album_key`, or "".

    `sibling_artists` is every NON-BLANK artist on other tracks in the
    same folder (duplicates fine, order irrelevant)."""
    if _declares_itself_a_compilation(album_key):
        return ""

    sibs = {s.strip() for s in (sibling_artists or []) if s and s.strip()}

    if len(sibs) == 1:                                    # 1. unanimity
        return next(iter(sibs))

    folder = parse_folder_artist(album_key)
    if not folder:
        return ""
    if not sibs:                                          # 2. uncontradicted
        return folder
    lowered = {s.casefold() for s in sibs}
    if folder.casefold() in lowered:                      # 3. corroborated
        return folder
    return ""
