#!/usr/bin/env python3
"""
tests/test_player_volume.py — RendererQueue volume behaviour.

Contract (2026-05-30 redesign):
  * At the FIRST track of a queue the renderer is set to a fixed
    STARTUP_VOLUME (22 on the Naim's 0–100 scale). We do NOT read the
    renderer's current volume first — a STOPPED Naim reports 0 via
    GetVolume, and adopting that as the baseline made every track play
    silent (the bug this redesign fixes).
  * Volume is NOT re-asserted per-track. A manual change on the Naim's
    own remote therefore sticks for the rest of the session.
  * Loudness gain is NOT applied (always 0) — every track plays at the
    same level.
  * The PWA slider (`set_user_trim_db`) is a relative trim around the
    startup baseline and fires SetVolume immediately so the change is
    audible mid-track.

Run standalone:
    python3 -m unittest tests.test_player_volume -v
"""
import os
import sys
import time
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_player import RendererQueue, STARTUP_VOLUME, GAIN_TO_VOLUME_RATIO


def _start_queue(tracks):
    """Start a queue with SOAP helpers mocked. Returns
    (queue, set_volume_calls, get_volume_calls, patches).

    SetVolume runs off-thread, so callers poll-wait for it to land."""
    set_volume_calls = []
    get_volume_calls = []

    def fake_set_volume(rc_url, level):
        set_volume_calls.append(level)
        return True

    def fake_get_volume(rc_url):
        get_volume_calls.append(rc_url)
        return 0          # a STOPPED Naim reports 0 — must NOT be used

    patches = [
        patch("dlna_avtransport.set_volume", side_effect=fake_set_volume),
        patch("dlna_avtransport.get_volume", side_effect=fake_get_volume),
        patch("dlna_avtransport.avtransport_send", return_value=True),
        patch("dlna_avtransport.avtransport_set_next_uri", return_value=True),
    ]
    for p in patches:
        p.start()

    q = RendererQueue()
    q.start("http://r/AVTransport", tracks, renderer_name="Naim",
            rc_url="http://r/Render")
    for _ in range(60):
        if set_volume_calls:
            break
        time.sleep(0.005)
    return q, set_volume_calls, get_volume_calls, patches


def _wait_for(calls, n, timeout=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline and len(calls) < n:
        time.sleep(0.005)


def _stop(patches, q):
    q._stop_event.set()
    for p in patches:
        p.stop()


class TestStartupVolume(unittest.TestCase):

    def test_first_track_sets_startup_volume(self):
        """First track → SetVolume(STARTUP_VOLUME), exactly once."""
        q, set_calls, get_calls, patches = _start_queue(
            [{"url": "a", "title": "A", "mime": ""}])
        try:
            self.assertEqual(set_calls, [STARTUP_VOLUME])
        finally:
            _stop(patches, q)

    def test_get_volume_is_never_called(self):
        """We must NOT read the renderer's volume — a stopped Naim
        reports 0 and adopting it silenced playback."""
        q, set_calls, get_calls, patches = _start_queue(
            [{"url": "a", "title": "A", "mime": ""}])
        try:
            self.assertEqual(get_calls, [],
                             "GetVolume must never be called")
        finally:
            _stop(patches, q)

    def test_next_track_does_not_reassert_volume(self):
        """Advancing to track 2 must NOT fire another SetVolume — a
        manual change on the Naim remote has to stick."""
        q, set_calls, get_calls, patches = _start_queue(
            [{"url": "a", "title": "A", "mime": ""},
             {"url": "b", "title": "B", "mime": ""}])
        try:
            self.assertEqual(set_calls, [STARTUP_VOLUME])
            q.next_track()
            time.sleep(0.1)
            self.assertEqual(set_calls, [STARTUP_VOLUME],
                             "volume must be set once per queue, not per track")
        finally:
            _stop(patches, q)

    def test_new_queue_resets_and_sets_startup_again(self):
        """A brand-new queue re-applies STARTUP_VOLUME (baseline reset)."""
        q, set_calls, _, patches = _start_queue(
            [{"url": "a", "title": "A", "mime": ""}])
        try:
            self.assertEqual(set_calls, [STARTUP_VOLUME])
            # Second queue on the same RendererQueue object.
            q.start("http://r/AVTransport",
                    [{"url": "b", "title": "B", "mime": ""}],
                    renderer_name="Naim", rc_url="http://r/Render")
            _wait_for(set_calls, 2)
            self.assertEqual(set_calls, [STARTUP_VOLUME, STARTUP_VOLUME])
        finally:
            _stop(patches, q)

    def test_no_rc_url_no_volume_calls(self):
        """A renderer without a RenderingControl URL → no SetVolume."""
        set_calls = []
        with patch("dlna_avtransport.set_volume",
                   side_effect=lambda u, l: set_calls.append(l)), \
             patch("dlna_avtransport.avtransport_send", return_value=True), \
             patch("dlna_avtransport.avtransport_set_next_uri",
                   return_value=True):
            q = RendererQueue()
            q.start("http://r/AV", [{"url": "a", "title": "A", "mime": ""}],
                    renderer_name="Naim", rc_url="")     # no RC
            time.sleep(0.1)
            q._stop_event.set()
        self.assertEqual(set_calls, [])


class TestUserTrim(unittest.TestCase):
    """The slider is a relative trim around STARTUP_VOLUME, applied
    immediately so the user hears it mid-track."""

    def test_trim_applies_around_startup_baseline(self):
        q, set_calls, _, patches = _start_queue(
            [{"url": "a", "title": "A", "mime": ""}])
        try:
            self.assertEqual(set_calls, [STARTUP_VOLUME])
            q.set_user_trim_db(+3.0)
            _wait_for(set_calls, 2)
            # STARTUP_VOLUME + round(3 * RATIO)
            self.assertEqual(set_calls[-1],
                             STARTUP_VOLUME + round(3.0 * GAIN_TO_VOLUME_RATIO))
        finally:
            _stop(patches, q)

    def test_trim_clamped_plus_minus_five_db(self):
        with patch("dlna_avtransport.set_volume", return_value=True):
            q = RendererQueue()
            q._rc_url = "http://r/Render"
            q._renderer_baseline = STARTUP_VOLUME
            q.set_user_trim_db(+99.0)
            self.assertEqual(q._user_trim_db, 5.0)
            q.set_user_trim_db(-99.0)
            self.assertEqual(q._user_trim_db, -5.0)

    def test_trim_before_first_play_is_deferred(self):
        """Moving the slider before baseline is known just stores it;
        the value is applied when the first track sets the baseline."""
        set_calls = []
        with patch("dlna_avtransport.set_volume",
                   side_effect=lambda u, l: set_calls.append(l) or True), \
             patch("dlna_avtransport.avtransport_send", return_value=True), \
             patch("dlna_avtransport.avtransport_set_next_uri",
                   return_value=True):
            q = RendererQueue()
            q._rc_url = "http://r/Render"          # rc known, but no queue yet
            q.set_user_trim_db(+2.0)               # baseline None → deferred
            self.assertEqual(set_calls, [],
                             "no SetVolume before the queue starts")
            self.assertEqual(q._user_trim_db, 2.0)
            q.start("http://r/AV", [{"url": "a", "title": "A", "mime": ""}],
                    renderer_name="Naim", rc_url="http://r/Render")
            _wait_for(set_calls, 1)
            q._stop_event.set()
            # New queue resets trim to 0 → plays at STARTUP_VOLUME.
            self.assertEqual(set_calls[-1], STARTUP_VOLUME)

    def test_trim_resets_on_new_queue(self):
        with patch("dlna_avtransport.set_volume", return_value=True), \
             patch("dlna_avtransport.avtransport_send", return_value=True), \
             patch("dlna_avtransport.avtransport_set_next_uri",
                   return_value=True):
            q = RendererQueue()
            q.start("http://r/AV", [{"url": "a", "title": "A", "mime": ""}],
                    renderer_name="Naim", rc_url="http://r/Render")
            q.set_user_trim_db(+4.0)
            self.assertEqual(q._user_trim_db, 4.0)
            q.start("http://r/AV", [{"url": "b", "title": "B", "mime": ""}],
                    renderer_name="Naim", rc_url="http://r/Render")
            self.assertEqual(q._user_trim_db, 0.0)
            q._stop_event.set()


class TestSetVolumeIsAsync(unittest.TestCase):
    """Regression guard (2026-05-05): a slow SetVolume must not block
    the queue's SetURI/Play."""

    def test_slow_set_volume_does_not_block_send(self):
        send_calls = []

        def slow_set_volume(rc_url, level):
            time.sleep(0.5)
            return True

        def fast_send(av_url, url, title, mime):
            send_calls.append(time.time())
            return True

        with patch("dlna_avtransport.set_volume", side_effect=slow_set_volume), \
             patch("dlna_avtransport.avtransport_send", side_effect=fast_send), \
             patch("dlna_avtransport.avtransport_set_next_uri",
                   return_value=True):
            q = RendererQueue()
            t0 = time.time()
            q.start("http://r/AV", [{"url": "a", "title": "A", "mime": ""}],
                    renderer_name="Naim", rc_url="http://r/Render")
            for _ in range(40):
                if send_calls:
                    break
                time.sleep(0.005)
            elapsed = (send_calls[0] if send_calls else time.time()) - t0
            q._stop_event.set()

        self.assertTrue(send_calls, "avtransport_send must have fired")
        self.assertLess(elapsed, 0.3,
                        f"send blocked {elapsed*1000:.0f} ms by slow SetVolume")


if __name__ == "__main__":
    unittest.main()
