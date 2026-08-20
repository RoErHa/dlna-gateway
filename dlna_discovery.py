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
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from dlna_discovery_probe import (  # noqa: F401 — re-exported for callers
    _PROBE_PATHS,
    _PROBE_PORTS,
    _probe_host,
    _tcp_open,
    subnet_scan,
    subnet_scan_if_empty,
)
from dlna_discovery_ssdp import (  # noqa: F401 — re-exported for callers
    SSDP_ADDR,
    SSDP_PORT,
    ssdp_discovery_thread,
)
from dlna_library import DEVICE_ROLES
from dlna_registry import (  # noqa: F401 — re-exported for callers
    MediaServer, MediaRenderer,
    ServerRegistry, RendererRegistry,
    SERVERS, RENDERERS, _STALE_SEC,
)

log = logging.getLogger("dlna.discovery")


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
        rc_url = None    # RenderingControl — used by the volume trim SetVolume
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
                srv = MediaServer(
                    udn=udn, name=name, location=location,
                    control_url=cd_url, base_url=base)
                servers.add(srv)
                # Bind a LibraryProvider for this UDN — Phase 1 of the
                # AssetUPnP migration. Every UPnP server gets a
                # UpnpProvider; the api_browse and Indexer paths look
                # this up via get_provider(udn). See CLAUDE.md
                # "Library backend migration (in flight)" for the plan.
                # Re-binding on re-probe is intentional (control_url
                # may have changed).
                from dlna_providers import bind_provider
                from dlna_providers.upnp import UpnpProvider
                bind_provider(udn, UpnpProvider(srv))

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
    before_servers = {s.udn for s in SERVERS.all()}
    _fetch_device(location, SERVERS, RENDERERS, gw_udn)
    # Call indexer hook for any newly added servers
    if _on_server_found:
        for srv in SERVERS.all():
            if srv.udn not in before_servers:
                log.debug(f"Triggering indexer for new server {srv.name!r}")
                _on_server_found(srv)



# ── Server heartbeat ─────────────────────────────────────────────

_heartbeat_fails: dict = {}   # udn → consecutive failure count


def _heartbeat_once():
    """One heartbeat pass over SERVERS. Extracted from the loop so it's
    unit-testable."""
    for srv in SERVERS.all():
        udn = srv.udn
        # The in-process LocalFs file server is ALWAYS online — it runs in this
        # process. HTTP-probing its base URL (`:8201/`, which 404s on GET /)
        # would falsely mark it offline → SERVERS.online() goes empty →
        # subnet_scan_if_empty fires a 254-host scan every 60 s (the FD sawtooth
        # / the EMFILE-crash contributor). Just keep last_seen fresh.
        if udn.startswith("uuid:localfs-"):
            SERVERS.touch(udn)
            continue
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


def heartbeat_thread(gw_udn: str = ""):
    """
    Background thread: ping each known server's location URL every 30 s.

    On success  → SERVERS.touch(udn) keeps last_seen fresh → no offline flicker.
    On failure  → increment per-server counter; after 2 consecutive failures
                  (≥ 60 s) set last_seen = 0 so the UI shows offline promptly.
    The in-process LocalFs server is exempt (always online) — see _heartbeat_once.
    """
    time.sleep(15)   # let SSDP / pre-probe settle first
    while True:
        _heartbeat_once()
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
    setup_logging(debug=True)
    log.info("=== dlna_discovery self-test (20 s SSDP) ===")

    # Use LAN IP
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except OSError as e:
        log.warning(f"LAN IP probe failed ({e}) — using 127.0.0.1")
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
