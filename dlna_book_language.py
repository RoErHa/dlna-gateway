#!/usr/bin/env python3
"""
dlna_book_language.py — What language is this audiobook in? Decided
purely, from what the index already holds.

The 🌐 letter-bar entry on the audiobooks source groups books by
language, and the evidence for that is thin: measured over 491 books,
**seven** carried a language tag (all "English") and the rest none.
OpenLibrary is no help either — a work there has editions in a dozen
languages, which is exactly how translated series names got into
`book_meta` before the English/Dutch rule was added.

What every book DOES carry is words: its folder path, its album tag and
its chapter titles. `detect_language` counts the function words of each
language it knows across those strings and answers only when one
language clearly wins. Otherwise it answers `''`, which the UI shows as
"Unknown" — never a default guess. A wrong language files a book where
nobody looks for it; an Unknown is visibly unfinished and gets fixed
(`tools/book_language.py --set`).

Three rules, each learned from the real library:

  * **A name is not evidence — but its connecting words are.** "Dan" is
    a Dutch word ("then"), and Dan Simmons' *Hyperion* came out Dutch
    because his name sits in every album tag. So the CAPITALISED words
    of the author/artist names the caller passes are dropped wherever
    they appear. The lowercase ones are kept and counted: "Betje Wolff
    *en* Aagje Deken" is Dutch for the same reason "Nick Cave *and* the
    Bad Seeds" is English.
  * **Each distinct string counts once.** An album tag is repeated on
    every chapter; counting it per row lets one tag outvote everything.
    Distinct chapter titles DO each count — fifty "Hoofdstuk N" files
    are fifty pieces of evidence.
  * **Words shared by both languages are left out of both lists** —
    `in`, `is`, `was`, `over`, `door`, `die`, `man`, `we`, `had`. A
    word that cannot tell the two apart is not evidence for either.

Only English and Dutch are detected: those are the languages this
library holds, and every word list added is another source of false
positives from names. Any other language arrives through a file tag or
a manual row, and `normalize_language` turns either into the same
ISO 639-1 code the detector emits.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# Distinctive function words + the audiobook vocabulary that shows up in
# chapter titles. Nothing here may appear in the other list.
_WORDS: dict[str, frozenset[str]] = {
    "nl": frozenset("""
        de het een van en niet zijn voor met naar ook maar nog hoe waar
        geen tot onder tegen uit bij aan dat wordt werd heeft hij zij ik
        mijn haar wij ze als om te op deze dit wie wat toen worden kunnen
        moet onze ons hoofdstuk deel boek boeken verhaal verhalen
        sprookje sprookjes eerste tweede derde laatste wereld alleen jaar
        mijlen ridder koning hun zich wel zo heel veel historie
        geschiedenis inleiding sectie voorwoord nawoord mejuffrouw
        hoofdstukken verteld gelezen voorgelezen
        """.split()),
    "en": frozenset("""
        the of and to that with for from by his her it this are be have
        you your my they at on an as not but all one two three how what
        who when where why into out up no new life world war death end
        night day first last king chapter part book books story stories
        tales prologue epilogue introduction volume trilogy collection
        audiobook unabridged abridged narrated edition read their our a
        act scene section mystery murder secret house game children time
        memory long sun green blue red gray grey dark light black white
        dead space stranger strange great little perfect girl boy shadow
        fantasy thriller horror fiction science guide complete
        """.split()),
}
assert not (_WORDS["nl"] & _WORDS["en"]), "a shared word is not evidence"

_MIN_HITS = 2        # one word is a coincidence, not a claim about a book
_MARGIN = 2          # the winner needs twice the runner-up's hits

_TOKEN = re.compile(r"[^\W\d_]+")    # letters only: splits `max_havelaar`


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def detect_language(texts: Iterable[str], names: Iterable[str] = ()) -> str:
    """ISO 639-1 code of the language `texts` are written in, or `''`.

    `texts` — the strings that describe one book (path components, album
    tags, chapter titles); duplicates are counted once. `names` — the
    people on it (author, narrator); their words are never evidence.
    """
    names = [n for n in names if n]
    skip = {w.lower() for n in names for w in _TOKEN.findall(n) if w[0].isupper()}
    hits = dict.fromkeys(_WORDS, 0)
    for text in {t for t in (*texts, *names) if t}:
        for tok in _tokens(text):
            if tok in skip:
                continue
            for lang, words in _WORDS.items():
                if tok in words:
                    hits[lang] += 1
    ranked = sorted(hits.items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_, second) = ranked[0], ranked[1]
    if top >= _MIN_HITS and top >= _MARGIN * second:
        return best
    return ""


# Tag / hand-typed spellings → ISO 639-1. ID3 TLAN is ISO 639-2 (three
# letters, both B and T forms in the wild); MP4 and Vorbis carry
# whatever the tagger wrote, usually an English name.
_ALIASES = {
    "en": "en", "eng": "en", "english": "en", "engels": "en",
    "nl": "nl", "nld": "nl", "dut": "nl", "dutch": "nl",
    "nederlands": "nl", "flemish": "nl", "vlaams": "nl",
    "de": "de", "deu": "de", "ger": "de", "german": "de", "deutsch": "de",
    "fr": "fr", "fra": "fr", "fre": "fr", "french": "fr", "français": "fr",
    "es": "es", "spa": "es", "spanish": "es", "español": "es",
    "it": "it", "ita": "it", "italian": "it", "italiano": "it",
}
_TWO_LETTER = re.compile(r"^[a-z]{2}$")


def normalize_language(value) -> str:
    """A tag value or a typed code → ISO 639-1, or `''` when it names no
    language. `XXX` / `und` / `zxx` / `mul` are the tag formats' own
    "unknown" markers and stay unknown. A region suffix is dropped
    (`en-GB` → `en`); an unrecognised two-letter code is trusted as one.
    """
    v = str(value or "").strip().lower().replace("_", "-")
    if not v:
        return ""
    if v in _ALIASES:
        return _ALIASES[v]
    head = v.split("-", 1)[0]
    if head in _ALIASES:
        return _ALIASES[head]
    return head if _TWO_LETTER.match(head) else ""
