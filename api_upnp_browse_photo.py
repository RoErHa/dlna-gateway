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
first, people A-Z, places biggest first. A person is one NAME, merged across
Immich users (each user names faces separately).

Each item's `<res>` AND its art point at `/gw/photo/<asset id>.jpg` on the
PLAIN :8765 bind (the TV does no TLS here): Immich's 1440 px JPEG preview,
because the TV shows no HEIC, NEF or WebP. See docs/PHOTO_SUPPORT.md.
"""
import calendar
import os

import api_upnp_ids as _ids
import dlna_photos
from api_upnp_didl import _Browse, _didl_container
from api_upnp_ids import _b64d, _b64e, _xml_esc

PHOTOS_TITLE = "\U0001F4F7 Photos"
# 1440 px fits JPEG_LRG (≤ 4096²); OP=00: no byte seeking for a still image.
PROTOCOL = ("http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG;DLNA.ORG_OP=00;"
            "DLNA.ORG_CI=0;DLNA.ORG_FLAGS=00d00000000000000000000000000000")
NO_CITY = "(no city)"
BRANCHES = (("phdates", "\U0001F4C5 By date"), ("phalbums", "\U0001F4DA Albums"),
            ("phpeople", "\U0001F464 People"), ("phplaces", "\U0001F4CD Places"),
            ("phfavs", "⭐ Favourites"))


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


def _photos(ctx: _Browse, parent: str, title: str, photos: list) -> tuple:
    if ctx.is_meta:
        return ctx.meta(parent, title, len(photos))
    base = photo_base()
    return ctx.listing([_didl_photo(p, ctx.obj_id, base) for p in ctx.page(photos)],
                       len(photos))


def _folders(ctx: _Browse, parent: str, title: str, kids: list) -> tuple:
    """kids: [(id, title, child count)]."""
    if ctx.is_meta:
        return ctx.meta(parent, title, len(kids))
    return ctx.listing([_didl_container(cid, ctx.obj_id, t, n)
                        for cid, t, n in ctx.page(kids)], len(kids))


def _arg(ctx: _Browse) -> str:
    return ctx.obj_id.split(":", 1)[1]


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
    months = sorted(m for m in lib.months if m[:4] == year)
    if not months:
        return ctx.empty()
    return _folders(ctx, "phdates", year, [
        (f"phmonth:{m}", f"{calendar.month_name[int(m[5:7])]} {year}", len(lib.months[m]))
        for m in months if m[5:7].isdigit() and 1 <= int(m[5:7]) <= 12])


def _br_phmonth(ctx: _Browse) -> tuple:
    m = _arg(ctx)
    photos = _lib().months.get(m)
    if not photos:
        return ctx.empty()
    return _photos(ctx, f"phyear:{m[:4]}",
                   f"{calendar.month_name[int(m[5:7])]} {m[:4]}", photos)


def _br_phalbums(ctx: _Browse) -> tuple:
    return _folders(ctx, "photos", BRANCHES[1][1],
                    [(f"phalbum:{a.id}", a.name, len(a.photos)) for a in _lib().albums])


def _br_phalbum(ctx: _Browse) -> tuple:
    album = next((a for a in _lib().albums if a.id == _arg(ctx)), None)
    if album is None:
        return ctx.empty()
    return _photos(ctx, "phalbums", album.name, album.photos)


def _br_phpeople(ctx: _Browse) -> tuple:
    return _folders(ctx, "photos", BRANCHES[2][1],
                    [(f"phperson:{_b64e(n)}", n, len(ps)) for n, ps in _lib().people])


def _br_phperson(ctx: _Browse) -> tuple:
    name = _b64d(_arg(ctx))
    photos = next((ps for n, ps in _lib().people if n == name), None) if name else None
    if not photos:
        return ctx.empty()
    return _photos(ctx, "phpeople", name, photos)


def _br_phplaces(ctx: _Browse) -> tuple:
    return _folders(ctx, "photos", BRANCHES[3][1], [
        (f"phcountry:{_b64e(c)}", c, len(cities)) for c, cities in _lib().places])


def _cities(country: str):
    return next((cs for c, cs in _lib().places if c == country), None) if country else None


def _br_phcountry(ctx: _Browse) -> tuple:
    country = _b64d(_arg(ctx))
    cities = _cities(country)
    if not cities:
        return ctx.empty()
    return _folders(ctx, "phplaces", country, [
        (f"phcity:{_b64e(country + chr(0) + city)}", city or NO_CITY, len(ps))
        for city, ps in cities])


def _br_phcity(ctx: _Browse) -> tuple:
    country, _, city = _b64d(_arg(ctx)).partition("\0")
    photos = next((ps for c, ps in (_cities(country) or []) if c == city), None)
    if not photos:
        return ctx.empty()
    return _photos(ctx, f"phcountry:{_b64e(country)}", city or NO_CITY, photos)


def _br_phfavs(ctx: _Browse) -> tuple:
    return _photos(ctx, "photos", BRANCHES[4][1], _lib().favourites)


def _br_ph(ctx: _Browse) -> tuple:
    """An item describes itself on BrowseMetadata; it has no children."""
    p = _lib().photos.get(_arg(ctx))
    if p is None or not ctx.is_meta:
        return ctx.empty()
    parent = f"phmonth:{p.date[:7]}" if p.date else "photos"
    return ctx.listing([_didl_photo(p, parent, photo_base())], 1)


EXACT = {"photos": _br_photos, "phdates": _br_phdates, "phalbums": _br_phalbums,
         "phpeople": _br_phpeople, "phplaces": _br_phplaces, "phfavs": _br_phfavs}
PREFIX = (("phyear:", _br_phyear), ("phmonth:", _br_phmonth), ("phalbum:", _br_phalbum),
          ("phperson:", _br_phperson), ("phcountry:", _br_phcountry),
          ("phcity:", _br_phcity), ("ph:", _br_ph))
