#!/usr/bin/env python3
"""
dlna_art_query.py — what to ASK when looking for an album cover.

3,998 albums sit in `album_art` as a sticky `notfound`. Sampling them
showed the SOURCES were never the bottleneck — the QUESTION was. Two
shapes, both measured on the live library:

  * **A rip-specific suffix.** The tag says "Ummagumma - Studio Album";
    MusicBrainz has "Ummagumma". Asked literally it fails forever — and
    that same library already holds "Pink Floyd | Ummagumma" as a
    successful `musicbrainz` hit. One album, two rows, opposite fates,
    decided entirely by a suffix the ripper added.

  * **A compilation keyed by ONE contributing performer.** We ask "does
    Harry Nilsson have an album called Voices of the 70s?" — no, and
    never will, because it isn't his. Dropping the artist and searching
    Cover Art Archive by TITLE ALONE found covers for 3 of 4 such
    albums.

**The exact name is always tried first**, so nothing that resolves
today can regress. The looser forms are fallbacks, in increasing order
of risk: a tidied title is still that album, while a title-only search
could in principle return a different record with the same name — which
is why it is offered only for multi-artist folders, where including the
artist is guaranteed to fail anyway.

Pure: no network, no DB. The caller does the asking.
"""
from __future__ import annotations

import re

# Bracketed groups whose CONTENT is format/scene noise. A year or a disc
# number is information and is handled separately, not lumped in here.
_CRUFT_INNER = re.compile(
    r"^\s*(?:"
    r"\d{1,2}\s*bit|24\s*bit|16\s*bit|flac|mp3|aac|wav|ape|dsd|"
    r"eac[\s\-]*flac|eac|cue|log|scans?|web|vinyl|remaster(?:ed)?|"
    r"deluxe(?:\s+edition)?|expanded|reissue|mono|stereo|hi-?res|"
    r"pmedia|oan|h33t|rlg|kitlope|int|retail|promo|\d{3,4}\s*kbps|"
    r"disc\s*\d+|cd\s*\d+|vol\.?\s*\d+\s*cd\s*\d+"
    r")\s*$", re.I)

# Edition words that follow a dash at the END of a title. Whole words,
# and only in that position: "Sign o' the Times - Live in Paris" is a
# title, "Ummagumma - Studio Album" is an edition marker.
_EDITION_TAIL = re.compile(
    r"\s*[-–—]\s*(?:studio|live|bonus|deluxe|special|expanded|remaster(?:ed)?)"
    r"\s+(?:album|edition|version|disc|cd)\s*$", re.I)

_DISC_TAIL = re.compile(r"\s*[-–—(\[]?\s*(?:disc|cd|disk)\s*\d+\s*[)\]]?\s*$", re.I)
_YEAR_TAIL = re.compile(r"\s*[(\[]?(?:19|20)\d{2}[)\]]?\s*$")


def tidy_album(name: str | None) -> str:
    """An album title with ripper/edition markings removed.

    For LOOKUP only — never for identity or display. A slightly-too-loose
    query costs one wasted request; a slightly-too-loose identity merges
    two albums."""
    out = name or ""
    # bracketed scene/format groups, innermost meaning first
    out = re.sub(r"[\[(\{]([^\[\](){}]*)[\])}]",
                 lambda m: "" if _CRUFT_INNER.match(m.group(1)) else m.group(0),
                 out)
    for _ in range(3):          # "… - Disc 2 (2001) [FLAC]" peels in layers
        before = out
        out = _EDITION_TAIL.sub("", out)
        out = _DISC_TAIL.sub("", out)
        out = _YEAR_TAIL.sub("", out)
        out = re.sub(r"\s{2,}", " ", out).strip(" -–—_.,")
        if out == before:
            break
    # Never hand back nothing: an empty query searches for everything
    # and caches the miss.
    return out or (name or "").strip()


_VARIOUS = ("various artists", "various", "va", "v.a.", "soundtrack")


def art_queries(artist: str, album: str, *,
                multi_artist: bool = False) -> list[tuple[str, str]]:
    """Ordered (artist, album) attempts for a cover lookup, best first.

    The caller stops at the first that yields a cover. An empty artist
    means "search by title alone"."""
    album = (album or "").strip()
    if not album:
        return []
    artist = (artist or "").strip()
    comp = multi_artist or artist.lower() in _VARIOUS
    out: list[tuple[str, str]] = []

    def add(a: str, b: str) -> None:
        if b and (a, b) not in out:
            out.append((a, b))

    add(artist, album)
    tidy = tidy_album(album)
    if tidy != album:
        add(artist, tidy)
    if comp:
        add("", tidy)
    return out
