#!/usr/bin/env python3
"""
dlna_asgi_artist.py — `GET /api/artist_info`, the artist panel's one
request.

Its own module because `dlna_asgi_browse.py` sits at 397 of its 400
lines, and because this is a feature seam rather than an overflow: the
panel is a self-contained read that will grow (photos, related acts)
while browse will not.

The endpoint resolves everything the panel would otherwise have to
decide:

  * The LINE-UP is already intersected with the track's year, so the
    PWA renders rows rather than reasoning about date ranges.
  * It arrives LABELLED — `lineup_tier` is 'inferred' for a line-up
    derived from membership dates and 'credits' when MusicBrainz names
    the players on that recording. Handing the UI a bare list would
    make presenting a guess as a fact a one-line mistake.
  * Comma-joined columns arrive as LISTS, so the same splitting does
    not end up living in two languages.

Nothing here touches the network: the facts were fetched offline by
tools/artist_meta.py, so opening the panel is a local read.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import dlna_asgi_state as _st
from dlna_lineup import lineup_at

router = APIRouter()


def _split(value) -> list[str]:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _as_year(value) -> int | None:
    """A track's year arrives from the client, so it is untrusted and
    may be junk. A bad year degrades to 'no line-up claim' — the same
    answer as no year at all — rather than failing the whole panel."""
    try:
        y = int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None
    return y if 1000 <= y <= 2999 else None


def _payload(artist: str, year) -> dict | None:
    row = _st.DB.artist_info_get(artist)
    # A row with an mbid but no facts yet means the sweep resolved the
    # id and has not fetched the rest. An empty panel is worse than no
    # panel, so that is a 404 like any unknown artist.
    if not row or not row.get("meta_fetched_at"):
        return None
    y = _as_year(year)
    members = [{"name": m["member_name"],
                "instruments": m["instruments"],
                "begin": m["begin_date"], "end": m["end_date"]}
               for m in _st.DB.artist_members_get(artist)]
    lineup = lineup_at(members, y)
    return {
        "artist": row.get("artist") or artist,
        "mbid": row.get("mbid") or "",
        "mb_type": row.get("mb_type") or "",
        "gender": row.get("gender") or "",
        "born": row.get("born") or "",
        "died": row.get("died") or "",
        "birth_place": row.get("birth_place") or "",
        "country": row.get("country") or "",
        "disambiguation": row.get("disambiguation") or "",
        "genres": _split(row.get("genres")),
        "bio": row.get("bio") or "",
        # Never sent without the other: CC BY-SA attribution.
        "bio_url": row.get("bio_url") or "",
        "image_url": row.get("image_url") or "",
        "top_tracks": _split(row.get("top_tracks")),
        "also_in": _split(row.get("notable")),
        "lineup": [{"name": m["name"], "instruments": m["instruments"]}
                   for m in lineup],
        "lineup_year": y if lineup else None,
        "lineup_tier": "inferred" if lineup else "",
        "track_count": _st.DB.artist_track_count(artist),
    }


@router.get("/api/artist_info")
async def artist_info(artist: str = "", year: str = ""):
    if not artist.strip():
        return _missing_artist()
    data = await run_in_threadpool(_payload, artist.strip(), year)
    if data is None:
        return JSONResponse({"error": "artist not in library"},
                            status_code=404)
    return data


def _missing_artist():
    return JSONResponse({"error": "missing artist"}, status_code=400)
