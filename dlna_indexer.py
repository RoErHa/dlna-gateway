#!/usr/bin/env python3
"""
dlna_indexer.py — Background crawler that populates LibraryDB.

  IndexState — thread-safe status dict exposed to /api/index/status
  Indexer    — launches a daemon thread that walks the Album Artist/Album
               container tree of a UPnP server and upserts every track

On successful completion, triggers AlbumArtFetcher (lazy-imported from
dlna_library to avoid the import cycle — by call time, dlna_library has
finished loading and ART_FETCHER is bound).

The `INDEXER` singleton is created in dlna_library (composition root)
and re-exported from there for backward compat.
"""
import logging
import threading
from typing import Optional

from dlna_events import EVENTS

log = logging.getLogger("dlna.library")


class IndexState:
    def __init__(self):
        self._lock = threading.Lock()
        self._d = {
            "status": "idle",   # idle | running | done | error
            "progress": 0,
            "total": 0,
            "tracks": 0,
            "server": "",
            "error": "",
        }

    def update(self, **kwargs):
        with self._lock:
            prev = self._d["status"]
            self._d.update(kwargs)
            changed = self._d["status"] != prev
            snap = dict(self._d) if changed else None
        # SSE (R2): push only on a status TRANSITION (idle→running→done/error)
        # — never on per-album progress, so subscribers aren't flooded. The
        # PWA reacts by re-fetching /api/index/status. No-op until the ASGI
        # event loop is bound (e.g. under the stdlib server / tests).
        if snap is not None:
            EVENTS.publish({"type": "index", "status": snap["status"],
                            "server": snap.get("server", ""),
                            "progress": snap.get("progress", 0),
                            "total": snap.get("total", 0)})

    def get(self) -> dict:
        with self._lock:
            return dict(self._d)

    @property
    def status(self) -> str:
        with self._lock:
            return self._d["status"]


class Indexer:
    """Background crawler that walks the Album Artist/Album tree and
    populates LibraryDB with all tracks."""

    def __init__(self, library):
        self.library   = library
        self.state     = IndexState()
        self._thread: Optional[threading.Thread] = None
        self._cancelled_udns: set = set()
        self._lock = threading.Lock()

    def cancel_udn(self, udn: str):
        """Prevent or abort indexing for this UDN (e.g. device reclassified as renderer)."""
        with self._lock:
            self._cancelled_udns.add(udn)
        log.info(f"Indexer: cancelled for UDN {udn[:16]}…")

    def _is_cancelled(self, udn: str) -> bool:
        with self._lock:
            return udn in self._cancelled_udns

    def start(self, server, force: bool = False):
        """Launch background indexer thread. Returns immediately."""
        if self._is_cancelled(server.udn):
            log.info(f"Indexer: skipping {server.name!r} — cancelled (renderer device)")
            return
        if (self._thread and self._thread.is_alive()
                and self.state.status == "running"):
            log.info("Indexer already running — skipping duplicate start")
            return
        self._thread = threading.Thread(
            target=self._run, args=(server, force),
            daemon=True, name=f"indexer-{server.udn[:8]}")
        self._thread.start()

    def _run_with_fts_heal(self, body_fn, *args, **kwargs):
        """Run ``body_fn(*args, **kwargs)``; if it raises a
        ``sqlite3.DatabaseError`` whose message contains "malformed"
        (the FTS5 shadow-table corruption symptom — see
        ``LibraryDB.repair_fts`` for the details), drop+recreate+rebuild
        ``tracks_fts`` and retry the body once.

        Any other exception, or a malformed error on the retry, is
        re-raised — we never loop indefinitely. The recurring failure
        in the wild (Apr 2026 ×2, May 2026) was a single FTS-corruption
        event that one repair always cleared.
        """
        # Single heal implementation lives in LibraryDB (2026-07-03) —
        # shared with clear()/upsert_tracks() and the LocalFs rescan.
        return self.library.run_with_fts_heal(body_fn, *args, **kwargs)

    def _run(self, server, force: bool):
        udn = server.udn

        if self._is_cancelled(udn):
            log.info(f"Indexer: {server.name!r} cancelled before start")
            self.state.update(status="idle")
            return

        existing = self.library.track_count(udn)

        if not force and existing > 0:
            self.state.update(status="done", tracks=existing, server=server.name)
            log.info(f"Index OK: {existing} tracks already in DB for {server.name}")
            return

        self.state.update(status="running", progress=0, total=0,
                          tracks=0, server=server.name, error="")
        log.info(f"Indexer started for {server.name}")

        try:
            self._run_with_fts_heal(self._index_body, server, force)
        except Exception as e:
            log.exception(f"Indexer error: {e}")
            self.state.update(status="error", error=str(e))

    def _index_body(self, server, force: bool):
        """The actual crawl — separated from ``_run`` so that
        ``_run_with_fts_heal`` can retry the full body once after a
        ``repair_fts()``."""
        from dlna_providers import bind_provider, get_provider
        from dlna_providers.upnp import UpnpProvider
        udn = server.udn

        # Resolve the provider for this UDN. Discovery should have
        # bound a UpnpProvider on add; defensive fallback for tests
        # or callers that constructed SERVERS by hand.
        provider = get_provider(udn)
        if provider is None:
            provider = UpnpProvider(server)
            bind_provider(udn, provider)

        try:
            # Find Album Artist/Album container.
            # Servers like AssetUPnP/MinimServer/Jellyfin expose it at root.
            # Plex nests it one level under a "Music" container.
            ALBUM_TITLES = ("Album Artist / Album", "Album Artist",
                            "Albums", "By Album")
            MUSIC_TITLES = ("Music", "Audio", "Music Library")

            def _find_album_cid(parent_cid: str):
                result = provider.cd_browse(parent_cid, count=50)
                for c in result.get("containers", []):
                    if c["title"].strip() in ALBUM_TITLES:
                        return c["id"], c["title"]
                return None, None

            album_cid, album_title = _find_album_cid("0")

            if not album_cid:
                # Plex-style: descend into a "Music" container first.
                root_result = provider.cd_browse("0", count=50)
                for c in root_result.get("containers", []):
                    if c["title"].strip() in MUSIC_TITLES:
                        log.info(f"Descending into {c['title']!r} id={c['id']} "
                                 f"to locate album container")
                        album_cid, album_title = _find_album_cid(c["id"])
                        if album_cid:
                            break

            if not album_cid:
                self.state.update(status="error",
                                  error="No Album Artist/Album container found")
                log.error(f"Indexer: cannot find album container for {server.name}")
                return

            log.info(f"Using container: {album_title!r} id={album_cid}")

            if force:
                self.library.clear(udn)

            # Phase 1: breadth-first walk to find leaf containers with tracks
            to_visit  = [album_cid]
            visited:set   = set()
            leaf_albums   = []

            log.info("Indexer: mapping album tree…")
            while to_visit:
                cid = to_visit.pop(0)
                if cid in visited:
                    continue
                visited.add(cid)
                sub_containers, items = provider.browse_all(cid, max_items=5000)
                if items:
                    leaf_albums.append(cid)
                elif sub_containers:
                    to_visit.extend(
                        c["id"] for c in sub_containers
                        if c["id"] not in visited)
                self.state.update(
                    total=len(leaf_albums) + len(to_visit))

            self.state.update(total=len(leaf_albums))
            log.info(f"Indexer: found {len(leaf_albums)} leaf containers")

            # Phase 2: index each leaf album
            attempted = 0
            for i, cid in enumerate(leaf_albums):
                if self.state.status == "idle":
                    break   # cancelled
                _, items = provider.browse_all(cid, max_items=500)
                if items:
                    self.library.upsert_tracks(udn, items)
                    attempted += len(items)
                self.state.update(progress=i + 1, tracks=attempted)
                if i % 100 == 0:
                    log.debug(f"Indexer: {i+1}/{len(leaf_albums)} albums, "
                              f"{attempted} tracks processed")

            self.library.mark_indexed(udn)
            self.library.rebuild_fts()

            actual  = self.library.track_count(udn)
            n_albums = self.library.album_count(udn)
            self.state.update(status="done", tracks=actual)

            log.info("=" * 60)
            log.info("✓  INDEX REBUILD COMPLETE")
            log.info(f"   Server  : {server.name}")
            log.info(f"   Albums  : {n_albums}  (distinct artist+album pairs)")
            log.info(f"   Tracks  : {actual}  "
                     f"({attempted - actual} duplicates suppressed)")
            log.info("=" * 60)

            # Any new bare albums from this crawl → look them up now.
            # Lazy import: by call-time dlna_library has finished loading
            # and ART_FETCHER is bound. Avoids an import cycle at module
            # load time (library imports Indexer, which would need ART_FETCHER).
            try:
                from dlna_library import ART_FETCHER
                ART_FETCHER.trigger()
            except Exception:
                pass   # module not yet fully initialised (test harness)

        except Exception:
            # Let everything bubble up — _run wraps this call in
            # _run_with_fts_heal, which catches a malformed-FTS
            # DatabaseError and retries once after repair_fts(). Any
            # other exception (or a malformed error on the retry) is
            # logged + state-updated by _run's outer except.
            raise
