#!/usr/bin/env python3
"""
api_upnp_browse_video.py — the 📹 Videos tree (GWMovies) as browsed
by the LG WebOS TV.

Split out of api_upnp.py on 2026-08-20, when that module reached
1,349 lines. The family is:

    api_upnp_ids.py          identity, id codecs, junk filter, library reads
    api_upnp_didl.py         DIDL-Lite renderers + the _Browse request context
    api_upnp_browse.py       music/books/playlists/favourites handlers + dispatch
    api_upnp_browse_video.py the GWMovies video tree handlers
    api_upnp_descriptors.py  device.xml + the two service SCPDs
    api_upnp_ssdp.py         SSDP announce/M-SEARCH + GENA eventing
    api_upnp.py              SOAP control endpoints + the public re-exports

api_upnp re-exports every public name, so `import api_upnp` and
`api_upnp.<anything>` keep working for callers and tests.

Shape: `videos` → 📅 By date (year → month) · 📍 By location (country blocks →
locations, with a "(no city)" bucket for country-only inferred rows) ·
👤 By person (Immich tags; the container is absent when no persons exist) ·
🎞 All videos. The flat ~3,000-item list was unbrowsable with a TV remote,
which is why the sub-containers exist at all.

Country CONTAINERS show full country names while their ids keep the ISO code
(2026-07-08) — the codec and the display string are deliberately different
things here. See docs/VIDEO_SUPPORT.md.
"""
import logging

from dlna_countries import country_name

import api_upnp_ids as _ids
from api_upnp_didl import (
    _DIDL_CLOSE,
    _DIDL_OPEN,
    _Browse,
    _didl_container,
    _didl_video,
)
from api_upnp_ids import _b64d, _b64e, _VIDEO_UDN

log = logging.getLogger("dlna.api.upnp")


# ── Handlers: videos tree (2026-07-06) ────────────────────────────
# The flat ~3,000-item list was unbrowsable with a TV remote. "videos"
# now holds three sub-containers: date drill-down (year → month),
# location A-Z (geocoded location_name; "(no location)" bucket last),
# and the old flat list under "vidall".

def _br_videos(ctx: _Browse) -> tuple:
    if ctx.is_meta:
        return ctx.meta("0", "\U0001F4F9 Videos", len(_ids.DB.all_videos(_VIDEO_UDN)))
    years  = _ids.DB.video_years(_VIDEO_UDN)
    locs   = _ids.DB.video_locations(_VIDEO_UDN)
    people = _ids.DB.video_people_list(_VIDEO_UDN)
    n      = len(_ids.DB.all_videos(_VIDEO_UDN))
    kids = [
        _didl_container("viddates", "videos", "\U0001F4C5 By date", len(years)),
        _didl_container("vidlocs", "videos", "\U0001F4CD By location", len(locs)),
    ]
    # "👤 By person" only when the Immich people sync has run — an
    # always-empty folder would just be noise for non-Immich setups.
    if people:
        kids.append(_didl_container("vidpeople", "videos",
                                    "\U0001F464 By person", len(people)))
    kids.append(_didl_container("vidall", "videos", "\U0001F39E All videos", n))
    # NOTE: deliberately NOT paginated — four fixed entries.
    return _DIDL_OPEN + "".join(kids) + _DIDL_CLOSE, len(kids), len(kids)


def _br_vidall(ctx: _Browse) -> tuple:
    vids = _ids.DB.all_videos(_VIDEO_UDN)
    if ctx.is_meta:
        return ctx.meta("videos", "\U0001F39E All videos", len(vids))
    items = [_didl_video(v, "vidall") for v in ctx.page(vids)]
    return ctx.listing(items, len(vids))


def _br_viddates(ctx: _Browse) -> tuple:
    years = _ids.DB.video_years(_VIDEO_UDN)
    if ctx.is_meta:
        return ctx.meta("videos", "\U0001F4C5 By date", len(years))
    items = [_didl_container(f"viddate:{y['year']}", "viddates",
                             y["year"], y["count"]) for y in ctx.page(years)]
    return ctx.listing(items, len(years))


def _br_viddate(ctx: _Browse) -> tuple:
    key = ctx.obj_id[len("viddate:"):]
    if len(key) == 4:                       # a year → its months
        months = _ids.DB.video_months(_VIDEO_UDN, key)
        if ctx.is_meta:
            return ctx.meta("viddates", key, len(months))
        items = [_didl_container(f"viddate:{m['month']}", ctx.obj_id,
                                 m["month"], m["count"]) for m in ctx.page(months)]
        return ctx.listing(items, len(months))
    vids = _ids.DB.videos_by_month(_VIDEO_UDN, key)    # 'YYYY-MM' → items
    if ctx.is_meta:
        return ctx.meta(f"viddate:{key[:4]}", key, len(vids))
    items = [_didl_video(v, ctx.obj_id) for v in ctx.page(vids)]
    return ctx.listing(items, len(vids))


def _br_vidlocs(ctx: _Browse) -> tuple:
    # 2026-07-06 v2: COUNTRY blocks first (A-Z by ISO code), then
    # "(no country)" for located-but-unknown-country videos, then the
    # "(no location)" bucket for GPS-less videos — each country drills
    # down to its locations (country_location, like the titles).
    countries = _ids.DB.video_countries(_VIDEO_UDN)
    no_loc = [r for r in _ids.DB.video_locations(_VIDEO_UDN)
              if not r["location_name"]]
    entries = []
    # selection level shows FULL country names (2026-07-08); the ids
    # (and titles/filenames elsewhere) keep the ISO code
    for c in countries:
        entries.append(("vidcountry-none" if not c["country"]
                        else f"vidcountry:{c['country']}",
                        country_name(c["country"]) or "(no country)",
                        c["count"]))
    for r in no_loc:
        entries.append(("vidloc-none", "(no location)", r["count"]))
    if ctx.is_meta:
        return ctx.meta("videos", "\U0001F4CD By location", len(entries))
    items = [_didl_container(cid, "vidlocs", title, n)
             for cid, title, n in ctx.page(entries)]
    return ctx.listing(items, len(entries))


def _br_vidcountry(ctx: _Browse) -> tuple:
    cc = ("" if ctx.obj_id == "vidcountry-none"
          else ctx.obj_id[len("vidcountry:"):])
    locs = _ids.DB.video_locations_for_country(_VIDEO_UDN, cc)
    if ctx.is_meta:
        return ctx.meta("vidlocs", country_name(cc) or "(no country)", len(locs))
    # '' location = the "(no city)" bucket — country-only videos
    # (Plan A inferred country, no specific place).
    items = [_didl_container(
        "vidcloc:" + _b64e(cc + "\x00" + r["location_name"]), ctx.obj_id,
        r["location_name"] or "(no city)", r["count"]) for r in ctx.page(locs)]
    return ctx.listing(items, len(locs))


def _br_vidcloc(ctx: _Browse) -> tuple:
    raw = _b64d(ctx.obj_id[len("vidcloc:"):])
    if "\x00" not in raw:
        return ctx.empty()
    cc, loc = raw.split("\x00", 1)
    vids = _ids.DB.videos_by_country_location(_VIDEO_UDN, cc, loc)
    if ctx.is_meta:
        return ctx.meta("vidcountry-none" if not cc else f"vidcountry:{cc}",
                        loc or "(no city)", len(vids))
    items = [_didl_video(v, ctx.obj_id) for v in ctx.page(vids)]
    return ctx.listing(items, len(vids))


def _br_vidpeople(ctx: _Browse) -> tuple:
    people = _ids.DB.video_people_list(_VIDEO_UDN)
    if ctx.is_meta:
        return ctx.meta("videos", "\U0001F464 By person", len(people))
    items = [_didl_container("vidperson:" + _b64e(p["person"]), "vidpeople",
                             p["person"], p["count"]) for p in ctx.page(people)]
    return ctx.listing(items, len(people))


def _br_vidperson(ctx: _Browse) -> tuple:
    person = _b64d(ctx.obj_id[len("vidperson:"):])
    if not person:
        return ctx.empty()
    vids = _ids.DB.videos_by_person(_VIDEO_UDN, person)
    if ctx.is_meta:
        return ctx.meta("vidpeople", person, len(vids))
    if not vids:
        return ctx.empty()
    items = [_didl_video(v, ctx.obj_id) for v in ctx.page(vids)]
    return ctx.listing(items, len(vids))


def _br_vidloc(ctx: _Browse) -> tuple:
    if ctx.obj_id == "vidloc-none":
        loc = ""
    else:
        loc = _b64d(ctx.obj_id[len("vidloc:"):])
        if not loc:
            return ctx.empty()
    vids = _ids.DB.videos_by_location(_VIDEO_UDN, loc)
    if ctx.is_meta:
        return ctx.meta("vidlocs", loc or "(no location)", len(vids))
    items = [_didl_video(v, ctx.obj_id) for v in ctx.page(vids)]
    return ctx.listing(items, len(vids))


def _br_vid(ctx: _Browse) -> tuple:
    v = _ids.DB.video_by_id(ctx.obj_id[len("vid:"):])
    if not v:
        return ctx.empty()
    # Both flags answer with the item itself — a leaf has no children.
    return _DIDL_OPEN + _didl_video(v, "vidall") + _DIDL_CLOSE, 1, 1
