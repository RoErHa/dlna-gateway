#!/usr/bin/env python3
"""
tools/test_find_corrupt_audio.py — unit tests over throw-away tempdirs.

Builds files with known-good and known-bad headers per format and
asserts the classifier verdicts. Neither --trash nor --hard-delete is
ever invoked — we only exercise _classify() and _scan() in their
default scan-only mode. Safe to run on any machine.

Run standalone:
    python3 -m unittest tools.test_find_corrupt_audio -v
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import find_corrupt_audio as F  # noqa: E402


# Minimal valid-enough headers per format. We only need the first 16
# bytes to satisfy each validator — the rest of the file can be junk
# because _classify() never reads past byte 16.
GOOD_HEADERS = {
    ".flac": b"fLaC" + b"\x00" * 12,
    ".mp3":  b"ID3\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
    ".ogg":  b"OggS" + b"\x00" * 12,
    ".opus": b"OggS" + b"\x00" * 12,
    ".m4a":  b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 4,
    ".alac": b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 4,
    ".aac":  b"\xff\xf1" + b"\x00" * 14,    # ADTS sync
    ".wav":  b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 4,
    ".aiff": b"FORM\x00\x00\x00\x00AIFF" + b"\x00" * 4,
    ".aif":  b"FORM\x00\x00\x00\x00AIFF" + b"\x00" * 4,
    ".wma":  b"\x30\x26\xb2\x75\x8e\x66\xcf\x11" + b"\x00" * 8,
    ".ape":  b"MAC " + b"\x00" * 12,
    ".dsf":  b"DSD " + b"\x00" * 12,
    ".dff":  b"FRM8" + b"\x00" * 12,
}

# Sample valid MP3 (sync-byte variant, no ID3 tag). The classifier
# accepts both ID3 prefix and raw sync; this exercises the second
# branch.
GOOD_MP3_NO_ID3 = b"\xff\xfb\x90\x00" + b"\x00" * 12


def _touch(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class TestClassify(unittest.TestCase):
    """_classify() is a pure function of the file's bytes — focused
    unit tests for every reason it can return."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="corrupt-test-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ── happy path: every format passes when given valid magic ──

    def test_every_format_valid_passes(self):
        for ext, head in GOOD_HEADERS.items():
            with self.subTest(ext=ext):
                f = self.root / f"track{ext}"
                _touch(f, head + b"PAD" * 100)
                self.assertIsNone(F._classify(f), f"good {ext} flagged")

    def test_mp3_raw_sync_no_id3_accepted(self):
        # MP3 files without an ID3 tag are common.
        f = self.root / "noid3.mp3"
        _touch(f, GOOD_MP3_NO_ID3 + b"PAD" * 100)
        self.assertIsNone(F._classify(f))

    def test_aiff_aifc_variant_accepted(self):
        # AIFC = compressed AIFF — different signature at offset 8.
        f = self.root / "compressed.aiff"
        _touch(f, b"FORM\x00\x00\x00\x00AIFC" + b"\x00" * 100)
        self.assertIsNone(F._classify(f))

    # ── corruption reasons ────────────────────────────────────────

    def test_zero_size_flagged(self):
        f = self.root / "empty.flac"
        _touch(f, b"")
        self.assertEqual(F._classify(f), "zero-size")

    def test_zero_header_flagged_regardless_of_extension(self):
        # The exact failure mode we saw on 2026-05-25: 13 MB FLAC files
        # whose first 16 bytes were all 0x00. The check must catch this
        # for ANY extension, not just FLAC — it's a generic "no audio".
        for ext in (".flac", ".mp3", ".m4a", ".wav", ".dsf"):
            with self.subTest(ext=ext):
                f = self.root / f"bad{ext}"
                _touch(f, b"\x00" * 16 + b"PAD" * 100)
                self.assertEqual(F._classify(f), "zero-header")

    def test_flac_magic_mismatch_flagged(self):
        # Random non-zero garbage doesn't satisfy the FLAC validator and
        # also doesn't trigger the zero-header check.
        f = self.root / "garbage.flac"
        _touch(f, b"NOTAFLACFILE\x00\x00\x00\x00" + b"x" * 100)
        self.assertEqual(F._classify(f), "magic-mismatch:.flac")

    def test_mp3_garbage_header_flagged(self):
        f = self.root / "garbage.mp3"
        # First byte isn't 0xFF and doesn't start with ID3 → mismatch.
        _touch(f, b"GARBAGE\x00\x00\x00\x00\x00\x00\x00\x00\x00" + b"x" * 50)
        self.assertEqual(F._classify(f), "magic-mismatch:.mp3")

    def test_wav_truncated_header_flagged(self):
        # Has RIFF but not WAVE at offset 8.
        f = self.root / "truncated.wav"
        _touch(f, b"RIFF\x00\x00\x00\x00NOPE" + b"x" * 50)
        self.assertEqual(F._classify(f), "magic-mismatch:.wav")

    def test_ogg_garbage_flagged(self):
        f = self.root / "garbage.ogg"
        _touch(f, b"NOPE" + b"\x01" * 12)
        self.assertEqual(F._classify(f), "magic-mismatch:.ogg")

    def test_opus_reuses_ogg_validator(self):
        # .opus files share the Ogg container — same magic.
        f1 = self.root / "good.opus"
        _touch(f1, b"OggS" + b"\x00" * 12 + b"x" * 100)
        self.assertIsNone(F._classify(f1))
        f2 = self.root / "bad.opus"
        _touch(f2, b"NOPE" + b"\x01" * 12)
        self.assertEqual(F._classify(f2), "magic-mismatch:.opus")

    def test_unknown_extension_treated_as_ok(self):
        # The walker filters by ext, so _classify shouldn't be called
        # on non-audio paths — but if it is, return None rather than
        # falsely flag a .txt file as corrupt.
        f = self.root / "notes.txt"
        _touch(f, b"\x00" * 16)
        # zero-header check still fires (generic), but no magic-mismatch
        self.assertEqual(F._classify(f), "zero-header")
        f2 = self.root / "notes2.txt"
        _touch(f2, b"hello world this is a text file")
        self.assertIsNone(F._classify(f2))


class TestScan(unittest.TestCase):
    """_scan() walks the tree and populates Stats correctly."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="corrupt-scan-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _scan(self, limit: int = 0, verbose: bool = False) -> F.Stats:
        stats = F.Stats()
        F._scan(self.root, F.DEFAULT_EXTS, stats, limit, verbose)
        return stats

    def test_scan_finds_only_corrupt(self):
        _touch(self.root / "Artist/Album/ok.flac",
               GOOD_HEADERS[".flac"] + b"x" * 100)
        _touch(self.root / "Artist/Album/bad.flac",
               b"\x00" * 16 + b"x" * 100)
        _touch(self.root / "Artist/Album/empty.flac", b"")
        _touch(self.root / "Artist/Album/garbage.mp3",
               b"GARBAGE\x00\x00\x00\x00\x00\x00\x00\x00\x00" + b"x" * 50)
        stats = self._scan()
        self.assertEqual(stats.files_scanned, 4)
        # Three corrupt; one good (the FLAC with valid magic).
        reasons = {p.name: r for p, r in stats.files_corrupt}
        self.assertEqual(set(reasons.keys()),
                         {"bad.flac", "empty.flac", "garbage.mp3"})
        self.assertEqual(reasons["bad.flac"],     "zero-header")
        self.assertEqual(reasons["empty.flac"],   "zero-size")
        self.assertEqual(reasons["garbage.mp3"],  "magic-mismatch:.mp3")

    def test_scan_skips_non_audio_extensions(self):
        # Files with non-audio extensions must not appear in files_scanned
        # — even if their bytes look "corrupt" by audio standards.
        _touch(self.root / "Artist/Album/notes.txt", b"\x00" * 16)
        _touch(self.root / "Artist/Album/cover.jpg",
               b"\xff\xd8\xff\xe0NOTAUDIO" + b"x" * 50)
        _touch(self.root / "Artist/Album/track.flac",
               GOOD_HEADERS[".flac"] + b"x" * 50)
        stats = self._scan()
        self.assertEqual(stats.files_scanned, 1)
        self.assertEqual(stats.files_corrupt, [])

    def test_case_insensitive_extension_match(self):
        # SAMDATA may have .FLAC / .Mp3 from older copies.
        _touch(self.root / "Album/track.FLAC", b"\x00" * 16 + b"x" * 50)
        _touch(self.root / "Album/song.Mp3",   GOOD_HEADERS[".mp3"] + b"x" * 50)
        stats = self._scan()
        self.assertEqual(stats.files_scanned, 2)
        self.assertEqual(len(stats.files_corrupt), 1)
        self.assertTrue(stats.files_corrupt[0][0].name == "track.FLAC")

    def test_limit_halts_cleanly(self):
        # Create 10 corrupt FLACs; --limit 3 should stop after 3 scanned.
        for i in range(10):
            _touch(self.root / f"Album/t{i}.flac",
                   b"\x00" * 16 + b"x" * 50)
        stats = self._scan(limit=3)
        self.assertTrue(stats.limit_reached)
        self.assertEqual(stats.files_scanned, 3)
        self.assertEqual(len(stats.files_corrupt), 3)

    def test_no_limit_zero_means_scan_all(self):
        for i in range(5):
            _touch(self.root / f"Album/t{i}.flac",
                   GOOD_HEADERS[".flac"] + b"x" * 50)
        stats = self._scan(limit=0)
        self.assertFalse(stats.limit_reached)
        self.assertEqual(stats.files_scanned, 5)
        self.assertEqual(stats.files_corrupt, [])

    def test_symlinks_not_followed(self):
        # A symlink loop or a link to a foreign volume must not be
        # descended (followlinks=False in os.walk).
        outside = Path(tempfile.mkdtemp(prefix="corrupt-outside-"))
        try:
            _touch(outside / "ghost.flac", b"\x00" * 16 + b"x" * 50)
            (self.root / "linkdir").symlink_to(outside)
            stats = self._scan()
            # The symlink-target file should NOT be scanned.
            self.assertEqual(stats.files_scanned, 0)
            self.assertEqual(stats.files_corrupt, [])
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_unreadable_file_recorded_separately(self):
        # File exists but is unreadable. Mode 0o000 → permission denied.
        f = self.root / "Album/blocked.flac"
        _touch(f, b"\x00" * 16 + b"x" * 50)
        f.chmod(0o000)
        try:
            stats = self._scan()
            # The walker tries to read; classify() returns "unreadable: …"
            # → goes into read_errors, NOT files_corrupt. We don't auto-
            # delete files we couldn't even open.
            self.assertEqual(len(stats.read_errors), 1)
            self.assertEqual(len(stats.files_corrupt), 0)
        finally:
            # Restore so tearDown can clean up.
            f.chmod(0o644)


class TestParseExts(unittest.TestCase):

    def test_parse_with_dots(self):
        self.assertEqual(F._parse_exts(".flac,.mp3"),
                         {".flac", ".mp3"})

    def test_parse_without_dots(self):
        self.assertEqual(F._parse_exts("flac,mp3,ogg"),
                         {".flac", ".mp3", ".ogg"})

    def test_parse_lowercases(self):
        self.assertEqual(F._parse_exts("FLAC,Mp3"),
                         {".flac", ".mp3"})

    def test_empty_string_returns_empty_set(self):
        self.assertEqual(F._parse_exts(""), set())

    def test_strips_whitespace(self):
        self.assertEqual(F._parse_exts(" flac , mp3 "),
                         {".flac", ".mp3"})


if __name__ == "__main__":
    unittest.main()
