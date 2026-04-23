#!/usr/bin/env python3
"""
tests/test_api_playback.py — Unit tests for api_playback handlers.

Run standalone:
    python3 -m unittest tests.test_api_playback -v
    python3 tests/test_api_playback.py

No network, no gateway. RENDERERS and QUEUES are patched with fakes.
"""
import json
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import api_playback


# ── Test doubles ──────────────────────────────────────────────────

class MockHandler:
    """Captures what the handler would send back over HTTP."""
    def __init__(self):
        self.status = None
        self.body   = None

    def _json(self, status, body):
        self.status = status
        self.body   = body


class MockRenderer:
    def __init__(self, udn="uuid:test", name="TestRenderer",
                 av_url="http://fake-renderer/av"):
        self.udn    = udn
        self.name   = name
        self.av_url = av_url


class FakeQueue:
    """Minimal RendererQueue stand-in: tracks which methods were called
    and returns a configurable snapshot."""
    def __init__(self, snapshot=None):
        self._snap = snapshot or {
            "state": "stopped", "alive": False, "renderer": "",
            "queue_len": 0, "queue_pos": 0,
        }
        self.start_calls = []
        self.pause_calls = 0
        self.stop_calls  = 0
        self.next_calls  = 0
        self.prev_calls  = 0

    def snapshot(self):       return dict(self._snap)
    def start(self, av_url, tracks, name):
        self.start_calls.append((av_url, list(tracks), name))
    def pause(self):          self.pause_calls += 1
    def stop(self):           self.stop_calls  += 1
    def next_track(self):     self.next_calls  += 1
    def prev_track(self):     self.prev_calls  += 1


class FakeRegistry:
    """Stand-in for QueueRegistry. Pre-populate with snapshots per UDN
    to simulate the 'this renderer is busy' state."""
    def __init__(self, busy_udns=None):
        self._queues: dict = {}
        self._busy = set(busy_udns or [])

    def get(self, udn):
        if udn not in self._queues:
            snap = None
            if udn in self._busy:
                snap = {"state": "playing", "alive": True,
                        "renderer": "BusyRenderer",
                        "title": "Currently Playing Song",
                        "artist": "Other Artist",
                        "queue_len": 5, "queue_pos": 2}
            self._queues[udn] = FakeQueue(snapshot=snap)
        return self._queues[udn]

    def peek(self, udn):
        return self._queues.get(udn)

    def is_busy(self, udn):
        return udn in self._busy

    def snapshot_all(self):
        return {udn: q.snapshot() for udn, q in self._queues.items()}


# ── render_queue handler ──────────────────────────────────────────

class TestRenderQueueHandler(unittest.TestCase):

    def _post(self, payload, renderers=None, registry=None):
        """Invoke the render_queue handler with patched globals. Returns
        the MockHandler so the test can inspect status/body."""
        h       = MockHandler()
        fakes   = renderers or {"uuid:test": MockRenderer()}
        fakeq   = registry or FakeRegistry()
        # Use a dict wrapper that exposes .get() like RendererRegistry does
        rend_ns = MagicMock()
        rend_ns.get.side_effect = fakes.get
        with patch.object(api_playback, "RENDERERS", rend_ns), \
             patch.object(api_playback, "QUEUES", fakeq):
            api_playback.render_queue(h, json.dumps(payload))
        # start() runs in a background thread; give it a moment to finish
        # so tests that assert on start_calls see the call
        time.sleep(0.05)
        return h, fakeq

    def test_unknown_udn_returns_404(self):
        h, _ = self._post({"udn": "uuid:ghost", "tracks": [{"url": "x"}]},
                          renderers={})
        self.assertEqual(h.status, 404)
        self.assertIn("error", h.body)

    def test_empty_tracks_returns_400(self):
        h, _ = self._post({"udn": "uuid:test", "tracks": []})
        self.assertEqual(h.status, 400)

    def test_missing_tracks_key_returns_400(self):
        h, _ = self._post({"udn": "uuid:test"})
        self.assertEqual(h.status, 400)

    def test_idle_renderer_accepts_queue(self):
        h, reg = self._post({"udn": "uuid:test",
                             "tracks": [{"url": "x", "title": "t"}]})
        self.assertEqual(h.status, 200)
        self.assertTrue(h.body.get("ok"))
        # Start was called on the right queue
        q = reg.peek("uuid:test")
        self.assertIsNotNone(q)
        self.assertEqual(len(q.start_calls), 1)

    def test_busy_renderer_returns_409_with_busy_with(self):
        reg = FakeRegistry(busy_udns={"uuid:test"})
        h, _ = self._post({"udn": "uuid:test",
                           "tracks": [{"url": "x", "title": "t"}]},
                          registry=reg)
        self.assertEqual(h.status, 409)
        self.assertEqual(h.body.get("error"), "renderer_busy")
        bw = h.body.get("busy_with") or {}
        self.assertEqual(bw.get("title"),  "Currently Playing Song")
        self.assertEqual(bw.get("artist"), "Other Artist")
        # And the queue's start() was NOT called — the conflict must
        # block the actual playback kick-off
        q = reg.peek("uuid:test")
        self.assertEqual(len(q.start_calls), 0,
                         "busy 409 must not start the incoming queue")

    def test_busy_renderer_with_force_accepts(self):
        reg = FakeRegistry(busy_udns={"uuid:test"})
        h, _ = self._post({"udn": "uuid:test", "force": True,
                           "tracks": [{"url": "x", "title": "t"}]},
                          registry=reg)
        self.assertEqual(h.status, 200)
        self.assertTrue(h.body.get("ok"))
        q = reg.peek("uuid:test")
        self.assertEqual(len(q.start_calls), 1,
                         "force=True must override the busy check")

    def test_thread_exception_is_caught_not_silent(self):
        """Regression: before the fix, the daemon thread's ValueError died
        silently to /tmp/dlna-gateway.err. The handler must wrap start()
        in try/except and log via log.exception."""
        h      = MockHandler()
        reg    = FakeRegistry()
        class Crashing(FakeQueue):
            def start(self, *_):
                raise RuntimeError("boom")
        reg._queues["uuid:test"] = Crashing()
        rend_ns = MagicMock()
        rend_ns.get.side_effect = {"uuid:test": MockRenderer()}.get
        with patch.object(api_playback, "RENDERERS", rend_ns), \
             patch.object(api_playback, "QUEUES", reg), \
             patch.object(api_playback, "log") as mlog:
            api_playback.render_queue(
                h, json.dumps({"udn": "uuid:test",
                               "tracks": [{"url": "x"}]}))
            time.sleep(0.10)
        # API returned 200 (the exception happens in the worker thread)
        self.assertEqual(h.status, 200)
        # log.exception was called from the worker thread's except block
        self.assertTrue(mlog.exception.called,
                        "worker thread exception must reach log.exception — "
                        "otherwise silent thread deaths recur")


# ── renderer_state handler ────────────────────────────────────────

class TestRendererStateHandler(unittest.TestCase):

    def _get(self, params, registry=None):
        h   = MockHandler()
        reg = registry or FakeRegistry()
        with patch.object(api_playback, "QUEUES", reg):
            api_playback.renderer_state(h, params)
        return h

    def test_udn_scoped_returns_single_snapshot(self):
        reg = FakeRegistry(busy_udns={"uuid:alpha"})
        h   = self._get({"udn": "uuid:alpha"}, registry=reg)
        self.assertEqual(h.status, 200)
        self.assertTrue(h.body.get("alive"))
        self.assertEqual(h.body.get("title"), "Currently Playing Song")
        # UDN-scoped response does NOT carry the full registry dump
        self.assertNotIn("queues", h.body)

    def test_no_udn_returns_legacy_plus_queues(self):
        reg = FakeRegistry(busy_udns={"uuid:alpha"})
        # Force the queue to exist so snapshot_all includes it
        reg.get("uuid:alpha")
        reg.get("uuid:beta")  # idle
        h = self._get({}, registry=reg)
        self.assertEqual(h.status, 200)
        # Legacy top-level fields present for stale UI tabs
        for key in ("state", "alive", "renderer", "queue_len", "queue_pos"):
            self.assertIn(key, h.body)
        # Full registry dump included
        self.assertIn("queues", h.body)
        self.assertEqual(set(h.body["queues"].keys()),
                         {"uuid:alpha", "uuid:beta"})
        # The flat view picks an alive queue when one exists
        self.assertTrue(h.body.get("alive"))

    def test_no_udn_with_nothing_playing_returns_stopped(self):
        h = self._get({})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.body.get("state"), "stopped")
        self.assertFalse(h.body.get("alive"))
        self.assertEqual(h.body.get("queues"), {})


# ── control handler — routes to the right per-UDN queue ───────────

class TestControlHandler(unittest.TestCase):

    def _post(self, payload, renderers=None, registry=None):
        h       = MockHandler()
        fakes   = renderers or {"uuid:alpha": MockRenderer(udn="uuid:alpha"),
                                 "uuid:beta":  MockRenderer(udn="uuid:beta")}
        fakeq   = registry or FakeRegistry()
        rend_ns = MagicMock()
        rend_ns.get.side_effect = fakes.get
        with patch.object(api_playback, "RENDERERS", rend_ns), \
             patch.object(api_playback, "QUEUES", fakeq):
            api_playback.control(h, json.dumps(payload))
        return h, fakeq

    def test_unknown_udn_returns_404(self):
        h, _ = self._post({"device": "upnp:uuid:ghost", "action": "pause"},
                          renderers={})
        self.assertEqual(h.status, 404)

    def test_pause_routes_to_correct_udn(self):
        h, reg = self._post({"device": "upnp:uuid:alpha", "action": "pause"})
        self.assertEqual(h.status, 200)
        qa = reg.peek("uuid:alpha")
        qb = reg.peek("uuid:beta")
        self.assertEqual(qa.pause_calls, 1,
                         "pause must hit the alpha queue")
        self.assertIsNone(qb, "beta queue must not have been created")

    def test_concurrent_renderers_have_independent_state(self):
        """The whole point of per-renderer queues: controlling one must
        not touch the other's state."""
        _, reg = self._post({"device": "upnp:uuid:alpha", "action": "stop"})
        self._post({"device": "upnp:uuid:beta", "action": "next"},
                   registry=reg)
        qa = reg.peek("uuid:alpha")
        qb = reg.peek("uuid:beta")
        self.assertEqual(qa.stop_calls, 1)
        self.assertEqual(qa.next_calls, 0,
                         "alpha must not receive beta's next_track")
        self.assertEqual(qb.next_calls, 1)
        self.assertEqual(qb.stop_calls, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
