#!/usr/bin/env python3
"""
tests/test_renderer_resume.py — audiobook resume on the renderer path (P3).

Covers avtransport_seek (SOAP body shape + failure paths), the H:MM:SS
formatter, RendererQueue's start_at seek + is_book flag, and the
render_queue payload passthrough. No network — SOAP layer mocked.
"""
import os
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_avtransport import _sec_to_hms, avtransport_seek


def _mock_http(status: int = 200, body: bytes = b"<ok/>"):
    captured = {}
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body

    class FakeConn:
        def __init__(self, host, timeout=None):
            captured["host"] = host
        def request(self, method, path, body=b"", headers=None):
            captured["body"] = body
            captured["headers"] = headers or {}
        def getresponse(self):
            return resp
        def close(self):
            pass

    return FakeConn, captured


class TestSecToHms(unittest.TestCase):

    def test_formats(self):
        self.assertEqual(_sec_to_hms(0), "0:00:00")
        self.assertEqual(_sec_to_hms(59), "0:00:59")
        self.assertEqual(_sec_to_hms(754.6), "0:12:34")
        self.assertEqual(_sec_to_hms(3661), "1:01:01")
        self.assertEqual(_sec_to_hms(-5), "0:00:00")   # clamped


class TestAvtransportSeek(unittest.TestCase):

    def test_body_shape(self):
        FakeConn, cap = _mock_http()
        with patch("dlna_avtransport.http.client.HTTPConnection", FakeConn):
            ok = avtransport_seek("http://r/AVT", 754)
        self.assertTrue(ok)
        body = cap["body"].decode()
        self.assertIn("<u:Seek", body)
        self.assertIn("<Unit>REL_TIME</Unit>", body)
        self.assertIn("<Target>0:12:34</Target>", body)
        self.assertIn("AVTransport:1#Seek", cap["headers"]["SOAPAction"])

    def test_http_fault_returns_false(self):
        FakeConn, _ = _mock_http(status=500)
        with patch("dlna_avtransport.http.client.HTTPConnection", FakeConn):
            self.assertFalse(avtransport_seek("http://r/AVT", 10))

    def test_connection_error_returns_false(self):
        class BoomConn:
            def __init__(self, *a, **k):
                pass
            def request(self, *a, **k):
                raise ConnectionRefusedError(61, "refused")
            def close(self):
                pass
        with patch("dlna_avtransport.http.client.HTTPConnection", BoomConn):
            self.assertFalse(avtransport_seek("http://r/AVT", 10))


class TestQueueResume(unittest.TestCase):
    """RendererQueue start_at + is_book plumbing (SOAP mocked out)."""

    def _queue(self):
        from dlna_player import RendererQueue
        return RendererQueue()

    def _start_book(self, q, states, positions, seeks, start_at=754.0):
        """Start a one-chapter book queue against a scripted renderer:
        `states` is what GetTransportInfo answers in turn (the last one
        repeats), `positions` what GetPositionInfo answers after a Seek."""
        states, positions = list(states), list(positions)
        q._RESUME_POLL_SEC = 0.01
        q._RESUME_SETTLE_SEC = 0.01
        q._RESUME_PLAYING_TIMEOUT = 1.0

        def probe(url):
            return (states.pop(0) if len(states) > 1 else states[0]), ""

        def pos(url):
            p = positions.pop(0) if len(positions) > 1 else positions[0]
            return {"position": p}

        patches = [
            patch("dlna_avtransport.avtransport_stop"),
            patch("dlna_avtransport.avtransport_send", return_value=True),
            patch("dlna_avtransport.avtransport_set_next_uri"),
            patch("dlna_avtransport.avtransport_probe_state", side_effect=probe),
            patch("dlna_avtransport.avtransport_get_position", side_effect=pos),
            patch("dlna_avtransport.avtransport_seek",
                  side_effect=lambda url, sec: seeks.append(
                      (sec, states[0]) ) or True),
            patch.object(q, "_monitor"),
            patch.object(q, "_apply_startup_volume"),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        q.start("http://r/AVT", [{"url": "http://x/ch7", "title": "Ch 7",
                                  "album_key": "Book"}],
                "Naim", start_at_sec=start_at, is_book=True)
        for _ in range(200):
            if q._resume_token is None:
                break
            threading.Event().wait(0.01)
        self.addCleanup(q._stop_event.set)

    def test_start_at_fires_seek(self):
        q = self._queue()
        seeks = []
        self._start_book(q, ["PLAYING"], [755.0], seeks)
        self.assertEqual([s for s, _ in seeks], [754.0])
        self.assertTrue(q._is_book)

    def test_seek_waits_until_the_renderer_is_playing(self):
        """2026-11-09: the Naim takes ~4 s to reach PLAYING and silently
        drops a Seek sent before then."""
        q = self._queue()
        seeks = []
        self._start_book(q, ["TRANSITIONING"] * 5 + ["PLAYING"], [755.0], seeks)
        self.assertEqual(seeks, [(754.0, "PLAYING")])

    def test_a_seek_that_did_not_land_is_retried_once(self):
        q = self._queue()
        seeks = []
        self._start_book(q, ["PLAYING"], [3.0, 756.0], seeks)
        self.assertEqual(len(seeks), 2)

    def test_gives_up_after_two_seeks_that_do_not_land(self):
        q = self._queue()
        seeks = []
        with self.assertLogs("dlna.player", "WARNING") as logs:
            self._start_book(q, ["PLAYING"], [3.0], seeks)
        self.assertEqual(len(seeks), 2)
        self.assertIn("did not land", "\n".join(logs.output))
        self.assertIsNone(q._resume_token)     # saving resumes afterwards

    def test_no_seek_if_the_renderer_never_plays(self):
        q = self._queue()
        seeks = []
        with self.assertLogs("dlna.player", "WARNING"):
            self._start_book(q, ["TRANSITIONING"], [None], seeks)
        self.assertEqual(seeks, [])
        self.assertIsNone(q._resume_token)

    def test_a_newer_queue_cancels_an_old_resume(self):
        """start() clears _stop_event right after cancelling, so the old
        seek thread must notice by its token, not by the event."""
        q = self._queue()
        q._resume_token = object()          # a newer queue took over
        with patch("dlna_avtransport.avtransport_probe_state",
                   return_value=("PLAYING", "")), \
             patch("dlna_avtransport.avtransport_seek") as seek:
            q._av_url = "http://r/AVT"
            q._resume_seek(754.0, object())
        seek.assert_not_called()

    def test_monitor_saves_no_position_while_the_resume_settles(self):
        """The bug that wiped the bookmarks: the monitor's first save
        landed ~3 s into the restarted chapter, before the Seek took."""
        q = self._queue()
        q._av_url = "http://r/AVT"
        q._tracks = [{"url": "http://x/ch7", "album_key": "Book"}]
        q._index = 0
        q._is_book = True
        q._resume_token = object()
        calls = {"n": 0}

        def probe(url):
            calls["n"] += 1
            if calls["n"] > 2:
                q._stop_event.set()
            return "PLAYING", ""

        with patch("dlna_avtransport.avtransport_probe_state", side_effect=probe), \
             patch("dlna_avtransport.avtransport_get_position",
                   return_value={"position": 3.0, "track_uri": "http://x/ch7"}), \
             patch("dlna_library.DB") as db, \
             patch.object(q._stop_event, "wait", return_value=False):
            q._monitor()
        db.position_set.assert_not_called()

    def test_no_seek_when_starting_from_zero(self):
        q = self._queue()
        with patch("dlna_avtransport.avtransport_stop"), \
             patch("dlna_avtransport.avtransport_send", return_value=True), \
             patch("dlna_avtransport.avtransport_set_next_uri"), \
             patch("dlna_avtransport.avtransport_seek") as seek, \
             patch.object(q, "_monitor"), \
             patch.object(q, "_apply_startup_volume"):
            q.start("http://r/AVT", [{"url": "http://x/ch1", "title": "Ch 1"}],
                    "Naim", start_at_sec=0.0, is_book=True)
            threading.Event().wait(0.1)
        seek.assert_not_called()
        q._stop_event.set()

    def test_music_queue_has_book_flag_off(self):
        q = self._queue()
        with patch("dlna_avtransport.avtransport_stop"), \
             patch("dlna_avtransport.avtransport_send", return_value=True), \
             patch("dlna_avtransport.avtransport_set_next_uri"), \
             patch.object(q, "_monitor"), \
             patch.object(q, "_apply_startup_volume"):
            q.start("http://r/AVT", [{"url": "http://x/t1", "title": "T"}],
                    "Naim")
        self.assertFalse(q._is_book)
        q._stop_event.set()


class TestRenderQueuePayload(unittest.TestCase):
    """/api/render_queue passes book + start_at_sec through to start()."""

    def test_passthrough(self):
        import json
        import api_playback
        from dlna_registry import MediaRenderer

        rnd = MediaRenderer(udn="uuid:r1", name="Naim",
                            location="http://r", av_url="http://r/AVT",
                            base_url="http://r", rc_url="http://r/RC")
        h = MagicMock()
        captured = {}

        class FakeQueue:
            def start(self, av_url, tracks, name, rc_url="",
                      start_at_sec=0.0, is_book=False):
                captured.update(start_at_sec=start_at_sec, is_book=is_book)
                captured["evt"].set()

        fq = FakeQueue()
        captured["evt"] = threading.Event()
        body = json.dumps({"udn": "uuid:r1", "book": True,
                           "start_at_sec": 321.5,
                           "tracks": [{"url": "http://x/ch2"}]})
        with patch.object(api_playback.RENDERERS, "get", return_value=rnd), \
             patch.object(api_playback.QUEUES, "is_busy", return_value=False), \
             patch.object(api_playback.QUEUES, "get", return_value=fq):
            api_playback.render_queue(h, body)
        self.assertTrue(captured["evt"].wait(2.0))
        self.assertEqual(captured["start_at_sec"], 321.5)
        self.assertTrue(captured["is_book"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
