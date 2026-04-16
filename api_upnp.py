#!/usr/bin/env python3
"""
api_upnp.py — UPnP ContentDirectory gateway + SSDP announcer.

Exposes the gateway's playlists/favourites as a UPnP MediaServer so that
the Naim Uniti (and other UPnP control points) can browse and play them.

Handles: GET /gw/device.xml, /gw/cd/desc.xml, /gw/cd/events
         POST /gw/cd/control  (SOAP ContentDirectory Browse)

Also exports: GW_UDN, GW_NAME, gw_ssdp_announcer, gw_ssdp_byebye
"""
import logging
import socket
import struct
import time
import xml.etree.ElementTree as ET

from dlna_library import DB

log = logging.getLogger("dlna.api.upnp")

# ── Gateway UPnP identity ─────────────────────────────────────────
GW_UDN  = "uuid:dlna-gateway-iina-8765"
GW_NAME = "DLNA Gateway (IINA)"


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
        '<serviceList><service>'
        '<serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>'
        '<serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>'
        '<SCPDURL>/gw/cd/desc.xml</SCPDURL>'
        '<controlURL>/gw/cd/control</controlURL>'
        '<eventSubURL>/gw/cd/events</eventSubURL>'
        '</service></serviceList>'
        '</device></root>'
    )


def _gw_cd_desc_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<scpd xmlns="urn:schemas-upnp-org:service-1-0">'
        '<specVersion><major>1</major><minor>0</minor></specVersion>'
        '<actionList><action><n>Browse</n><argumentList>'
        '<argument><n>ObjectID</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_ObjectID</relatedStateVariable></argument>'
        '<argument><n>BrowseFlag</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_BrowseFlag</relatedStateVariable></argument>'
        '<argument><n>Filter</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Filter</relatedStateVariable></argument>'
        '<argument><n>StartingIndex</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Index</relatedStateVariable></argument>'
        '<argument><n>RequestedCount</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '<argument><n>SortCriteria</n><direction>in</direction>'
        '<relatedStateVariable>A_ARG_TYPE_SortCriteria</relatedStateVariable></argument>'
        '<argument><n>Result</n><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Result</relatedStateVariable></argument>'
        '<argument><n>NumberReturned</n><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '<argument><n>TotalMatches</n><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '<argument><n>UpdateID</n><direction>out</direction>'
        '<relatedStateVariable>A_ARG_TYPE_Count</relatedStateVariable></argument>'
        '</argumentList></action></actionList>'
        '<serviceStateTable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_ObjectID</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_BrowseFlag</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_Filter</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_Index</n>'
        '<dataType>ui4</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_Count</n>'
        '<dataType>ui4</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_SortCriteria</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="no"><n>A_ARG_TYPE_Result</n>'
        '<dataType>string</dataType></stateVariable>'
        '<stateVariable sendEvents="yes"><n>SystemUpdateID</n>'
        '<dataType>ui4</dataType></stateVariable>'
        '</serviceStateTable></scpd>'
    )


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

    OPEN  = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" '
             'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">')
    CLOSE = '</DIDL-Lite>'

    if obj_id == "0":
        if browse_flag == "BrowseMetadata":
            return OPEN + container("0", "-1", GW_NAME, 1) + CLOSE, 1, 1
        n_pls = len(DB.pl_list())
        return OPEN + container("playlists", "0", "Playlists", n_pls) + CLOSE, 1, 1

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

    return OPEN + CLOSE, 0, 0


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

def cd_control(h, body):
    try:
        root   = ET.fromstring(body.decode("utf-8"))
        ns     = {"s": "http://schemas.xmlsoap.org/soap/envelope/",
                  "u": "urn:schemas-upnp-org:service:ContentDirectory:1"}
        browse = root.find(".//u:Browse", ns)
        if browse is None:
            browse = root.find(".//Browse")
        if browse is None:
            log.debug("GW Browse: no Browse element in SOAP body (ignored)")
            h._html(400, "<h1>Missing Browse element</h1>")
            return
        obj_id = browse.findtext("ObjectID") or "0"
        flag   = browse.findtext("BrowseFlag") or "BrowseDirectChildren"
        start  = int(browse.findtext("StartingIndex") or 0)
        count  = int(browse.findtext("RequestedCount") or 0)
        if count == 0:
            count = 9999
        result_xml, n_ret, total = _gw_browse(obj_id, flag, start, count)
        resp = _gw_browse_response(result_xml, n_ret, total)
        log.debug(f"GW SOAP Browse {obj_id!r} → {n_ret}/{total}")
        h._xml_response(200, resp)
    except Exception as e:
        log.error(f"GW Browse error: {e}")
        h._html(500, f"<h1>Browse error: {e}</h1>")


# ── SSDP announcer ────────────────────────────────────────────────

def _gw_ssdp_notify(lan_ip: str, port: int, alive: bool = True):
    location = f"http://{lan_ip}:{port}/gw/device.xml"
    entries = [
        ("upnp:rootdevice",
         f"{GW_UDN}::upnp:rootdevice"),
        (GW_UDN,
         GW_UDN),
        ("urn:schemas-upnp-org:device:MediaServer:1",
         f"{GW_UDN}::urn:schemas-upnp-org:device:MediaServer:1"),
        ("urn:schemas-upnp-org:service:ContentDirectory:1",
         f"{GW_UDN}::urn:schemas-upnp-org:service:ContentDirectory:1"),
    ]
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
