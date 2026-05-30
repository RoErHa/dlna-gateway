"""
dlna_localfs_wiring.py — boot-time wiring of the LocalFs provider.

Phase 4 of the AssetUPnP migration. Kept in its own module to keep
`dlna_gateway.py` slim (the run_all.py lint enforces < 350 lines).

`maybe_start_localfs()` is gated on the `LOCALFS_MUSIC_ROOT` env var
(or `localfs.root` in config.json). When that's set, the function:

  1. Starts the LocalFs HTTP file server on its own port (default
     8200 / honors `$LOCALFS_PORT`). Binds `0.0.0.0` so the Naim
     can reach it on the LAN.
  2. Computes the file server's base_url (`$LOCALFS_BASE_URL`
     overrides; otherwise auto-detects the LAN IP via the same
     `get_lan_ip` helper the gateway uses for SSDP).
  3. Constructs a `LocalFsProvider` against `library.db` with that
     base_url. Track URLs written into `tracks.url` at scan time are
     then Naim-fetchable directly — no translation layer needed
     downstream.
  4. Binds the provider to its synthesised UDN via
     `dlna_providers.bind_provider`, and inserts a matching
     `MediaServer` record into `SERVERS` so api_browse exposes it
     alongside any AssetUPnP / MinimServer entries.
  5. Kicks off a background initial scan. Subsequent runs are
     incremental thanks to the `localfs_files` (mtime, size) cache
     introduced in P2.

The fallback when the env var is unset is the existing UPnP-only
behaviour — no functional change for users who haven't opted in.
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("dlna.localfs.wiring")


def maybe_start_localfs(get_lan_ip):
    """Caller passes the gateway's own `get_lan_ip` function so this
    module doesn't need to re-implement LAN-IP detection."""
    # Imports kept INSIDE the function so the gateway core doesn't
    # pay the cost (or the mutagen/watchdog requirement) when LocalFs
    # isn't enabled.
    from dlna_config import load_config

    root_env = os.environ.get("LOCALFS_MUSIC_ROOT", "").strip()
    if not root_env:
        cfg = load_config()
        root_env = (cfg.get("localfs") or {}).get("root", "").strip()
    if not root_env:
        log.debug("LocalFs disabled (LOCALFS_MUSIC_ROOT not set, "
                  "no localfs.root in config.json)")
        return

    root_path = Path(root_env).expanduser()
    if not root_path.exists():
        log.warning(f"LocalFs root not found: {root_env} — skipping "
                    "(is the volume mounted / unlocked?)")
        return

    port = int(os.environ.get("LOCALFS_PORT", "8200"))
    lan_ip = get_lan_ip()
    base_url = os.environ.get(
        "LOCALFS_BASE_URL",
        f"http://{lan_ip}:{port}").rstrip("/")

    try:
        from dlna_config import DB_FILE
        from dlna_library import DB
        from dlna_localfs_server import start_server
        from dlna_providers import bind_provider
        from dlna_providers.localfs import LocalFsProvider
        from dlna_registry import MediaServer
        import dlna_discovery as _disc
    except ImportError as e:
        log.warning(f"LocalFs imports failed: {e} — skipping")
        return

    log.info(f"LocalFs enabled: root={root_env} port={port} "
             f"base_url={base_url}")

    try:
        start_server(DB_FILE, port=port, host="0.0.0.0",
                     allowed_roots=(str(root_path.resolve()),))
    except OSError as e:
        log.error(f"LocalFs file server failed to bind :{port}: {e} — "
                  "is the port in use? Set $LOCALFS_PORT to override.")
        return

    provider = LocalFsProvider(DB, root_path, base_url=base_url)
    bind_provider(provider.udn, provider)

    # Synthetic MediaServer entry so SERVERS.all() lists the LocalFs
    # library next to any AssetUPnP / MinimServer entries. The PWA's
    # server picker reads from here.
    _disc.SERVERS.add(MediaServer(
        udn=provider.udn,
        name="RoHaLocalFS",
        location=base_url,
        control_url=base_url,
        base_url=base_url))

    # Initial scan in the background — same lazy posture as the
    # existing AcoustID / Loudness mop-ups so the gateway doesn't
    # block on a big tree at boot.
    def _initial_scan():
        try:
            stats = provider.rescan()
            log.info(f"LocalFs initial scan complete: {stats}")
        except Exception as e:                                # noqa: BLE001
            log.exception(f"LocalFs initial scan failed: {e}")
    threading.Thread(target=_initial_scan, daemon=True,
                     name="localfs-initial-scan").start()


__all__ = ["maybe_start_localfs"]
