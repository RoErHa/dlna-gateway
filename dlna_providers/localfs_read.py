#!/usr/bin/env python3
"""
localfs_read.py — `ReadMixin`: the `LibraryProvider` Protocol read
surface of `LocalFsProvider` (artists / albums / tracks / search), the
stream-URL builder, and the watchdog change hook.

Split out of localfs.py (2026-08-20), which had reached 725 lines.
`LocalFsProvider` INHERITS this mixin, so the provider's public surface
and the registry entry are unchanged.

The seam is READ vs WRITE: everything here answers questions, and does
so by delegating to `LibraryDB` (which is why the existing browse layer
works transparently for LocalFs UDNs). The scanning half — the walk, the
mutagen read, the upsert — stays in localfs.py, and deliberately so:
the tests patch `dlna_providers.localfs._read_tags` and friends, which
only works while `rescan` resolves those names in that module.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

from . import Album, Artist, Track
from .localfs_tags import _AUDIO_EXTENSIONS

log = logging.getLogger("dlna.provider.localfs")


class ReadMixin:
    """See module docstring. Mixed into `LocalFsProvider`; never
    instantiated on its own — it relies on `self._library`, `self.udn`
    and `self._base_url` from the host class."""

    # ── LibraryProvider Protocol — high-level surface ────────────
    # Wired through LibraryDB so the existing browse layer works
    # transparently for LocalFs UDNs. stream_url is intentionally
    # NotImplemented per the P2 spec; watch_changes goes through
    # watchdog when available.

    def list_artists(self) -> Iterator[Artist]:
        for row in self._library.all_artists(self.udn):
            yield Artist(id=row["artist"], name=row["artist"],
                         album_count=row.get("album_count", 0))

    def list_albums(self, artist_id: str) -> Iterator[Album]:
        for row in self._library.artist_albums(self.udn, artist_id):
            yield Album(id=row["album"], name=row["album"],
                        artist_id=artist_id, artist_name=artist_id,
                        track_count=row.get("track_count", 0),
                        art_url=row.get("art") or "")

    def list_tracks(self, album_id: str) -> Iterator[Track]:
        # album_id maps onto the album name (LibraryDB's natural key).
        # Without an artist_id we lean on the LibraryDB browse method
        # by inferring artist from the row.
        with self._library._pool.read() as conn:
            cur = conn.execute(
                "SELECT obj_id, url, title, artist, album, duration, "
                "       art, mime, genre, file_path, bit_depth, "
                "       sample_rate, year "
                "  FROM tracks "
                " WHERE udn=? AND album=? "
                " ORDER BY title COLLATE NOCASE",
                (self.udn, album_id))
            for r in cur.fetchall():
                yield Track(
                    id=r["obj_id"],
                    title=r["title"] or "",
                    artist_name=r["artist"] or "",
                    album_name=r["album"] or "",
                    year=r["year"],
                    art_url=r["art"] or "",
                    mime=r["mime"] or "",
                    file_path=r["file_path"] or "",
                    bit_depth=r["bit_depth"],
                    sample_rate=r["sample_rate"],
                    genre=r["genre"] or "",
                )

    def get_track(self, track_id: str) -> Track | None:
        with self._library._pool.read() as conn:
            r = conn.execute(
                "SELECT obj_id, url, title, artist, album, duration, "
                "       art, mime, genre, file_path, bit_depth, "
                "       sample_rate, year "
                "  FROM tracks WHERE udn=? AND obj_id=?",
                (self.udn, track_id)).fetchone()
        if not r:
            return None
        return Track(
            id=r["obj_id"],
            title=r["title"] or "",
            artist_name=r["artist"] or "",
            album_name=r["album"] or "",
            year=r["year"],
            art_url=r["art"] or "",
            mime=r["mime"] or "",
            file_path=r["file_path"] or "",
            bit_depth=r["bit_depth"],
            sample_rate=r["sample_rate"],
            genre=r["genre"] or "",
        )

    def set_base_url(self, base_url: str) -> None:
        """P3: tell the provider where its file server is reachable.
        Called by `dlna_localfs_server.start_server` (or by tests).
        Once set, `stream_url(track_id)` returns a full URL the Naim
        can fetch from."""
        self._base_url = base_url.rstrip("/")

    def stream_url(self, track_id: str) -> str:
        # P3: real URLs are emitted once the file server is up and
        # its base URL has been bound via set_base_url(). Until then,
        # signal that the streaming half isn't wired yet.
        if not self._base_url:
            raise NotImplementedError(
                "LocalFsProvider.stream_url needs a base URL — the "
                "file server (dlna_localfs_server) hasn't been started "
                "yet. Call set_base_url() after start_server().")
        return f"{self._base_url}/localfs/stream/{track_id}"

    def search(self, q: str, limit: int = 50) -> Iterator[Track]:
        # Delegate to LibraryDB FTS5 search, filtered by this UDN.
        for row in self._library.search(self.udn, q):
            tracks_section = row.get("tracks", [])
            for r in tracks_section[:limit]:
                yield Track(
                    id=r.get("id", ""),
                    title=r.get("title", ""),
                    artist_name=r.get("artist", ""),
                    album_name=r.get("album", ""),
                    art_url=r.get("art", ""),
                )

    def watch_changes(self, on_change: Callable[[], None]) -> None:
        """FSEvents-based incremental change subscription. Optional —
        raises NotImplementedError if `watchdog` isn't installed."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError as e:
            raise NotImplementedError(
                "LocalFsProvider.watch_changes needs `watchdog` — "
                "install with `pip install watchdog`."
            ) from e

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                if not event.is_directory:
                    src = getattr(event, "src_path", "")
                    if Path(src).suffix.lower() in _AUDIO_EXTENSIONS:
                        on_change()

        obs = Observer()
        obs.schedule(_Handler(), str(self._root), recursive=True)
        obs.daemon = True
        obs.start()
        self._fs_watcher = obs

