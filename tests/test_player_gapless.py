#!/usr/bin/env python3
"""
tests/test_player_gapless.py — Phase 4: RendererQueue pre-queues the
NEXT track via SetNextAVTransportURI so the renderer transitions
gaplessly. Pins:

  * After a successful avtransport_send, avtransport_set_next_uri is
    called with the next track's URL / title / mime.
  * On the LAST track of the queue, set_next is called with an EMPTY
    URL so the renderer clears any previously queued next.
  * A failure in set_next_uri is logged but is NOT fatal — the
    existing STOPPED→advance path continues to work.

Run standalone:
    python3 -m unittest tests.test_player_gapless -v
"""
import os
import sys
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_player import RendererQueue


def _track(idx: int, mime: str = "audio/flac") -> dict:
    return {
        "url":      f"http://fake/track{idx}.flac",
        "title":    f"Track {idx}",
        "artist":   "Artist",
        "album":    "Album",
        "mime":     mime,
        "duration": "0:03:00.000",
    }


class TestGaplessQueueing(unittest.TestCase):

    def test_next_uri_set_for_track_after_first_play(self):
        q = RendererQueue()
        with patch("dlna_avtransport.avtransport_stop", return_value=True), \
             patch("dlna_avtransport.avtransport_send", return_value=True), \
             patch("dlna_avtransport.avtransport_probe_state",
                   return_value=("STOPPED", "")), \
             patch("dlna_avtransport.avtransport_set_next_uri",
                   return_value=True) as setnext:
            try:
                q.start("http://fake-av-url/ctrl",
                        [_track(1), _track(2), _track(3)],
                        "Naim")
            finally:
                q._cancel()
        # Among the calls, at least one should be the next URL = track 2
        urls = [c.args[1] for c in setnext.call_args_list]
        self.assertIn("http://fake/track2.flac", urls,
                      "After playing track 1, gateway must queue track 2")

    def test_next_uri_carries_track_title(self):
        q = RendererQueue()
        with patch("dlna_avtransport.avtransport_stop", return_value=True), \
             patch("dlna_avtransport.avtransport_send", return_value=True), \
             patch("dlna_avtransport.avtransport_probe_state",
                   return_value=("STOPPED", "")), \
             patch("dlna_avtransport.avtransport_set_next_uri",
                   return_value=True) as setnext:
            try:
                q.start("http://fake-av-url/ctrl",
                        [_track(1), _track(2)], "Naim")
            finally:
                q._cancel()
        # title is positional arg #3 (index 2)
        titles = [c.args[2] for c in setnext.call_args_list]
        self.assertIn("Track 2", titles)

    def test_next_uri_cleared_after_last_track(self):
        q = RendererQueue()
        with patch("dlna_avtransport.avtransport_stop", return_value=True), \
             patch("dlna_avtransport.avtransport_send", return_value=True), \
             patch("dlna_avtransport.avtransport_probe_state",
                   return_value=("STOPPED", "")), \
             patch("dlna_avtransport.avtransport_set_next_uri",
                   return_value=True) as setnext:
            try:
                # Start a single-track queue. After Play, there's no
                # next track → set_next_uri must be called with ''
                # so the renderer doesn't try to flow into stale state.
                q.start("http://fake-av-url/ctrl", [_track(1)], "Naim")
            finally:
                q._cancel()
        # Every call's media_url arg
        urls = [c.args[1] for c in setnext.call_args_list]
        # The first send (for the only track) must produce a CLEAR
        # call (empty next URL). Other interleavings from monitor
        # are fine, but the first clear must appear.
        self.assertIn("", urls,
                      "Last-track play must clear the queued next URI")

    def test_set_next_failure_is_non_fatal(self):
        # If the renderer refuses SetNextAVTransportURI, _send_current
        # must still return True (the current Play succeeded) and
        # not raise. Gapless degrades to "small click at track end"
        # — that's the pre-P4 behaviour, not a regression.
        q = RendererQueue()
        with patch("dlna_avtransport.avtransport_stop", return_value=True), \
             patch("dlna_avtransport.avtransport_send",
                   return_value=True), \
             patch("dlna_avtransport.avtransport_probe_state",
                   return_value=("STOPPED", "")), \
             patch("dlna_avtransport.avtransport_set_next_uri",
                   return_value=False):
            try:
                q.start("http://fake-av-url/ctrl",
                        [_track(1), _track(2)], "Naim")
                # Reaching here at all is the test — no exception
                # propagated out of _send_current's set_next call.
            finally:
                q._cancel()


if __name__ == "__main__":
    unittest.main()
