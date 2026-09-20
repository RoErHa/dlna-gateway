#!/usr/bin/env python3
"""
dlna_mbid.py — "is this MusicBrainz artist OUR artist?", decided purely.

The keystone of the metadata work: MusicBrainz, Wikidata, Wikipedia and
ListenBrainz all key off an artist MBID, so one wrong id here is wrong
in four places at once. And a wrong id is worse than none — a blank
invites a fix, a plausible-looking biography for the OTHER band called
Nirvana never gets questioned. Hence the governing rule, the same one
`dlna_artist_infer` follows: **refuse rather than guess.**

No network here. The caller does the HTTP; this module decides what to
ask and what to believe, which is what makes both testable.

Measured on a random sample of this library's artists (not the popular
head): 83% matched cleanly on the raw name alone, and the failures were
all one of two shapes, which `norm_artist` and `query_names` handle —
punctuation/article variants that are the SAME artist, and
featuring-credits that name a collaborator.

⚠ The `&` rule is the one that took evidence to get right — see
`query_names`.
"""
from __future__ import annotations

import re

from dlna_library_sql import _norm_title

# Punctuation MusicBrainz and a file tag disagree about freely:
# "Jr. Walker" vs "Jr Walker", "Guns N' Roses" vs "Guns N’ Roses".
_PUNCT = re.compile(r"[.,'\"!?()\[\]/\\:;]")

# EVERY dash, folded to a space. This is the highest-value rule here:
# MusicBrainz spells artist names with TYPOGRAPHIC dashes while file
# tags carry the ASCII hyphen-minus — or no dash at all. Measured on the
# first live sweep, both of these were refused at score 100:
#     tag 'Bachman-Turner Overdrive'  (U+002D)
#     MB  'Bachman–Turner Overdrive'  (U+2013 EN DASH)
#     tag 'Jean Michel Jarre'         (space)
#     MB  'Jean‐Michel Jarre'         (U+2010 HYPHEN)
# Folding to a SPACE rather than deleting is what makes the second pair
# work, and it cannot merge distinct acts — a name that differs only by
# hyphen-vs-space is the same act ('Jay-Z' / 'Jay Z').
_DASHES = re.compile(r"[\u002d\u2010\u2011\u2012\u2013\u2014\u2015\u2212]")

# '&' and 'and' are one word: 'Delaney And Bonnie' in the tags,
# 'Delaney & Bonnie' in MusicBrainz. Word-bounded on purpose — a blind
# replace would maul 'Andrews' and 'Anderson'.
_AND = re.compile(r"\band\b")
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.I)

# A credited collaborator, not part of the act's name.
_FEAT = re.compile(r"\s+(feat\.?|ft\.?|featuring|with)\s+", re.I)

MIN_SCORE = 95            # MB text-relevance floor


def norm_artist(name: str | None) -> str:
    """Comparison key for two spellings of one artist.

    Builds on `_norm_title` (NFKD + combining-mark strip, smart quotes →
    ASCII, whitespace collapse, lowercase) and additionally drops
    punctuation and a leading article, because those are exactly what MB
    and a file tag differ on."""
    if not name:
        return ""
    s = _norm_title(name)
    s = _DASHES.sub(" ", s)
    s = _PUNCT.sub("", s)
    s = _AND.sub("&", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Only drop the article when something survives it: a band actually
    # called "The" must not normalise to the empty string, which would
    # match every other empty key.
    stripped = _LEADING_ARTICLE.sub("", s).strip()
    return stripped or s


def query_names(name: str) -> list[str]:
    """The names to search, best first. The caller tries each in order
    and stops at the first accepted match.

    **The full credit is always first.** That ordering is the whole
    safety argument for the `&` fallback: `&` sits inside band names far
    more often than it joins two artists — measured here, 'Nick Cave &
    The Bad Seeds', 'Billy Larkin & The Delegates' and 'Jr Walker & The
    All Stars' are all single acts that match whole. Splitting on `&`
    eagerly would have broken all three. Trying the split form only
    AFTER the full name has failed rescues a genuine pairing like 'Hans
    Zimmer & Benjamin Wallfisch' at no risk to the bands.

    A comma is never split at all: 'Anderson, Bruford, Wakeman, Howe' is
    the band's name, and a comma list is indistinguishable from it."""
    raw = (name or "").strip()
    if not raw:
        return []
    out = [raw]

    def _add(v: str) -> None:
        v = v.strip(" ,;&")
        if v and v not in out:
            out.append(v)

    parts = _FEAT.split(raw, maxsplit=1)
    if len(parts) > 1:
        _add(parts[0])
    if ";" in raw:
        _add(raw.split(";", 1)[0])
    if "&" in raw:
        _add(raw.split("&", 1)[0])
    # MusicBrainz's QUOTED search returns nothing at all for
    # artist:"The New York Dolls" (measured live), so a leading article
    # gets a bare attempt too. Free: accept_match still has to approve
    # whatever comes back.
    unarticled = _LEADING_ARTICLE.sub("", raw).strip()
    if unarticled and unarticled != raw:
        _add(unarticled)
    return out


def _alias_keys(cand: dict) -> set[str]:
    """Every spelling MusicBrainz records for this artist. MB omits the
    key entirely when there are none."""
    out = set()
    for a in (cand.get("aliases") or []):
        for field in ("name", "sort-name"):
            v = a.get(field)
            if v:
                out.add(norm_artist(v))
    return out


def accept_match(query: str, candidates: list[dict]) -> str | None:
    """The mbid to trust, or None.

    Three things must hold: the top hit clears `MIN_SCORE`, its name
    means the same as what we asked for, and **no other candidate at
    that score shares the name**. MB's score is TEXT relevance — 'Elbo'
    can score 100 for 'Elbow' — so the name check is what does the real
    work, and the namesake check is what stops us silently picking one
    of the several real bands called Nirvana."""
    key = norm_artist(query)
    if not key or not candidates:
        return None

    scored = [c for c in candidates
              if int(c.get("score") or 0) >= MIN_SCORE]
    by_name = [c for c in scored if norm_artist(c.get("name")) == key]
    # An ALIAS is how MB records the shorter or punctuated spelling a
    # tag usually carries: 'Gonzales' for 'Chilly Gonzales', 'AC-DC'
    # for 'AC/DC'. They arrive on the same search response, so widening
    # to them costs no extra request. Measured: recovers ~15% of
    # refusals. A canonical NAME is stronger evidence, so aliases are
    # only consulted when nothing matched by name.
    hits = by_name or [c for c in scored if key in _alias_keys(c)]

    # The ambiguity guard must span both, or widening the match quietly
    # reintroduces the namesake problem it exists to prevent — several
    # real acts are called Nirvana, and picking the popular one is a
    # guess that is silently wrong forever.
    if len(hits) != 1:
        return None
    return hits[0].get("id") or None
