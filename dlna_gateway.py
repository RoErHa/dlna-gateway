#!/usr/bin/env python3
"""
dlna_gateway.py — module wiring + background-service boot.

The HTTP edge is the ASGI app (`hypercorn dlna_asgi:app`); this module is no
longer a server entrypoint (Cleanup C retired the stdlib server). It owns
start_background_services() + get_lan_ip() (imported by the ASGI lifespan) and
the device-DB CLIs:
    ./setup.sh --run --list-devices    # show known devices, then exit
    ./setup.sh --run --reset-devices   # clear the device DB, then exit

Test individual modules:
    python dlna_config.py
    python dlna_discovery.py [http://probe-url]
    python dlna_content.py <control-url>
    python dlna_library.py
    python dlna_player.py [http://test-url]
"""
import argparse
import logging
import socket
import threading

import dlna_discovery as _disc
from dlna_config import load_config, raise_fd_limit, save_config, setup_logging
from dlna_events import EVENTS
from dlna_fdmon import start_fd_monitor
from dlna_library import DB, INDEXER, DEVICE_ROLES, ART_FETCHER
from api_upnp import (GW_UDN, gw_ssdp_announcer, gw_ssdp_byebye,  # noqa: F401
                      gw_ssdp_responder)

log = logging.getLogger("dlna.gateway")


# This module is no longer an HTTP entrypoint (Cleanup C retired the stdlib
# server). It keeps start_background_services() + get_lan_ip() — the ASGI
# lifespan (dlna_asgi._lifespan) imports both to boot the gateway — plus the
# device-DB CLIs (main(): --list-devices / --reset-devices). The web edge is
# `hypercorn dlna_asgi:app`; the PWA shell is served from static/ by that app.




# ── Helpers ───────────────────────────────────────────────────────

def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _on_server_found(server):
    """Hook called by discovery when a new MediaServer is registered.
    Skip indexing for combined devices (e.g. Naim Uniti) that appear as
    both a MediaServer and a MediaRenderer — they have no music library."""
    from dlna_discovery import RENDERERS
    EVENTS.publish({"type": "devices"})     # SSE: source list changed (R2)
    if RENDERERS.get(server.udn):
        log.info(f"Skipping indexer for {server.name!r} "
                 f"— registered as renderer (combined device)")
        return
    INDEXER.start(server, force=False)


# ── Background services ───────────────────────────────────────────

def start_background_services(lan_ip: str, port: int, *, probe: str = "") -> None:
    """Start every daemon thread + background worker the gateway needs:
    discovery (DB pre-probe / SSDP / subnet-scan fallback / heartbeat), the
    gateway-as-MediaServer SSDP announcer, the album-art + AcoustID startup
    mop-up scans, and the LocalFs provider wiring. Optionally kick a one-off
    `probe` URL.

    Extracted from main() so the 2.0 ASGI app starts the SAME services from
    its Hypercorn lifespan (dlna_asgi._lifespan) — ONE definition of "boot the
    gateway's background work", shared by the stdlib entrypoint and the ASGI
    server. Run exactly one of the two (they'd otherwise double-announce on
    SSDP). `port` is the gateway-as-MediaServer advert port."""
    # FD watchdog — logs open-FD count vs the limit so an FD leak shows up as a
    # rising trajectory (and an lsof breakdown in the danger zone) BEFORE it
    # exhausts the limit and crashes the gateway. See dlna_fdmon.
    start_fd_monitor()

    # Wire the indexer callback into discovery
    _disc._on_server_found = _on_server_found

    # Load persistent device role cache BEFORE any discovery thread starts, so
    # a combined device (Uniti) is classified renderer-only with no race.
    DEVICE_ROLES.load()

    # Immediately probe all previously-known servers from the DB cache — gets
    # them online in < 1s, before SSDP / subnet scan run.
    known_servers = DEVICE_ROLES.known_servers()
    if known_servers:
        log.info(f"Pre-probing {len(known_servers)} known server(s) from DB…")
        for s in known_servers:
            log.info(f"  → {s['name']!r}  {s['location']}")
            threading.Thread(
                target=_disc.probe_url,
                args=(s["location"], GW_UDN),
                daemon=True,
                name=f"probe-{s['udn'][:8]}").start()

    # SSDP discovery — finds MediaServers AND MediaRenderers
    threading.Thread(
        target=_disc.ssdp_discovery_thread,
        args=(lan_ip, GW_UDN),
        daemon=True, name="ssdp").start()

    # Gateway SSDP announcer — broadcasts ourselves as a MediaServer
    threading.Thread(
        target=gw_ssdp_announcer,
        args=(lan_ip, port),
        daemon=True, name="gw-ssdp").start()

    # Gateway SSDP M-SEARCH responder — answers active discovery so control
    # points that SEARCH (the Naim app/device) find us immediately, not only
    # if they happen to catch a periodic NOTIFY alive.
    threading.Thread(
        target=gw_ssdp_responder,
        args=(lan_ip, port),
        daemon=True, name="gw-ssdp-respond").start()

    # Subnet scanner fallback — only fires if nothing was found via pre-probe
    # or SSDP (a genuinely fresh install with no DB cache).
    threading.Thread(
        target=_disc.subnet_scan_if_empty,
        args=(lan_ip, GW_UDN),
        daemon=True, name="subnet-scan").start()

    # Server heartbeat — keeps last_seen fresh, eliminates offline flicker
    threading.Thread(
        target=_disc.heartbeat_thread,
        args=(GW_UDN,),
        daemon=True, name="heartbeat").start()

    # Album-art fetcher — one-shot startup scan 120s after boot to mop up
    # anything left bare by a previous interrupted run. Steady-state refills
    # come from Indexer._run() on each successful crawl; no periodic poll.
    ART_FETCHER.start_initial_scan()

    # LocalFs provider — wires in the in-process indexer + file server when
    # LOCALFS_MUSIC_ROOT is configured. Additive: UPnP discovery keeps running.
    from dlna_localfs_wiring import maybe_start_localfs
    maybe_start_localfs(get_lan_ip)

    # CLI --probe or config.json probe (fresh install / manual override). On
    # subsequent runs the DB cache handles this — but honour an explicit probe.
    probe_url = probe
    if not probe_url and not known_servers:
        cfg = load_config()
        probe_url = cfg.get("probe", "")
        if probe_url:
            log.info(f"No DB cache yet — probing saved URL: {probe_url}")

    if probe_url:
        def _probe():
            _disc.probe_url(probe_url, GW_UDN)
            cfg = load_config()
            cfg["probe"] = probe_url
            save_config(cfg)
        threading.Thread(target=_probe, daemon=True).start()


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="DLNA/UPnP Music Gateway",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./setup.sh --run                                  # Normal start
  ./setup.sh --run --probe http://192.168.1.x:port/DeviceDescription.xml
                                                    # Add server that doesn't respond to SSDP
  ./setup.sh --run --reset-devices                  # Clear device DB and rediscover everything
  ./setup.sh --run --list-devices                   # Show known devices table then exit
        """)
    # Legacy stdlib-server flags — accepted-but-ignored so forwarded args from
    # `setup.sh --run …` don't crash argparse. The HTTP edge is Hypercorn now
    # (dlna_asgi:app); only --list-devices/--reset-devices/--debug still act.
    parser.add_argument("--host",          default="0.0.0.0", help=argparse.SUPPRESS)
    parser.add_argument("--port",          type=int, default=8765, help=argparse.SUPPRESS)
    parser.add_argument("--probe",         default="", help=argparse.SUPPRESS)
    parser.add_argument("--no-browser",    action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--debug",         action="store_true")
    parser.add_argument("--reset-devices", action="store_true",
                        help="Clear the device_roles table and rediscover from scratch. "
                             "Use when a device was mis-classified or you want to "
                             "remove a device that no longer exists.")
    parser.add_argument("--list-devices",  action="store_true",
                        help="Print the known devices table and exit.")
    args = parser.parse_args()

    setup_logging(debug=args.debug)
    # macOS shells default to a 256 open-file soft limit; raise it so the
    # gateway doesn't hit EMFILE → sqlite 'unable to open database file' under
    # load. (1.x gets this from its launchd plist; shell-launched 2.x doesn't.)
    raise_fd_limit()

    # ── --list-devices: print table and exit ──────────────────────
    if args.list_devices:
        rows = DB.roles_all()
        if not rows:
            print("\nNo devices in DB yet. Run the gateway once to populate.\n")
        else:
            print(f"\n{'UDN':<42} {'Name':<28} {'Host':<16} {'Server':<8} {'Renderer':<10} Last seen")
            print("─" * 115)
            for r in rows:
                print(f"{r['udn']:<42} {(r['name'] or ''):<28} "
                      f"{(r['host'] or ''):<16} "
                      f"{'yes' if r['is_server'] else '':<8} "
                      f"{'yes' if r['is_renderer'] else '':<10} "
                      f"{r['last_seen']}")
            print()
        return

    # ── --reset-devices: wipe and exit (don't start the gateway) ──
    if args.reset_devices:
        rows_before = DB.roles_all()
        conn = DB._connect()
        conn.execute("DELETE FROM device_roles")
        conn.commit()
        conn.close()
        print(f"\n✓  Cleared {len(rows_before)} device(s) from device_roles table.")
        print("   Start the gateway normally to rediscover all devices:")
        print("   ./setup.sh --run")
        print("   Add --probe <url> if a server doesn't respond to SSDP.\n")
        return

    # ── No utility flag: the gateway runs via the ASGI stack now ──
    # Cleanup C retired the stdlib HTTP server; this entrypoint no longer
    # serves. start_background_services()/get_lan_ip() live on for the ASGI
    # lifespan (dlna_asgi._lifespan) to import. Point the user at the real
    # entrypoints and exit.
    print()
    print("  The DLNA Gateway now runs as an ASGI app (Hypercorn + dlna_asgi:app).")
    print("  Start it with one of:")
    print("    • production (launchd):  launchctl kickstart -k gui/$(id -u)/com.roha.dlna-gateway")
    print("                             (or ./setup.sh --restart)")
    print("    • foreground / dev:      ./run-2.0-asgi.sh")
    print()
    print("  This entrypoint only keeps the device-DB utilities:")
    print("    ./setup.sh --run --list-devices    # show known devices")
    print("    ./setup.sh --run --reset-devices   # clear the device DB")
    print()


if __name__ == "__main__":
    main()