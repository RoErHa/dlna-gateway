#!/usr/bin/env python3
"""
tests/test_player_volume.py — RendererQueue per-track loudness-gain
+ user-trim integration.

The user's volume slider is a *relative trim* (-5..+5 dB around the
renderer's current absolute volume), NOT an absolute level. The track's
effective renderer-volume on each play is:

    baseline + round((loudness_gain_db + user_trim_db) * RATIO)
    └ clamped 0..100 ──────────────────────────────────────────┘

Where:
  * baseline = the renderer's volume when the queue first started
    (read via GetVolume on first play — adopts whatever the user has
    set on the Naim's own remote).
  * loudness_gain_db = per-track replay-gain from track_loudness.
  * user_trim_db    = the slider's offset, default 0.

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
    """Returns (queue, set_volume_calls, get_volume_calls, patches).

    Note: SetVolume now runs in a daemon thread (non-blocking for the
    queue / HTTP handler). Tests wait briefly for set_calls to populate.

    `gain_map` keys = track URL, value = gain_db. Missing URLs → 0.0.
    `get_volume_returns` = what the renderer reports as its current volume
    when GetVolume is called once on first play.
    """
    import time as _t
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
    # SetVolume is dispatched off-thread; wait briefly for it to land.
    for _ in range(40):
        if set_volume_calls: break
        _t.sleep(0.005)
    return q, set_volume_calls, get_volume_calls, patches


def _wait_for_set_calls(set_calls, count: int, timeout: float = 0.2):
    """Poll-wait helper for tests that fire additional SetVolume calls
    after the queue is started (e.g. set_user_trim_db, next_track)."""
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline and len(set_calls) < count:
        _t.sleep(0.005)


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
            _wait_for_set_calls(set_calls, 2)
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


class TestUserTrim(unittest.TestCase):
    """User-trim semantics: the slider is a relative offset, not an
    absolute level. Default 0 dB → renderer plays at its natural
    baseline. ±5 dB clamp prevents accidental ear damage from the user
    flicking the slider to the rail."""

    def test_default_trim_is_zero(self):
        """A fresh queue starts with trim=0, so the first track plays
        at baseline + loudness_gain — never at the slider's rail."""
        tracks = [{"url": "http://t/0.flac", "title": "T0"}]
        q, set_calls, _, patches = _start_queue(
            tracks, gain_map={"http://t/0.flac": 0.0},
            get_volume_returns=70)
        try:
            self.assertEqual(q._user_trim_db, 0.0)
            self.assertEqual(set_calls[0], 70,
                             "trim=0 + gain=0 → baseline only")
        finally:
            _stop_patches(patches)

    def test_set_user_trim_db_immediate_call(self):
        """Moving the slider mid-queue must SetVolume on the renderer
        so the user hears the change — not wait for the next track.
        SetVolume runs in a daemon thread (so the HTTP handler returns
        instantly even if Naim is slow), but the test waits briefly
        so the assertion sees the call."""
        import time as _t
        set_calls = []

        def fake_set_volume(rc_url, level):
            set_calls.append(level); return True

        with patch("dlna_content.set_volume", side_effect=fake_set_volume):
            q = RendererQueue()
            q._rc_url             = "http://r/Render"
            q._renderer_baseline  = 50
            q.set_user_trim_db(2.0)
            # set_user_trim_db dispatches to a worker thread; give it
            # a beat to land. 100 ms is generous for an in-process call.
            for _ in range(20):
                if set_calls: break
                _t.sleep(0.005)
            # baseline 50 + (0 gain + 2 dB trim) * RATIO=2 → 54
            self.assertEqual(set_calls, [54])
            self.assertEqual(q._user_trim_db, 2.0)

    def test_trim_clamped_to_plus_minus_five_db(self):
        """User can flick the slider, but ±5 dB max — protects ears."""
        with patch("dlna_content.set_volume", return_value=True):
            q = RendererQueue()
            q._rc_url            = "http://r/Render"
            q._renderer_baseline = 50
            q.set_user_trim_db(+99.0)
            self.assertEqual(q._user_trim_db, 5.0)
            q.set_user_trim_db(-99.0)
            self.assertEqual(q._user_trim_db, -5.0)

    def test_trim_persists_across_tracks(self):
        """Moving the trim once should affect every subsequent track —
        it's session state, not a one-shot."""
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

            # First track played at baseline=50 + gain=0 + trim=0 → 50
            _wait_for_set_calls(set_calls, 1)
            self.assertEqual(set_calls[-1], 50)

            # User trims to +3 dB
            q.set_user_trim_db(+3.0)
            _wait_for_set_calls(set_calls, 2)
            # Immediate SetVolume: 50 + (0 + 3) * 2 = 56
            self.assertEqual(set_calls[-1], 56)

            # Next track also at baseline=50 + (gain=0 + trim=3) * 2 = 56
            q.next_track()
            _wait_for_set_calls(set_calls, 3)
            self.assertEqual(set_calls[-1], 56)

    def test_trim_resets_on_new_queue(self):
        """Each new queue should start with trim = 0 — yesterday's
        trim isn't carried into today's session."""
        with patch("dlna_content.set_volume", return_value=True), \
             patch("dlna_content.get_volume", return_value=50), \
             patch("dlna_content.avtransport_send", return_value=True), \
             patch("dlna_library.DB.gain_db_for_url", return_value=0.0):

            q = RendererQueue()
            q.start("http://r/AV", [{"url": "http://t/0", "title": "T0"}],
                    renderer_name="Naim", rc_url="http://r/Render")
            q.set_user_trim_db(+4.0)
            self.assertEqual(q._user_trim_db, 4.0)

            # New queue → trim resets
            q.start("http://r/AV", [{"url": "http://t/1", "title": "T1"}],
                    renderer_name="Naim", rc_url="http://r/Render")
            self.assertEqual(q._user_trim_db, 0.0)


class TestEffectiveLevel(unittest.TestCase):
    """The interaction between baseline / loudness gain / user trim."""

    def test_loudness_gain_and_trim_compose_additively(self):
        """A track with gain=-4 dB + user trim of +2 dB →
        net -2 dB → SetVolume(baseline - 4 in renderer units)."""
        tracks = [{"url": "http://t/0.flac", "title": "T0"}]
        q, set_calls, _, patches = _start_queue(
            tracks, gain_map={"http://t/0.flac": -4.0},
            get_volume_returns=70)
        try:
            # Without trim: 70 + round(-4 * 2) = 70 - 8 = 62
            self.assertEqual(set_calls[0], 62)
            # Now add trim: 70 + round((-4 + 2) * 2) = 70 - 4 = 66
            q.set_user_trim_db(+2.0)
            _wait_for_set_calls(set_calls, 2)
            self.assertEqual(set_calls[-1], 66)
        finally:
            _stop_patches(patches)


class TestSetVolumeIsAsync(unittest.TestCase):
    """Regression guard for the playout-freeze bug (2026-05-05): if
    SetVolume blocks (Naim's SOAP slow because GetTransportInfo poll is
    in flight), the queue must NOT block — `_send_current` must call
    `avtransport_send` regardless of how long SetVolume takes."""

    def test_slow_set_volume_does_not_block_avtransport_send(self):
        import time as _t
        # SetVolume sleeps for half a second; avtransport_send must
        # still fire well before that.
        send_calls = []
        set_volume_started = []

        def slow_set_volume(rc_url, level):
            set_volume_started.append(_t.time())
            _t.sleep(0.5)
            return True

        def fast_send(av_url, url, title, mime):
            send_calls.append(_t.time())
            return True

        with patch("dlna_content.set_volume", side_effect=slow_set_volume), \
             patch("dlna_content.get_volume", return_value=50), \
             patch("dlna_content.avtransport_send", side_effect=fast_send), \
             patch("dlna_library.DB.gain_db_for_url", return_value=0.0):

            q = RendererQueue()
            t0 = _t.time()
            q.start("http://r/AV", [{"url": "http://t/0", "title": "T0"}],
                    renderer_name="Naim", rc_url="http://r/Render")
            # Wait for avtransport_send to fire — must NOT take 500 ms
            for _ in range(40):
                if send_calls: break
                _t.sleep(0.005)
            elapsed = (send_calls[0] if send_calls else _t.time()) - t0
            q._stop_event.set()

        self.assertTrue(send_calls, "avtransport_send must have fired")
        self.assertLess(elapsed, 0.3, (
            f"avtransport_send was blocked by slow SetVolume for "
            f"{elapsed*1000:.0f} ms — the queue's _send_current must "
            f"NOT wait on SetVolume."))


if __name__ == "__main__":
    unittest.main()
