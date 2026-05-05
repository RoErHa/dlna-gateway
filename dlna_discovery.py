#!/usr/bin/env python3
"""
dlna_discovery.py — SSDP discovery for MediaServers AND MediaRenderers.

Writes into two thread-safe registries (SERVERS, RENDERERS) that live
in dlna_registry. Callers elsewhere keep importing SERVERS/RENDERERS
from this module — they're re-exported below for backward compat.

Standalone test (runs SSDP for 20 s and prints found devices):
    python dlna_discovery.py
"""
import logging
import re
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from dlna_library import DEVICE_ROLES
from dlna_registry import (  # noqa: F401 — re-exported for callers
    MediaServer, MediaRenderer,
    ServerRegistry, RendererRegistry,
    SERVERS, RENDERERS, _STALE_SEC,
)

log = logging.getLogger("dlna.discovery")

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

# Hardcoded policy: the gateway tracks exactly one external MediaServer —
# AssetUPnP. Other discovered MediaServers (Naim Uniti playqueue, LG TV
# media share, Plex DLNA, etc.) are dropped at discovery time, even if
# they're valid UPnP, so the UI never shows competing libraries.
# The gateway's own self-announce is excluded separately via gw_udn.
ALLOWED_SERVER_NAME_PREFIX = "Asset UPnP"


# ── Device description fetcher ────────────────────────────────────

def _fetch_device(location: str,
                  servers: ServerRegistry,
                  renderers: RendererRegistry,
                  gw_udn: str = ""):
    """
    Fetch UPnP device description XML.
    Registers as MediaServer, MediaRenderer, or both.
    Skips the gateway's own UDN to prevent self-discovery.
    """
    try:
        req = urllib.request.Request(
            location, headers={"User-Agent": "DLNAGateway/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            xml_data = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"_fetch_device({location}): {e}")
        return

    try:
        root   = ET.fromstring(xml_data)
        ns     = {"u": "urn:schemas-upnp-org:device-1-0"}
        parsed = urllib.parse.urlparse(location)
        base   = f"{parsed.scheme}://{parsed.netloc}"

        device = root.find(".//u:device", ns)
        if device is None:
            return

        udn  = device.findtext("u:UDN", "", ns) or f"uuid:{uuid.uuid4()}"
        name = device.findtext("u:friendlyName", "Unknown Device", ns)
        dev_type = device.findtext("u:deviceType", "", ns)

        if gw_udn and udn == gw_udn:
            log.debug(f"Skipping self-discovery ({udn})")
            return

        # Extract host IP for cross-UDN matching (combined devices like Naim Uniti
        # use different UDNs for ContentDirectory vs AVTransport services)
        host = parsed.hostname or ""

        # Check for ContentDirectory → MediaServer
        cd_url = None
        av_url = None
        rc_url = None    # RenderingControl — used by loudness normalization SetVolume
        for svc in device.findall(".//u:service", ns):
            stype = svc.findtext("u:serviceType", "", ns)
            ctrl  = svc.findtext("u:controlURL", "", ns) or ""
            ctrl  = ctrl if ctrl.startswith("http") else base + ctrl
            if "ContentDirectory" in stype:
                cd_url = ctrl
            if "AVTransport" in stype:
                av_url = ctrl
            if "RenderingControl" in stype:
                rc_url = ctrl

        if cd_url and "MediaServer" in dev_type and not av_url:
            if not name.startswith(ALLOWED_SERVER_NAME_PREFIX):
                log.debug(f"Ignoring non-allowlisted MediaServer {name!r} @ {host}")
            else:
                DEVICE_ROLES.mark(udn, name, location=location, host=host, is_server=True)
                servers.add(MediaServer(
                    udn=udn, name=name, location=location,
                    control_url=cd_url, base_url=base))

        if av_url:
            DEVICE_ROLES.mark(udn, name, location=location, host=host, is_renderer=True)
            renderers.add(MediaRenderer(
                udn=udn, name=name, location=location,
                av_url=av_url, base_url=base, rc_url=rc_url or ""))
            if "MediaServer" in dev_type:
                log.debug(f"Combined device {name!r}: has AVTransport → "
                          f"renderer only, skipping ContentDirectory")

    except ET.ParseError as e:
        log.debug(f"XML parse error ({location}): {e}")
    except Exception as e:
        log.debug(f"_fetch_device parse error ({location}): {e}")


# ── Registration entry point ──────────────────────────────────────

_seen_locations: set = set()
_seen_lock = threading.Lock()

# Injected by gateway at startup to avoid forward import
_on_server_found = None   # callable(MediaServer) — starts indexer


def _register_location(location: str, gw_udn: str = ""):
    with _seen_lock:
        if location in _seen_locations:
            return
        _seen_locations.add(location)
    time.sleep(1.5)   # let AssetUPnP finish booting if just started
    before_servers = set(s.udn for s in SERVERS.all())
    _fetch_device(location, SERVERS, RENDERERS, gw_udn)
    # Call indexer hook for any newly added servers
    if _on_server_found:
        for srv in SERVERS.all():
            if srv.udn not in before_servers:
                log.debug(f"Triggering indexer for new server {srv.name!r}")
                _on_server_found(srv)


# ── SSDP discovery thread ─────────────────────────────────────────

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
            try: rx.close()
            except Exception: pass
        rx = None

    def handle(data: bytes):
        msg = data.decode("utf-8", errors="replace")
        m = re.search(r"LOCATION:\s*(\S+)", msg, re.IGNORECASE)
        if not m:
            return
        loc = m.group(1).strip()
        threading.Thread(target=_register_location, args=(loc, gw_udn),
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
            except socket.timeout:
                pass
            except Exception as e:
                log.debug(f"SSDP recv: {e}")


# ── Subnet scanner ────────────────────────────────────────────────

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
    except Exception:
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
                    threading.Thread(target=_register_location,
                                     args=(url, gw_udn), daemon=True).start()
                    return
            except Exception:
                pass


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
                    with _seen_lock:
                        _seen_locations.discard(s["location"])
                    _register_location(s["location"], gw_udn)
            else:
                saved = probe_url_fn() if probe_url_fn else ""
                if saved:
                    log.info(f"Servers offline — re-probing {saved}")
                    with _seen_lock:
                        _seen_locations.discard(saved)
                    _register_location(saved, gw_udn)
                else:
                    log.info("Servers offline — subnet scan…")
                    subnet_scan(lan_ip, gw_udn)


# ── Server heartbeat ─────────────────────────────────────────────

_heartbeat_fails: dict = {}   # udn → consecutive failure count


def heartbeat_thread(gw_udn: str = ""):
    """
    Background thread: ping each known server's location URL every 30 s.

    On success  → SERVERS.touch(udn) keeps last_seen fresh → no offline flicker.
    On failure  → increment per-server counter; after 2 consecutive failures
                  (≥ 60 s) set last_seen = 0 so the UI shows offline promptly.
    """
    time.sleep(15)   # let SSDP / pre-probe settle first
    while True:
        for srv in SERVERS.all():
            udn = srv.udn
            try:
                req = urllib.request.Request(
                    srv.location, headers={"User-Agent": "DLNAGateway/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    resp.read(512)   # just enough to confirm the server is alive
                SERVERS.touch(udn)
                if udn in _heartbeat_fails:
                    log.info(f"Heartbeat: {srv.name!r} back online")
                    _heartbeat_fails.pop(udn)
                else:
                    log.debug(f"Heartbeat OK: {srv.name!r}")
            except Exception as e:
                fails = _heartbeat_fails.get(udn, 0) + 1
                _heartbeat_fails[udn] = fails
                log.debug(f"Heartbeat fail ({fails}×): {srv.name!r}: {e}")
                if fails == 2:
                    # First crossover: force offline so the UI reflects reality
                    # within 60 s. Subsequent ticks skip both the log and the
                    # write — already offline, idempotent.
                    with SERVERS._lock:
                        if udn in SERVERS._d:
                            SERVERS._d[udn].last_seen = 0
                    log.info(f"Heartbeat: {srv.name!r} marked offline "
                             f"(2 consecutive failures)")
        time.sleep(30)


# ── Direct probe ──────────────────────────────────────────────────

def probe_url(location: str, gw_udn: str = ""):
    """Directly register a device by URL — bypasses SSDP."""
    log.info(f"Probing {location} …")
    with _seen_lock:
        # Allow re-probing the saved URL even if seen before
        _seen_locations.discard(location)
    _register_location(location, gw_udn)


# ── Standalone test ───────────────────────────────────────────────

def _test():
    import sys
    from dlna_config import setup_logging
    log_root = setup_logging(debug=True)
    log.info("=== dlna_discovery self-test (20 s SSDP) ===")

    # Use LAN IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        lan_ip = "127.0.0.1"

    log.info(f"LAN IP: {lan_ip}")

    if len(sys.argv) > 1:
        # Direct probe mode: python dlna_discovery.py http://...
        probe_url(sys.argv[1])
        time.sleep(5)
    else:
        t = threading.Thread(target=ssdp_discovery_thread, args=(lan_ip,),
                             daemon=True)
        t.start()
        log.info("Listening for SSDP… (20 s)")
        time.sleep(20)

    servers   = SERVERS.all()
    renderers = RENDERERS.all()

    print(f"\n{'─'*50}")
    print(f"SERVERS   ({len(servers)}):")
    for s in servers:
        print(f"  ✓ {s.name!r}  udn={s.udn}  ctrl={s.control_url}")
    print(f"RENDERERS ({len(renderers)}):")
    for r in renderers:
        print(f"  ✓ {r.name!r}  udn={r.udn}  av={r.av_url}")
    print(f"{'─'*50}")

    ok = len(servers) > 0 or len(renderers) > 0
    print(f"{'PASS' if ok else 'NOTE: nothing found (VPN? SSDP blocked?)'}")


if __name__ == "__main__":
    _test()
