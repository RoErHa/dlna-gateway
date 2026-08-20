"""
dlna_providers.mock — in-memory provider with canned data.

Phase 0 test scaffold per CLAUDE.md ("Library backend migration").
Lets the gateway exercise the LibraryProvider seam — registry
lookup, browse traversal, search, change notifications — without
any network or filesystem dependency.

Not used in production. The call site has to import and construct
MockProvider explicitly; the @register_provider decorator gives the
class a name in the registry but does not auto-bind any UDN.

Usage:

    from dlna_providers import bind_provider, Artist, Album, Track
    from dlna_providers.mock import MockProvider

    p = MockProvider(udn="uuid:test")
    p.seed_artist(Artist(id="a1", name="Pink Floyd"))
    p.seed_album(Album(id="al1", name="The Wall", artist_id="a1"))
    p.seed_track(Track(id="t1", title="Comfortably Numb",
                       album_id="al1", artist_id="a1"))
    bind_provider("uuid:test", p)
"""
from __future__ import annotations

from collections.abc import Callable, Iterator

from . import Album, Artist, Track, register_provider


@register_provider("mock")
class MockProvider:
    """LibraryProvider with canned data, for tests.

    Satisfies the LibraryProvider Protocol structurally. The seed_*
    methods are test-only — production providers populate their data
    from UPnP / Plex API / filesystem, etc."""

    name = "mock"

    def __init__(self, udn: str = "uuid:mock"):
        self.udn: str = udn
        self._artists: dict[str, Artist] = {}
        self._albums:  dict[str, Album]  = {}
        self._tracks:  dict[str, Track]  = {}
        self._reachable: bool = True
        self._watchers:  list[Callable[[], None]] = []

    # ── Seed helpers (test-only) ─────────────────────────────────

    def seed_artist(self, artist: Artist) -> None:
        self._artists[artist.id] = artist

    def seed_album(self, album: Album) -> None:
        self._albums[album.id] = album

    def seed_track(self, track: Track) -> None:
        self._tracks[track.id] = track

    def set_reachable(self, reachable: bool) -> None:
        self._reachable = reachable

    def fire_change(self) -> None:
        """Trigger every subscribed on_change callback. Test-only."""
        for cb in list(self._watchers):
            cb()

    # ── LibraryProvider interface ────────────────────────────────

    def list_artists(self) -> Iterator[Artist]:
        yield from self._artists.values()

    def list_albums(self, artist_id: str) -> Iterator[Album]:
        for al in self._albums.values():
            if al.artist_id == artist_id:
                yield al

    def list_tracks(self, album_id: str) -> Iterator[Track]:
        # Sort by track_number when present; fall back to id for stability.
        relevant = [t for t in self._tracks.values() if t.album_id == album_id]
        relevant.sort(key=lambda t: (t.track_number or 0, t.id))
        yield from relevant

    def get_track(self, track_id: str) -> Track | None:
        return self._tracks.get(track_id)

    def stream_url(self, track_id: str) -> str:
        if track_id not in self._tracks:
            return ""
        return f"mock://{self.udn}/track/{track_id}"

    def search(self, q: str, limit: int = 50) -> Iterator[Track]:
        ql = (q or "").lower()
        if not ql:
            return
        n = 0
        for t in self._tracks.values():
            if (ql in t.title.lower()
                or ql in t.artist_name.lower()
                or ql in t.album_name.lower()):
                yield t
                n += 1
                if n >= limit:
                    return

    def probe(self) -> bool:
        return self._reachable

    def watch_changes(self, on_change: Callable[[], None]) -> None:
        self._watchers.append(on_change)


__all__ = ["MockProvider"]
