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


if __name__ == "__main__":
    unittest.main()
