#!/usr/bin/env python3
"""
dlna_registry.py — data models + thread-safe device registries.

Separated from dlna_discovery so the "what's a device and who's online"
data layer is independent of the SSDP/probe/heartbeat behaviour. Other
modules import `SERVERS` and `RENDERERS` directly; discovery writes to
them, handlers read from them.
"""
import logging
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("dlna.discovery")

# A device is considered offline if we haven't heard from it in 5 min.
# Imported by heartbeat + handlers to decide "is this UDN reachable".
_STALE_SEC = 300


# ── Data models ───────────────────────────────────────────────────

@dataclass
class MediaServer:
    """A UPnP ContentDirectory server (AssetUPnP, MinimServer, …)."""
    udn:         str
    name:        str
    location:    str
    control_url: str   # ContentDirectory control URL
    base_url:    str
    last_seen:   float = field(default_factory=time.time)
    meta_containers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"udn": self.udn, "name": self.name, "location": self.location}


@dataclass
class MediaRenderer:
    """A UPnP AVTransport renderer (Naim Uniti, BubbleUPnP, …)."""
    udn:       str
    name:      str
    location:  str
    av_url:    str   # AVTransport control URL
    base_url:  str
    rc_url:    str = ""   # RenderingControl control URL (volume); may be empty
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"udn": self.udn, "name": self.name, "location": self.location,
                "av_url": self.av_url, "rc_url": self.rc_url}


# ── Thread-safe registries ────────────────────────────────────────

class ServerRegistry:
    def __init__(self):
        self._d: dict[str, MediaServer] = {}
        self._lock = threading.Lock()

    def add(self, srv: MediaServer):
        with self._lock:
            if srv.udn not in self._d:
                log.info(f"[SERVER +] {srv.name!r}  @ {srv.location}")
            srv.last_seen = time.time()
            self._d[srv.udn] = srv

    def touch(self, udn: str):
        """Refresh last_seen for a known server (e.g. after a successful SOAP call)."""
        with self._lock:
            if udn in self._d:
                self._d[udn].last_seen = time.time()

    def get(self, udn: str) -> MediaServer | None:
        """Always returns the server if ever discovered — regardless of staleness."""
        with self._lock:
            return self._d.get(udn)

    def all(self) -> list[MediaServer]:
        """Return all known servers; online ones first."""
        now = time.time()
        with self._lock:
            return sorted(self._d.values(),
                          key=lambda s: now - s.last_seen)

    def online(self) -> list[MediaServer]:
        """Return only recently-seen servers."""
        now = time.time()
        with self._lock:
            return [s for s in self._d.values()
                    if now - s.last_seen < _STALE_SEC]

    def is_online(self, udn: str) -> bool:
        now = time.time()
        with self._lock:
            s = self._d.get(udn)
            return s is not None and now - s.last_seen < _STALE_SEC

    def empty(self) -> bool:
        """True only if no server has ever been discovered (registry is bare)."""
        with self._lock:
            return len(self._d) == 0


class RendererRegistry:
    def __init__(self):
        self._d: dict[str, MediaRenderer] = {}
        self._lock = threading.Lock()

    def add(self, rnd: MediaRenderer):
        with self._lock:
            if rnd.udn not in self._d:
                log.info(f"[RENDERER+] {rnd.name!r}  @ {rnd.location}")
            rnd.last_seen = time.time()
            self._d[rnd.udn] = rnd

    def get(self, udn: str) -> MediaRenderer | None:
        """Always returns if ever discovered."""
        with self._lock:
            return self._d.get(udn)

    def all(self) -> list[MediaRenderer]:
        now = time.time()
        with self._lock:
            return sorted(self._d.values(),
                          key=lambda r: now - r.last_seen)


# Singletons — imported by all other modules
SERVERS   = ServerRegistry()
RENDERERS = RendererRegistry()
