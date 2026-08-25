"""
dlna_providers.localfs — `LocalFsProvider`, the in-process backend.

Phase 2 of the AssetUPnP migration. Walks a music root on disk,
reads tags + embedded art via `mutagen`, and populates the gateway's
existing `library.db` under a synthesised UDN of its own. The
existing browse / search / API paths work against the new UDN
without any further changes — the proof of P2 is that
`SELECT COUNT(*) FROM tracks WHERE udn = '<localfs-udn>'` matches
the AssetUPnP UDN's count (modulo cosmetic differences in
file/folder layout).

Per CLAUDE.md → "Library backend migration (in flight)":

- Pure read, zero risk, runs alongside AssetUPnP. No file serving in
  P2; `stream_url()` raises NotImplementedError.
- Stable track IDs: `sha1(rel_path)[:16]`. Survive renumbering
  across rescans — the lesson from AssetUPnP's d-id chaos.
- Per-file (mtime, size) cache in `library.db.localfs_files` so
  rescans only re-tag changed files.
- Transactional commits — the served view is never half-updated.
- Malformed files flagged (logged at WARNING), not silently dropped.

What's deliberately NOT in P2:

- Audio serving (`stream_url`, `/localfs/stream/<id>`, DLNA headers,
  Range support). That's P3.
- High-level Protocol methods (`list_artists` etc.) are wired through
  the existing `LibraryDB` browse methods so the seam fully works
  for browse; `stream_url` and the playback path are P3+.
- Embedded album art extraction is best-effort: bytes are hashed
  and the hash stored on the track row as a placeholder marker
  (`localfs-art:<sha1>`). Resolving the marker to actual bytes is
  P3 work (file server + /art route).
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any
from collections.abc import Iterator

from .localfs_read import ReadMixin
# Pure helpers moved to localfs_tags 2026-08-20; re-exported here
# so existing imports (and the tests that patch these names on
# this module) keep working unchanged.
from .localfs_tags import (  # noqa: F401
    _AUDIO_EXTENSIONS,
    _album_key_for,
    _extract_art_bytes,
    _extract_art_hash,
    _format_duration,
    _is_audio_file,
    _mime_for,
    _read_tags,
    _require_mutagen,
    _sniff_image_mime,
    _track_id_for,
    _udn_for_root,
)
from . import (
    register_provider,
)

log = logging.getLogger("dlna.providers.localfs")


# ── The provider ───────────────────────────────────────────────

@register_provider("localfs")
class LocalFsProvider(ReadMixin):
    """LibraryProvider backed by a local filesystem walk + mutagen.

    Construct with `LocalFsProvider(library_db, music_root)`. The
    UDN is derived from the music root path. Call `rescan()` to
    populate library.db's `tracks` rows under the provider's UDN.

    `library_db` is a `LibraryDB` instance — this provider writes
    via `library_db.upsert_tracks(self.udn, items)`, so the
    existing browse layer (api_browse, /api/artists, etc.) reads
    the LocalFs view transparently."""

    name = "localfs"

    def __init__(self, library_db: Any, music_root: Path | str,
                 *, base_url: str = "", id_namespace: str = "",
                 collect_unknown_artists: bool = True):
        self._library = library_db
        self._root = Path(music_root).expanduser().resolve()
        self.udn: str = _udn_for_root(self._root)
        self._fs_watcher: Any | None = None    # watchdog Observer
        # P3: the URL prefix the file server is reachable at. Lets
        # stream_url(track_id) emit a full Naim-fetchable URL. Empty
        # means the server isn't running yet → stream_url raises.
        self._base_url: str = base_url.rstrip("/")
        # Secondary roots (audiobooks) salt their track ids so a
        # rel_path shared with the music root can't collide on obj_id
        # (the file server resolves ids across ALL localfs UDNs). The
        # music root passes '' → ids unchanged.
        self._id_namespace: str = id_namespace
        # After each scan, sweep tracks we could not attribute into the
        # hand-editing worklist. OFF for audiobooks: a chapter with no
        # artist tag is ordinary there (the author lives in `book_meta`),
        # so sweeping them in would bury the music that needs the work.
        self._collect_unknown_artists = bool(collect_unknown_artists)

    # ── Public surface ──────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    def probe(self) -> bool:
        """True iff the music root is accessible — on macOS this
        catches the TCC-locked-SAMDATA case (`Operation not
        permitted`) cleanly."""
        try:
            return self._root.exists() and self._root.is_dir() and any(
                # any() short-circuits — confirms read access without
                # walking the whole tree
                True for _ in self._root.iterdir())
        except (PermissionError, OSError) as e:
            log.warning(f"LocalFs probe failed for {self._root}: {e}")
            return False

    def rescan(self, force: bool = False) -> dict:
        """Walk the music root, diff against the per-file cache, and
        upsert any new/changed files into `library.db`. Returns a
        stats dict: {scanned, new, changed, unchanged, removed,
        malformed, elapsed_sec}.

        `force=True` ignores the (mtime, size) cache and re-reads
        tags for every file — useful when the schema changed or you
        suspect cache poisoning."""
        _require_mutagen()
        if not self.probe():
            raise RuntimeError(
                f"LocalFs root not accessible: {self._root} — "
                "is the volume mounted / unlocked?")
        t0 = time.time()
        stats = {
            "scanned":   0,
            "new":       0,
            "changed":   0,
            "unchanged": 0,
            "removed":   0,
            "malformed": 0,
        }
        cache = self._load_cache()
        seen_paths: set[str] = set()
        new_rows: list[dict] = []
        cache_writes: list[tuple] = []
        scan_epoch = int(time.time())

        for fs_path in self._walk_audio():
            stats["scanned"] += 1
            try:
                st = fs_path.stat()
            except OSError as e:
                log.warning(f"LocalFs: stat failed for {fs_path}: {e}")
                stats["malformed"] += 1
                continue
            abs_path = str(fs_path)
            seen_paths.add(abs_path)

            cached = cache.get(abs_path)
            unchanged = (cached is not None
                         and abs(cached[0] - st.st_mtime) < 0.001
                         and cached[1] == st.st_size
                         and not force)
            if unchanged:
                stats["unchanged"] += 1
                # Refresh last_scanned so removed-detection works
                cache_writes.append((abs_path, st.st_mtime, st.st_size,
                                     cached[2], scan_epoch))
                continue

            # Either new or changed — read tags afresh
            try:
                rel = str(fs_path.relative_to(self._root))
            except ValueError:
                rel = abs_path     # symlink-out / weird case; defensive
            track_id = _track_id_for(rel, self._id_namespace)
            tags = _read_tags(fs_path)
            if tags is None:
                stats["malformed"] += 1
                continue

            art_hash = _extract_art_hash(fs_path)
            # P4: when the LocalFs file server's base_url is configured,
            # write a real Naim-fetchable URL into tracks.url. The
            # renderer pulls bytes directly from `http://<lan-ip>:8200/
            # localfs/stream/<id>`. If base_url is unset (P2 testing
            # / pre-server scans), write the `localfs://<udn>/<id>`
            # placeholder — `LocalFsProvider.stream_url()` will then
            # raise NotImplementedError until set_base_url() is called.
            if self._base_url:
                url = f"{self._base_url}/localfs/stream/{track_id}"
            else:
                url = f"localfs://{self.udn}/{track_id}"
            row = {
                "id":          track_id,
                "url":         url,
                "art":         f"localfs-art:{art_hash}" if art_hash else "",
                "file_path":   abs_path,
                "album_key":   _album_key_for(rel),
                **tags,
            }
            new_rows.append(row)
            cache_writes.append((abs_path, st.st_mtime, st.st_size,
                                 track_id, scan_epoch))
            if cached is None:
                stats["new"] += 1
            else:
                stats["changed"] += 1

        # Files in cache that we didn't see this walk → removed
        removed_paths = [p for p in cache.keys() if p not in seen_paths]
        stats["removed"] = len(removed_paths)

        # Single transaction: upsert new/changed + update cache +
        # delete removed. Existing upsert_tracks already wraps its
        # own write in a transaction; the cache write goes through
        # the same write-pool connection.
        if new_rows:
            self._library.upsert_tracks(self.udn, new_rows)
        # The removed-paths DELETE below fires the FTS delete triggers —
        # heal-and-retry on the recurring shadow-table corruption (see
        # LibraryDB.run_with_fts_heal). Retry-safe: REPLACE/DELETE/UPDATE.
        self._library.run_with_fts_heal(
            self._rescan_finalize, cache_writes, removed_paths)
        if self._collect_unknown_artists:
            # Never let this fail a scan: the index is the product, the
            # worklist is a convenience laid on top of it.
            try:
                sync = self._library.sync_unknown_artist_playlist(self.udn)
                stats["unknown_artists"] = sync["total"]
            except Exception as e:                    # noqa: BLE001
                log.warning(f"unknown-artist sweep skipped: {e!r}")
        stats["elapsed_sec"] = round(time.time() - t0, 2)
        log.info(f"LocalFs rescan complete: {stats}")
        return stats

    def _rescan_finalize(self, cache_writes, removed_paths):
        with self._library._pool.write() as conn:
            if cache_writes:
                conn.executemany(
                    "INSERT OR REPLACE INTO localfs_files "
                    "(path, mtime, size, track_id, last_scanned) "
                    "VALUES (?,?,?,?,?)", cache_writes)
            if removed_paths:
                ph = ",".join("?" * len(removed_paths))
                conn.execute(
                    f"DELETE FROM localfs_files WHERE path IN ({ph})",
                    removed_paths)
                # Drop the corresponding tracks rows — keep our view
                # of library.db consistent with the filesystem.
                # Match on file_path since track URLs are synthetic.
                conn.execute(
                    f"DELETE FROM tracks WHERE udn=? AND file_path IN "
                    f"({ph})",
                    [self.udn] + removed_paths)

            # Self-heal stream URLs. The per-file URL is only (re)written
            # for new/changed rows above — so an "unchanged" row whose URL
            # was written by a base_url-less scan (e.g. the CLI
            # tools/localfs_scan.py, or any pre-server scan) keeps its
            # `localfs://…` placeholder forever and never becomes
            # renderer-fetchable. Likewise a LAN-IP / port change leaves
            # every URL pointing at the old host. Both are fixed here in
            # one cache-independent pass: when base_url is set, force every
            # row's URL to the canonical `<base_url>/localfs/stream/<id>`.
            # Idempotent (the WHERE skips already-correct rows) and keyed on
            # obj_id, which the file server resolves against.
            if self._base_url:
                expected = self._base_url + "/localfs/stream/"
                healed = conn.execute(
                    "UPDATE tracks SET url = ? || obj_id "
                    "WHERE udn = ? AND obj_id != '' "
                    "  AND url != ? || obj_id",
                    (expected, self.udn, expected)).rowcount
                if healed:
                    log.info(f"LocalFs healed {healed} stream URL(s) "
                             f"→ {self._base_url}")

                # Same heal for embedded-art markers. Scan time writes a
                # `localfs-art:<hash>` placeholder (the bytes aren't read
                # until serve time); turn every such marker into the
                # real `<base_url>/localfs/art/<id>` URL the `/art` proxy
                # can fetch. Only rows that still carry a marker are
                # touched — http art (sibling-harvested / MusicBrainz)
                # is left alone. Idempotent, cache-independent, keyed on
                # obj_id (the id the art route resolves against).
                art_expected = self._base_url + "/localfs/art/"
                healed_art = conn.execute(
                    "UPDATE tracks SET art = ? || obj_id "
                    "WHERE udn = ? AND obj_id != '' "
                    "  AND art LIKE 'localfs-art:%'",
                    (art_expected, self.udn)).rowcount
                if healed_art:
                    log.info(f"LocalFs healed {healed_art} art URL(s) "
                             f"→ {self._base_url}")

            # Backfill folder-based album_key for rows that predate the
            # column or were written by an older scan (the "unchanged"
            # fast-path never rebuilds a row, so its album_key would stay
            # empty). Pure function of file_path → only rows missing it
            # are touched; idempotent and base_url-independent. Computed
            # in Python because the disc-subfolder fold can't be done in
            # SQL.
            missing = conn.execute(
                "SELECT id, file_path FROM tracks "
                "WHERE udn=? AND (album_key IS NULL OR album_key='') "
                "  AND file_path != ''",
                (self.udn,)).fetchall()
            ak_writes = []
            for row_id, fp in missing:
                try:
                    rel = str(Path(fp).relative_to(self._root))
                except ValueError:
                    continue        # file outside root (defensive)
                ak = _album_key_for(rel)
                if ak:
                    ak_writes.append((ak, row_id))
            if ak_writes:
                conn.executemany(
                    "UPDATE tracks SET album_key=? WHERE id=?", ak_writes)
                log.info(f"LocalFs backfilled {len(ak_writes)} album_key(s)")

    # ── Internal helpers ─────────────────────────────────────────

    def _walk_audio(self) -> Iterator[Path]:
        """Yield every audio file under the root. Symlinks are NOT
        followed — mirrors the safety choice in
        tools/find_corrupt_audio.py."""
        for dirpath, dirnames, filenames in os.walk(
                self._root, followlinks=False):
            # Skip macOS Trash + hidden directories
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d != ".Trashes"]
            for fname in filenames:
                if fname.startswith("."):
                    continue
                p = Path(dirpath) / fname
                if _is_audio_file(p):
                    yield p

    def _load_cache(self) -> dict[str, tuple[float, int, str]]:
        """Snapshot localfs_files into a path → (mtime, size,
        track_id) map. Done once per scan; cheap even for 50k rows.

        Scoped to THIS provider's root (2026-07-13): the table is shared
        by every LocalFs provider (music + audiobooks), and the rescan's
        removed-detection treats any cached path it didn't walk as
        deleted — unscoped, each provider's scan would purge the other's
        cache rows on every pass."""
        prefix = str(self._root) + os.sep
        out: dict[str, tuple[float, int, str]] = {}
        with self._library._pool.read() as conn:
            for path, mtime, size, track_id in conn.execute(
                    "SELECT path, mtime, size, track_id "
                    "FROM localfs_files"):
                if path.startswith(prefix):
                    out[path] = (mtime, size, track_id)
        return out

    def __repr__(self) -> str:
        return f"LocalFsProvider(root={self._root!r}, udn={self.udn!r})"


__all__ = [
    "LocalFsProvider",
    "_audio_extensions",   # for tests
    "_track_id_for",       # for tests
    "_udn_for_root",       # for tests
    "_read_tags",          # for tests
    "_format_duration",    # for tests
]


# Public test-only references — keeps the test suite from poking
# private names.
_audio_extensions = _AUDIO_EXTENSIONS
