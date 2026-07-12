#!/usr/bin/env python3
"""
tests/test_provider_localfs.py — LocalFsProvider (Phase 2 of the
AssetUPnP migration). Mocks the filesystem walk and mutagen so no
real audio files / mutagen invocations are needed.

Covered:
  - Pure helpers: _track_id_for stability, _udn_for_root stability,
    _format_duration shape, _is_audio_file extension allowlist.
  - Schema: LibraryDB now creates `localfs_files` table.
  - Provider construction + Protocol conformance + registry hookup.
  - probe() over a real tempdir (covers PermissionError too).
  - rescan() full path with a tempdir of fake audio files and a
    mocked _read_tags / _extract_art_hash; verifies stats, cache
    behaviour (skip-unchanged, detect-changed, detect-removed),
    track rows in library.db, dedup invariants, transactional commit.
  - stream_url raises NotImplementedError (P2 spec).
  - watch_changes raises NotImplementedError when watchdog missing.

Run standalone:
    python3 -m unittest tests.test_provider_localfs -v
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
from dlna_providers import LibraryProvider, get_provider_class
from dlna_providers.localfs import (
    LocalFsProvider,
    _album_key_for,
    _audio_extensions,
    _format_duration,
    _is_audio_file,
    _track_id_for,
    _udn_for_root,
)


# ── Pure helpers ────────────────────────────────────────────────

class TestPureHelpers(unittest.TestCase):

    def test_track_id_stable_across_calls(self):
        a = _track_id_for("Pink Floyd/The Wall/05 Another Brick.flac")
        b = _track_id_for("Pink Floyd/The Wall/05 Another Brick.flac")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 16, "16-char sha1 prefix")

    def test_track_id_different_for_different_paths(self):
        a = _track_id_for("Pink Floyd/The Wall/05 Another Brick.flac")
        b = _track_id_for("Pink Floyd/The Wall/06 Mother.flac")
        self.assertNotEqual(a, b)

    def test_track_id_unicode_safe(self):
        # Should not raise; deterministic
        a = _track_id_for("Sigur Rós/( )/06.flac")
        b = _track_id_for("Sigur Rós/( )/06.flac")
        self.assertEqual(a, b)

    def test_udn_is_stable_per_root(self):
        a = _udn_for_root(Path("/Music"))
        b = _udn_for_root(Path("/Music"))
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("uuid:localfs-"))

    def test_udn_different_per_root(self):
        a = _udn_for_root(Path("/Music"))
        b = _udn_for_root(Path("/Volumes/SAMDATA/Music"))
        self.assertNotEqual(a, b)

    def test_album_key_is_containing_folder(self):
        self.assertEqual(
            _album_key_for("Pink Floyd/The Wall/05 Another Brick.flac"),
            "Pink Floyd/The Wall")

    def test_album_key_same_folder_groups_tracks(self):
        # A compilation: different performers, ONE folder → one key.
        a = _album_key_for("Various Artists - 80s Radio Hits/01 X.flac")
        b = _album_key_for("Various Artists - 80s Radio Hits/41 Y.flac")
        self.assertEqual(a, b)

    def test_album_key_different_folders_stay_distinct(self):
        # Same album NAME, different real albums → different keys.
        a = _album_key_for("Brian Hyland/Greatest Hits/01 X.flac")
        b = _album_key_for("Bee Gees/Greatest Hits/01 Y.flac")
        self.assertNotEqual(a, b)

    def test_album_key_folds_disc_subfolders(self):
        # Multi-disc release: CD1 / CD2 fold up to the album folder.
        base = "John Denver - The Essential"
        for disc in ("CD1", "CD 2", "Disc 3", "Disk_4", "cd1", "Side 1"):
            with self.subTest(disc=disc):
                self.assertEqual(
                    _album_key_for(f"{base}/{disc}/08 Goodbye.flac"), base)

    def test_album_key_no_disc_subfolder_unchanged(self):
        # A folder that merely contains "cd"/"disc" as a word is NOT a
        # disc subfolder (no trailing number) → not folded.
        self.assertEqual(
            _album_key_for("Various/Discovery/01 X.flac"), "Various/Discovery")

    def test_album_key_root_level_file_is_empty(self):
        self.assertEqual(_album_key_for("loose.flac"), "")

    def test_format_duration_zero_is_empty(self):
        self.assertEqual(_format_duration(0), "")
        self.assertEqual(_format_duration(-5), "")

    def test_format_duration_under_minute(self):
        self.assertEqual(_format_duration(45.250), "0:00:45.250")

    def test_format_duration_minutes_and_seconds(self):
        self.assertEqual(_format_duration(213.456), "0:03:33.456")

    def test_format_duration_over_hour(self):
        # 1h 2m 3.5s
        self.assertEqual(_format_duration(3723.5), "1:02:03.500")

    def test_is_audio_file_recognises_common_formats(self):
        for ext in (".flac", ".mp3", ".m4a", ".dsf", ".wav", ".opus"):
            with self.subTest(ext=ext):
                self.assertTrue(_is_audio_file(Path(f"/x/y.{ext.lstrip('.')}")))

    def test_is_audio_file_rejects_non_audio(self):
        for path in ("/x/y.mp4", "/x/y.txt", "/x/y.jpg", "/x/y"):
            with self.subTest(path=path):
                self.assertFalse(_is_audio_file(Path(path)))

    def test_audio_extensions_set_excludes_mp4(self):
        # CLAUDE.md: mp4 stays OUT of music allowlist (music-video case)
        self.assertNotIn(".mp4", _audio_extensions)


# ── Schema ──────────────────────────────────────────────────────

class TestSchema(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)

    def tearDown(self):
        os.unlink(self._p)

    def test_localfs_files_table_created(self):
        with self.db._pool.read() as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(localfs_files)")}
        for required in ("path", "mtime", "size", "track_id", "last_scanned"):
            self.assertIn(required, cols)


# ── Provider construction + Protocol conformance ───────────────

class TestProviderConstruction(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.root = Path(tempfile.mkdtemp(prefix="localfs-test-")).resolve()

    def tearDown(self):
        os.unlink(self._p)
        # Clean up the tempdir tree
        for p in sorted(self.root.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        self.root.rmdir()

    def test_class_registered_under_localfs(self):
        self.assertIs(get_provider_class("localfs"), LocalFsProvider)

    def test_provider_is_library_provider(self):
        p = LocalFsProvider(self.db, self.root)
        self.assertIsInstance(p, LibraryProvider)

    def test_name_is_localfs(self):
        self.assertEqual(LocalFsProvider(self.db, self.root).name, "localfs")

    def test_udn_derived_from_root(self):
        p = LocalFsProvider(self.db, self.root)
        self.assertEqual(p.udn, _udn_for_root(self.root))


# ── probe() ────────────────────────────────────────────────────

class TestProbe(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.root = Path(tempfile.mkdtemp(prefix="localfs-test-")).resolve()

    def tearDown(self):
        os.unlink(self._p)
        for p in sorted(self.root.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        if self.root.exists():
            self.root.rmdir()

    def test_probe_true_when_root_has_contents(self):
        (self.root / "Album").mkdir()
        (self.root / "Album" / "song.flac").write_bytes(b"")
        p = LocalFsProvider(self.db, self.root)
        self.assertTrue(p.probe())

    def test_probe_false_when_root_missing(self):
        # Delete the root before probing
        self.root.rmdir()
        p = LocalFsProvider(self.db, self.root)
        self.assertFalse(p.probe())

    def test_probe_false_on_permission_error(self):
        p = LocalFsProvider(self.db, self.root)
        with patch.object(type(self.root), "iterdir",
                          side_effect=PermissionError("denied")):
            self.assertFalse(p.probe())


# ── rescan() ───────────────────────────────────────────────────

class TestRescan(unittest.TestCase):
    """Drives rescan() with a real tempdir of fake audio files and
    mocked tag reading. Verifies the cache / diff / commit flow."""

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.root = Path(tempfile.mkdtemp(prefix="localfs-test-")).resolve()

    def tearDown(self):
        os.unlink(self._p)
        for p in sorted(self.root.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        if self.root.exists():
            self.root.rmdir()

    def _write_file(self, rel: str, body: bytes = b"\0" * 16) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        return p

    def _fake_tags(self, path: Path) -> dict:
        # Synthesise plausible tags from the path so tests can
        # assert on artist/album/title without real mutagen.
        parts = path.relative_to(self.root).parts
        artist = parts[0] if len(parts) > 0 else "Unknown"
        album  = parts[1] if len(parts) > 1 else "Unknown"
        title  = path.stem
        return {
            "title": title, "artist": artist, "album": album,
            "genre": "Rock", "duration": "0:03:33.456",
            "bit_depth": 16, "sample_rate": 44100,
            "year": 1979, "track_number": 1,
            "mime": "audio/flac",
        }

    def test_rescan_indexes_audio_files(self):
        self._write_file("Pink Floyd/The Wall/01 In the Flesh.flac")
        self._write_file("Pink Floyd/The Wall/02 The Thin Ice.flac")
        self._write_file("README.txt")     # non-audio — must be skipped
        p = LocalFsProvider(self.db, self.root)
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            stats = p.rescan()
        self.assertEqual(stats["scanned"], 2)
        self.assertEqual(stats["new"], 2)
        self.assertEqual(stats["unchanged"], 0)
        self.assertEqual(stats["malformed"], 0)

        n = self.db.track_count(p.udn)
        self.assertEqual(n, 2)

    def test_rescan_skips_unchanged_files_on_second_pass(self):
        self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            p.rescan()
            stats = p.rescan()
        self.assertEqual(stats["scanned"], 1)
        self.assertEqual(stats["unchanged"], 1)
        self.assertEqual(stats["new"], 0)
        self.assertEqual(stats["changed"], 0)

    def test_rescan_force_rereads_unchanged_files(self):
        self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            p.rescan()
            stats = p.rescan(force=True)
        # force=True counts the file as changed (since we re-read).
        # The exact bucket is "changed" because cache row already exists.
        self.assertEqual(stats["unchanged"], 0)
        self.assertEqual(stats["changed"] + stats["new"], 1)

    def test_rescan_detects_removed_files(self):
        f = self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            p.rescan()
            f.unlink()
            stats = p.rescan()
        self.assertEqual(stats["removed"], 1)
        self.assertEqual(self.db.track_count(p.udn), 0)

    def test_rescan_detects_changed_files(self):
        f = self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            p.rescan()
            # Modify the file so size differs
            f.write_bytes(b"\0" * 64)
            stats = p.rescan()
        self.assertEqual(stats["changed"], 1)
        self.assertEqual(stats["unchanged"], 0)

    def test_rescan_updates_metadata_of_retagged_file(self):
        """In-place retagging (beets writes new tags; mtime/size change)
        must land in the tracks row on the next rescan — the 2026-07-12
        fix. Before it, INSERT OR IGNORE swallowed the fresh row and the
        old metadata stuck forever (workaround was DELETE + rebuild)."""
        f = self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)

        def retagged(path):
            return {**self._fake_tags(path),
                    "title": "Canonical Title", "artist": "Canonical Artist",
                    "album": "Canonical Album", "genre": "Jazz",
                    "year": 1969}

        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            with patch("dlna_providers.localfs._read_tags",
                       side_effect=self._fake_tags):
                p.rescan()
            f.write_bytes(b"\0" * 64)   # beets rewrote the file in place
            with patch("dlna_providers.localfs._read_tags",
                       side_effect=retagged):
                stats = p.rescan()

        self.assertEqual(stats["changed"], 1)
        self.assertEqual(self.db.track_count(p.udn), 1)   # updated, not added
        with self.db._pool.read() as conn:
            row = dict(conn.execute(
                "SELECT title, artist, album, genre, year FROM tracks "
                "WHERE udn=?", (p.udn,)).fetchone())
        self.assertEqual(row["title"], "Canonical Title")
        self.assertEqual(row["artist"], "Canonical Artist")
        self.assertEqual(row["album"], "Canonical Album")
        self.assertEqual(row["genre"], "Jazz")
        self.assertEqual(row["year"], 1969)

    def test_force_rescan_updates_metadata_without_file_change(self):
        """`force=True` re-reads tags even when (mtime, size) is
        unchanged — new tag values must reach the tracks row."""
        self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)

        def retagged(path):
            return {**self._fake_tags(path), "title": "Fixed Title"}

        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            with patch("dlna_providers.localfs._read_tags",
                       side_effect=self._fake_tags):
                p.rescan()
            with patch("dlna_providers.localfs._read_tags",
                       side_effect=retagged):
                p.rescan(force=True)

        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT title FROM tracks WHERE udn=?", (p.udn,)).fetchone()
        self.assertEqual(row["title"], "Fixed Title")

    def test_rescan_skips_dot_directories(self):
        self._write_file(".Trashes/old.flac")
        self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            stats = p.rescan()
        self.assertEqual(stats["scanned"], 1)

    def test_rescan_records_malformed_files(self):
        self._write_file("Artist/Album/broken.flac")
        p = LocalFsProvider(self.db, self.root)
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   return_value=None), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            stats = p.rescan()
        self.assertEqual(stats["malformed"], 1)
        self.assertEqual(self.db.track_count(p.udn), 0)

    def test_rescan_writes_localfs_placeholder_url_when_base_url_unset(self):
        # P2 behaviour preserved: when base_url isn't configured, the
        # url column is the `localfs://<udn>/<id>` placeholder. Callers
        # that try to play one without setting base_url get a clean
        # NotImplementedError from stream_url().
        self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            p.rescan()
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT url FROM tracks WHERE udn=?", (p.udn,)
            ).fetchone()
        self.assertTrue(row["url"].startswith("localfs://"))
        self.assertIn(p.udn, row["url"])

    def test_rescan_writes_naim_fetchable_url_when_base_url_set(self):
        # P4 behaviour: with base_url configured (i.e. file server
        # running), tracks.url is a real http://… URL the renderer
        # can fetch directly. No translation layer needed downstream.
        self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root,
                            base_url="http://192.168.1.100:8200")
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            p.rescan()
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT url FROM tracks WHERE udn=?", (p.udn,)
            ).fetchone()
        self.assertTrue(row["url"].startswith(
            "http://192.168.1.100:8200/localfs/stream/"))

    def test_rescan_heals_placeholder_url_when_base_url_set(self):
        # Regression for the 2026-05-30 "nothing plays" bug. A row first
        # written by a base_url-less scan (the CLI tools/localfs_scan.py,
        # or any pre-server scan) keeps its `localfs://` placeholder. On
        # the next scan WITH a base_url the file is an mtime/size cache
        # hit, so the per-file URL write is skipped — only the cache-
        # independent heal pass can fix it. Without the heal, the track
        # is never renderer-fetchable and silently fails to play.
        self._write_file("Artist/Album/song.flac")
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            # First scan: no base_url → placeholder + cache populated.
            LocalFsProvider(self.db, self.root, base_url="").rescan()
            with self.db._pool.read() as conn:
                url = conn.execute(
                    "SELECT url FROM tracks WHERE udn LIKE 'uuid:localfs-%'"
                ).fetchone()["url"]
            self.assertTrue(url.startswith("localfs://"))

            # Second scan: WITH base_url, same db+root. File unchanged
            # (cache hit) so heal is the only thing that can fix the URL.
            p = LocalFsProvider(self.db, self.root,
                                base_url="http://10.0.0.5:8200")
            stats = p.rescan()
        self.assertEqual(stats["unchanged"], 1)   # proves fast path ran
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT obj_id, url FROM tracks WHERE udn=?", (p.udn,)
            ).fetchone()
        self.assertEqual(
            row["url"],
            f"http://10.0.0.5:8200/localfs/stream/{row['obj_id']}")

    def test_rescan_heals_url_on_base_url_change(self):
        # A LAN-IP / port change must repoint every URL on the next scan,
        # cache-independent. Same heal path; different trigger.
        self._write_file("Artist/Album/song.flac")
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            LocalFsProvider(self.db, self.root,
                            base_url="http://10.0.0.5:8200").rescan()
            p = LocalFsProvider(self.db, self.root,
                                base_url="http://192.168.1.9:8200")
            p.rescan()
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT obj_id, url FROM tracks WHERE udn=?", (p.udn,)
            ).fetchone()
        self.assertEqual(
            row["url"],
            f"http://192.168.1.9:8200/localfs/stream/{row['obj_id']}")

    def test_rescan_heals_art_marker_to_url_when_base_url_set(self):
        # Companion to the URL heal: a `localfs-art:<hash>` marker
        # written at scan time must become a real
        # `<base_url>/localfs/art/<id>` URL the /art proxy can fetch.
        # Cache-independent (runs on an unchanged-file scan), keyed on
        # obj_id, and leaves non-marker art (http) untouched.
        self._write_file("Artist/Album/song.flac")
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value="deadbeefcafe0001"):
            # First scan: no base_url → art stays a marker.
            LocalFsProvider(self.db, self.root, base_url="").rescan()
            with self.db._pool.read() as conn:
                art = conn.execute(
                    "SELECT art FROM tracks WHERE udn LIKE 'uuid:localfs-%'"
                ).fetchone()["art"]
            self.assertTrue(art.startswith("localfs-art:"))

            # Second scan WITH base_url: unchanged file (cache hit), so
            # only the heal pass can convert the marker.
            p = LocalFsProvider(self.db, self.root,
                                base_url="http://10.0.0.5:8200")
            stats = p.rescan()
        self.assertEqual(stats["unchanged"], 1)
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT obj_id, art FROM tracks WHERE udn=?", (p.udn,)
            ).fetchone()
        self.assertEqual(
            row["art"],
            f"http://10.0.0.5:8200/localfs/art/{row['obj_id']}")

    def test_rescan_art_heal_leaves_http_art_untouched(self):
        # A row whose art is already an http URL (sibling-harvested /
        # MusicBrainz) must NOT be rewritten by the art heal.
        self._write_file("Artist/Album/song.flac")
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            p = LocalFsProvider(self.db, self.root,
                                base_url="http://10.0.0.5:8200")
            p.rescan()
            # Simulate an externally-harvested cover.
            with self.db._pool.write() as conn:
                conn.execute(
                    "UPDATE tracks SET art='http://cover/art.jpg' "
                    "WHERE udn=?", (p.udn,))
            p.rescan()
        with self.db._pool.read() as conn:
            art = conn.execute(
                "SELECT art FROM tracks WHERE udn=?", (p.udn,)
            ).fetchone()["art"]
        self.assertEqual(art, "http://cover/art.jpg")

    def test_rescan_uses_supplied_bit_depth_and_sample_rate(self):
        self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)
        def fake_24bit_tags(path):
            t = self._fake_tags(path)
            t["bit_depth"] = 24
            t["sample_rate"] = 96000
            return t
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=fake_24bit_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            p.rescan()
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT bit_depth, sample_rate FROM tracks "
                "WHERE udn=?", (p.udn,)).fetchone()
        self.assertEqual((row["bit_depth"], row["sample_rate"]),
                         (24, 96000))

    def test_rescan_raises_if_root_missing(self):
        self.root.rmdir()
        p = LocalFsProvider(self.db, self.root)
        with self.assertRaises(RuntimeError):
            with patch("dlna_providers.localfs._require_mutagen"):
                p.rescan()


# ── stream_url + watch_changes (P2 spec contracts) ─────────────

class TestNotImplementedSurface(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.root = Path(tempfile.mkdtemp(prefix="localfs-test-")).resolve()

    def tearDown(self):
        os.unlink(self._p)
        if self.root.exists():
            self.root.rmdir()

    def test_stream_url_raises_when_base_url_unset(self):
        p = LocalFsProvider(self.db, self.root)
        with self.assertRaises(NotImplementedError) as cm:
            p.stream_url("any-id")
        self.assertIn("base", str(cm.exception).lower())

    def test_stream_url_returns_full_url_after_set_base_url(self):
        p = LocalFsProvider(self.db, self.root)
        p.set_base_url("http://192.168.1.100:8200")
        self.assertEqual(
            p.stream_url("abc123"),
            "http://192.168.1.100:8200/localfs/stream/abc123")

    def test_set_base_url_strips_trailing_slash(self):
        p = LocalFsProvider(self.db, self.root)
        p.set_base_url("http://192.168.1.100:8200/")
        self.assertEqual(
            p.stream_url("abc123"),
            "http://192.168.1.100:8200/localfs/stream/abc123")

    def test_base_url_via_constructor(self):
        p = LocalFsProvider(self.db, self.root,
                            base_url="http://localhost:8200")
        self.assertEqual(p.stream_url("xyz"),
                         "http://localhost:8200/localfs/stream/xyz")

    def test_watch_changes_raises_when_watchdog_missing(self):
        p = LocalFsProvider(self.db, self.root)
        # Force the import to fail by hiding watchdog from the import path
        import builtins
        real_import = builtins.__import__
        def fake_import(name, *args, **kwargs):
            if name.startswith("watchdog"):
                raise ImportError("simulated missing watchdog")
            return real_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=fake_import), \
             self.assertRaises(NotImplementedError):
            p.watch_changes(lambda: None)


# ── album_key (folder-based album grouping, 2026-05-31) ─────────

class TestAlbumKey(unittest.TestCase):
    """Folder-based album identity written by the scanner + backfilled
    in rescan. Mirrors TestRescan's tempdir / fake-tags fixtures (kept
    standalone so TestRescan's suite isn't re-run under this name)."""

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.root = Path(tempfile.mkdtemp(prefix="localfs-test-")).resolve()

    def tearDown(self):
        os.unlink(self._p)
        for p in sorted(self.root.rglob("*"), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                p.rmdir()
        if self.root.exists():
            self.root.rmdir()

    def _write_file(self, rel: str, body: bytes = b"\0" * 16) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        return p

    def _fake_tags(self, path: Path) -> dict:
        parts = path.relative_to(self.root).parts
        return {
            "title": path.stem,
            "artist": parts[0] if parts else "Unknown",
            "album": parts[1] if len(parts) > 1 else "Unknown",
            "genre": "Rock", "duration": "0:03:33.456",
            "bit_depth": 16, "sample_rate": 44100,
            "year": 1979, "track_number": 1, "mime": "audio/flac",
        }

    def _album_keys(self, udn: str) -> list:
        with self.db._pool.read() as conn:
            return [r[0] for r in conn.execute(
                "SELECT album_key FROM tracks WHERE udn=? ORDER BY url",
                (udn,)).fetchall()]

    def _scan(self, p):
        with patch("dlna_providers.localfs._require_mutagen"), \
             patch("dlna_providers.localfs._read_tags",
                   side_effect=self._fake_tags), \
             patch("dlna_providers.localfs._extract_art_hash",
                   return_value=None):
            return p.rescan()

    def test_schema_has_album_key_column(self):
        with self.db._pool.read() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
        self.assertIn("album_key", cols)

    def test_scanner_populates_album_key(self):
        self._write_file("Pink Floyd/The Wall/01 In the Flesh.flac")
        p = LocalFsProvider(self.db, self.root)
        self._scan(p)
        self.assertEqual(self._album_keys(p.udn), ["Pink Floyd/The Wall"])

    def test_compilation_shares_one_album_key(self):
        self._write_file("VA - 80s Radio Hits/01 A.flac")
        self._write_file("VA - 80s Radio Hits/02 B.flac")
        p = LocalFsProvider(self.db, self.root)
        self._scan(p)
        keys = set(self._album_keys(p.udn))
        self.assertEqual(keys, {"VA - 80s Radio Hits"})

    def test_multidisc_folds_to_one_album_key(self):
        self._write_file("Artist/Big Album/CD1/01 X.flac")
        self._write_file("Artist/Big Album/CD2/01 Y.flac")
        p = LocalFsProvider(self.db, self.root)
        self._scan(p)
        keys = set(self._album_keys(p.udn))
        self.assertEqual(keys, {"Artist/Big Album"})

    def test_rescan_backfills_missing_album_key(self):
        self._write_file("Artist/Album/song.flac")
        p = LocalFsProvider(self.db, self.root)
        self._scan(p)
        # Simulate a pre-column / old-scan row: wipe album_key, leaving
        # the file unchanged so the next rescan takes the cache fast-path.
        with self.db._pool.write() as conn:
            conn.execute("UPDATE tracks SET album_key='' WHERE udn=?",
                         (p.udn,))
        self.assertEqual(self._album_keys(p.udn), [""])
        stats = self._scan(p)
        self.assertEqual(stats["unchanged"], 1)   # cache fast-path
        self.assertEqual(self._album_keys(p.udn), ["Artist/Album"])

    def test_upsert_persists_album_key(self):
        self.db.upsert_tracks("uuid:localfs-x", [{
            "id": "abc", "url": "localfs://uuid:localfs-x/abc",
            "title": "T", "artist": "A", "album": "Alb",
            "album_key": "A/Alb", "file_path": "/m/A/Alb/t.flac",
        }])
        with self.db._pool.read() as conn:
            ak = conn.execute(
                "SELECT album_key FROM tracks WHERE obj_id='abc'").fetchone()[0]
        self.assertEqual(ak, "A/Alb")


if __name__ == "__main__":
    unittest.main()

