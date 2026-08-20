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

import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterator

from . import (
    Album,
    Artist,
    Track,
    register_provider,
)

log = logging.getLogger("dlna.providers.localfs")


# ── Audio file detection ────────────────────────────────────────
# Reuses the extension set from tools/find_corrupt_audio.py +
# tools/prune_empty_music_dirs.py. mp4 is deliberately excluded
# (music-video case — see "prune_empty_music_dirs.py" section in
# CLAUDE.md).
_AUDIO_EXTENSIONS = frozenset((
    ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac",
    ".wav", ".aiff", ".aif", ".alac",
    ".dsf", ".dff", ".ape", ".wma",
    ".m4b",   # audiobooks (MP4 container, same as .m4a)
))


def _is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in _AUDIO_EXTENSIONS


# A trailing disc subfolder (CD1 / "Disc 2" / Disk_3 / "Side A") is
# folded into its parent so a multi-disc release groups as ONE album.
_DISC_SUBDIR_RE = re.compile(r"^(cd|disc|disk|side)[\s._-]*\d+[a-z]?$", re.I)


def _album_key_for(rel_path: str) -> str:
    """Folder-based album identity for a track, relative to the music
    root. An album == its containing folder; a trailing disc subfolder
    is folded into the parent (multi-disc releases group as one album).

    This is the grouping key the browse layer uses instead of the
    per-track (artist, album): a compilation lives in ONE folder so it
    groups as a single album even though every track's `artist` is a
    different performer, while two distinct albums that merely share a
    name ("Greatest Hits") live in different folders and stay separate.

    Returns "" for a root-level loose file (no containing folder)."""
    parent = os.path.dirname(rel_path)
    if _DISC_SUBDIR_RE.match(os.path.basename(parent)):
        parent = os.path.dirname(parent)
    return parent


def _track_id_for(rel_path: str, namespace: str = "") -> str:
    """Stable per-file identifier — survives rescans. The lesson
    from AssetUPnP's `d-<id>` was: NEVER auto-increment. Use a
    content-derived hash of the relative path so renaming a file
    legitimately produces a new id while a re-walk produces the same
    id.

    `namespace` (2026-07-13, audiobooks): a SECOND provider root can
    contain the same rel_path as the music root, and the file server
    resolves `/localfs/stream/<id>` by obj_id across ALL localfs UDNs
    — so ids from secondary roots are salted with a namespace. The
    music root keeps namespace='' so every existing URL/playlist row
    stays byte-identical."""
    key = f"{namespace}\x00{rel_path}" if namespace else rel_path
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _udn_for_root(root: Path) -> str:
    """Synthesise a stable UDN per music root path so multiple
    LocalFs providers (different roots) coexist."""
    h = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()
    return f"uuid:localfs-{h[:32]}"


# ── mutagen-backed tag reader ───────────────────────────────────
# All mutagen access lives behind these helpers so the rest of the
# module can be tested with mocks. Importing mutagen lazily lets
# `dlna_providers.localfs` be imported in environments where mutagen
# isn't installed (e.g. CI containers running the UPnP-only path) —
# only `rescan()` will then raise.

def _require_mutagen():
    try:
        import mutagen          # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "LocalFsProvider needs `mutagen` — install with "
            "`pip install -r requirements.txt`. See CLAUDE.md → "
            "'Library backend migration' for the dependency story."
        ) from e


def _read_tags(path: Path) -> dict | None:
    """Read tags + technical info via mutagen, returning a dict in
    the shape `LibraryDB.upsert_tracks` expects. Returns None when
    mutagen can't open the file (corrupt / unrecognized format);
    the caller should log this as a malformed-file warning."""
    import mutagen
    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception as e:                                    # noqa: BLE001
        log.warning(f"LocalFs: mutagen failed to open {path!s}: {e}")
        return None
    if audio is None:
        log.warning(f"LocalFs: mutagen does not recognise {path!s}")
        return None

    def _first(key: str) -> str:
        vals = audio.get(key, [])
        return (vals[0] if vals else "").strip()

    duration_sec = getattr(audio.info, "length", 0.0) or 0.0
    info = audio.info
    bit_depth = (getattr(info, "bits_per_sample", None)
                 or getattr(info, "bits_per_raw_sample", None))
    sample_rate = getattr(info, "sample_rate", None)

    # Year: mutagen-easy gives a 'date' field, often 'YYYY' or
    # 'YYYY-MM-DD'. Take just the first 4 digits if plausible.
    raw_date = _first("date") or _first("year")
    year: int | None = None
    if raw_date:
        m = re.match(r"^(\d{4})", raw_date)
        if m:
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                year = y

    # Track number — strip the "/totaltracks" suffix some tags carry
    raw_tn = _first("tracknumber")
    tn_match = re.match(r"^(\d+)", raw_tn)
    track_number = int(tn_match.group(1)) if tn_match else 0

    return {
        "title":       _first("title")  or path.stem,
        "artist":      _first("artist") or _first("albumartist"),
        "album":       _first("album"),
        "genre":       _first("genre"),
        "duration":    _format_duration(duration_sec),
        "bit_depth":   int(bit_depth) if bit_depth else None,
        "sample_rate": int(sample_rate) if sample_rate else None,
        "year":        year,
        "track_number": track_number,
        "mime":        _mime_for(path.suffix),
    }


def _format_duration(seconds: float) -> str:
    """Render seconds → AssetUPnP-shaped `H:MM:SS.fff` string so
    `dlna_player._dur_to_sec` accepts it identically."""
    if not seconds or seconds < 0:
        return ""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}"


_MIME_BY_EXT = {
    ".flac": "audio/flac",
    ".mp3":  "audio/mpeg",
    ".ogg":  "audio/ogg",
    ".opus": "audio/opus",
    ".m4a":  "audio/mp4",
    ".m4b":  "audio/mp4",
    ".aac":  "audio/aac",
    ".wav":  "audio/x-wav",
    ".aiff": "audio/x-aiff",
    ".aif":  "audio/x-aiff",
    ".alac": "audio/mp4",
    ".dsf":  "audio/x-dsf",
    ".dff":  "audio/x-dff",
    ".ape":  "audio/x-ape",
    ".wma":  "audio/x-ms-wma",
}


def _mime_for(suffix: str) -> str:
    return _MIME_BY_EXT.get(suffix.lower(), "application/octet-stream")


def _sniff_image_mime(data: bytes) -> str:
    """Best-effort image MIME from magic bytes. Embedded-art metadata
    sometimes lies about (or omits) its MIME, so sniff the bytes rather
    than trust the container. Falls back to image/jpeg."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _extract_art_bytes(path: Path) -> tuple[bytes, str] | None:
    """Return `(picture_bytes, mime)` for the first embedded cover, or
    None if the file has no embedded art / can't be read. Single source
    of cover bytes: used by `_extract_art_hash` (stable marker at scan
    time) AND the file server's `/localfs/art/<id>` route (serve on
    demand). All mutagen access is funnelled here so tests can mock it."""
    import mutagen
    try:
        audio = mutagen.File(str(path))
    except Exception:                                         # noqa: BLE001
        return None
    if audio is None:
        return None
    art_bytes: bytes | None = None
    # FLAC: .pictures
    pics = getattr(audio, "pictures", None)
    if pics:
        art_bytes = pics[0].data
    # ID3 (MP3): tags.getall('APIC')
    if not art_bytes and getattr(audio, "tags", None):
        try:
            apics = audio.tags.getall("APIC")
            if apics:
                art_bytes = apics[0].data
        except Exception:                                     # noqa: BLE001
            pass
        # M4A / MP4: 'covr' atom
        try:
            covr = audio.tags.get("covr") if hasattr(audio.tags, "get") else None
            if covr:
                art_bytes = bytes(covr[0])
        except Exception:                                     # noqa: BLE001
            pass
    if not art_bytes:
        return None
    return (art_bytes, _sniff_image_mime(art_bytes))


def _extract_art_hash(path: Path) -> str | None:
    """Return sha1(first-embedded-cover-bytes) or None. The marker lets
    the existing `_backfill_album_art` propagate a consistent value
    across the album's siblings; the actual bytes are served on demand
    by the file server's `/localfs/art/<id>` route, and `rescan()` heals
    the marker into a real `<base_url>/localfs/art/<id>` URL when
    base_url is set."""
    got = _extract_art_bytes(path)
    if not got:
        return None
    return hashlib.sha1(got[0]).hexdigest()[:24]


# ── The provider ───────────────────────────────────────────────

@register_provider("localfs")
class LocalFsProvider:
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
                 *, base_url: str = "", id_namespace: str = ""):
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
