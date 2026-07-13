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

    def test_start_at_fires_seek(self):
        q = self._queue()
        seeks = []
        with patch("dlna_avtransport.avtransport_stop"), \
             patch("dlna_avtransport.avtransport_send", return_value=True), \
             patch("dlna_avtransport.avtransport_set_next_uri"), \
             patch("dlna_avtransport.avtransport_seek",
                   side_effect=lambda url, sec: seeks.append(sec) or True), \
             patch.object(q, "_monitor"), \
             patch.object(q, "_apply_startup_volume"):
            q.start("http://r/AVT", [{"url": "http://x/ch7", "title": "Ch 7",
                                      "album_key": "Book"}],
                    "Naim", start_at_sec=754.0, is_book=True)
            # _seek_async waits ~1.5s on the stop event before seeking.
            for _ in range(80):
                if seeks:
                    break
                threading.Event().wait(0.05)
        self.assertEqual(seeks, [754.0])
        self.assertTrue(q._is_book)
        q._stop_event.set()

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
