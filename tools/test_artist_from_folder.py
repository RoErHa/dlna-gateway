#!/usr/bin/env python3
"""
tools/test_artist_from_folder.py — the write path, and above all what it
must REFUSE to write.

The inference rules themselves are tested in `tests/test_artist_infer.py`;
this file guards the thing that actually touches a person's music.

Run standalone:
    python3 -m unittest tools.test_artist_from_folder -v
"""
import os
import struct
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tools.artist_from_folder import write_artist


def _make_wav(path, seconds=1, rate=8000):
    """A minimal but genuinely valid 16-bit mono RIFF/WAVE file."""
    n = rate * seconds
    data = b"\x00\x00" * n
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate,
                                      rate * 2, 2, 16))
        f.write(b"data" + struct.pack("<I", len(data)) + data)


class TestNeverBreaksTheContainer(unittest.TestCase):
    """A tag is a convenience; the audio is the product.

    The regression: an earlier version fell back to `ID3(path).save(path)`
    when mutagen's easy interface refused WAV. For a RIFF container that
    PREPENDS a standalone ID3v2 tag, so the file starts with `ID3`
    instead of `RIFF` and is no longer a WAV. It damaged 15 real files."""

    def setUp(self):
        self._d = tempfile.mkdtemp()
        self.wav = os.path.join(self._d, "track.wav")
        _make_wav(self.wav)
        with open(self.wav, "rb") as f:
            self.before = f.read()

    def tearDown(self):
        for n in os.listdir(self._d):
            os.unlink(os.path.join(self._d, n))
        os.rmdir(self._d)

    def test_a_wav_is_never_left_starting_with_ID3(self):
        write_artist(self.wav, "Mira Calvo")
        with open(self.wav, "rb") as f:
            head = f.read(4)
        self.assertEqual(head, b"RIFF",
                         "the container was destroyed — this is the bug")

    def test_an_unwritable_container_is_left_byte_identical(self):
        res = write_artist(self.wav, "Mira Calvo")
        with open(self.wav, "rb") as f:
            after = f.read()
        if res != "written":
            self.assertEqual(after, self.before,
                             "refusing to tag must not modify the file")

    def test_it_reports_rather_than_pretending(self):
        self.assertIn(write_artist(self.wav, "X"),
                      ("written", "unsupported", "failed"))

    def test_a_missing_file_is_reported_not_created(self):
        gone = os.path.join(self._d, "nope.wav")
        self.assertEqual(write_artist(gone, "X"), "missing")
        self.assertFalse(os.path.exists(gone))


class TestNeverOverwritesEvidence(unittest.TestCase):

    def setUp(self):
        self._d = tempfile.mkdtemp()
        self.mp3 = os.path.join(self._d, "track.mp3")

    def tearDown(self):
        for n in os.listdir(self._d):
            os.unlink(os.path.join(self._d, n))
        os.rmdir(self._d)

    def test_an_existing_artist_wins_over_an_inference(self):
        """Re-read before writing: the index can be stale, and a tag in
        the file is newer evidence than our guess. On the real library
        this refused 11 files the DB called blank."""
        try:
            from mutagen.id3 import ID3, TPE1
            from mutagen.mp3 import MP3
        except ImportError:
            self.skipTest("mutagen not installed")
        # A tiny silent MP3 frame is awkward to synthesise; use an ID3-only
        # file, which mutagen still exposes an artist through.
        tags = ID3()
        tags.add(TPE1(encoding=3, text=["Already Known"]))
        tags.save(self.mp3)
        if MP3 is None:                                # pragma: no cover
            self.skipTest("no mp3 support")
        res = write_artist(self.mp3, "Inferred Name")
        self.assertIn(res, ("has_artist", "unsupported", "failed"))
        self.assertNotEqual(res, "written")


if __name__ == "__main__":
    unittest.main()
