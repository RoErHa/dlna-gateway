#!/usr/bin/env python3
"""
dlna_photos_split.py — how a big 📷 Photos folder breaks up so a TV remote
can get through it (2026-11-10).

Some folders were thousands of photos long: the album "Recent" 11,840,
"Vastgelegd door mij" 9,121, December 2023 6,645 (an import date). A folder
holding more than SPLIT_OVER photos shows sub-folders instead, in the order
the owner asked for:

    country → city → year → month → week

A level that would not divide the photos (one country, one year) is skipped,
so a month splits by place and then straight into weeks. A group that is
small enough lists its photos; one still too big splits again, from the next
level down. Past the week, the photos are listed as they are.

Order: places biggest first (like 📍 Places), dates old to new (like the
photos in a folder); the "no place"/"no date" group always last. Names follow
the rest of the tree: "Netherlands", "Hoorn", "2023", "December 2023",
"Week 49 · 2023" (ISO weeks).

Pure: photos in, groups and names out. The ids are api_upnp_browse_photo's.
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date

SPLIT_OVER = 200
LEVELS = "ctymw"        # country, town (city), year, month, week
NO_PLACE = "(no place)"
NO_CITY = "(no city)"
NO_DATE = "(no date)"


def month_title(m: str) -> str:
    """'2023-12' -> 'December 2023'; '' for anything else."""
    if len(m) != 7 or not m[5:7].isdigit() or not 1 <= int(m[5:7]) <= 12:
        return ""
    return f"{calendar.month_name[int(m[5:7])]} {m[:4]}"


def key(level: str, p) -> str:
    if level == "c":
        return p.country
    if level == "t":
        return p.city
    if level == "y":
        return p.date[:4]
    if level == "m":
        return p.date[:7]
    try:
        y, w, _ = date.fromisoformat(p.date[:10]).isocalendar()
    except ValueError:
        return ""
    return f"{y}-W{w:02d}"


def label(level: str, k: str) -> str:
    if level == "c":
        return k or NO_PLACE
    if level == "t":
        return k or NO_CITY
    if not k:
        return NO_DATE
    if level == "m":
        return month_title(k) or NO_DATE
    if level == "w":
        y, _, w = k.partition("-W")
        return f"Week {int(w)} · {y}" if w.isdigit() else NO_DATE
    return k


def split(photos: list, after: int = -1):
    """(level, [(key, photos)]) for the first level below LEVELS[after] that
    divides `photos` into two or more groups; None when they list as they are."""
    if len(photos) <= SPLIT_OVER:
        return None
    for level in LEVELS[after + 1:]:
        groups = defaultdict(list)
        for p in photos:
            groups[key(level, p)].append(p)
        if len(groups) > 1:
            if level in "ct":
                order = lambda kv: (kv[0] == "", -len(kv[1]), kv[0])     # noqa: E731
            else:
                order = lambda kv: (kv[0] == "", kv[0])                  # noqa: E731
            return level, sorted(groups.items(), key=order)
    return None


def size(photos: list, after: int) -> int:
    """How many children a folder of `photos` shows: groups, or photos."""
    cut = split(photos, after)
    return len(cut[1]) if cut else len(photos)


def steps_ok(steps: list) -> bool:
    """Each step is '<level>=<key>', levels strictly in LEVELS order."""
    last = -1
    for s in steps:
        lv, eq, _ = s.partition("=")
        if not eq or len(lv) != 1 or lv not in LEVELS or LEVELS.index(lv) <= last:
            return False
        last = LEVELS.index(lv)
    return bool(steps)
