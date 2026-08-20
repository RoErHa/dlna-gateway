#!/usr/bin/env python3
"""
api_upnp_ssdp.py — SSDP presence (NOTIFY + M-SEARCH) and GENA
eventing for the gateway-as-MediaServer.

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

Both halves of discovery are required: the announcer NOTIFYs every 60s for
control points listening passively, and the M-SEARCH responder answers the
ACTIVE searches a control point makes on startup. With only the first, a
client that boots after our last NOTIFY never finds us.

⚠ The initial GENA NOTIFY fires on a daemon `threading.Thread`, NOT
`asyncio.create_task` — an un-referenced task is garbage-collected before it
runs, the NOTIFY never sends, and GUPnP/dLeyna then re-SUBSCRIBEs forever and
never browses. That was the final bug of the 2026-06-13 session (commit
90afef7); do not "modernise" it into a task.
"""
import logging
import socket
import time
import uuid
from urllib.parse import urlparse

import http.client

from api_upnp_ids import GW_UDN, _xml_esc

log = logging.getLogger("dlna.api.upnp")


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
            f'{inner}</e:propertyset>').encode()
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
    ).encode()


def _gw_msearch_replies(data: bytes, location: str) -> list:
    """If `data` is an SSDP M-SEARCH this MediaServer should answer, return the
    [(ST, USN, response_bytes), …] to unicast back; else []. Answers ssdp:all,
    upnp:rootdevice, our UDN, the MediaServer device type and the
    ContentDirectory service. Pure → unit-testable without sockets."""
    try:
        msg = data.decode("utf-8", "replace")
    except (AttributeError, TypeError):
        # errors="replace" cannot raise UnicodeDecodeError, so the only
        # real failure here is a non-bytes payload from a bad caller.
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
