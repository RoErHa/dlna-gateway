#!/usr/bin/env python3
"""
dlna_devices.py — DeviceRoleCache (in-memory mirror of device_roles).

The cache is populated once at startup by LibraryDB.roles_load() so SSDP
can classify a previously-seen device on its first packet with zero
latency. Write-through: every update goes to both the cache and the DB.

Separated from dlna_library so the SQL-layer class and the thread-safe
in-memory classifier live in their own modules. The singleton
`DEVICE_ROLES` is created in dlna_library (composition root) and
re-exported from there for backward compat.
"""
import logging
import threading
from typing import Optional

log = logging.getLogger("dlna.library")


class DeviceRoleCache:
    """In-memory mirror of the device_roles SQLite table.

    Loaded once on startup — so the very first SSDP packet for a
    previously-seen device is classified correctly with zero latency,
    no races, no 12-second wait.

    Write-through: every update goes to both the cache and the DB.
    """

    def __init__(self, db):
        self._db    = db
        self._lock  = threading.Lock()
        self._cache: dict = {}   # udn → {name, location, host, is_server, is_renderer}
        self._host_index: dict = {}  # host → set of roles

    def load(self):
        """Call once at startup to populate from DB."""
        rows = self._db.roles_load()
        with self._lock:
            self._cache = rows
            self._host_index = self._build_host_index(rows)
        log.info(f"DeviceRoleCache: loaded {len(rows)} known devices from DB")
        for udn, r in rows.items():
            roles = []
            if r["is_server"]:   roles.append("server")
            if r["is_renderer"]: roles.append("renderer")
            log.debug(f"  {r['name']!r} ({udn[:16]}…) host={r['host']} "
                      f"→ {', '.join(roles) or 'unknown'}")

    def _build_host_index(self, cache: dict) -> dict:
        """host → set of roles {"server","renderer"} across all UDNs from that host."""
        idx: dict = {}
        for info in cache.values():
            h = info.get("host", "")
            if not h:
                continue
            if h not in idx:
                idx[h] = set()
            if info["is_server"]:   idx[h].add("server")
            if info["is_renderer"]: idx[h].add("renderer")
        return idx

    def mark(self, udn: str, name: str, location: str = "", host: str = "",
             is_server: bool = False, is_renderer: bool = False):
        """Record or update a device's roles (write-through to DB)."""
        with self._lock:
            existing = self._cache.get(udn, {})
            new_server   = existing.get("is_server",   False) or is_server
            new_renderer = existing.get("is_renderer", False) or is_renderer
            new_location = location or existing.get("location", "")
            new_host     = host or existing.get("host", "")
            self._cache[udn] = {
                "name":        name,
                "location":    new_location,
                "host":        new_host,
                "is_server":   new_server,
                "is_renderer": new_renderer,
            }
            # Rebuild host index entry
            if new_host:
                if new_host not in self._host_index:
                    self._host_index[new_host] = set()
                if new_server:   self._host_index[new_host].add("server")
                if new_renderer: self._host_index[new_host].add("renderer")
        self._db.role_set(udn, name, location=new_location, host=new_host,
                          is_server=new_server, is_renderer=new_renderer)

    def is_renderer(self, udn: str) -> bool:
        """True if this exact UDN is a known renderer."""
        with self._lock:
            return self._cache.get(udn, {}).get("is_renderer", False)

    def is_server(self, udn: str) -> bool:
        with self._lock:
            return self._cache.get(udn, {}).get("is_server", False)

    def get(self, udn: str) -> Optional[dict]:
        with self._lock:
            return self._cache.get(udn)

    def known_servers(self) -> list:
        """Return cached entries known to be pure servers (not renderers),
        with location URLs."""
        with self._lock:
            return [
                {"udn": udn, **info}
                for udn, info in self._cache.items()
                if info.get("is_server") and not info.get("is_renderer")
                and info.get("location")
                # Also exclude if the host is known as a renderer
                and not self._host_index.get(info.get("host",""), set()).issuperset({"renderer"})
            ]

    def all(self) -> list:
        return self._db.roles_all()
