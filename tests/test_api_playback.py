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
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import dlna_art_cache

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
                 av_url="http://fake-renderer/av",
                 rc_url="http://fake-renderer/rc"):
        self.udn    = udn
        self.name   = name
        self.av_url = av_url
        self.rc_url = rc_url


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
    def start(self, av_url, tracks, name, rc_url="",
              start_at_sec=0.0, is_book=False):
        self.start_calls.append((av_url, list(tracks), name, rc_url))
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


# ── /art image-proxy handler ──────────────────────────────────────

class TestArtHandler(unittest.TestCase):
    """The /art endpoint is the iOS lock-screen artwork proxy. Without it,
    MediaSession artwork silently 404s on iPhones — exactly the kind of
    mobile/PWA-only bug that never surfaces in dev."""

    class _ArtHandler:
        """Captures send_response/send_header/send_error/wfile output."""
        def __init__(self):
            self.status = None
            self.headers = {}
            self.body = b""
            self.error_status = None
            self.error_message = None
            self.wfile = self
        def send_response(self, status): self.status = status
        def send_header(self, k, v):     self.headers[k] = v
        def send_error(self, status, message=None):
            self.error_status = status
            self.error_message = message
        def end_headers(self): pass
        def write(self, data): self.body += data

    def setUp(self):
        # art() now routes through art_fetch_cached → isolate the on-disk cache
        # to a throwaway dir so tests don't pollute (or get served stale art by)
        # the real art_cache/ and the http.client mocks are actually exercised.
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_cache_dir = dlna_art_cache.CACHE_DIR
        dlna_art_cache.CACHE_DIR = self._tmp.name

    def tearDown(self):
        dlna_art_cache.CACHE_DIR = self._saved_cache_dir
        self._tmp.cleanup()

    def test_missing_url_returns_400(self):
        h = self._ArtHandler()
        api_playback.art(h, {})
        self.assertEqual(h.error_status, 400)

    def test_bad_scheme_returns_400(self):
        h = self._ArtHandler()
        api_playback.art(h, {"url": "ftp://example/x.jpg"})
        self.assertEqual(h.error_status, 400)

    def test_successful_image_proxies_through(self):
        h = self._ArtHandler()
        jpeg = b"\xff\xd8\xff\xe0" + b"FAKEJPEG" * 16   # >= _ART_MIN_BYTES (64)
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.read.return_value = jpeg
        fake_resp.getheader.side_effect = lambda k: {
            "Content-Type": "image/jpeg",
        }.get(k)
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = fake_resp
        with patch("http.client.HTTPConnection", return_value=fake_conn):
            api_playback.art(h, {"url": "http://fake.local/cover.jpg"})
        self.assertEqual(h.status, 200)
        self.assertEqual(h.headers.get("Content-Type"), "image/jpeg")
        self.assertEqual(h.body, jpeg)
        self.assertIn("max-age=", h.headers.get("Cache-Control", ""))

    def test_non_image_upstream_rejected(self):
        """Guards against serving HTML (upstream 404 page) as an image —
        that would poison the Service Worker's art cache."""
        h = self._ArtHandler()
        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.read.return_value = b"<html>404 not found</html>"
        fake_resp.getheader.side_effect = lambda k: {
            "Content-Type": "text/html",
        }.get(k)
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = fake_resp
        with patch("http.client.HTTPConnection", return_value=fake_conn):
            api_playback.art(h, {"url": "http://fake.local/missing"})
        self.assertEqual(h.error_status, 502)

    def test_oversized_image_rejected(self):
        """Prevents a malicious/broken upstream from forcing arbitrary
        memory allocation in the gateway."""
        h = self._ArtHandler()
        fake_resp = MagicMock()
        fake_resp.status = 200
        # _ART_MAX_BYTES+1 — just over the cap so len > max
        fake_resp.read.return_value = b"x" * (api_playback._ART_MAX_BYTES + 1)
        fake_resp.getheader.return_value = "image/jpeg"
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = fake_resp
        with patch("http.client.HTTPConnection", return_value=fake_conn):
            api_playback.art(h, {"url": "http://fake.local/huge.jpg"})
        self.assertEqual(h.error_status, 502)

    def test_upstream_404_forwards_status(self):
        h = self._ArtHandler()
        fake_resp = MagicMock()
        fake_resp.status = 404
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = fake_resp
        with patch("http.client.HTTPConnection", return_value=fake_conn):
            api_playback.art(h, {"url": "http://fake.local/missing.jpg"})
        self.assertEqual(h.error_status, 404)

    # ── redirect following (coverartarchive front-500 → archive.org CDN) ──
    @staticmethod
    def _resp(status, *, location=None, ctype=None, body=b""):
        r = MagicMock()
        r.status = status
        r.read.return_value = body
        r.getheader.side_effect = lambda k: {
            "Location": location, "Content-Type": ctype}.get(k)
        return r

    def test_follows_307_redirect_to_image(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"A" * 200
        redirect = self._resp(307, location="https://cdn.test/real.jpg")
        image = self._resp(200, ctype="image/jpeg", body=jpeg)
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = [redirect, image]
        with patch("http.client.HTTPSConnection", return_value=fake_conn):
            code, ctype, body = api_playback.art_fetch(
                "https://coverartarchive.org/release-group/x/front-500")
        self.assertEqual((code, ctype, body), (200, "image/jpeg", jpeg))

    def test_follows_relative_redirect(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"B" * 200
        redirect = self._resp(302, location="/cdn/real.jpg")   # relative Location
        image = self._resp(200, ctype="image/jpeg", body=jpeg)
        fake_conn = MagicMock()
        fake_conn.getresponse.side_effect = [redirect, image]
        with patch("http.client.HTTPSConnection", return_value=fake_conn):
            code, _ctype, body = api_playback.art_fetch("https://host.test/front")
        self.assertEqual(code, 200)
        self.assertEqual(body, jpeg)

    def test_redirect_without_location_is_not_an_infinite_loop(self):
        bad = self._resp(302, location=None)
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = bad
        with patch("http.client.HTTPSConnection", return_value=fake_conn):
            code, _msg, body = api_playback.art_fetch("https://host.test/x")
        self.assertEqual(code, 302)
        self.assertEqual(body, b"")

    def test_too_many_redirects_bails(self):
        loop = self._resp(307, location="https://host.test/again")
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = loop      # always redirects
        with patch("http.client.HTTPSConnection", return_value=fake_conn):
            code, _msg, body = api_playback.art_fetch("https://host.test/start")
        self.assertNotEqual(code, 200)                 # gave up, didn't hang
        self.assertEqual(body, b"")

    def test_tiny_body_rejected(self):
        # A 200 image/* with a junk near-empty body (the 3–12 B entries seen in
        # the live cache) must NOT be served/cached as a real cover.
        tiny = self._resp(200, ctype="image/jpeg", body=b"\xff\xd8\xff")
        fake_conn = MagicMock()
        fake_conn.getresponse.return_value = tiny
        with patch("http.client.HTTPConnection", return_value=fake_conn):
            code, _msg, body = api_playback.art_fetch("http://host.test/junk.jpg")
        self.assertEqual(code, 502)
        self.assertEqual(body, b"")


# ── /api/client_log observability endpoint ────────────────────────

class TestClientLogHandler(unittest.TestCase):
    """The PWA posts browser-side MediaError events and play() rejections
    here so we can see them in gateway.log instead of silently losing
    them to DevTools that nobody's watching on a phone."""

    def test_well_formed_report_accepted(self):
        h = MockHandler()
        body = json.dumps({
            "kind": "audio_error",
            "code": 4, "codeName": "unsupported",
            "message": "bad mime",
            "title": "Song", "retries": 0,
            "ua": "Mozilla/5.0 iPhone",
        })
        api_playback.client_log(h, body)
        self.assertEqual(h.status, 200)
        self.assertTrue(h.body.get("ok"))

    def test_malformed_json_returns_400(self):
        h = MockHandler()
        api_playback.client_log(h, "{not json")
        self.assertEqual(h.status, 400)

    def test_empty_body_returns_400(self):
        h = MockHandler()
        api_playback.client_log(h, "[]")   # list, not dict
        self.assertEqual(h.status, 400)


class TestIndexRebuildDispatch(unittest.TestCase):
    """LocalFs-style providers (rescan, no cd_browse) must be rebuilt via
    provider.rescan(), NOT the UPnP Indexer (which calls cd_browse and
    crashes on a LocalFsProvider — the 2026-06-03 reindex bug)."""

    class _Srv:
        udn = "uuid:x"
        name = "X"

    class _SyncThread:
        """Runs the target inline so the test can observe the effect."""
        def __init__(self, target=None, daemon=None, name=None):
            self._t = target

        def start(self):
            self._t()

    def _run(self, provider):
        with patch.object(api_playback, "SERVERS", {"uuid:x": self._Srv()}), \
             patch.object(api_playback, "get_provider", lambda u: provider), \
             patch.object(api_playback, "INDEXER", MagicMock()) as idx, \
             patch.object(api_playback, "threading", MagicMock()) as thr:
            thr.Thread = self._SyncThread
            h = MockHandler()
            api_playback.index_rebuild(h, {"udn": "uuid:x"})
            return h, idx

    def test_localfs_provider_dispatches_to_rescan(self):
        calls = {}

        class FakeLocalFs:
            def rescan(self, force=False):
                calls["force"] = force
                return {"scanned": 7}

        h, idx = self._run(FakeLocalFs())
        self.assertEqual(h.status, 200)
        self.assertIn("LocalFs", h.body["message"])
        self.assertEqual(calls.get("force"), True)
        idx.start.assert_not_called()

    def test_upnp_provider_uses_indexer(self):
        class FakeUpnp:
            def cd_browse(self, *a, **k):
                return {}

        h, idx = self._run(FakeUpnp())
        self.assertEqual(h.status, 200)
        idx.start.assert_called_once()

    def test_no_provider_falls_back_to_indexer(self):
        h, idx = self._run(None)
        self.assertEqual(h.status, 200)
        idx.start.assert_called_once()

    def test_unknown_udn_returns_404(self):
        with patch.object(api_playback, "SERVERS", {}):
            h = MockHandler()
            api_playback.index_rebuild(h, {"udn": "nope"})
        self.assertEqual(h.status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
