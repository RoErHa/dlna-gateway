#!/usr/bin/env python3
"""
api_upnp.py — UPnP ContentDirectory gateway + SSDP announcer.

Exposes the library as a UPnP MediaServer so the Naim Uniti (and other UPnP
control points) can browse and play it without the PWA. Root tree: Artists →
Albums → Tracks, an Albums A–Z list, Genres → Albums → Tracks (all backed by
LibraryDB on the primary library udn, DB.primary_udn()), plus the ⭐ Favourite
Albums + Playlists convenience trees. Lists paginate via StartingIndex /
RequestedCount; album ObjectIDs carry the LocalFs album_key so folder-albums
(incl. Various-Artists comps) resolve correctly.

Handles: GET /gw/device.xml, /gw/cd/desc.xml, /gw/cd/events
         POST /gw/cd/control  (SOAP ContentDirectory Browse)

Also exports: GW_UDN, GW_NAME, gw_ssdp_announcer, gw_ssdp_byebye
"""
import base64
import http.client
import logging
import os
import re
import socket
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from dlna_library import DB

log = logging.getLogger("dlna.api.upnp")

# ── Gateway UPnP identity ─────────────────────────────────────────
# Env-overridable so a side-by-side 2.0 instance announces a DISTINCT
# MediaServer (different UDN + friendly name) on the LAN — otherwise the
# Naim sees two servers with the same identity. Defaults keep 1.x behaviour.
GW_UDN  = os.environ.get("GW_UDN",  "uuid:dlna-gateway-iina-8765")
GW_NAME = os.environ.get("GW_NAME", "DLNA Gateway (IINA)")


def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


def _xml_esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;").replace('"', "&quot;")


# ── Device / service description XML ─────────────────────────────

def _gw_device_xml(lan_ip: str, port: int) -> str:
    base = f"http://{lan_ip}:{port}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<root xmlns="urn:schemas-upnp-org:device-1-0">'
        '<specVersion><major>1</major><minor>0</minor></specVersion>'
        f'<URLBase>{base}</URLBase>'
        '<device>'
        '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>'
        f'<friendlyName>{GW_NAME}</friendlyName>'
        '<manufacturer>dlna-gateway</manufacturer>'
        '<modelName>dlna-gateway</modelName>'
        f'<UDN>{GW_UDN}</UDN>'
        # DLNA device marker — lets a strict DLNA control point (the Naim)
        # recognise us as a Digital Media Server, not just a bare UPnP device.
        '<dlna:X_DLNADOC xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
        'DMS-1.50</dlna:X_DLNADOC>'
        # Icons — some control points (TVs especially) won't list a server
        # without one. Served by the ASGI app (/icon-192.png, /icon-512.png).
        '<iconList>'
        '<icon><mimetype>image/png</mimetype><width>192</width>'
        '<height>192</height><depth>24</depth><url>/icon-192.png</url></icon>'
        '<icon><mimetype>image/png</mimetype><width>512</width>'
        '<height>512</height><depth>24</depth><url>/icon-512.png</url></icon>'
        '</iconList>'
        '<serviceList>'
        '<service>'
        '<serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>'
        '<serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>'
        '<SCPDURL>/gw/cd/desc.xml</SCPDURL>'
        '<controlURL>/gw/cd/control</controlURL>'
        '<eventSubURL>/gw/cd/events</eventSubURL>'
        '</service>'
        # ConnectionManager is MANDATORY for a DLNA Media Server — without it
        # strict clients (LG TV, Naim) reject the device and never browse.
        '<service>'
        '<serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType>'
        '<serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>'
        '<SCPDURL>/gw/cm/desc.xml</SCPDURL>'
        '<controlURL>/gw/cm/control</controlURL>'
        '<eventSubURL>/gw/cm/events</eventSubURL>'
        '</service>'
        '</serviceList>'
        '</device></root>'
    )


def _gw_cd_desc_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        '<specVersion><major>1</major><minor>0</minor></specVersion>'
        '<actionList><action><name>Browse</name><argumentList>'
        '<argument><name>ObjectID</name><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>'
        '<argument><name>BrowseFlag</name><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>'
        '<argument><name>Filter</name><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>'
        '<argument><name>StartingIndex</name><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>'
        '<argument><name>RequestedCount</name><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '<argument><name>SortCriteria</name><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>'
        '<argument><name>Result</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>'
        '<argument><name>NumberReturned</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '<argument><name>TotalMatches</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '<argument><name>UpdateID</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '</argumentList></action>'
        '<action><name>GetSearchCapabilities</name><argumentList>'
        '<argument><name>SearchCaps</name><direction>out</direction>'
        '<relatedStateVariable>SearchCapabilities</relatedStateVariable></argument>'
        '</argumentList></action>'
        '<action><name>GetSortCapabilities</name><argumentList>'
        '<argument><name>SortCaps</name><direction>out</direction>'
        '<relatedStateVariable>SortCapabilities</relatedStateVariable></argument>'
        '</argumentList></action>'
        '<action><name>GetSystemUpdateID</name><argumentList>'
        '<argument><name>Id</name><direction>out</direction>'
        '<relatedStateVariable>SystemUpdateID</relatedStateVariable></argument>'
        '</argumentList></action>'
        '</actionList>'
        '<serviceStateTable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ObjectID</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_BrowseFlag</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Filter</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Index</name>'
        '<dataType>ui4</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Count</name>'
        '<dataType>ui4</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_SortCriteria</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Result</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="yes"><name>SystemUpdateID</name>'
        '<dataType>ui4</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>SearchCapabilities</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>SortCapabilities</name>'
        '<dataType>string</dataType></stateVariable>'
        '</serviceStateTable></scpd>'
    )


# Video library udn (mirrors dlna_localfs_wiring.VIDEO_UDN). Videos are exposed
# as a "📹 Videos" folder so a TV (LG) can browse + play them; the Naim sees the
# folder but is audio-only, so the user just doesn't open it.
_VIDEO_UDN = "uuid:localfs-movies"


def _fmt_duration(sec) -> str:
    """Seconds → UPnP res 'H:MM:SS' (empty when unknown)."""
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(max(sec, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# ── ContentDirectory Browse ───────────────────────────────────────

def _gw_browse(obj_id: str, browse_flag: str,
               start: int, count: int) -> tuple:
    """Returns (DIDL-Lite XML, number_returned, total_matches)."""
    def container(cid, parent, title, child_count):
        return (f'<container id="{_xml_esc(cid)}" parentID="{_xml_esc(parent)}" '
                f'restricted="1" childCount="{child_count}">'
                f'<dc:title>{_xml_esc(title)}</dc:title>'
                f'<upnp:class>object.container.playlistContainer</upnp:class>'
                f'</container>')

    def track_item(t, parent_id):
        url   = _xml_esc(t.get("url", ""))
        title = _xml_esc(t.get("title", ""))
        art   = _xml_esc(t.get("art", ""))
        dur   = t.get("duration", "")
        mime  = _xml_esc(t.get("mime", "") or "audio/x-flac")
        art_tag = f'<upnp:albumArtURI>{art}</upnp:albumArtURI>' if art else ""
        return (
            f'<item id="tr:{_xml_esc(t.get("url",""))}" '
            f'parentID="{_xml_esc(parent_id)}" restricted="1">'
            f'<dc:title>{title}</dc:title>'
            f'<dc:creator>{_xml_esc(t.get("artist",""))}</dc:creator>'
            f'<upnp:artist>{_xml_esc(t.get("artist",""))}</upnp:artist>'
            f'<upnp:album>{_xml_esc(t.get("album",""))}</upnp:album>'
            f'{art_tag}'
            f'<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            f'<res protocolInfo="http-get:*:{mime}:*" '
            f'duration="{dur}">{url}</res>'
            f'</item>')

    def album_container(r, parent):
        """A galbum:* container from an album row (all_albums / artist_albums /
        genre_albums shape: artist, album, album_key, track_count)."""
        artist = r.get("artist", "")
        album  = r.get("album", "")
        cid    = _encode_lib_album_id(artist, album, r.get("album_key", ""))
        show_artist = bool(artist) and not _is_junk_name(artist)
        if album and show_artist:
            title = f"{album} — {artist}"
        else:                       # avoid a leading " — " when one side is blank
            title = album or (artist if show_artist else "") or "(album)"
        return container(cid, parent, title, r.get("track_count", 0))

    def video_item(v, parent_id):
        url   = _xml_esc(v.get("url", ""))
        title = _xml_esc(v.get("title", ""))
        mime  = v.get("mime") or "video/mp4"
        dur   = _fmt_duration(v.get("duration"))
        attrs = [f'protocolInfo="http-get:*:{_xml_esc(mime)}:'
                 'DLNA.ORG_OP=01;'
                 'DLNA.ORG_FLAGS=01700000000000000000000000000000"']
        if v.get("width") and v.get("height"):
            attrs.append(f'resolution="{v["width"]}x{v["height"]}"')
        if v.get("size"):
            attrs.append(f'size="{v["size"]}"')
        if dur:
            attrs.append(f'duration="{dur}"')
        art = ""
        if v.get("poster"):
            poster_url = v.get("url", "").replace("/localfs/video/",
                                                  "/localfs/poster/")
            art = f'<upnp:albumArtURI>{_xml_esc(poster_url)}</upnp:albumArtURI>'
        return (f'<item id="vid:{_xml_esc(v.get("id", ""))}" '
                f'parentID="{_xml_esc(parent_id)}" restricted="1">'
                f'<dc:title>{title}</dc:title>{art}'
                f'<upnp:class>object.item.videoItem.movie</upnp:class>'
                f'<res {" ".join(attrs)}>{url}</res>'
                f'</item>')

    OPEN  = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">')
    CLOSE = '</DIDL-Lite>'

    if obj_id == "0":
        n_videos = len(DB.all_videos(_VIDEO_UDN))
        if browse_flag == "BrowseMetadata":
            return (OPEN + container("0", "-1", GW_NAME,
                                     5 + (1 if n_videos else 0)) + CLOSE, 1, 1)
        udn       = DB.primary_udn()
        n_artists = len(_lib_artists(udn))   if udn else 0
        n_albums  = len(_album_letters(udn)) if udn else 0   # # of letter buckets
        n_genres  = len(_lib_genres(udn))    if udn else 0
        n_favs    = len(DB.album_fav_list())
        n_pls     = len(DB.pl_list())
        items  = [
            container("artists",   "0", "Artists",            n_artists),
            container("albums",    "0", "Albums",             n_albums),
            container("genres",    "0", "Genres",             n_genres),
            container("favalbums", "0", "⭐ Favourite Albums", n_favs),
            container("playlists", "0", "Playlists",          n_pls),
        ]
        # Videos folder — only when there ARE videos (so it never clutters the
        # Naim's view unless GWMovies is enabled + populated).
        if n_videos:
            items.append(container("videos", "0", "\U0001F4F9 Videos", n_videos))
        n = len(items)
        return OPEN + "".join(items) + CLOSE, n, n

    # ── Full-library tree (Artists / Albums / Genres) ──────────────
    # Backed by LibraryDB on the primary library udn (the LocalFs backend).
    # Each list paginates via StartingIndex/RequestedCount; album rows carry
    # album_key so a LocalFs folder-album (incl. Various-Artists comps)
    # resolves correctly through album_tracks.
    if obj_id == "artists":
        udn   = DB.primary_udn()
        rows  = _lib_artists(udn) if udn else []
        total = len(rows)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container("artists", "0", "Artists", total) + CLOSE, 1, 1)
        page  = rows[start:start + count] if count else rows[start:]
        items = [container("gartist:" + _b64e(r["artist"]), "artists",
                           r["artist"], r.get("album_count", 0))
                 for r in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id.startswith("gartist:"):
        artist = _b64d(obj_id[len("gartist:"):])
        udn    = DB.primary_udn()
        rows   = [r for r in DB.artist_albums(udn, artist)
                  if not _is_junk_name(r.get("album"))] if udn else []
        total  = len(rows)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, "artists",
                                     artist or "(artist)", total) + CLOSE, 1, 1)
        page  = rows[start:start + count] if count else rows[start:]
        items = [album_container(r, obj_id) for r in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    # "Albums" is a #-0-A..Z letter index (not one flat 2,000-entry list).
    if obj_id == "albums":
        udn     = DB.primary_udn()
        letters = _album_letters(udn) if udn else []
        total   = len(letters)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container("albums", "0", "Albums", total) + CLOSE, 1, 1)
        page  = letters[start:start + count] if count else letters[start:]
        items = [container("albumltr:" + L, "albums", L, cnt) for L, cnt in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id.startswith("albumltr:"):
        letter = obj_id[len("albumltr:"):]
        udn    = DB.primary_udn()
        rows   = [r for r in _lib_albums(udn)
                  if _letter_of(r.get("album")) == letter] if udn else []
        total  = len(rows)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, "albums", letter, total) + CLOSE, 1, 1)
        page  = rows[start:start + count] if count else rows[start:]
        items = [album_container(r, obj_id) for r in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id.startswith("galbum:"):
        artist, album, album_key = _decode_lib_album_id(obj_id)
        udn    = DB.primary_udn()
        tracks = DB.album_tracks(udn, artist, album, album_key=album_key) if udn else []
        total  = len(tracks)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, "albums",
                                     album or "(album)", total) + CLOSE, 1, 1)
        page  = tracks[start:start + count] if count else tracks[start:]
        items = [track_item(t, obj_id) for t in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id == "genres":
        udn   = DB.primary_udn()
        rows  = _lib_genres(udn) if udn else []
        total = len(rows)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container("genres", "0", "Genres", total) + CLOSE, 1, 1)
        page  = rows[start:start + count] if count else rows[start:]
        items = [container("ggenre:" + _b64e(r["genre"]), "genres",
                           r["genre"], r.get("album_count", 0))
                 for r in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id.startswith("ggenre:"):
        genre = _b64d(obj_id[len("ggenre:"):])
        udn   = DB.primary_udn()
        rows  = [r for r in DB.genre_albums(udn, genre)
                 if not _is_junk_name(r.get("album"))] if udn else []
        total = len(rows)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, "genres",
                                     genre or "(genre)", total) + CLOSE, 1, 1)
        page  = rows[start:start + count] if count else rows[start:]
        items = [album_container(r, obj_id) for r in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    # ── Videos tree (2026-07-06) ─────────────────────────────────────
    # The flat ~3,000-item list was unbrowsable with a TV remote. "videos"
    # now holds three sub-containers: date drill-down (year → month),
    # location A-Z (geocoded location_name; "(no location)" bucket last),
    # and the old flat list under "vidall".
    if obj_id == "videos":
        if browse_flag == "BrowseMetadata":
            n = len(DB.all_videos(_VIDEO_UDN))
            return (OPEN + container("videos", "0", "\U0001F4F9 Videos", n)
                    + CLOSE, 1, 1)
        years = DB.video_years(_VIDEO_UDN)
        locs  = DB.video_locations(_VIDEO_UDN)
        n     = len(DB.all_videos(_VIDEO_UDN))
        kids = [
            container("viddates", "videos", "\U0001F4C5 By date", len(years)),
            container("vidlocs", "videos", "\U0001F4CD By location", len(locs)),
            container("vidall", "videos", "\U0001F39E All videos", n),
        ]
        return OPEN + "".join(kids) + CLOSE, len(kids), len(kids)

    if obj_id == "vidall":
        vids  = DB.all_videos(_VIDEO_UDN)
        total = len(vids)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container("vidall", "videos",
                                     "\U0001F39E All videos", total)
                    + CLOSE, 1, 1)
        page  = vids[start:start + count] if count else vids[start:]
        items = [video_item(v, "vidall") for v in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id == "viddates":
        years = DB.video_years(_VIDEO_UDN)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container("viddates", "videos",
                                     "\U0001F4C5 By date", len(years))
                    + CLOSE, 1, 1)
        page  = years[start:start + count] if count else years[start:]
        items = [container(f"viddate:{y['year']}", "viddates",
                           y["year"], y["count"]) for y in page]
        return OPEN + "".join(items) + CLOSE, len(items), len(years)

    if obj_id.startswith("viddate:"):
        key = obj_id[len("viddate:"):]
        if len(key) == 4:                       # a year → its months
            months = DB.video_months(_VIDEO_UDN, key)
            if browse_flag == "BrowseMetadata":
                return (OPEN + container(obj_id, "viddates", key,
                                         len(months)) + CLOSE, 1, 1)
            page  = months[start:start + count] if count else months[start:]
            items = [container(f"viddate:{m['month']}", obj_id,
                               m["month"], m["count"]) for m in page]
            return OPEN + "".join(items) + CLOSE, len(items), len(months)
        vids = DB.videos_by_month(_VIDEO_UDN, key)    # 'YYYY-MM' → items
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, f"viddate:{key[:4]}", key,
                                     len(vids)) + CLOSE, 1, 1)
        page  = vids[start:start + count] if count else vids[start:]
        items = [video_item(v, obj_id) for v in page]
        return OPEN + "".join(items) + CLOSE, len(items), len(vids)

    if obj_id == "vidlocs":
        # 2026-07-06 v2: COUNTRY blocks first (A-Z by ISO code), then
        # "(no country)" for located-but-unknown-country videos, then the
        # "(no location)" bucket for GPS-less videos — each country drills
        # down to its locations (country_location, like the titles).
        countries = DB.video_countries(_VIDEO_UDN)
        no_loc = [r for r in DB.video_locations(_VIDEO_UDN)
                  if not r["location_name"]]
        entries = []
        for c in countries:
            entries.append(("vidcountry-none" if not c["country"]
                            else f"vidcountry:{c['country']}",
                            c["country"] or "(no country)", c["count"]))
        for r in no_loc:
            entries.append(("vidloc-none", "(no location)", r["count"]))
        if browse_flag == "BrowseMetadata":
            return (OPEN + container("vidlocs", "videos",
                                     "\U0001F4CD By location", len(entries))
                    + CLOSE, 1, 1)
        page  = entries[start:start + count] if count else entries[start:]
        items = [container(cid, "vidlocs", title, n)
                 for cid, title, n in page]
        return OPEN + "".join(items) + CLOSE, len(items), len(entries)

    if obj_id == "vidcountry-none" or obj_id.startswith("vidcountry:"):
        cc = ("" if obj_id == "vidcountry-none"
              else obj_id[len("vidcountry:"):])
        locs = DB.video_locations_for_country(_VIDEO_UDN, cc)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, "vidlocs",
                                     cc or "(no country)", len(locs))
                    + CLOSE, 1, 1)
        page  = locs[start:start + count] if count else locs[start:]
        # '' location = the "(no city)" bucket — country-only videos
        # (Plan A inferred country, no specific place).
        items = [container(
            "vidcloc:" + _b64e(cc + "\x00" + r["location_name"]), obj_id,
            r["location_name"] or "(no city)", r["count"]) for r in page]
        return OPEN + "".join(items) + CLOSE, len(items), len(locs)

    if obj_id.startswith("vidcloc:"):
        raw = _b64d(obj_id[len("vidcloc:"):])
        if "\x00" not in raw:
            return OPEN + CLOSE, 0, 0   # garbled id → empty, never 500
        cc, loc = raw.split("\x00", 1)
        vids = DB.videos_by_country_location(_VIDEO_UDN, cc, loc)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, "vidcountry-none" if not cc
                                     else f"vidcountry:{cc}",
                                     loc or "(no city)",
                                     len(vids)) + CLOSE, 1, 1)
        page  = vids[start:start + count] if count else vids[start:]
        items = [video_item(v, obj_id) for v in page]
        return OPEN + "".join(items) + CLOSE, len(items), len(vids)

    if obj_id == "vidloc-none" or obj_id.startswith("vidloc:"):
        if obj_id == "vidloc-none":
            loc = ""
        else:
            loc = _b64d(obj_id[len("vidloc:"):])
            if not loc:
                return OPEN + CLOSE, 0, 0   # garbled id → empty, never 500
        vids = DB.videos_by_location(_VIDEO_UDN, loc)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, "vidlocs",
                                     loc or "(no location)", len(vids))
                    + CLOSE, 1, 1)
        page  = vids[start:start + count] if count else vids[start:]
        items = [video_item(v, obj_id) for v in page]
        return OPEN + "".join(items) + CLOSE, len(items), len(vids)

    if obj_id.startswith("vid:"):
        v = DB.video_by_id(obj_id[len("vid:"):])
        if not v:
            return OPEN + CLOSE, 0, 0
        return OPEN + video_item(v, "vidall") + CLOSE, 1, 1

    if obj_id == "playlists":
        pls   = DB.pl_list()
        total = len(pls)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container("playlists", "0", "Playlists", total) + CLOSE, 1, 1)
        page  = pls[start:start + count] if count else pls[start:]
        items = [container(f"pl:{p['id']}", "playlists", p["name"], p["count"])
                 for p in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id.startswith("pl:"):
        pl_id  = obj_id[3:]
        pl     = DB.pl_get(pl_id)
        if not pl:
            return OPEN + CLOSE, 0, 0
        tracks = pl["tracks"]
        total  = len(tracks)
        if browse_flag == "BrowseMetadata":
            return (OPEN + container(obj_id, "playlists", pl["name"], total) + CLOSE, 1, 1)
        page  = tracks[start:start + count] if count else tracks[start:]
        items = [track_item(t, obj_id) for t in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id == "favalbums":
        favs  = DB.album_fav_list()
        total = len(favs)
        if browse_flag == "BrowseMetadata":
            return (OPEN
                    + container("favalbums", "0", "⭐ Favourite Albums", total)
                    + CLOSE, 1, 1)
        page  = favs[start:start + count] if count else favs[start:]
        items = [container(_encode_album_id(f["artist"], f["album"],
                                            f.get("album_key", "")),
                           "favalbums",
                           f"{f['album']} — {f['artist']}" if f["artist"]
                                                          else f["album"],
                           f["track_count"])
                 for f in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    if obj_id.startswith("favalbum:"):
        artist, album, album_key = _decode_album_id(obj_id)
        # Resolve the udn lazily — the favourite is keyed by album_key
        # (LocalFs folder) when present, else (artist, album), not by
        # server. If the album isn't in any indexed library we silently
        # return an empty container rather than 500 — a Naim control point
        # handles "0 results" gracefully.
        if album_key:
            fav = next((f for f in DB.album_fav_list()
                        if f.get("album_key") == album_key), None)
        else:
            fav = next((f for f in DB.album_fav_list()
                        if f["artist"] == artist and f["album"] == album
                        and not f.get("album_key")), None)
        if not fav or not fav["udn"]:
            return OPEN + CLOSE, 0, 0
        tracks = DB.album_tracks(fav["udn"], artist, album, album_key=album_key)
        total  = len(tracks)
        if browse_flag == "BrowseMetadata":
            return (OPEN
                    + container(obj_id, "favalbums",
                                album or "(album)", total)
                    + CLOSE, 1, 1)
        page  = tracks[start:start + count] if count else tracks[start:]
        items = [track_item(t, obj_id) for t in page]
        return OPEN + "".join(items) + CLOSE, len(items), total

    return OPEN + CLOSE, 0, 0


# ── Album-favourite ObjectID encoding ────────────────────────────
# UPnP ObjectIDs travel through SOAP XML and back; an artist or album
# can contain any unicode character (incl. quotes, ampersands, NULs).
# Base64-urlsafe of "artist\x00album" is unambiguous and round-trips
# cleanly through XML escaping.

def _b64e(s: str) -> str:
    """URL-safe base64 of a single string (artist / genre names) for use in a
    UPnP ObjectID — round-trips arbitrary unicode through SOAP/XML."""
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(s: str) -> str:
    s += "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _encode_lib_album_id(artist: str, album: str, album_key: str = "") -> str:
    """galbum:* ObjectID for a full-library album (distinct from favalbum:* —
    a galbum resolves via the primary library udn, not the favourites row)."""
    raw = f"{artist}\x00{album}\x00{album_key}".encode("utf-8")
    return "galbum:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_lib_album_id(obj_id: str) -> tuple:
    """Return (artist, album, album_key) from a galbum:* id; ('', '', '') on junk."""
    payload = obj_id[len("galbum:"):]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ("", "", "")
    parts = raw.split("\x00")
    return (parts[0] if len(parts) > 0 else "",
            parts[1] if len(parts) > 1 else "",
            parts[2] if len(parts) > 2 else "")


# ── Junk-name filter (untagged / track-number-as-name) ───────────
# The library carries filename-derived metadata gaps (artist "07", album
# "10. Some Title …", or a blank album name). These clutter the Naim browse,
# so the gateway-as-MediaServer tree hides them (the PWA still shows the raw
# data; beets enrichment fixes the tags over time). DISPLAY-only filter.
_JUNK_PREFIX_RE = re.compile(r'^\d+\s*[.):\-]')   # "10.", "1)", "07 -", "3:"

def _is_junk_name(s: str) -> bool:
    s = (s or "").strip()
    if not s:                       # blank → "(album)" / unnamed
        return True
    if _JUNK_PREFIX_RE.match(s):    # track-number-as-name
        return True
    if re.fullmatch(r'\d{1,2}', s): # bare 1–2 digit number ("07")
        return True
    return False


def _lib_artists(udn: str) -> list:
    return [r for r in DB.all_artists(udn) if not _is_junk_name(r.get("artist"))]


def _lib_albums(udn: str) -> list:
    return [r for r in DB.all_albums(udn) if not _is_junk_name(r.get("album"))]


def _lib_genres(udn: str) -> list:
    return [r for r in DB.all_genres(udn) if not _is_junk_name(r.get("genre"))]


def _letter_of(name: str) -> str:
    """First-letter bucket matching LibraryDB.browse_letter / the PWA letter
    bar: 'A'..'Z', '0' for a leading digit, '#' for anything else."""
    s = (name or "").strip()
    if not s:
        return "#"
    c = s[0].upper()
    if "A" <= c <= "Z":
        return c
    if "0" <= c <= "9":
        return "0"
    return "#"


_LETTER_ORDER = ["#", "0"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]

def _album_letters(udn: str) -> list:
    """Ordered (letter, count) for the non-empty album-letter buckets
    (junk-filtered), so the Naim's 'Albums' folder is a #-0-A..Z index
    rather than one flat 2,000-entry list."""
    buckets: dict = {}
    for r in _lib_albums(udn):
        L = _letter_of(r.get("album"))
        buckets[L] = buckets.get(L, 0) + 1
    return [(L, buckets[L]) for L in _LETTER_ORDER if L in buckets]


def _encode_album_id(artist: str, album: str, album_key: str = "") -> str:
    # NUL-delimited (artist, album, album_key). album_key (LocalFs folder
    # identity) lets a Various-Artists compilation round-trip as one album;
    # empty for (artist, album)-keyed favourites.
    raw = f"{artist}\x00{album}\x00{album_key}".encode("utf-8")
    return "favalbum:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_album_id(obj_id: str) -> tuple:
    """Return (artist, album, album_key). Tolerates legacy 2-field ids
    (no album_key) by defaulting album_key to ''."""
    if not obj_id.startswith("favalbum:"):
        return ("", "", "")
    payload = obj_id[len("favalbum:"):]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ("", "", "")
    parts = raw.split("\x00")
    artist    = parts[0] if len(parts) > 0 else ""
    album     = parts[1] if len(parts) > 1 else ""
    album_key = parts[2] if len(parts) > 2 else ""
    return (artist, album, album_key)


def _gw_browse_response(result_xml: str, n_returned: int,
                        total: int, update_id: int = 1) -> bytes:
    escaped = (result_xml.replace("&", "&amp;")
                         .replace("<", "&lt;")
                         .replace(">", "&gt;"))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        '<u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        f'<Result>{escaped}</Result>'
        f'<NumberReturned>{n_returned}</NumberReturned>'
        f'<TotalMatches>{total}</TotalMatches>'
        f'<UpdateID>{update_id}</UpdateID>'
        '</u:BrowseResponse>'
        '</s:Body></s:Envelope>'
    )
    return body.encode("utf-8")


# ── GET handlers ──────────────────────────────────────────────────

def device_xml(h, params):
    lan_ip = _get_lan_ip()
    port   = h.server.server_address[1]
    h._xml_response(200, _gw_device_xml(lan_ip, port).encode())


def cd_desc_xml(h, params):
    h._xml_response(200, _gw_cd_desc_xml().encode())


def cd_events(h, params):
    h.send_response(200)
    h.end_headers()


# ── POST handler ──────────────────────────────────────────────────

_SOAP_CTYPE = 'text/xml; charset="utf-8"'
_CD_NS = "urn:schemas-upnp-org:service:ContentDirectory:1"
_CM_NS = "urn:schemas-upnp-org:service:ConnectionManager:1"
# Source protocolInfo advertised via ConnectionManager#GetProtocolInfo — the
# formats the gateway can serve (LocalFs streams the original bytes). A DLNA
# control point uses this to decide it can talk to us.
_GW_SOURCE_PROTOCOLS = (
    "http-get:*:audio/mpeg:*,http-get:*:audio/flac:*,"
    "http-get:*:audio/x-flac:*,http-get:*:audio/mp4:*,"
    "http-get:*:audio/aac:*,http-get:*:audio/x-aac:*,"
    "http-get:*:audio/wav:*,http-get:*:audio/x-wav:*,"
    "http-get:*:audio/L16:*,http-get:*:audio/ogg:*,"
    "http-get:*:application/ogg:*,http-get:*:audio/x-aiff:*,"
    "http-get:*:audio/aiff:*,http-get:*:audio/dsd:*,"
    "http-get:*:application/octet-stream:*"
)
_EMPTY_DIDL = ('&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
               'xmlns:dc="http://purl.org/dc/elements/1.1/" '
               'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
               '&lt;/DIDL-Lite&gt;')


def _cd_action_name(root) -> str:
    """The ContentDirectory action element name under <s:Body> (e.g. 'Browse')."""
    body = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
    if body is None or len(body) == 0:
        return ""
    return body[0].tag.split("}")[-1]


def _soap_svc_response(ns: str, action: str, inner: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action}Response xmlns:u="{ns}">{inner}'
        f'</u:{action}Response></s:Body></s:Envelope>'
    ).encode("utf-8")


def _soap_cd_response(action: str, inner: str) -> bytes:
    return _soap_svc_response(_CD_NS, action, inner)


def cm_control_soap(body: bytes):
    """ConnectionManager SOAP handler → (status, content_type, body_bytes).
    A DLNA Media Server MUST expose ConnectionManager (alongside
    ContentDirectory) or strict clients (LG TV, Naim) reject the device and
    never browse. We have no real connections — GetProtocolInfo advertises the
    formats we serve; the connection actions report the single static id 0."""
    try:
        root   = ET.fromstring(body.decode("utf-8"))
        action = _cd_action_name(root)
        if action == "GetProtocolInfo":
            inner = (f"<Source>{_xml_esc(_GW_SOURCE_PROTOCOLS)}</Source>"
                     "<Sink></Sink>")
            return 200, _SOAP_CTYPE, _soap_svc_response(_CM_NS, action, inner)
        if action == "GetCurrentConnectionIDs":
            return 200, _SOAP_CTYPE, _soap_svc_response(
                _CM_NS, action, "<ConnectionIDs>0</ConnectionIDs>")
        if action == "GetCurrentConnectionInfo":
            inner = ("<RcsID>-1</RcsID><AVTransportID>-1</AVTransportID>"
                     "<ProtocolInfo></ProtocolInfo>"
                     "<PeerConnectionManager></PeerConnectionManager>"
                     "<PeerConnectionID>-1</PeerConnectionID>"
                     "<Direction>Output</Direction><Status>OK</Status>")
            return 200, _SOAP_CTYPE, _soap_svc_response(_CM_NS, action, inner)
        log.debug("GW CM: unhandled action %r", action)
        return 400, "text/html", f"<h1>Unsupported action: {action}</h1>".encode()
    except Exception as e:
        log.error(f"GW CM control error: {e}")
        return 500, "text/html", f"<h1>error: {e}</h1>".encode("utf-8")


def _gw_cm_desc_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        '<specVersion><major>1</major><minor>0</minor></specVersion>'
        '<actionList>'
        '<action><name>GetProtocolInfo</name><argumentList>'
        '<argument><name>Source</name><direction>out</direction>'
        '<relatedStateVariable>SourceProtocolInfo</relatedStateVariable></argument>'
        '<argument><name>Sink</name><direction>out</direction>'
        '<relatedStateVariable>SinkProtocolInfo</relatedStateVariable></argument>'
        '</argumentList></action>'
        '<action><name>GetCurrentConnectionIDs</name><argumentList>'
        '<argument><name>ConnectionIDs</name><direction>out</direction>'
        '<relatedStateVariable>CurrentConnectionIDs</relatedStateVariable></argument>'
        '</argumentList></action>'
        '<action><name>GetCurrentConnectionInfo</name><argumentList>'
        '<argument><name>ConnectionID</name><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>'
        '<argument><name>RcsID</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_RcsID</relatedStateVariable></argument>'
        '<argument><name>AVTransportID</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_AVTransportID</relatedStateVariable></argument>'
        '<argument><name>ProtocolInfo</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_ProtocolInfo</relatedStateVariable></argument>'
        '<argument><name>PeerConnectionManager</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_ConnectionManager</relatedStateVariable></argument>'
        '<argument><name>PeerConnectionID</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_ConnectionID</relatedStateVariable></argument>'
        '<argument><name>Direction</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Direction</relatedStateVariable></argument>'
        '<argument><name>Status</name><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_ConnectionStatus</relatedStateVariable></argument>'
        '</argumentList></action>'
        '</actionList>'
        '<serviceStateTable>'
        '<stateVariable sendEvents="yes"><name>SourceProtocolInfo</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="yes"><name>SinkProtocolInfo</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="yes"><name>CurrentConnectionIDs</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionStatus</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionManager</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_Direction</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ProtocolInfo</name>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_ConnectionID</name>'
        '<dataType>i4</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_AVTransportID</name>'
        '<dataType>i4</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><name>A_ARG_TYPE_RcsID</name>'
        '<dataType>i4</dataType></stateVariable>'
        '</serviceStateTable></scpd>'
    )


def cd_control_soap(body: bytes):
    """Pure ContentDirectory SOAP handler → (status, content_type, body_bytes).
    Shared by the native ASGI /gw/cd/control route and the legacy stdlib handler.

    Implements the DLNA handshake a control point (the Naim) runs BEFORE it will
    browse — GetSearchCapabilities / GetSortCapabilities / GetSystemUpdateID
    (+ the optional GetSortExtensionCapabilities / GetFeatureList / Search) —
    returning empty-but-valid responses, plus Browse. Without the handshake
    actions NaimUPnP got HTTP 400 and dropped the server (2026-06-12)."""
    try:
        root   = ET.fromstring(body.decode("utf-8"))
        action = _cd_action_name(root)

        if action == "Browse":
            ns = {"s": "http://schemas.xmlsoap.org/soap/envelope/", "u": _CD_NS}
            browse = root.find(".//u:Browse", ns) or root.find(".//Browse")
            if browse is None:
                return 400, "text/html", b"<h1>Missing Browse element</h1>"
            obj_id = browse.findtext("ObjectID") or "0"
            flag   = browse.findtext("BrowseFlag") or "BrowseDirectChildren"
            start  = int(browse.findtext("StartingIndex") or 0)
            count  = int(browse.findtext("RequestedCount") or 0) or 9999
            result_xml, n_ret, total = _gw_browse(obj_id, flag, start, count)
            log.debug(f"GW SOAP Browse {obj_id!r} → {n_ret}/{total}")
            return 200, _SOAP_CTYPE, _gw_browse_response(result_xml, n_ret, total)

        if action == "GetSearchCapabilities":
            return 200, _SOAP_CTYPE, _soap_cd_response(action, "<SearchCaps></SearchCaps>")
        if action == "GetSortCapabilities":
            return 200, _SOAP_CTYPE, _soap_cd_response(action, "<SortCaps></SortCaps>")
        if action == "GetSortExtensionCapabilities":
            return 200, _SOAP_CTYPE, _soap_cd_response(
                action, "<SortExtensionCaps></SortExtensionCaps>")
        if action == "GetSystemUpdateID":
            return 200, _SOAP_CTYPE, _soap_cd_response(action, "<Id>1</Id>")
        if action == "GetFeatureList":
            return 200, _SOAP_CTYPE, _soap_cd_response(
                action, "<FeatureList>&lt;Features "
                'xmlns="urn:schemas-upnp-org:av:avs" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                'xsi:schemaLocation="urn:schemas-upnp-org:av:avs '
                'http://www.upnp.org/schemas/av/avs.xsd"&gt;&lt;/Features&gt;'
                "</FeatureList>")
        if action == "Search":
            inner = (f"<Result>{_EMPTY_DIDL}</Result>"
                     "<NumberReturned>0</NumberReturned>"
                     "<TotalMatches>0</TotalMatches><UpdateID>1</UpdateID>")
            return 200, _SOAP_CTYPE, _soap_cd_response("Search", inner)

        log.debug("GW CD: unhandled action %r", action)
        return 400, "text/html", f"<h1>Unsupported action: {action}</h1>".encode()
    except Exception as e:
        log.error(f"GW CD control error: {e}")
        return 500, "text/html", f"<h1>error: {e}</h1>".encode("utf-8")


def cd_control(h, body):
    """Legacy (h, body) wrapper around cd_control_soap. Cleanup C made /gw/*
    native in the ASGI app (which calls cd_control_soap directly), so this is
    no longer on a live path — retained as the dlna_routes fallback shape."""
    status, _ctype, payload = cd_control_soap(body)
    if status == 200:
        h._xml_response(200, payload)
    else:
        h._html(status, payload.decode("utf-8"))


# ── SSDP announcer + M-SEARCH responder ───────────────────────────

def _gw_ssdp_entries() -> list:
    """(NT/ST, USN) pairs the gateway-as-MediaServer advertises in NOTIFY and
    answers M-SEARCH for: the root device, its UDN, the MediaServer device
    type, and the ContentDirectory service."""
    return [
        ("upnp:rootdevice", f"{GW_UDN}::upnp:rootdevice"),
        (GW_UDN, GW_UDN),
        ("urn:schemas-upnp-org:device:MediaServer:1",
         f"{GW_UDN}::urn:schemas-upnp-org:device:MediaServer:1"),
        ("urn:schemas-upnp-org:service:ContentDirectory:1",
         f"{GW_UDN}::urn:schemas-upnp-org:service:ContentDirectory:1"),
    ]


def _gw_ssdp_notify(lan_ip: str, port: int, alive: bool = True):
    location = f"http://{lan_ip}:{port}/gw/device.xml"
    entries = _gw_ssdp_entries()
    msgs = []
    for nt, usn in entries:
        if alive:
            m = (f"NOTIFY * HTTP/1.1\r\n"
                 f"HOST: 239.255.255.250:1900\r\n"
                 f"CACHE-CONTROL: max-age=1800\r\n"
                 f"LOCATION: {location}\r\n"
                 f"NT: {nt}\r\n"
                 f"NTS: ssdp:alive\r\n"
                 f"SERVER: Python/3 UPnP/1.0 dlna-gateway/1.0\r\n"
                 f"USN: {usn}\r\n\r\n")
        else:
            m = (f"NOTIFY * HTTP/1.1\r\n"
                 f"HOST: 239.255.255.250:1900\r\n"
                 f"NT: {nt}\r\n"
                 f"NTS: ssdp:byebye\r\n"
                 f"USN: {usn}\r\n\r\n")
        msgs.append(m.encode("utf-8"))
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(lan_ip))
        for m in msgs:
            sock.sendto(m, ("239.255.255.250", 1900))
            time.sleep(0.05)
        sock.close()
    except Exception as e:
        log.debug(f"GW SSDP notify: {e}")


def gw_ssdp_announcer(lan_ip: str, port: int):
    """Background thread: send SSDP alive every 60 s."""
    time.sleep(3)
    while True:
        _gw_ssdp_notify(lan_ip, port, alive=True)
        log.debug("GW SSDP alive sent")
        time.sleep(60)


def gw_ssdp_byebye(lan_ip: str, port: int):
    _gw_ssdp_notify(lan_ip, port, alive=False)


# ── GENA eventing (ContentDirectory /gw/cd/events) ────────────────
# We don't push real state changes (SystemUpdateID is constant), but a strict
# GUPnP/dLeyna control point (the Naim) requires a VALID SUBSCRIBE response —
# an SID + TIMEOUT — and an initial NOTIFY, or it treats device setup as failed
# and never browses. A bare 200 (the old stub) is not enough.

def _parse_callback(header: str) -> str:
    """First callback URL from a GENA CALLBACK header like '<url1><url2>'."""
    if not header:
        return ""
    start = header.find("<")
    end = header.find(">", start + 1)
    return header[start + 1:end].strip() if (start != -1 and end != -1) else ""


def gw_event_subscribe(headers: dict):
    """Handle a GENA SUBSCRIBE (or renewal). Returns
    (response_headers, callback_url, sid). A renewal carries SID (echo it);
    a new subscription gets a fresh SID + a callback to push the initial
    NOTIFY to."""
    h = {k.lower(): v for k, v in headers.items()}
    sid = h.get("sid")
    if sid:                                   # renewal — just extend
        return {"SID": sid, "TIMEOUT": "Second-1800"}, "", sid
    new_sid = "uuid:" + str(uuid.uuid4())
    callback = _parse_callback(h.get("callback", ""))
    return ({"SID": new_sid, "TIMEOUT": "Second-1800",
             "SERVER": "Python/3 UPnP/1.0 dlna-gateway/1.0"},
            callback, new_sid)


def gw_event_initial_notify(callback: str, sid: str, props: dict):
    """Push the GENA initial event (the service's evented variables) to a new
    subscriber's callback so a GUPnP-based control point completes its
    subscription. Sent slightly after the SUBSCRIBE response so the client has
    the SID first. `props` = {variable: value} for this service."""
    if not callback:
        return
    time.sleep(0.3)
    inner = "".join(f"<e:property><{k}>{_xml_esc(str(v))}</{k}></e:property>"
                    for k, v in props.items())
    body = (f'<?xml version="1.0"?>'
            f'<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">'
            f'{inner}</e:propertyset>').encode("utf-8")
    try:
        u = urlparse(callback)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=4)
        conn.request("NOTIFY", u.path or "/", body, {
            "HOST": f"{u.hostname}:{u.port or 80}",
            "CONTENT-TYPE": 'text/xml; charset="utf-8"',
            "NT": "upnp:event", "NTS": "upnp:propchange",
            "SID": sid, "SEQ": "0"})
        resp = conn.getresponse()
        log.debug("GW event initial NOTIFY → %s : HTTP %s", callback, resp.status)
        conn.close()
    except Exception as e:
        log.debug("GW event initial NOTIFY to %s failed: %s", callback, e)


def _gw_msearch_response(st: str, usn: str, location: str) -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        f"LOCATION: {location}\r\n"
        "SERVER: Python/3 UPnP/1.0 dlna-gateway/1.0\r\n"
        "EXT:\r\n"
        f"ST: {st}\r\n"
        f"USN: {usn}\r\n\r\n"
    ).encode("utf-8")


def _gw_msearch_replies(data: bytes, location: str) -> list:
    """If `data` is an SSDP M-SEARCH this MediaServer should answer, return the
    [(ST, USN, response_bytes), …] to unicast back; else []. Answers ssdp:all,
    upnp:rootdevice, our UDN, the MediaServer device type and the
    ContentDirectory service. Pure → unit-testable without sockets."""
    try:
        msg = data.decode("utf-8", "replace")
    except Exception:
        return []
    if not msg.split("\n", 1)[0].strip().upper().startswith("M-SEARCH"):
        return []
    if "ssdp:discover" not in msg.lower():
        return []
    st = ""
    for line in msg.splitlines():
        if line.lower().startswith("st:"):
            st = line.split(":", 1)[1].strip()
            break
    entries = _gw_ssdp_entries()
    chosen = entries if st in ("", "ssdp:all") else [
        (s, u) for s, u in entries if s == st]
    return [(s, u, _gw_msearch_response(s, u, location)) for s, u in chosen]


def gw_ssdp_responder(lan_ip: str, port: int):
    """Answer SSDP M-SEARCH so ACTIVE control points (the Naim app/device, which
    search on demand) discover the gateway-as-MediaServer immediately — the 60s
    NOTIFY alive only reaches passive listeners, so without this the gateway is
    invisible to a Naim that searches after the last NOTIFY's cache expired.
    Binds :1900 alongside the discovery listener (SO_REUSEPORT); degrades to
    passive-NOTIFY-only if the bind fails."""
    location = f"http://{lan_ip}:{port}/gw/device.xml"
    time.sleep(2)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.bind(("0.0.0.0", 1900))
        mreq = socket.inet_aton("239.255.255.250") + socket.inet_aton(lan_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                        socket.inet_aton(lan_ip))
    except OSError as e:
        log.warning(f"GW SSDP M-SEARCH responder: cannot bind :1900 ({e}) "
                    f"— passive NOTIFY only")
        return
    log.info(f"GW SSDP M-SEARCH responder active ({lan_ip}:1900 → {location})")
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except Exception as e:
            log.debug(f"GW SSDP responder recv: {e}")
            time.sleep(0.2)
            continue
        try:
            for _st, _usn, resp in _gw_msearch_replies(data, location):
                sock.sendto(resp, addr)
                time.sleep(0.02)
        except Exception as e:
            log.debug(f"GW SSDP responder reply: {e}")
