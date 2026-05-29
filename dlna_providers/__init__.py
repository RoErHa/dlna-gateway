"""
dlna_providers — the LibraryProvider seam.

The gateway speaks to library backends (AssetUPnP, MinimServer, Plex,
Jellyfin, our own in-process LocalFs) through this one Protocol.
Adding a new backend means creating one file under this package that
implements the Protocol and registers itself via @register_provider.
The gateway core never imports a backend directly.

See `CLAUDE.md` → "Library backend migration (in flight)" for the
full design, the non-negotiable rules, and the phase plan.

Phase 0 — this file (the seam) + dlna_providers/mock.py (test scaffold).
No real backends yet. The existing UPnP code path will be wrapped in
dlna_providers/upnp.py during Phase 1 with no functional change.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Callable, Iterator, Optional, Protocol, runtime_checkable


# ── Dataclasses ───────────────────────────────────────────────────
# The seam's value vocabulary. Deliberately minimal at Phase 0 —
# fields can be widened later without breaking implementers because
# they all have defaults.

@dataclass(frozen=True)
class Artist:
    id: str
    name: str
    album_count: int = 0


@dataclass(frozen=True)
class Album:
    id: str
    name: str
    artist_id: str = ""
    artist_name: str = ""
    year: Optional[int] = None
    art_url: str = ""
    track_count: int = 0


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist_id: str = ""
    artist_name: str = ""
    album_id: str = ""
    album_name: str = ""
    track_number: int = 0
    duration_sec: float = 0.0
    year: Optional[int] = None
    art_url: str = ""
    mime: str = ""
    file_path: str = ""              # LocalFs only; "" for UPnP/Plex/Jellyfin
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    genre: str = ""


# ── Protocol ──────────────────────────────────────────────────────

@runtime_checkable
class LibraryProvider(Protocol):
    """One library source. Instances are bound 1:1 to a UDN.

    Iterator-returning methods MAY be lazy; callers materialise with
    list() when they need a concrete sequence. The gateway core relies
    only on these methods — never on a specific implementation's
    internals.

    Optional methods (search, watch_changes) can raise
    NotImplementedError if the backend doesn't support them; the
    gateway falls back to LibraryDB FTS5 / periodic rescans
    respectively."""

    name: str    # provider class name: 'upnp' | 'plex' | 'jellyfin' | 'localfs' | 'mock'
    udn: str     # stable unique id for this provider instance

    def list_artists(self) -> Iterator[Artist]:
        """All artists in the library."""
        ...

    def list_albums(self, artist_id: str) -> Iterator[Album]:
        """All albums by a given artist."""
        ...

    def list_tracks(self, album_id: str) -> Iterator[Track]:
        """All tracks on a given album, in track-number order if known."""
        ...

    def get_track(self, track_id: str) -> Optional[Track]:
        """Single track by id, or None if not found."""
        ...

    def stream_url(self, track_id: str) -> str:
        """URL the *renderer* will fetch bytes from.

        For UPnP / Plex / Jellyfin this is the source server's URL.
        For LocalFs it's our own HTTP file server. NEVER a gateway
        /api proxy — the Naim must reach the bytes directly. Empty
        string if the track isn't streamable."""
        ...

    def search(self, q: str, limit: int = 50) -> Iterator[Track]:
        """Tracks matching a free-text query. Providers without
        native search may raise NotImplementedError."""
        ...

    def probe(self) -> bool:
        """True if the backend is reachable right now."""
        ...

    def watch_changes(self, on_change: Callable[[], None]) -> None:
        """Subscribe to incremental change notifications. The provider
        invokes on_change() whenever something is added / changed /
        removed. Optional — providers without a change feed may raise
        NotImplementedError."""
        ...


# ── Registry ──────────────────────────────────────────────────────
# Two layers:
#   * Provider CLASSES — registered by name at import time
#     via @register_provider. One class per backend implementation.
#   * Provider INSTANCES — bound to UDNs at runtime via
#     bind_provider. Many instances of the same class are fine
#     (multiple UPnP servers, etc.) — each gets its own UDN binding.

_PROVIDER_CLASSES:    dict[str, type] = {}
_PROVIDER_INSTANCES:  dict[str, LibraryProvider] = {}
_LOCK = RLock()


def register_provider(name: str) -> Callable[[type], type]:
    """Class decorator. Registers a LibraryProvider implementation by name.

    The name is what callers pass to construct an instance:
        cls = get_provider_class('upnp')
        provider = cls(server_info)
        bind_provider(udn, provider)

    Re-registering the same name replaces the previous class —
    intentional for test re-imports."""
    def wrap(cls: type) -> type:
        with _LOCK:
            _PROVIDER_CLASSES[name] = cls
        return cls
    return wrap


def get_provider_class(name: str) -> Optional[type]:
    """Class registered under NAME, or None."""
    with _LOCK:
        return _PROVIDER_CLASSES.get(name)


def list_provider_names() -> list[str]:
    """Names of all registered provider classes, alphabetical."""
    with _LOCK:
        return sorted(_PROVIDER_CLASSES.keys())


def bind_provider(udn: str, provider: LibraryProvider) -> None:
    """Bind a provider instance to a UDN. Replaces any prior binding."""
    if not udn:
        raise ValueError("udn must be a non-empty string")
    with _LOCK:
        _PROVIDER_INSTANCES[udn] = provider


def get_provider(udn: str) -> Optional[LibraryProvider]:
    """Provider bound to UDN, or None if no binding exists."""
    with _LOCK:
        return _PROVIDER_INSTANCES.get(udn)


def unbind_provider(udn: str) -> Optional[LibraryProvider]:
    """Drop the binding for UDN. Returns the previous provider, or None."""
    with _LOCK:
        return _PROVIDER_INSTANCES.pop(udn, None)


def list_bound_udns() -> list[str]:
    """UDNs that currently have a provider bound, alphabetical."""
    with _LOCK:
        return sorted(_PROVIDER_INSTANCES.keys())


# ── Test-only utilities ───────────────────────────────────────────
# Production code should not call these. Tests use them in
# setUp/tearDown to keep registry state hermetic.

def clear_bindings() -> None:
    """Drop every UDN → instance binding. Class registrations are kept."""
    with _LOCK:
        _PROVIDER_INSTANCES.clear()


def clear_provider_classes() -> None:
    """Drop every name → class registration AND every binding.

    Almost never the right thing in production — re-importing a
    provider module is the supported way to re-register. Provided
    for tests that need a fully blank registry."""
    with _LOCK:
        _PROVIDER_INSTANCES.clear()
        _PROVIDER_CLASSES.clear()


__all__ = [
    # Dataclasses
    "Artist", "Album", "Track",
    # Protocol
    "LibraryProvider",
    # Class registry
    "register_provider", "get_provider_class", "list_provider_names",
    # Instance registry
    "bind_provider", "get_provider", "unbind_provider", "list_bound_udns",
    # Test utilities
    "clear_bindings", "clear_provider_classes",
]
