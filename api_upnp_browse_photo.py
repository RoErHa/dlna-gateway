#!/usr/bin/env python3
"""
api_upnp_browse_photo.py — the 📷 Photos tree (Immich albums) as browsed
by the LG TV's media player (2026-11-09).

Part of the api_upnp family (see api_upnp_browse_video.py for the list).
The data comes from `dlna_photos.INDEX`, which a background thread keeps
fresh, so a Browse never waits on Immich.

Shape: `photos` → `phalbum:<album id>` → `ph:<asset id>`
(object.item.imageItem.photo). The folder is absent from the root while the
index holds no album, exactly like 📹 Videos.

Each item's `<res>` AND its art point at `/gw/photo/<asset id>.jpg` on the
PLAIN :8765 bind (the TV does no TLS here). That route serves Immich's 1440 px
JPEG preview and nothing else: the TV shows no HEIC, NEF or WebP, and Immich's
small thumbnails are WebP. See docs/PHOTO_SUPPORT.md.
"""
import os

import api_upnp_ids as _ids
import dlna_photos
from api_upnp_didl import _Browse, _didl_container
from api_upnp_ids import _xml_esc

PHOTOS_TITLE = "\U0001F4F7 Photos"
# 1440 px fits JPEG_LRG (≤ 4096²); OP=00: no byte seeking for a still image.
PROTOCOL = ("http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG;DLNA.ORG_OP=00;"
            "DLNA.ORG_CI=0;DLNA.ORG_FLAGS=00d00000000000000000000000000000")


def photo_url(asset_id: str) -> str:
    """Where the TV fetches a photo: this Mac's LAN address, plain port."""
    port = int(os.environ.get("GATEWAY_PLAIN_PORT", "8765"))
    return f"http://{_ids._get_lan_ip()}:{port}/gw/photo/{asset_id}.jpg"


def _didl_photo(p: dict, parent_id: str) -> str:
    url = _xml_esc(photo_url(p["id"]))
    date = f'<dc:date>{_xml_esc(p["date"])}</dc:date>' if p.get("date") else ""
    return (f'<item id="ph:{_xml_esc(p["id"])}" parentID="{_xml_esc(parent_id)}" '
            f'restricted="1"><dc:title>{_xml_esc(p["title"])}</dc:title>{date}'
            f'<upnp:albumArtURI>{url}</upnp:albumArtURI>'
            f'<upnp:class>object.item.imageItem.photo</upnp:class>'
            f'<res protocolInfo="{PROTOCOL}">{url}</res></item>')


def photo_album_count() -> int:
    return len(dlna_photos.INDEX.albums)


def _br_photos(ctx: _Browse) -> tuple:
    albums = list(dlna_photos.INDEX.albums)
    if ctx.is_meta:
        return ctx.meta("0", PHOTOS_TITLE, len(albums))
    kids = [_didl_container(f"phalbum:{a.id}", "photos", a.name, len(a.photos))
            for a in ctx.page(albums)]
    return ctx.listing(kids, len(albums))


def _br_phalbum(ctx: _Browse) -> tuple:
    album = dlna_photos.INDEX.album(ctx.obj_id[len("phalbum:"):])
    if album is None:
        return ctx.empty()
    if ctx.is_meta:
        return ctx.meta("photos", album.name, len(album.photos))
    items = [_didl_photo(p, ctx.obj_id) for p in ctx.page(album.photos)]
    return ctx.listing(items, len(album.photos))


def _br_ph(ctx: _Browse) -> tuple:
    """An item describes itself on BrowseMetadata; it has no children."""
    album, p = dlna_photos.INDEX.photo(ctx.obj_id[len("ph:"):])
    if p is None or not ctx.is_meta:
        return ctx.empty()
    return ctx.listing([_didl_photo(p, f"phalbum:{album.id}")], 1)
