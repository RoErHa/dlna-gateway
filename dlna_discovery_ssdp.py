#!/usr/bin/env python3
"""
dlna_discovery_ssdp.py — the SSDP multicast side of discovery: the
periodic M-SEARCH sender and the NOTIFY/response listener.

Split out of dlna_discovery.py (2026-08-20), which had reached 474
lines. This module owns the SSDP wire constants; `dlna_discovery`
re-exports them so `dlna_discovery.SSDP_ADDR` still resolves.

Every device-description URL this finds is handed to `_register`
below rather than parsed here — see that function for why the import
is deliberately lazy.
"""
from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time

from dlna_config import close_quietly

log = logging.getLogger("dlna.discovery")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900


def _register(location: str, gw_udn: str = "") -> None:
    """Hand a discovered device-description URL to the registrar.

    Imported lazily and ON EVERY CALL, on purpose. `dlna_discovery`
    imports this module, so a module-level import would be a cycle — and
    more importantly `_register_location` reads the `_on_server_found`
    indexer hook that `dlna_gateway` INJECTS into `dlna_discovery` at
    startup, so the binding has to be resolved at call time to see it.
    """
    import dlna_discovery
    dlna_discovery._register_location(location, gw_udn)


_SEARCH_TYPES = [
    "ssdp:all",
    "urn:schemas-upnp-org:device:MediaServer:1",
    "urn:schemas-upnp-org:device:MediaRenderer:1",
]


def ssdp_discovery_thread(lan_ip: str, gw_udn: str = ""):
    """
    Sends periodic SSDP M-SEARCH and listens for NOTIFY alive.
    Discovers both MediaServers and MediaRenderers.
    """
    log.info(f"SSDP discovery starting on {lan_ip}")

    def make_msearch(st: str) -> bytes:
        return "\r\n".join([
            "M-SEARCH * HTTP/1.1",
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}",
            'MAN: "ssdp:discover"',
            "MX: 3",
            f"ST: {st}",
            "", ""
        ]).encode()

    # tx_sock: M-SEARCH + unicast replies
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
    try:
        tx.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                      socket.inet_aton(lan_ip))
    except OSError as e:
        log.warning(f"Multicast bind: {e}")
    tx.settimeout(2.0)

    # rx_sock: passive NOTIFY listener
    rx = None
    try:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        rx.bind(("0.0.0.0", SSDP_PORT))
        rx.settimeout(2.0)
        mcast = struct.pack("4s4s", socket.inet_aton(SSDP_ADDR),
                            socket.inet_aton(lan_ip))
        rx.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mcast)
        log.debug("SSDP multicast listener active")
    except OSError as e:
        log.warning(f"Cannot bind SSDP port 1900 ({e}) — M-SEARCH only")
        if rx:
            close_quietly(rx)
        rx = None

    def handle(data: bytes):
        msg = data.decode("utf-8", errors="replace")
        m = re.search(r"LOCATION:\s*(\S+)", msg, re.IGNORECASE)
        if not m:
            return
        loc = m.group(1).strip()
        threading.Thread(target=_register, args=(loc, gw_udn),
                         daemon=True).start()

    socks = [s for s in (tx, rx) if s]
    last_search = 0.0

    while True:
        now = time.time()
        if now - last_search >= 30:
            for st in _SEARCH_TYPES:
                try:
                    tx.sendto(make_msearch(st), (SSDP_ADDR, SSDP_PORT))
                except Exception as e:
                    log.debug(f"M-SEARCH error ({st}): {e}")
            last_search = now
            log.debug(f"M-SEARCH sent × {len(_SEARCH_TYPES)} types")

        for s in socks:
            try:
                data, _ = s.recvfrom(4096)
                handle(data)
            except TimeoutError:
                pass
            except Exception as e:
                log.debug(f"SSDP recv: {e}")
