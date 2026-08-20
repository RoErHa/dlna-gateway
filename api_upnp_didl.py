#!/usr/bin/env python3
"""
api_upnp_didl.py — DIDL-Lite renderers and the `_Browse` request
context shared by every ContentDirectory handler.

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

`_Browse` exists so the 26 browse handlers stop re-implementing the same
envelope and slice arithmetic. Two rules live in it rather than in the
callers:
  * `count == 0` means UNLIMITED in the ContentDirectory spec — an
    off-by-one here silently truncates the Naim's view of the library.
  * `.empty()` is how an unresolvable id answers. A Naim control point
    handles "0 results" gracefully but abandons the entire browse on a SOAP
    fault, so nothing in this family may raise at a client.
"""
import logging

from api_upnp_ids import (
    _encode_lib_album_id,
    _fmt_duration,
    _is_junk_name,
    _xml_esc,
)

log = logging.getLogger("dlna.api.upnp")


# ── ContentDirectory Browse ───────────────────────────────────────
#
# `_gw_browse` was a single 491-line function with 26 sequential
# `if obj_id == …: return` branches and ~99 branch points — the least
# reviewable function in the repo. Split (2026-08-20) into:
#
#   * four module-level DIDL-Lite renderers (they were nested closures
#     that captured nothing, so nesting bought nothing and cost testability)
#   * one `_Browse` context carrying (obj_id, flag, start, count) plus the
#     pagination + envelope logic that all 26 branches repeated verbatim
#   * one handler per container type, registered in two dispatch tables
#
# `_gw_browse` itself is now the table lookup. Behaviour is byte-identical:
# `tests/test_upnp_browse_dispatch.py` replays the whole tree and compares
# XML against the pre-split implementation's captured output.

_DIDL_OPEN  = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
               'xmlns:dc="http://purl.org/dc/elements/1.1/" '
               'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">')


_DIDL_CLOSE = '</DIDL-Lite>'


def _didl_container(cid, parent, title, child_count):
    return (f'<container id="{_xml_esc(cid)}" parentID="{_xml_esc(parent)}" '
            f'restricted="1" childCount="{child_count}">'
            f'<dc:title>{_xml_esc(title)}</dc:title>'
            f'<upnp:class>object.container.playlistContainer</upnp:class>'
            f'</container>')


def _didl_track(t, parent_id):
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


def _didl_album(r, parent):
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
    return _didl_container(cid, parent, title, r.get("track_count", 0))


def _didl_video(v, parent_id):
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


class _Browse:
    """One Browse request. Carries the SOAP arguments and the three
    response shapes every handler needs, so the 26 handlers stop
    re-implementing the same envelope + slice arithmetic.

    `count == 0` means "no limit" in the ContentDirectory spec, which is
    why the slice is conditional rather than a plain [start:start+count]."""

    __slots__ = ("obj_id", "flag", "start", "count")

    def __init__(self, obj_id: str, flag: str, start: int, count: int):
        self.obj_id, self.flag, self.start, self.count = obj_id, flag, start, count

    @property
    def is_meta(self) -> bool:
        return self.flag == "BrowseMetadata"

    def page(self, rows: list) -> list:
        return (rows[self.start:self.start + self.count] if self.count
                else rows[self.start:])

    def meta(self, parent: str, title: str, total: int, cid: str = None) -> tuple:
        """The BrowseMetadata answer: this container describing itself."""
        return (_DIDL_OPEN
                + _didl_container(self.obj_id if cid is None else cid,
                                  parent, title, total)
                + _DIDL_CLOSE, 1, 1)

    def listing(self, items: list, total: int) -> tuple:
        return _DIDL_OPEN + "".join(items) + _DIDL_CLOSE, len(items), total

    def empty(self) -> tuple:
        """Garbled / unresolvable id → an empty container, never a 500.
        A Naim control point handles '0 results' gracefully; a fault makes
        it abandon the whole browse."""
        return _DIDL_OPEN + _DIDL_CLOSE, 0, 0
