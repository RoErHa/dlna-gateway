#!/usr/bin/env python3
"""
api_upnp.py — the gateway as a UPnP/DLNA MediaServer: SOAP control
endpoints, and the public face of the api_upnp module family.

Exposes the library so the Naim Uniti (and the LG WebOS TV) can browse and
play it without the PWA. Root tree: Artists → Albums → Tracks, an Albums A–Z
letter index, Genres → Albums → Tracks (all backed by LibraryDB on
DB.primary_udn()), plus ⭐ Favourite Albums, Playlists, 📹 Videos and
📖 Audiobooks. Lists paginate via StartingIndex/RequestedCount; album
ObjectIDs carry the LocalFs album_key so folder-albums (incl.
Various-Artists comps) resolve correctly.

Handles: GET  /gw/device.xml, /gw/cd/desc.xml, /gw/cd/events
         POST /gw/cd/control  (SOAP ContentDirectory)
         POST /gw/cm/control  (SOAP ConnectionManager)

── Module family ────────────────────────────────────────────────────
This file was 1,349 lines until 2026-08-20. It is now the SOAP layer plus
re-exports; everything else moved to a sibling:

    api_upnp_ids.py          identity, ObjectID codecs, junk filter, DB reads
    api_upnp_didl.py         DIDL-Lite renderers + the _Browse context
    api_upnp_browse.py       music/books/playlists/favourites + dispatch tables
    api_upnp_browse_video.py the GWMovies video tree
    api_upnp_descriptors.py  device.xml + the two service SCPDs
    api_upnp_ssdp.py         SSDP announce/M-SEARCH + GENA eventing

Every public name is re-exported below, so `import api_upnp` and
`api_upnp.<anything>` behave exactly as before for callers AND tests.

⚠ `DB` is bound ONCE, in api_upnp_ids. To inject a temp library in a test,
patch it THERE (`patch.object(api_upnp_ids, "DB", db)`) — patching
`api_upnp.DB` only rebinds this module's re-export and would leave the
browse handlers pointed at the real library.db.
"""
import logging
import xml.etree.ElementTree as ET

# ── Re-exports: the family's public surface ──────────────────────────
from api_upnp_browse import (  # noqa: F401
    _BROWSE_EXACT,
    _BROWSE_PREFIX,
    _gw_browse,
    _gw_browse_response,
)
from api_upnp_descriptors import (  # noqa: F401
    _gw_cd_desc_xml,
    _gw_cm_desc_xml,
    _gw_device_xml,
)
from api_upnp_didl import (  # noqa: F401
    _DIDL_CLOSE,
    _DIDL_OPEN,
    _Browse,
    _didl_album,
    _didl_container,
    _didl_track,
    _didl_video,
)
from api_upnp_ids import (  # noqa: F401
    DB,
    GW_NAME,
    GW_UDN,
    _ab_udn,
    _album_letters,
    _b64d,
    _b64e,
    _decode_ab_book_id,
    _decode_album_id,
    _decode_lib_album_id,
    _encode_ab_book_id,
    _encode_album_id,
    _encode_lib_album_id,
    _fmt_duration,
    _get_lan_ip,
    _is_junk_name,
    _letter_of,
    _lib_albums,
    _lib_artists,
    _lib_genres,
    _VIDEO_UDN,
    _xml_esc,
)
from api_upnp_ssdp import (  # noqa: F401
    _gw_msearch_replies,
    _gw_msearch_response,
    _gw_ssdp_entries,
    _gw_ssdp_notify,
    _parse_callback,
    gw_event_initial_notify,
    gw_event_subscribe,
    gw_ssdp_announcer,
    gw_ssdp_byebye,
    gw_ssdp_responder,
)

log = logging.getLogger("dlna.api.upnp")


# ── SOAP control ─────────────────────────────────────────────────────

# ── POST handler ──────────────────────────────────────────────────

_SOAP_CTYPE = 'text/xml; charset="utf-8"'


_CD_NS = "urn:schemas-upnp-org:service:ContentDirectory:1"


_CM_NS = "urn:schemas-upnp-org:service:ConnectionManager:1"


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
    ).encode()


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
        return 500, "text/html", f"<h1>error: {e}</h1>".encode()


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
        return 500, "text/html", f"<h1>error: {e}</h1>".encode()


def cd_control(h, body):
    """Legacy (h, body) wrapper around cd_control_soap. Cleanup C made /gw/*
    native in the ASGI app (which calls cd_control_soap directly), so this is
    no longer on a live path — retained as the dlna_routes fallback shape."""
    status, _ctype, payload = cd_control_soap(body)
    if status == 200:
        h._xml_response(200, payload)
    else:
        h._html(status, payload.decode("utf-8"))


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
