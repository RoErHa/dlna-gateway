#!/usr/bin/env python3
"""
tests/test_player_volume.py — RendererQueue per-track loudness-gain integration.

Tests how RendererQueue applies the new per-track SetVolume just before
each SetURI/Play. The track's gain_db is looked up from the DB at play
time; this test patches that lookup and the SOAP helpers so we can
assert the exact SetVolume sequence without a renderer.

Run standalone:
    python3 -m unittest tests.test_player_volume -v
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dlna_player
from dlna_player import RendererQueue, GAIN_TO_VOLUME_RATIO


# Convenience: build a queue + start it with a stack of mocked SOAP helpers.
def _start_queue(tracks, gain_map=None, get_volume_returns=70):
    """Returns (queue, set_volume_calls, get_volume_calls).

    `gain_map` keys = track URL, value = gain_db. Missing URLs → 0.0.
    `get_volume_returns` = what the renderer reports as its current volume
    when GetVolume is called once on first play.
    """
    set_volume_calls = []
    get_volume_calls = []

    def fake_set_volume(rc_url, level):
        set_volume_calls.append(level)
        return True

    def fake_get_volume(rc_url):
        get_volume_calls.append(rc_url)
        return get_volume_returns

    def fake_avtransport_send(av_url, url, title, mime):
        return True

    def fake_gain_lookup(url):
        return (gain_map or {}).get(url, 0.0)

    q = RendererQueue()

    patches = [
        patch("dlna_content.set_volume", side_effect=fake_set_volume),
        patch("dlna_content.get_volume", side_effect=fake_get_volume),
        patch("dlna_content.avtransport_send", side_effect=fake_avtransport_send),
        patch("dlna_library.DB.gain_db_for_url", side_effect=fake_gain_lookup),
    ]
    for p in patches:
        p.start()

    try:
        q.start("http://r/AVTransport", tracks, renderer_name="Naim",
                rc_url="http://r/Render")
    finally:
        # Stop the monitor thread so the test exits clean
        q._cancel = MagicMock()  # prevent further side effects on teardown
        q._stop_event.set()
    # Caller is responsible for stopping patches via .stop() — return them.
    return q, set_volume_calls, get_volume_calls, patches


def _stop_patches(patches):
    for p in patches:
        p.stop()


class TestPerTrackGain(unittest.TestCase):

    def test_first_play_calls_get_volume_once_then_set_volume(self):
        """On the very first track, the queue reads the renderer's current
        volume (so it adopts whatever the user has set on the Naim's own
        remote as the reference), then sends SetVolume with the per-track
        adjustment."""
        tracks = [{"url": "http://t/0.flac", "title": "T0"}]
        q, set_calls, get_calls, patches = _start_queue(
            tracks, gain_map={"http://t/0.flac": 0.0},
            get_volume_returns=70)
        try:
            self.assertEqual(len(get_calls), 1,
                             "GetVolume must be called exactly once on first play")
            self.assertEqual(len(set_calls), 1,
                             "SetVolume must be called once per track")
            # gain=0 → SetVolume(70)
            self.assertEqual(set_calls[0], 70)
        finally:
            _stop_patches(patches)

    def test_subsequent_track_no_get_volume(self):
        """After the reference is read on track 1, track 2 should NOT call
        GetVolume again — we just adjust from the cached reference."""
        tracks = [{"url": "http://t/0.flac", "title": "T0"},
                  {"url": "http://t/1.flac", "title": "T1"}]
        q, set_calls, get_calls, patches = _start_queue(
            tracks, gain_map={"http://t/0.flac": 0.0,
                              "http://t/1.flac": 0.0},
            get_volume_returns=70)
        try:
            q.next_track()
            self.assertEqual(len(get_calls), 1,
                             "GetVolume must NOT be called for tracks 2+")
            self.assertEqual(len(set_calls), 2,
                             "SetVolume must fire once per track")
        finally:
            _stop_patches(patches)

    def test_positive_gain_boosts_volume(self):
        """Quiet track (measured -22 LUFS, gain +4 dB) → SetVolume +8 above ref."""
        tracks = [{"url": "http://t/0.flac", "title": "T0"}]
        q, set_calls, _, patches = _start_queue(
            tracks, gain_map={"http://t/0.flac": +4.0},
            get_volume_returns=70)
        try:
            expected = 70 + round(4.0 * GAIN_TO_VOLUME_RATIO)
            self.assertEqual(set_calls[0], expected)
        finally:
            _stop_patches(patches)

    def test_negative_gain_attenuates_volume(self):
        """Loud track (-10 LUFS, gain -8 dB) → SetVolume -16 below ref."""
        tracks = [{"url": "http://t/0.flac", "title": "T0"}]
        q, set_calls, _, patches = _start_queue(
            tracks, gain_map={"http://t/0.flac": -8.0},
            get_volume_returns=70)
        try:
            expected = 70 + round(-8.0 * GAIN_TO_VOLUME_RATIO)
            self.assertEqual(set_calls[0], expected)
        finally:
            _stop_patches(patches)

    def test_clamps_at_100(self):
        """Reference 95 + boost 10 dB would be 95+20=115 → clamped to 100."""
        tracks = [{"url": "http://t/0.flac", "title": "T0"}]
        q, set_calls, _, patches = _start_queue(
            tracks, gain_map={"http://t/0.flac": +10.0},
            get_volume_returns=95)
        try:
            self.assertEqual(set_calls[0], 100)
        finally:
            _stop_patches(patches)

    def test_clamps_at_zero(self):
        tracks = [{"url": "http://t/0.flac", "title": "T0"}]
        q, set_calls, _, patches = _start_queue(
            tracks, gain_map={"http://t/0.flac": -20.0},
            get_volume_returns=10)
        try:
            self.assertEqual(set_calls[0], 0)
        finally:
            _stop_patches(patches)

    def test_no_gain_row_is_noop(self):
        """Track without an analysis row uses the reference volume unchanged."""
        tracks = [{"url": "http://t/unknown.flac", "title": "Unknown"}]
        q, set_calls, _, patches = _start_queue(
            tracks, gain_map={},   # nothing → gain_db_for_url returns 0
            get_volume_returns=70)
        try:
            self.assertEqual(set_calls[0], 70)
        finally:
            _stop_patches(patches)


class TestSetUserVolume(unittest.TestCase):

    def test_set_user_volume_updates_reference_and_calls_renderer(self):
        """When the user moves the gateway volume slider on a UPnP output,
        we (1) push that as the new reference for future tracks, and
        (2) SetVolume the renderer immediately so the user hears the change."""
        set_calls = []

        def fake_set_volume(rc_url, level):
            set_calls.append(level)
            return True

        with patch("dlna_content.set_volume", side_effect=fake_set_volume):
            q = RendererQueue()
            q._rc_url = "http://r/Render"   # simulate post-discovery
            q.set_user_volume(55)

        self.assertEqual(set_calls, [55])
        self.assertEqual(q._user_volume, 55)

    def test_set_user_volume_changes_reference_for_next_track(self):
        """After set_user_volume(80), the next track's per-track gain is
        computed from 80, not from whatever was first read via GetVolume."""
        set_calls = []
        get_calls = []

        def fake_set_volume(rc_url, level):
            set_calls.append(level); return True
        def fake_get_volume(rc_url):
            get_calls.append(rc_url); return 50
        def fake_send(av_url, url, title, mime):
            return True

        gain_map = {"http://t/0.flac": 0.0, "http://t/1.flac": 0.0}

        with patch("dlna_content.set_volume", side_effect=fake_set_volume), \
             patch("dlna_content.get_volume", side_effect=fake_get_volume), \
             patch("dlna_content.avtransport_send", side_effect=fake_send), \
             patch("dlna_library.DB.gain_db_for_url",
                   side_effect=lambda u: gain_map.get(u, 0.0)):

            q = RendererQueue()
            q.start("http://r/AV",
                    [{"url": "http://t/0.flac", "title": "T0"},
                     {"url": "http://t/1.flac", "title": "T1"}],
                    renderer_name="Naim", rc_url="http://r/Render")

            # Track 1 used reference=50 from GetVolume → SetVolume(50)
            self.assertEqual(set_calls[-1], 50)

            # User bumps to 80
            q.set_user_volume(80)
            self.assertEqual(set_calls[-1], 80)

            # Next track now uses 80 as the reference
            q.next_track()
            self.assertEqual(set_calls[-1], 80)

            q._stop_event.set()


if __name__ == "__main__":
    unittest.main()
