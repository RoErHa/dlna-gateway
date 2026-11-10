#!/usr/bin/env python3
"""
api_upnp_browse_photo.py — the 📷 Photos tree (Immich) as browsed by the
LG TV's media player (2026-11-09 one album; 2026-11-10 the full tree).

Part of the api_upnp family (see api_upnp_browse_video.py for the list).
The data comes from `dlna_photos.INDEX.lib`, a snapshot a background thread
rebuilds and swaps in whole, so a Browse never waits on Immich. Each handler
reads `lib` ONCE, so a swap mid-request cannot mix two snapshots.

Shape (an empty branch is left out, like 📹 Videos):

    photos
    ├── 📅 By date      phdates → phyear:<YYYY> → phmonth:<YYYY-MM> → photos
    ├── 📚 Albums       phalbums → phalbum:<album id> → photos
    ├── 👤 People       phpeople → phperson:<b64 name> → photos
    ├── 📍 Places       phplaces → phcountry:<b64> → phcity:<b64 country\\0city> → photos
    └── ⭐ Favourites   phfavs → photos
    photo = ph:<asset id>, object.item.imageItem.photo

Years are newest first, months and photos chronological, albums newest
first (the screenshot albums last), people A-Z, places biggest first. A
person is one NAME, merged across Immich users.

A photo folder over 200 photos shows sub-folders instead (country → city →
year → month → week, dlna_photos_split): `phsplit:<b64 base id \\x1f
level=key …>`. The base is the folder it came from, so its photos are found
the same way and then narrowed one step at a time.

Each item's `<res>` AND its art point at `/gw/photo/<asset id>.jpg` on the
PLAIN :8765 bind (the TV does no TLS here): Immich's 1440 px JPEG preview,
because the TV shows no HEIC, NEF or WebP. See docs/PHOTO_SUPPORT.md.
"""
import os

import api_upnp_ids as _ids
import dlna_photos
import dlna_photos_split as _split
from api_upnp_didl import _Browse, _didl_container
from api_upnp_ids import _b64d, _b64e, _xml_esc
from dlna_photos_split import NO_CITY, month_title

PHOTOS_TITLE = "\U0001F4F7 Photos"
# 1440 px fits JPEG_LRG (≤ 4096²); OP=00: no byte seeking for a still image.
PROTOCOL = ("http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG;DLNA.ORG_OP=00;"
            "DLNA.ORG_CI=0;DLNA.ORG_FLAGS=00d00000000000000000000000000000")
BRANCHES = (("phdates", "\U0001F4C5 By date"), ("phalbums", "\U0001F4DA Albums"),
            ("phpeople", "\U0001F464 People"), ("phplaces", "\U0001F4CD Places"),
            ("phfavs", "⭐ Favourites"))
SEP = "\x1f"


def photo_base() -> str:
    """Where the TV fetches photos: this Mac's LAN address, plain port.
    Asked once per Browse — a month can hold a thousand items."""
    port = int(os.environ.get("GATEWAY_PLAIN_PORT", "8765"))
    return f"http://{_ids._get_lan_ip()}:{port}/gw/photo/"


def _didl_photo(p, parent_id: str, base: str) -> str:
    url = _xml_esc(f"{base}{p.id}.jpg")
    date = f"<dc:date>{_xml_esc(p.date)}</dc:date>" if p.date else ""
    return (f'<item id="ph:{_xml_esc(p.id)}" parentID="{_xml_esc(parent_id)}" '
            f'restricted="1"><dc:title>{_xml_esc(p.title)}</dc:title>{date}'
            f'<upnp:albumArtURI>{url}</upnp:albumArtURI>'
            f'<upnp:class>object.item.imageItem.photo</upnp:class>'
            f'<res protocolInfo="{PROTOCOL}">{url}</res></item>')


def _lib():
    return dlna_photos.INDEX.lib


def _counts(lib) -> dict:
    return {"phdates": len({m[:4] for m in lib.months}), "phalbums": len(lib.albums),
            "phpeople": len(lib.people), "phplaces": len(lib.places),
            "phfavs": len(lib.favourites)}


def photo_root_count() -> int:
    """How many branches 📷 Photos has; 0 keeps it out of the root."""
    return sum(1 for n in _counts(_lib()).values() if n)


def _split_id(path: list) -> str:
    return "phsplit:" + _b64e(SEP.join(path))


def _photos(ctx: _Browse, parent: str, title: str, photos: list,
            path: list, after: int = -1) -> tuple:
    """A folder of photos — or, when it is too big, of the groups it splits
    into. `path` is [base id, step …], `after` the level of the last step."""
    cut = _split.split(photos, after)
    if cut is None:
        if ctx.is_meta:
            return ctx.meta(parent, title, len(photos))
        base = photo_base()
        return ctx.listing([_didl_photo(p, ctx.obj_id, base) for p in ctx.page(photos)],
                           len(photos))
    level, groups = cut
    at = _split.LEVELS.index(level)
    return _folders(ctx, parent, title, [
        (_split_id(path + [f"{level}={k}"]), _split.label(level, k), _split.size(ps, at))
        for k, ps in groups])


def _folders(ctx: _Browse, parent: str, title: str, kids: list) -> tuple:
    """kids: [(id, title, child count)]."""
    if ctx.is_meta:
        return ctx.meta(parent, title, len(kids))
    return ctx.listing([_didl_container(cid, ctx.obj_id, t, n)
                        for cid, t, n in ctx.page(kids)], len(kids))


def _arg(ctx: _Browse) -> str:
    return ctx.obj_id.split(":", 1)[1]


def _cities(lib, country: str):
    return next((cs for c, cs in lib.places if c == country), None) if country else None


def _find(lib, oid: str):
    """(parent, title, photos) of a folder of photos, else None."""
    prefix, _, arg = oid.partition(":")
    found = None
    if oid == "phfavs":
        found = ("photos", BRANCHES[4][1], lib.favourites)
    elif prefix == "phmonth" and month_title(arg):
        found = (f"phyear:{arg[:4]}", month_title(arg), lib.months.get(arg))
    elif prefix == "phalbum":
        a = next((a for a in lib.albums if a.id == arg), None)
        found = a and ("phalbums", a.name, a.photos)
    elif prefix == "phperson" and _b64d(arg):
        name = _b64d(arg)
        found = ("phpeople", name, next((ps for n, ps in lib.people if n == name), None))
    elif prefix == "phcity":
        country, _, city = _b64d(arg).partition("\0")
        ps = next((ps for c, ps in (_cities(lib, country) or []) if c == city), None)
        found = (f"phcountry:{_b64e(country)}", city or NO_CITY, ps)
    return found if found and found[2] else None


def _br_list(ctx: _Browse) -> tuple:
    """phfavs, phmonth:, phalbum:, phperson:, phcity: — a folder of photos."""
    found = _find(_lib(), ctx.obj_id)
    if found is None:
        return ctx.empty()
    return _photos(ctx, *found, [ctx.obj_id])


def _br_phsplit(ctx: _Browse) -> tuple:
    """One group of a big folder: the base folder's photos, narrowed step by step."""
    path = _b64d(_arg(ctx)).split(SEP)
    found = _find(_lib(), path[0]) if _split.steps_ok(path[1:]) else None
    if found is None:
        return ctx.empty()
    photos = found[2]
    for step in path[1:]:
        level, _, k = step.partition("=")
        photos = [p for p in photos if _split.key(level, p) == k]
    if not photos:
        return ctx.empty()
    level, _, k = path[-1].partition("=")
    parent = path[0] if len(path) == 2 else _split_id(path[:-1])
    return _photos(ctx, parent, _split.label(level, k), photos, path,
                   _split.LEVELS.index(level))


def _br_photos(ctx: _Browse) -> tuple:
    counts = _counts(_lib())
    return _folders(ctx, "0", PHOTOS_TITLE,
                    [(cid, t, counts[cid]) for cid, t in BRANCHES if counts[cid]])


def _br_phdates(ctx: _Browse) -> tuple:
    years = {}
    for m in _lib().months:
        years[m[:4]] = years.get(m[:4], 0) + 1
    return _folders(ctx, "photos", BRANCHES[0][1],
                    [(f"phyear:{y}", y, n) for y, n in sorted(years.items(), reverse=True)])


def _br_phyear(ctx: _Browse) -> tuple:
    year, lib = _arg(ctx), _lib()
    months = sorted(m for m in lib.months if m[:4] == year and month_title(m))
    if not months:
        return ctx.empty()
    return _folders(ctx, "phdates", year, [
        (f"phmonth:{m}", month_title(m), _split.size(lib.months[m], -1)) for m in months])


def _br_phalbums(ctx: _Browse) -> tuple:
    return _folders(ctx, "photos", BRANCHES[1][1], [
        (f"phalbum:{a.id}", a.name, _split.size(a.photos, -1)) for a in _lib().albums])


def _br_phpeople(ctx: _Browse) -> tuple:
    return _folders(ctx, "photos", BRANCHES[2][1], [
        (f"phperson:{_b64e(n)}", n, _split.size(ps, -1)) for n, ps in _lib().people])


def _br_phplaces(ctx: _Browse) -> tuple:
    return _folders(ctx, "photos", BRANCHES[3][1], [
        (f"phcountry:{_b64e(c)}", c, len(cities)) for c, cities in _lib().places])


def _br_phcountry(ctx: _Browse) -> tuple:
    country = _b64d(_arg(ctx))
    cities = _cities(_lib(), country)
    if not cities:
        return ctx.empty()
    return _folders(ctx, "phplaces", country, [
        (f"phcity:{_b64e(country + chr(0) + city)}", city or NO_CITY, _split.size(ps, -1))
        for city, ps in cities])


def _br_ph(ctx: _Browse) -> tuple:
    """An item describes itself on BrowseMetadata; it has no children."""
    p = _lib().photos.get(_arg(ctx))
    if p is None or not ctx.is_meta:
        return ctx.empty()
    parent = f"phmonth:{p.date[:7]}" if p.date else "photos"
    return ctx.listing([_didl_photo(p, parent, photo_base())], 1)


EXACT = {"photos": _br_photos, "phdates": _br_phdates, "phalbums": _br_phalbums,
         "phpeople": _br_phpeople, "phplaces": _br_phplaces, "phfavs": _br_list}
PREFIX = (("phyear:", _br_phyear), ("phmonth:", _br_list), ("phalbum:", _br_list),
          ("phperson:", _br_list), ("phcountry:", _br_phcountry),
          ("phcity:", _br_list), ("phsplit:", _br_phsplit), ("ph:", _br_ph))
