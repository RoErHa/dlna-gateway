#!/usr/bin/env python3
"""
dlna_lineup.py — who was in the band when this song was recorded?

Step 3's distinctive piece, and a pure function so the awkward cases
are tests rather than discoveries made in front of a listener.

**Why it exists.** Measured against the live MusicBrainz API on this
library, per-RECORDING performer credits exist for only ~17% of tracks
(release-level ~22%); Pink Floyd's 'Shine On You Crazy Diamond' has
none. So a "who played on this" feature built only on credits would be
blank four times out of five. Band MEMBERSHIP, however, is near
universal for groups AND carries instruments and date ranges — which
makes the per-song question answerable by intersecting those ranges
with the recording's year.

**It is inference, and the caller must label it as such.** The UI shows
tier-1 recording credits as "credited on this recording" and this as
"line-up in 1975". Presenting a guess in the same voice as a fact is
the failure mode `dlna_artist_infer` was written to avoid, one domain
over.

The two cases that make this a real function rather than a list
lookup:

  * **Members rejoin.** Richard Wright is TWO rows on Pink Floyd
    (1965–1981 and 1987–2008). A first-match lookup reports him absent
    from The Division Bell, which he played on.
  * **A missing date is not an absent member.** MusicBrainz often has
    the membership without the range. Dropping those would make a band
    look emptier than it was, so they are always included — the same
    honesty level as the rest of the inference.
"""
from __future__ import annotations

import re

_YEAR = re.compile(r"^(\d{4})")


def _year_of(value: str | None) -> int | None:
    """Leading year of an ISO-ish MusicBrainz date ('1968-02-18',
    '1965', ''). None when there is no date at all."""
    if not value:
        return None
    m = _YEAR.match(str(value).strip())
    return int(m.group(1)) if m else None


def lineup_at(members, year: int | None) -> list[dict]:
    """The members active in `year`, in the order given.

    Returns [] when `year` is unknown. That is deliberate and is the
    whole contract: the claim being made is "who was in the band THEN",
    so with no year there is no claim, and returning the full roster
    would quietly assert something different and wrong.

    A member whose stint has no dates at all is always included; a
    member with only a start is treated as still present."""
    if not members or not year:
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for m in members:
        begin = _year_of(m.get("begin"))
        end = _year_of(m.get("end"))
        # Inclusive on both ends: a year that equals the join or leave
        # year overlaps it, since a bare '1965' means "some time in
        # 1965" and a recording year is no more precise than that.
        if begin is not None and year < begin:
            continue
        if end is not None and year > end:
            continue
        name = (m.get("name") or "").strip()
        if not name or name in seen:
            continue          # one person, not one row per stint
        seen.add(name)
        out.append(m)
    return out
