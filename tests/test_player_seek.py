#!/usr/bin/env python3
"""
tests/test_player_seek.py — seek on a renderer, and the `state` events and
trim a client needs to follow it (2.4.0, docs/gateway-contract-renderers.md).

Two layers: RendererQueue against tests/fake_renderer.py (real SOAP over
loopback, so a Seek is really sent), and the /api/control handler on doubles.
"""
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (PROJECT, os.path.join(PROJECT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import api_playback
import api_playback_state
import dlna_events
from dlna_player import RendererQueue
from dlna_player_policy import SEEK_END_GUARD_SEC, seek_target
from dlna_player_registry import QueueRegistry
from fake_renderer import FakeRenderer


class _FastEvent(threading.Event):
    """The monitor's waits, shortened so a test does not take its 4 s."""
    def wait(self, timeout=None):
        return super().wait(min(timeout, 0.05) if timeout else timeout)


def _tracks(n=3, **extra):
    return [{"url": f"http://music.invalid/{i}.flac", "title": f"T{i}",
             "artist": "A", "album": "L", "mime": "audio/flac",
             "duration": "0:05:00", **extra} for i in range(1, n + 1)]


class _WithFake(unittest.TestCase):
    track_sec = 300.0

    def setUp(self):
        self.fake = FakeRenderer(track_sec=self.track_sec).start()
        self.addCleanup(self.fake.stop)
        base = f"http://127.0.0.1:{self.fake._httpd.server_port}"
        self.av, self.rc = base + "/AVTransport/control", base + "/RenderingControl/control"
        self.q = RendererQueue("uuid:r")
        self.q._stop_event = _FastEvent()
        self.addCleanup(self.q._cancel)
        pub = patch.object(dlna_events.EVENTS, "publish")
        self.pub = pub.start()
        self.addCleanup(pub.stop)

    def play(self, tracks=None):
        self.q.start(self.av, tracks or _tracks(), "Fake", rc_url=self.rc)

    def events(self):
        return [c.args[0] for c in self.pub.call_args_list
                if c.args[0].get("type") == "state"]

    def until(self, what, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if what():
                return True
            time.sleep(0.02)
        return False

    def at(self):
        with self.fake._lock:
            return self.fake._position(time.monotonic())


class TestSeekTarget(unittest.TestCase):
    def test_relative_and_absolute(self):
        self.assertEqual(seek_target(100, 30, True, 300), 130)
        self.assertEqual(seek_target(100, -30, True, 300), 70)
        self.assertEqual(seek_target(100, 12, False, 300), 12)

    def test_never_before_the_start(self):
        self.assertEqual(seek_target(10, -30, True, 300), 0)
        self.assertEqual(seek_target(0, -5, False, 300), 0)

    def test_never_past_the_end(self):
        """+30 near the end plays the last seconds; it does not skip."""
        end = 300 - SEEK_END_GUARD_SEC
        self.assertEqual(seek_target(290, 30, True, 300), end)
        self.assertEqual(seek_target(0, 9999, False, 300), end)
        self.assertEqual(seek_target(0, 30, True, 1), 0)

    def test_unknown_duration_is_not_a_limit(self):
        self.assertEqual(seek_target(1000, 30, True, 0), 1030)


class TestSeek(_WithFake):
    def test_absolute(self):
        self.play()
        self.assertEqual(self.q.seek(120, relative=False), ("ok", 120))
        self.assertAlmostEqual(self.at(), 120, delta=2)

    def test_relative_is_from_where_it_is_now(self):
        self.play()
        self.q.seek(100, relative=False)
        self.q._seek_mark = None                 # as if long ago
        code, target = self.q.seek(30, relative=True)
        self.assertEqual(code, "ok")
        self.assertAlmostEqual(target, 130, delta=2)
        self.assertAlmostEqual(self.at(), 130, delta=2)

    def test_quick_presses_add_up_while_the_position_is_unknown(self):
        """After a Seek the Naim reports 0:00:00 (= None) while it fetches.
        Taking that as 0 would throw the second +30 back to 0:30."""
        self.play()
        self.q.seek(100, relative=False)
        with patch("dlna_avtransport.avtransport_get_position",
                   return_value={"position": None, "duration": 300.0}):
            self.q.seek(30, relative=True)
            _, target = self.q.seek(30, relative=True)
        self.assertAlmostEqual(target, 160, delta=2)

    def test_unknown_position_without_a_recent_seek_is_refused(self):
        self.play()
        self.q._started_at = time.monotonic() - 60
        with patch("dlna_avtransport.avtransport_get_position",
                   return_value={"position": None, "duration": None}):
            self.assertEqual(self.q.seek(30, relative=True)[0], "not_seekable")
        self.assertEqual(self.fake.calls["Seek"], 0)

    def test_the_start_of_a_track_counts_as_zero(self):
        self.play()
        code, target = self.q.seek(30, relative=True)
        self.assertEqual(code, "ok")
        self.assertAlmostEqual(target, 30, delta=2)

    def test_a_seek_is_forgotten_on_the_next_track(self):
        self.play()
        self.q.seek(200, relative=False)
        self.q.next_track()
        _, target = self.q.seek(30, relative=True)
        self.assertAlmostEqual(target, 30, delta=2)

    def test_a_seek_is_forgotten_by_a_new_queue(self):
        """Same queue position, other track: 200 must not be its base."""
        self.play()
        self.q.seek(200, relative=False)
        self.play()
        _, target = self.q.seek(30, relative=True)
        self.assertAlmostEqual(target, 30, delta=2)

    def test_paused_stays_paused(self):
        self.play()
        self.q.pause()
        self.assertEqual(self.q.seek(60, relative=False)[0], "ok")
        self.assertEqual(self.fake.state, "PAUSED_PLAYBACK")

    def test_refused_when_nothing_of_ours_plays(self):
        self.assertEqual(self.q.seek(10, relative=False)[0], "not_seekable")
        self.play()
        self.q.stop()
        self.assertEqual(self.q.seek(10, relative=False)[0], "not_seekable")
        self.assertEqual(self.fake.calls["Seek"], 0)

    def test_a_queue_that_is_over_does_not_seek_someone_elses_music(self):
        """Our queue ended; the renderer plays something from its own app."""
        self.play()
        self.q._cancel()
        self.fake.handle("/AVTransport/control", "Play", "")
        self.assertEqual(self.q.seek(10, relative=False)[0], "not_seekable")
        self.assertEqual(self.fake.calls["Seek"], 0)

    def test_radio_is_refused(self):
        self.play(_tracks(1, is_stream=True))
        self.assertEqual(self.q.seek(10, relative=True)[0], "not_seekable")
        self.assertEqual(self.fake.calls["Seek"], 0)

    def test_transitioning_is_refused(self):
        """A Seek before PLAYING is accepted and dropped (Naim, 2026-11-09)."""
        self.play()
        with patch("dlna_avtransport.avtransport_probe_state",
                   return_value=("TRANSITIONING", "")):
            self.assertEqual(self.q.seek(10, relative=False)[0], "not_seekable")

    def test_renderer_refusal_is_reported(self):
        self.play()
        with patch("dlna_avtransport.avtransport_seek", return_value=False):
            self.assertEqual(self.q.seek(10, relative=False), ("refused", None))

    def test_a_seek_wins_over_a_pending_resume(self):
        self.play()
        self.q._resume_token = object()
        self.q.seek(10, relative=False)
        self.assertIsNone(self.q._resume_token)

    def test_a_failed_seek_leaves_a_pending_resume_alone(self):
        self.play()
        token = self.q._resume_token = object()
        with patch("dlna_avtransport.avtransport_seek", return_value=False):
            self.q.seek(10, relative=False)
        self.assertIs(self.q._resume_token, token)

    def test_the_watchdog_clock_follows(self):
        """Rewinding must not make the track look overdue."""
        self.play()
        self.q._started_at = time.monotonic() - 290
        self.q.seek(10, relative=False)
        self.assertAlmostEqual(time.monotonic() - self.q._started_at, 10, delta=2)
        self.q.seek(10 ** 9, relative=False)
        self.assertGreater(self.q._started_at, 0)


class TestStateEvents(_WithFake):
    track_sec = 0.6

    def test_every_event_names_the_renderer(self):
        self.play()
        self.q.pause()
        self.q.set_user_trim_db(1)
        self.q.stop()
        self.assertGreaterEqual(len(self.events()), 4)
        self.assertEqual({e.get("udn") for e in self.events()}, {"uuid:r"})

    def _count(self, action):
        n = len(self.events())
        action()
        return len(self.events()) - n

    def test_pause_trim_and_seek_send_one(self):
        with patch.object(self.q, "_monitor"):       # only the calls themselves
            self.fake.track_sec = 300.0
            self.play()
            self.assertEqual(self._count(self.q.pause), 1)
            self.assertEqual(self._count(self.q.pause), 1)
            self.assertEqual(self._count(lambda: self.q.set_user_trim_db(2)), 1)
            self.assertEqual(self._count(lambda: self.q.seek(5, relative=False)), 1)

    def test_a_pause_on_the_device_is_noticed(self):
        self.fake.track_sec = 300.0
        self.play()
        self.assertTrue(self.until(lambda: len(self.events()) >= 2))
        n = len(self.events())
        self.fake.handle("/AVTransport/control", "Pause", "")
        self.assertTrue(self.until(lambda: len(self.events()) > n))

    def test_a_gapless_change_and_the_end_are_noticed(self):
        self.play(_tracks(2))
        self.assertTrue(self.until(lambda: self.q._index == 1))
        n = len(self.events())
        self.assertGreaterEqual(n, 2)
        self.assertTrue(self.until(self.q._stop_event.is_set))
        self.assertGreater(len(self.events()), n)

    def test_a_steady_track_is_quiet(self):
        self.fake.track_sec = 300.0
        self.play()
        self.until(lambda: len(self.events()) >= 2, 1.0)
        n = len(self.events())
        time.sleep(0.5)
        self.assertEqual(len(self.events()), n)

    def test_an_aborted_queue_is_noticed(self):
        self.fake.stop()
        self.play(_tracks(7))
        self.assertTrue(self.q._stop_event.is_set())
        self.assertGreaterEqual(len(self.events()), 1)

    def test_the_state_read_after_an_event_is_fresh(self):
        self.fake.track_sec = 300.0
        self.play()
        self.q.snapshot()
        self.q.set_user_trim_db(3)
        self.assertEqual(self.q.snapshot()["trim_db"], 3.0)

    def test_the_registry_tells_each_queue_its_renderer(self):
        self.assertEqual(QueueRegistry().get("uuid:a")._udn, "uuid:a")


class TestTrimInState(_WithFake):
    def test_trim_is_in_the_state_and_resets_with_a_new_queue(self):
        self.play()
        self.assertEqual(self.q.snapshot()["trim_db"], 0.0)
        self.q.set_user_trim_db(99)
        self.assertEqual(self.q.snapshot()["trim_db"], 5.0)      # clamped
        self.play()
        self.assertEqual(self.q.snapshot()["trim_db"], 0.0)


class _H:
    def _json(self, status, body):
        self.status, self.body = status, body


class TestControlSeek(unittest.TestCase):
    def post(self, body, answer=("ok", 42.0)):
        h, q = _H(), MagicMock()
        q.seek.return_value = answer
        rnd, queues = MagicMock(), MagicMock()
        rnd.get.return_value = object()
        queues.get.return_value = q
        with patch.object(api_playback_state, "RENDERERS", rnd), \
             patch.object(api_playback_state, "QUEUES", queues):
            api_playback.control(h, json.dumps(
                {"device": "upnp:uuid:a", **body}))
        return h, q

    def test_seek_is_relative_and_seek_abs_is_not(self):
        for action, rel in (("seek", True), ("seek_abs", False)):
            h, q = self.post({"action": action, "value": -30})
            q.seek.assert_called_once_with(-30.0, relative=rel)
            self.assertEqual((h.status, h.body), (200, {"ok": True, "position": 42.0}))

    def test_not_seekable_is_a_409_and_a_refusal_a_502(self):
        for answer, status, code in ((("not_seekable", None), 409, "not_seekable"),
                                     (("refused", None), 502, "seek_failed")):
            h, _ = self.post({"action": "seek", "value": 30}, answer)
            self.assertEqual((h.status, h.body["error"]), (status, code))
            self.assertTrue(h.body["message"])

    def test_bad_values_are_a_400(self):
        for value in (None, "x", [], float("nan"), float("inf"), "nan"):
            for action in ("seek", "seek_abs", "trim_db"):
                with self.subTest(action=action, value=value):
                    h, q = self.post({"action": action, "value": value})
                    self.assertEqual(h.status, 400)
                    q.seek.assert_not_called()
                    q.set_user_trim_db.assert_not_called()

    def test_a_seek_needs_a_value(self):
        h, q = self.post({"action": "seek"})
        self.assertEqual(h.status, 400)
        q.seek.assert_not_called()


if __name__ == "__main__":
    unittest.main()
