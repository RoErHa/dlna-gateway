#!/usr/bin/env python3
"""
dlna_gateway.py — Entry point. Wires all modules together and starts the server.

Usage:
    python dlna_gateway.py [--host 0.0.0.0] [--port 8765]
                           [--probe http://<ip>:<port>/desc.xml]
                           [--debug] [--no-browser]

Test individual modules:
    python dlna_config.py
    python dlna_discovery.py [http://probe-url]
    python dlna_content.py <control-url>
    python dlna_library.py
    python dlna_player.py [http://test-url]
    python dlna_server.py
"""
import argparse
import logging
import os
import socket
import ssl
import subprocess
import threading
import time

import dlna_discovery as _disc
from dlna_config import load_config, save_config, setup_logging
from dlna_library import DB, INDEXER, DEVICE_ROLES, ART_FETCHER, LOUDNESS_SCANNER
from dlna_server import (GW_UDN, ThreadedHTTPServer, TLSThreadedHTTPServer,
                         GatewayHandler, gw_ssdp_announcer, gw_ssdp_byebye)

log = logging.getLogger("dlna.gateway")


# ── Web UI ────────────────────────────────────────────────────────
# Imported by dlna_server.GatewayHandler when serving GET /

# ── Web UI is now served from static/ directory ──────────────────
# See static/index.html, static/app.css, static/app.js
# The server routes GET / to static/index.html




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


def open_browser(port: int):
    url = f"http://localhost:{port}/"
    for cmd in (["open", url], ["xdg-open", url]):
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return
        except FileNotFoundError:
            continue


def _on_server_found(server):
    """Hook called by discovery when a new MediaServer is registered.
    Skip indexing for combined devices (e.g. Naim Uniti) that appear as
    both a MediaServer and a MediaRenderer — they have no music library."""
    from dlna_discovery import RENDERERS
    if RENDERERS.get(server.udn):
        log.info(f"Skipping indexer for {server.name!r} "
                 f"— registered as renderer (combined device)")
        return
    INDEXER.start(server, force=False)


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
    parser.add_argument("--host",          default="0.0.0.0")
    parser.add_argument("--port",          type=int, default=8765)
    parser.add_argument("--tls-port",      type=int, default=8443,
                        help="HTTPS port (only used when --tls-cert is set)")
    parser.add_argument("--tls-cert",      default="",
                        help="Path to TLS certificate (.crt) for HTTPS")
    parser.add_argument("--tls-key",       default="",
                        help="Path to TLS private key (.key) for HTTPS")
    parser.add_argument("--probe",         default="",
                        help="Direct device URL — bypasses SSDP, adds permanently to DB")
    parser.add_argument("--no-browser",    action="store_true")
    parser.add_argument("--debug",         action="store_true")
    parser.add_argument("--reset-devices", action="store_true",
                        help="Clear the device_roles table and rediscover from scratch. "
                             "Use when a device was mis-classified or you want to "
                             "remove a device that no longer exists.")
    parser.add_argument("--list-devices",  action="store_true",
                        help="Print the known devices table and exit.")
    args = parser.parse_args()

    setup_logging(debug=args.debug)

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

    setup_logging(debug=args.debug)

    lan_ip = get_lan_ip()
    url    = f"http://localhost:{args.port}/ "

    print()
    print("  ┌──────────────────────────────────────────────┐")
    print("  │         DLNA / UPnP  Music  Gateway  v2      │")
    print("  ├──────────────────────────────────────────────┤")
    print(f" │  Web UI   :  {url:<33}                       │")
    print(f" │  LAN IP   :  {lan_ip:<33}                    │")
    print("  ├──────────────────────────────────────────────┤")
    print("  │  Remote   :  http://<tailscale-ip>:8765/     │")
    print("  │  HTTPS    :  https://<tailscale-ip>:8443/    │")
    print("  │  Module tests:  python dlna_config.py        │")
    print("  │                 python dlna_discovery.py     │")
    print("  │                 python dlna_library.py       │")
    print("  └──────────────────────────────────────────────┘")
    print()

    # Wire the indexer callback into discovery
    _disc._on_server_found = _on_server_found

    # Load persistent device role cache BEFORE any discovery thread starts.
    # This means the Uniti (or any other combined device seen before) is
    # classified as renderer-only instantly, with no race condition.
    DEVICE_ROLES.load()

    # Immediately probe all previously-known servers from the DB cache.
    # This gets AssetUPnP (and any other servers) online in < 1 second,
    # before SSDP or subnet scan have a chance to run.
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
        args=(lan_ip, args.port),
        daemon=True, name="gw-ssdp").start()

    # Subnet scanner fallback — only fires if nothing was found via
    # pre-probe or SSDP (i.e. a genuinely fresh install with no DB cache).
    threading.Thread(
        target=_disc.subnet_scan_if_empty,
        args=(lan_ip, GW_UDN),
        daemon=True, name="subnet-scan").start()

    # Server heartbeat — keeps last_seen fresh, eliminates offline flicker
    threading.Thread(
        target=_disc.heartbeat_thread,
        args=(GW_UDN,),
        daemon=True, name="heartbeat").start()

    # Album-art fetcher — one-shot startup scan 120s after boot to mop
    # up anything left bare by a previous interrupted run. Steady-state
    # refills come from Indexer._run() triggering on each successful
    # crawl; there is no periodic poll. Rate-limited to ≤1 MB req/sec.
    ART_FETCHER.start_initial_scan()
    # Loudness analysis — one-shot startup mop-up too. Heavier per item
    # (~1 sec ffmpeg call) but CPU-bound and niced 10; happy to share.
    LOUDNESS_SCANNER.start_initial_scan()

    # CLI --probe or config.json probe (fresh install / manual override).
    # On subsequent runs the DB cache handles this — but honour explicit CLI.
    probe_url = args.probe
    if not probe_url and not known_servers:
        # Only fall back to config.json probe if DB has no known servers
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

    # HTTP server
    server = ThreadedHTTPServer((args.host, args.port), GatewayHandler)
    server.tls_port = None   # filled in below if HTTPS starts successfully

    # ── HTTPS server (Tailscale certs) ───────────────────────────
    # Auto-detect cert files in working directory if not specified
    tls_cert = args.tls_cert
    tls_key  = args.tls_key
    if not tls_cert:
        import glob
        certs = glob.glob(os.path.join(os.getcwd(), "*.crt"))
        keys  = glob.glob(os.path.join(os.getcwd(), "*.key"))
        if len(certs) == 1 and len(keys) == 1:
            tls_cert = certs[0]
            tls_key  = keys[0]
            log.info(f"Auto-detected TLS cert: {os.path.basename(tls_cert)}")

    tls_server = None
    if tls_cert and tls_key:
        if os.path.isfile(tls_cert) and os.path.isfile(tls_key):
            try:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(tls_cert, tls_key)
                tls_server = TLSThreadedHTTPServer(
                    (args.host, args.tls_port), GatewayHandler)
                # do_handshake_on_connect=False is critical: it makes
                # SSLSocket.accept() return immediately with an unhandshaked
                # socket so a stalled client cannot block the accept loop.
                # The handshake happens lazily on first read in the worker
                # thread, bounded by TLSThreadedHTTPServer.REQUEST_TIMEOUT.
                tls_server.socket = ctx.wrap_socket(
                    tls_server.socket,
                    server_side=True,
                    do_handshake_on_connect=False,
                    suppress_ragged_eofs=True,
                )
                threading.Thread(
                    target=tls_server.serve_forever,
                    daemon=True, name="https").start()
                server.tls_port = args.tls_port   # tells HTTP handler where to redirect
                log.info(f"HTTPS ready → https://localhost:{args.tls_port}/")
            except Exception as e:
                log.warning(f"HTTPS failed to start: {e}")
                tls_server = None
        else:
            log.warning(f"TLS cert/key not found: {tls_cert} / {tls_key}")

    if not args.no_browser:
        threading.Timer(1.0, open_browser, args=(args.port,)).start()

    tls_note = f"  HTTPS  → https://localhost:{args.tls_port}/" if tls_server else ""
    log.info(f"Gateway ready → {url}{('  |  ' + tls_note) if tls_note else ''}   (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        log.info("Shutting down…")
        if tls_server:
            tls_server.shutdown()
        gw_ssdp_byebye(lan_ip, args.port)
        server.shutdown()


if __name__ == "__main__":
    main()