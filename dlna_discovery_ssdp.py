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

THIS IS THE UNAUTHENTICATED INPUT PATH. Anything on the LAN can send
UDP to port 1900, unconnected and with a forgeable source, and have
`parse_location` read it. Two rules follow, and both are enforced
below rather than trusted to the sender (audit Track B3, 2026-08-21):

  * **A packet may not name a URL that isn't the sender's own.** A
    device announces where to fetch ITS description; a LOCATION
    pointing anywhere else turns the gateway into an unauthenticated
    HTTP reflector.
  * **A packet may not cost an unbounded amount of work.** Each one
    used to spawn a thread that slept 1.5 s and then made a request,
    so a few thousand packets a second exhausted threads and file
    descriptors — surfacing as SQLite's "unable to open database
    file", which doesn't look like an attack at all. Registrations
    are now capped and excess packets are DROPPED, which is safe
    because devices re-announce on a timer.
"""
from __future__ import annotations

import logging
import re
import socket
import struct
import threading
import time
import urllib.parse

from dlna_config import close_quietly

log = logging.getLogger("dlna.discovery")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

# Only the head of a datagram is scanned for headers. A real SSDP packet is
# a few hundred bytes; this bounds the regex against a jumbo datagram.
_MAX_SCAN_BYTES = 2048
# Device-description URLs are short. 512 is generous for the longest real
# one seen (a Windows Media Player UUID path).
_MAX_LOCATION_LEN = 512
# Concurrent registrations in flight. Discovery is a background convenience:
# a handful at a time finds every device on a home LAN within one announce
# cycle, and the cap is what makes a flood cost nothing.
_MAX_INFLIGHT = 8
_INFLIGHT = threading.Semaphore(_MAX_INFLIGHT)
# One WARNING per source per minute — a flood must not become a log flood.
_DROP_WARN_SEC = 60.0
_last_drop_warn = 0.0


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


def parse_location(data: bytes, src_ip: str = "") -> str | None:
    """Extract a usable device-description URL from one SSDP datagram, or
    `None` to drop the packet. Pure — no sockets, no threads — so the
    hostile cases are directly testable (`tests/test_ssdp_parsing.py`).

    `src_ip` is the datagram's source address. When given, the URL's host
    must BE that address: a device announces its own description, and
    allowing anything else lets any peer aim the gateway's HTTP client at a
    third party. Passing `""` skips that check, for the callers that
    genuinely have no peer (the subnet-scan path).

    Note the host comparison uses `urlsplit().hostname`, which is the part
    after any `user@` — so `http://192.168.1.5@elsewhere/` is correctly read
    as a URL for `elsewhere`, and refused.
    """
    msg = data[:_MAX_SCAN_BYTES].decode("utf-8", errors="replace")
    # Anchored at line start so a LOCATION cannot be smuggled inside another
    # header's value. NOT anchored at line end: SSDP lines end `\r\n`, and a
    # `$` would then have to account for the `\r` — getting that wrong
    # rejects every real device, which is how this was first written.
    # `\S+` already stops at the `\r`.
    m = re.search(r"^LOCATION:[ \t]*(\S+)", msg,
                  re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    loc = m.group(1).strip()
    if not loc or len(loc) > _MAX_LOCATION_LEN:
        return None
    try:
        parts = urllib.parse.urlsplit(loc)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if src_ip and parts.hostname != src_ip:
        log.debug(f"SSDP: {src_ip} announced a URL for "
                  f"{parts.hostname} — dropped")
        return None
    return loc


def _spawn_registration(location: str, gw_udn: str) -> bool:
    """Register `location` on a worker thread if a slot is free.

    Returns False when the cap is reached, meaning the packet was dropped.
    That is the correct outcome rather than queueing: SSDP devices announce
    repeatedly, so a device dropped now is seen on its next NOTIFY, while an
    unbounded queue is exactly the resource a flood is trying to consume.
    """
    global _last_drop_warn
    if not _INFLIGHT.acquire(blocking=False):
        now = time.time()
        if now - _last_drop_warn >= _DROP_WARN_SEC:
            _last_drop_warn = now
            log.warning(f"SSDP: {_MAX_INFLIGHT} registrations already in "
                        "flight — dropping announcements until one finishes "
                        "(normal under a burst; sustained means a flood)")
        return False

    def _run():
        try:
            _register(location, gw_udn)
        finally:
            _INFLIGHT.release()

    threading.Thread(target=_run, daemon=True,
                     name="ssdp-register").start()
    return True


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

    def handle(data: bytes, src: tuple):
        loc = parse_location(data, src[0] if src else "")
        if loc:
            _spawn_registration(loc, gw_udn)

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
                data, src = s.recvfrom(4096)
                handle(data, src)
            except TimeoutError:
                pass
            except Exception as e:
                log.debug(f"SSDP recv: {e}")
