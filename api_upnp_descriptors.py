#!/usr/bin/env python3
"""
api_upnp_descriptors.py — the device descriptor and the two service
SCPDs that make strict DLNA clients willing to browse us at all.

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

HARD-WON (2026-06-13) — the Naim (dLeyna/GUPnP) and the LG WebOS TV both
REFUSED to browse until every one of these was right, and each failure was
silent:
  * `MediaServer:1` + `<dlna:X_DLNADOC>DMS-1.50` + an `<iconList>` — TVs will
    not list an icon-less server.
  * a serviceList carrying BOTH ContentDirectory:1 AND ConnectionManager:1.
    ConnectionManager is MANDATORY; its absence was why both clients quit.
  * the SCPDs must use `<name>` tags — a stray `<n>` made clients fail to
    parse the service entirely.
Diagnose with GATEWAY_DEBUG=1 and the `GW /gw/…` lines in gateway.log, which
show exactly what a client asks for, in order.
"""
import logging

from api_upnp_ids import GW_NAME, GW_UDN

log = logging.getLogger("dlna.api.upnp")


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
