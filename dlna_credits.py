#!/usr/bin/env python3
"""
dlna_credits.py — is this songwriting credit a NAME, or machine junk?

One pure decision, shared by every surface that shows composer/lyricist
(the PWA now-playing panel today; Subsonic/UPnP when they grow the
field), so the surfaces can never disagree about what is displayable.

**Display-only.** The tag in the file and the row in `tracks` are left
exactly as they are — this decides what to SHOW. A tool that repairs
files is a separate job with a separate risk profile.

Why it exists: scene releases advertise in the tag fields. Measured on
the live library, 691 of 8,570 credited tracks (8%) carry
`www.t.me/pmedia_music` or `www.thenzbplace.com` as composer or
lyricist, so the panel read "Words www.t.me/pmedia_music".

Both rules below were measured against ALL 2,447 distinct credit values
in the library before being adopted: each matches exactly 2 of them,
with zero false positives.

⚠ **The rule that was tried and REJECTED** — "a credit containing no
A-Za-z letter is junk". It matched 55 rows, every one a real composer:
Стравинский, Прокофьев, Чайковский, Рахманинов, Ջիվան Գասպարյան. A
shape test that encodes "looks English" erases real data — the same
lesson as `dlna_artist_infer.is_a_performer_name`'s `allow_numeric`
(112, 911, 98° are bands). Never require a particular script.
"""
from __future__ import annotations

import re

# A credit advertising a website or a Telegram channel is an advert.
_URLISH = re.compile(r"(https?://|www\.|\bt\.me/)", re.I)

# Three or more of the SAME punctuation character in a row ('....',
# '¤¤¤¤'). Machine noise — no human name has it, while single dots
# (initials: 'J.S. Bach') and single separators stay untouched.
_PUNCT_RUN = re.compile(r"([^\w\s])\1{2,}")


def clean_credit(value: str | None) -> str:
    """The credit as it should be DISPLAYED, or '' when it is not a name.

    '' is the same answer as "no credit in the file", so every caller
    already handles it — a junk value simply collapses the line rather
    than needing a separate branch."""
    if not value:
        return ""
    v = value.strip()
    if not v:
        return ""
    if _URLISH.search(v):
        return ""
    if _PUNCT_RUN.search(v):
        return ""
    return v
