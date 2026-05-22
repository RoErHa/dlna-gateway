#!/usr/bin/env python3
"""
tests/test_player.py — Unit tests for dlna_player.

Run standalone:
    python3 -m unittest tests.test_player -v
    python3 tests/test_player.py

No network, no gateway. All SOAP calls are mocked. Fast (<1s).
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dlna_player
from dlna_player import (_dur_to_sec, _monitor_decision,
                         QueueRegistry, RendererQueue, WATCHDOG_GRACE_SEC)


# ── _dur_to_sec — regression guard for today's ValueError bug ─────

class TestDurToSec(unittest.TestCase):
    """The library DB stores duration as TEXT in 'H:MM:SS.fff' format.
    Before the fix, int(dur) raised ValueError and killed the daemon
    thread before SetURI ever fired. This test locks in the parser."""

    def test_upnp_hms_strings(self):
        # These are exactly the strings that appeared in /tmp/dlna-gateway.err
        self.assertEqual(_dur_to_sec("0:04:51.000"), 291)
        self.assertEqual(_dur_to_sec("0:04:04.000"), 244)
        self.assertEqual(_dur_to_sec("0:04:13.000"), 253)
        self.assertEqual(_dur_to_sec("0:07:07.000"), 427)

    def test_mm_ss(self):
        self.assertEqual(_dur_to_sec("3:45"), 225)
        self.assertEqual(_dur_to_sec("0:30"), 30)

    def test_hms_no_fractional(self):
        self.assertEqual(_dur_to_sec("12:34:56"), 12*3600 + 34*60 + 56)
        self.assertEqual(_dur_to_sec("1:00:00"),  3600)

    def test_numeric_types(self):
        self.assertEqual(_dur_to_sec(0), 0)
        self.assertEqual(_dur_to_sec(42), 42)
        self.assertEqual(_dur_to_sec(42.7), 42)

    def test_empty_and_none(self):
        self.assertEqual(_dur_to_sec(""), 0)
        self.assertEqual(_dur_to_sec("   "), 0)
        self.assertEqual(_dur_to_sec(None), 0)

    def test_malformed_never_raises(self):
        # Core guarantee: unparseable input becomes 0, never an exception.
        # If this breaks, the daemon thread dies silently again.
        for bad in ("abc", "not:a:duration", ":::", "x:y:z", "--", "0:bad"):
            with self.subTest(bad=bad):
                self.assertEqual(_dur_to_sec(bad), 0)


# ── QueueRegistry — per-renderer queue ownership ──────────────────

class TestQueueRegistry(unittest.TestCase):

    def test_get_is_lazy_and_idempotent(self):
        reg = QueueRegistry()
        self.assertEqual(reg.snapshot_all(), {})
        q1 = reg.get("uuid:a")
        q2 = reg.get("uuid:a")
        self.assertIs(q1, q2, "get() must return the same queue for same UDN")

    def test_each_udn_gets_its_own_queue(self):
        reg = QueueRegistry()
        qa = reg.get("uuid:a")
        qb = reg.get("uuid:b")
        self.assertIsNot(qa, qb, "different UDNs must have different queues")

    def test_peek_does_not_create(self):
        reg = QueueRegistry()
        self.assertIsNone(reg.peek("uuid:ghost"))
        self.assertEqual(reg.snapshot_all(), {})

    def test_is_busy_false_for_never_touched_udn(self):
        reg = QueueRegistry()
        self.assertFalse(reg.is_busy("uuid:never"))
        self.assertEqual(reg.snapshot_all(), {},
                         "is_busy must NOT allocate a queue for an unknown UDN")

    def test_is_busy_reflects_queue_alive_state(self):
        reg = QueueRegistry()
        q = reg.get("uuid:a")
        # A fresh queue with no tracks → snapshot().alive=False → not busy
        self.assertFalse(reg.is_busy("uuid:a"))
        # Simulate a live queue: inject state so snapshot() returns alive=True
        with patch.object(q, "snapshot",
                          return_value={"alive": True, "title": "Song",
                                         "artist": "Artist", "renderer": "R"}):
            self.assertTrue(reg.is_busy("uuid:a"))

    def test_snapshot_all_returns_all_queues(self):
        reg = QueueRegistry()
        reg.get("uuid:a")
        reg.get("uuid:b")
        snaps = reg.snapshot_all()
        self.assertEqual(set(snaps.keys()), {"uuid:a", "uuid:b"})


# ── RendererQueue — end-to-end regression for the duration bug ────

class TestRendererQueueDurationSafety(unittest.TestCase):
    """Direct regression for today's bug: RendererQueue.start() must NOT
    raise when a track carries a 'H:MM:SS' duration string. Before the
    fix this killed the daemon thread before SetURI was ever sent."""

    def _make_track(self, duration):
        return {
            "url":      "http://127.0.0.1:1/song.flac",
            "title":    "Some Song",
            "artist":   "Some Artist",
            "album":    "Some Album",
            "mime":     "audio/flac",
            "duration": duration,
        }

    def test_start_with_hms_duration_does_not_raise(self):
        q = RendererQueue()
        # Mock BOTH the initial stop and the SetURI/Play so no network
        # traffic happens. We only care that _send_current() completes
        # without raising.
        with patch("dlna_content.avtransport_stop", return_value=True), \
             patch("dlna_content.avtransport_send", return_value=True), \
             patch("dlna_content.avtransport_probe_state", return_value=("STOPPED", "")):
            try:
                q.start("http://fake-av-url/ctrl",
                        [self._make_track("0:04:51.000")],
                        "TestRenderer")
            finally:
                q._cancel()

    def test_start_with_varied_duration_formats(self):
        q = RendererQueue()
        tracks = [
            self._make_track("0:04:51.000"),
            self._make_track("3:45"),
            self._make_track(""),
            self._make_track(None),
            self._make_track(42),
            self._make_track("malformed"),
        ]
        with patch("dlna_content.avtransport_stop", return_value=True), \
             patch("dlna_content.avtransport_send", return_value=True) as send, \
             patch("dlna_content.avtransport_probe_state", return_value=("STOPPED", "")):
            try:
                q.start("http://fake-av-url/ctrl", tracks, "TestRenderer")
            finally:
                q._cancel()
        # _send_current fired for the first track; the monitor thread
        # may have advanced further but that's not what we're asserting.
        self.assertGreaterEqual(send.call_count, 1,
                                "SetURI/Play must be attempted at least once")


# ── _monitor_decision — stall-guard regression (2026-05-20) ───────

class TestMonitorDecision(unittest.TestCase):
    """Regression for the 'Starman stuck 36 minutes' incident: the
    renderer went STOPPED → UNKNOWN mid-track and the monitor, which
    only advanced on PLAYING → STOPPED, stalled on one track forever.
    The watchdog must advance on a duration-based timeout once the
    renderer stops reporting a usable state."""

    GRACE = WATCHDOG_GRACE_SEC

    def test_normal_finish(self):
        advance, reason = _monitor_decision("PLAYING", "STOPPED", 250, 242)
        self.assertEqual((advance, reason), (True, "finished"))

    def test_finish_from_transitioning(self):
        advance, reason = _monitor_decision(
            "TRANSITIONING", "NO_MEDIA_PRESENT", 250, 242)
        self.assertTrue(advance)
        self.assertEqual(reason, "finished")

    def test_playing_does_not_advance(self):
        advance, _ = _monitor_decision("PLAYING", "PLAYING", 30, 242)
        self.assertFalse(advance)

    def test_unknown_before_duration_holds(self):
        # Renderer unreachable but the track hasn't run past its
        # duration yet — a transient blip, do NOT skip.
        advance, _ = _monitor_decision("PLAYING", "UNKNOWN", 100, 242)
        self.assertFalse(advance)

    def test_watchdog_fires_when_stuck_unknown_past_duration(self):
        # The exact incident: state UNKNOWN, well past duration+grace.
        advance, reason = _monitor_decision(
            "UNKNOWN", "UNKNOWN", 242 + self.GRACE + 10, 242)
        self.assertTrue(advance)
        self.assertEqual(reason, "watchdog")

    def test_watchdog_fires_when_wedged_in_stopped(self):
        # Renderer never reached PLAYING and sits in STOPPED forever.
        advance, reason = _monitor_decision(
            "STOPPED", "STOPPED", 242 + self.GRACE + 10, 242)
        self.assertTrue(advance)
        self.assertEqual(reason, "watchdog")

    def test_watchdog_does_not_skip_a_paused_queue(self):
        # A deliberately paused track may sit far past its duration —
        # never skip it.
        advance, _ = _monitor_decision(
            "PAUSED_PLAYBACK", "PAUSED_PLAYBACK",
            242 + self.GRACE + 999, 242)
        self.assertFalse(advance)

    def test_watchdog_inert_without_a_duration(self):
        # No duration → watchdog can't time out; the UNKNOWN-abort
        # guard in _monitor handles this case instead.
        advance, _ = _monitor_decision("UNKNOWN", "UNKNOWN", 99999, 0)
        self.assertFalse(advance)

    def test_long_playing_track_never_watchdogged(self):
        # A track still genuinely PLAYING is excluded even if its DB
        # duration metadata is wrong/short.
        advance, _ = _monitor_decision("PLAYING", "PLAYING", 99999, 60)
        self.assertFalse(advance)


# ── RendererQueue — send-failure abort logic ──────────────────────

class TestRendererQueueSendFailure(unittest.TestCase):
    """When a renderer is wedged (SOAP returns False forever), the queue
    must abort after _MAX_CONSECUTIVE_FAILS instead of chewing through
    every track silently."""

    def test_aborts_after_max_consecutive_send_failures(self):
        q = RendererQueue()
        tracks = [
            {"url": f"http://127.0.0.1:1/t{i}", "title": f"T{i}",
             "artist": "A", "album": "B", "mime": "audio/flac",
             "duration": "0:03:00.000"}
            for i in range(20)
        ]
        with patch("dlna_content.avtransport_stop", return_value=True), \
             patch("dlna_content.avtransport_send", return_value=False) as send, \
             patch("dlna_content.avtransport_probe_state", return_value=("STOPPED", "")):
            try:
                q.start("http://fake-av-url/ctrl", tracks, "Wedged")
            finally:
                q._cancel()
        # With every send failing, the queue must NOT have attempted
        # every one of the 20 tracks — it should abort at _MAX_CONSECUTIVE_FAILS.
        self.assertLessEqual(send.call_count,
                             RendererQueue._MAX_CONSECUTIVE_FAILS,
                             f"Aborted too late: {send.call_count} sends "
                             f"(limit {RendererQueue._MAX_CONSECUTIVE_FAILS})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
