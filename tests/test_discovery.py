#!/usr/bin/env python3
"""tests/test_discovery.py — discovery heartbeat regression.

Guards the fix for the perpetual `Servers offline — subnet scan` loop: the
in-process LocalFs file server must NOT be HTTP-heartbeat-probed (its base URL
404s on GET /), otherwise it's falsely marked offline → SERVERS.online() goes
empty → subnet_scan fires a 254-host sweep every 60 s (the FD sawtooth and the
EMFILE-crash contributor).
"""
import os
import sys
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dlna_discovery as d
from dlna_registry import MediaServer, ServerRegistry


class TestHeartbeatLocalFs(unittest.TestCase):
    def setUp(self):
        d._heartbeat_fails.clear()

    @staticmethod
    def _mk(udn, loc):
        return MediaServer(udn=udn, name="srv", location=loc,
                           control_url=loc, base_url=loc)

    def test_localfs_not_probed_kept_online(self):
        reg = ServerRegistry()
        reg.add(self._mk("uuid:localfs-abc", "http://127.0.0.1:8201/"))
        with reg._lock:                                   # force stale
            reg._d["uuid:localfs-abc"].last_seen = 0
        with mock.patch.object(d, "SERVERS", reg), \
             mock.patch("urllib.request.urlopen") as up:
            d._heartbeat_once()
        up.assert_not_called()                            # never HTTP-probed
        self.assertTrue(reg.is_online("uuid:localfs-abc"))  # touched → online
        self.assertEqual(len(reg.online()), 1)

    def test_real_server_is_probed(self):
        reg = ServerRegistry()
        reg.add(self._mk("uuid:asset-1", "http://192.168.1.50:26125/desc.xml"))
        with mock.patch.object(d, "SERVERS", reg), \
             mock.patch("urllib.request.urlopen") as up:
            d._heartbeat_once()
        up.assert_called()                                # real server IS probed

    def test_real_server_offline_after_two_fails(self):
        reg = ServerRegistry()
        reg.add(self._mk("uuid:asset-1", "http://192.168.1.50:26125/desc.xml"))
        with mock.patch.object(d, "SERVERS", reg), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            d._heartbeat_once()
            self.assertTrue(reg.is_online("uuid:asset-1"))   # 1 fail: still online
            d._heartbeat_once()
            self.assertFalse(reg.is_online("uuid:asset-1"))  # 2 fails: offline


if __name__ == "__main__":
    unittest.main(verbosity=2)
