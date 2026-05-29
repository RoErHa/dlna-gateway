"""
dlna_providers.upnp — `UpnpProvider` for AssetUPnP, MinimServer, or any
generic UPnP ContentDirectory MediaServer.

Phase 1 of the AssetUPnP migration. This is a thin wrapper: every
ContentDirectory call delegates to `dlna_content.cd_browse / cd_search
/ browse_all` (the existing SOAP client). Pure refactor — no
behavioural change. The Indexer and api_browse stop importing
`dlna_content` directly; they fetch a provider via `get_provider(udn)`
and call its wire-level helpers.

What this file changes:

- The gateway core no longer imports `dlna_content`. The single
  import lives in this file.
- Provider construction happens at discovery time
  (`dlna_discovery.probe_url`), so by the time anything tries to
  browse a server, there's a bound `UpnpProvider`.
- AVTransport callers (`dlna_player`, `api_playback`) still import
  AVTransport functions via the existing `dlna_content` re-export
  shim. That's a separate concern (the renderer-control side) and
  is intentionally NOT part of the LibraryProvider seam.

What this file deliberately does NOT change (yet):

- The Indexer keeps its BFS-walk algorithm — it calls
  `provider.cd_browse / provider.browse_all`, which still walks the
  UPnP container tree exactly as before. Replacing the BFS with the
  high-level `list_artists / list_albums / list_tracks` seam methods
  is P2+ work.
- The high-level Protocol methods on `UpnpProvider` are stubbed with
  `NotImplementedError` for now. They're declared so the class
  matches the Protocol structurally; they get real bodies once a
  caller actually needs them.

See `CLAUDE.md` → "Library backend migration (in flight)" for the
phase plan.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Iterator, Optional

# The ONE allowed import of dlna_content's ContentDirectory surface.
# Any code outside dlna_providers/upnp.py that needs to browse a UPnP
# server must do it via `get_provider(udn)`.
import dlna_content as _wire

from . import (
    Album,
    Artist,
    LibraryProvider,
    Track,
    register_provider,
)

log = logging.getLogger("dlna.providers.upnp")


@register_provider("upnp")
class UpnpProvider:
    """LibraryProvider backed by a UPnP MediaServer (AssetUPnP,
    MinimServer, Plex's DLNA face, Jellyfin's DLNA face).

    `server` accepts either a `dlna_registry.MediaServer` dataclass or
    any object exposing `.udn`, `.name`, `.control_url`, `.base_url`.
    The constructor only stores references, so a discovery process
    that updates the server's `last_seen` continues to work.
    """

    name = "upnp"

    def __init__(self, server: Any):
        self._server = server
        # Snapshot the attributes that the wire layer needs. The
        # ServerRegistry mutates `server.last_seen` in place but those
        # fields below are stable for a given UDN — re-discovering a
        # server with a different control_url would re-add it as a
        # different record, so caching is safe.
        self.udn: str = server.udn
        self._control_url: str = server.control_url
        self._base_url: str = getattr(server, "base_url", "")
        self._name: str = getattr(server, "name", "")

    # ── Wire-level helpers (transitional) ────────────────────────
    # P1's Indexer + api_browse still need the BFS / object-id browse
    # primitives — they go through these methods so the gateway core
    # doesn't import dlna_content directly. P2+ may eventually drop
    # these in favour of the high-level Protocol methods.

    def cd_browse(self, object_id: str = "0", start: int = 0,
                  count: int = 50) -> dict:
        """Wire-level ContentDirectory Browse. Returns the same dict
        shape as `dlna_content.cd_browse`."""
        return _wire.cd_browse(self._control_url, object_id,
                               start=start, count=count)

    def cd_search(self, query: str, count: int = 200) -> dict:
        """Wire-level ContentDirectory Search."""
        return _wire.cd_search(self._control_url, query, count=count)

    def browse_all(self, container_id: str,
                   max_items: int = 5000) -> tuple[list, list]:
        """Wire-level paginated browse: returns
        `(sub_containers, items)` for the given container."""
        return _wire.browse_all(self._control_url, container_id,
                                max_items=max_items)

    # ── LibraryProvider Protocol — high-level surface ────────────
    # These are declared so the class satisfies the Protocol
    # structurally (the @runtime_checkable isinstance() check
    # passes). Real bodies arrive when a caller needs them — P2+ when
    # we generalise the Indexer / browse paths off the UPnP BFS.

    def list_artists(self) -> Iterator[Artist]:
        raise NotImplementedError(
            "UpnpProvider.list_artists not implemented yet — the "
            "Indexer still walks the UPnP container tree directly via "
            "browse_all. P2+ generalises this off UPnP-specific "
            "internals.")

    def list_albums(self, artist_id: str) -> Iterator[Album]:
        raise NotImplementedError(
            "UpnpProvider.list_albums not implemented yet (see "
            "list_artists docstring).")

    def list_tracks(self, album_id: str) -> Iterator[Track]:
        raise NotImplementedError(
            "UpnpProvider.list_tracks not implemented yet (see "
            "list_artists docstring).")

    def get_track(self, track_id: str) -> Optional[Track]:
        raise NotImplementedError(
            "UpnpProvider.get_track not implemented yet — current "
            "callers fetch track rows from LibraryDB by URL/obj_id.")

    def stream_url(self, track_id: str) -> str:
        """For UPnP, `track_id` IS the renderer-fetchable URL. The
        Indexer captures it on `tracks.url` at index time; the
        gateway's player passes it straight to AVTransport
        `SetURI`. So this method is a near-identity — present for
        Protocol completeness and for backends where the id and the
        URL diverge."""
        return track_id

    def search(self, q: str, limit: int = 50) -> Iterator[Track]:
        raise NotImplementedError(
            "UpnpProvider.search not implemented yet — the gateway "
            "currently searches via LibraryDB FTS5 on its local "
            "mirror, which is faster than re-issuing UPnP Search SOAP.")

    def probe(self) -> bool:
        """True if a trivial Browse(root) succeeds."""
        try:
            result = self.cd_browse("0", count=1)
        except Exception as e:                                # noqa: BLE001
            log.debug(f"probe failed for {self._name!r}: {e}")
            return False
        return "error" not in result

    def watch_changes(self, on_change: Callable[[], None]) -> None:
        raise NotImplementedError(
            "UPnP has no native change feed for the gateway's use. "
            "Catalogue refresh happens via periodic /api/index/rebuild "
            "or the indexer-tail trigger after a successful crawl.")

    # ── Bookkeeping ──────────────────────────────────────────────

    def __repr__(self) -> str:
        return (f"UpnpProvider(udn={self.udn!r}, name={self._name!r}, "
                f"control_url={self._control_url!r})")


# Re-export the DIDL-Lite parser so tests don't need to import
# `dlna_content` directly. The parser is UPnP-specific (and only used
# inside `cd_browse`); making it reachable here keeps the P1 invariant
# "only dlna_providers.upnp imports dlna_content" true for tests too.
from dlna_content import _parse_didl  # noqa: E402,F401

__all__ = ["UpnpProvider", "_parse_didl"]
