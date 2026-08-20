#!/usr/bin/env python3
"""
dlna_discovery_probe.py — the non-SSDP fallbacks: a TCP/HTTP subnet
sweep for UPnP device descriptors, and the "only scan if SSDP found
nothing" guard around it.

Split out of dlna_discovery.py (2026-08-20), which had reached 474
lines. This path exists because SSDP is multicast and therefore
unreliable across VLANs, sleeping hosts and some switches — the sweep
is the belt to SSDP's braces, not a normal-operation code path.

Device-description URLs go to `_register` below rather than being
parsed here — see that function for why the import is deliberately lazy.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.parse

from dlna_registry import SERVERS

log = logging.getLogger("dlna.discovery")


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


def _forget(location: str) -> None:
    """Drop a location from the registrar's already-seen set so the next
    `_register` re-probes it. Same lazy-import rationale as `_register`:
    the seen-set lives in `dlna_discovery` and must be the live one."""
    import dlna_discovery
    with dlna_discovery._seen_lock:
        dlna_discovery._seen_locations.discard(location)


_PROBE_PORTS = [26125, 1780, 49152, 49153, 49154, 49155, 8200, 8096, 7359]
_PROBE_PATHS = ["/description.xml", "/DeviceDescription.xml",
                "/rootDesc.xml", "/upnp/desc/MediaServer/description.xml"]


def _tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        s.close()
        return True
    except OSError as e:
        log.debug(f"port probe {host}:{port} closed/unreachable ({e})")
        return False


def _probe_host(host: str, stop: threading.Event, gw_udn: str):
    for port in _PROBE_PORTS:
        if stop.is_set():
            return
        if not _tcp_open(host, port):
            continue
        for path in _PROBE_PATHS:
            if stop.is_set():
                return
            url = f"http://{host}:{port}{path}"
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "DLNAGateway/1.0"})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    body = resp.read(2048).decode("utf-8", errors="replace")
                if "MediaServer" in body or "ContentDirectory" in body \
                        or "MediaRenderer" in body:
                    log.info(f"Subnet scan found device at {url}")
                    stop.set()
                    threading.Thread(target=_register,
                                     args=(url, gw_udn), daemon=True).start()
                    return
            except Exception as e:                # keep scanning other hosts
                log.debug(f"subnet scan: {url} did not answer ({e})")


def subnet_scan(lan_ip: str, gw_udn: str = ""):
    """Scan /24 for UPnP servers and renderers."""
    prefix = lan_ip.rsplit(".", 1)[0]
    log.info(f"Subnet scan: {prefix}.1–254")
    stop = threading.Event()
    threads = []
    for i in range(1, 255):
        host = f"{prefix}.{i}"
        if host == lan_ip:
            continue
        t = threading.Thread(target=_probe_host,
                             args=(host, stop, gw_udn), daemon=True)
        threads.append(t)
        t.start()
        if i % 32 == 0:
            time.sleep(0.05)
    for t in threads:
        t.join(timeout=5)
    if not stop.is_set():
        log.warning("Subnet scan found nothing. Use --probe <url>")


def subnet_scan_if_empty(lan_ip: str, gw_udn: str = "",
                         probe_url_fn=None):
    """
    Wait 15 s for SSDP; subnet-scan only if nothing found AND no known
    servers were pre-probed. Then every 60 s, re-probe offline known servers.
    """
    time.sleep(15)
    if SERVERS.empty():
        log.info("No servers found via SSDP or pre-probe — subnet scan…")
        subnet_scan(lan_ip, gw_udn)

    while True:
        time.sleep(60)
        if SERVERS.empty():
            log.info("No servers seen yet — subnet scan…")
            subnet_scan(lan_ip, gw_udn)
        elif not SERVERS.online():
            # Known servers all went offline — re-probe their saved locations
            from dlna_library import DEVICE_ROLES
            known = DEVICE_ROLES.known_servers()
            if known:
                for s in known:
                    log.info(f"Re-probing known server {s['name']!r} @ {s['location']}")
                    _forget(s["location"])
                    _register(s["location"], gw_udn)
            else:
                saved = probe_url_fn() if probe_url_fn else ""
                if saved:
                    log.info(f"Servers offline — re-probing {saved}")
                    _forget(saved)
                    _register(saved, gw_udn)
                else:
                    log.info("Servers offline — subnet scan…")
                    subnet_scan(lan_ip, gw_udn)
