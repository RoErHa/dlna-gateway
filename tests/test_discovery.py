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
        reg.add(self._mk("uuid:upnp-1", "http://192.168.1.50:26125/desc.xml"))
        with mock.patch.object(d, "SERVERS", reg), \
             mock.patch("urllib.request.urlopen") as up:
            d._heartbeat_once()
        up.assert_called()                                # real server IS probed

    def test_real_server_offline_after_two_fails(self):
        reg = ServerRegistry()
        reg.add(self._mk("uuid:upnp-1", "http://192.168.1.50:26125/desc.xml"))
        with mock.patch.object(d, "SERVERS", reg), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
            d._heartbeat_once()
            self.assertTrue(reg.is_online("uuid:upnp-1"))   # 1 fail: still online
            d._heartbeat_once()
            self.assertFalse(reg.is_online("uuid:upnp-1"))  # 2 fails: offline


class TestServerAllowlist(unittest.TestCase):
    """UPNP_SERVER_ALLOW decides which external MediaServers are tracked.
    It replaced a hardcoded "Asset UPnP" prefix that, once that server was
    retired, silently dropped EVERY external server — MinimServer included."""

    def test_unset_tracks_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("UPNP_SERVER_ALLOW", None)
            self.assertFalse(d.server_allowed("MinimServer[mini]"))

    def test_empty_and_blank_entries_track_nothing(self):
        for allow in ("", " ", ",", " , "):
            self.assertFalse(d.server_allowed("MinimServer", allow), allow)
        # A blank prefix must never match everything.
        self.assertFalse(d.server_allowed("", ","))

    def test_prefix_match_is_case_insensitive_and_trimmed(self):
        self.assertTrue(d.server_allowed("MinimServer[mini]", " minimserver "))
        self.assertTrue(d.server_allowed("Plex Media Server: x", "Foo, plex"))
        self.assertFalse(d.server_allowed("[TV] LG webOS", "MinimServer,Plex"))

    def test_prefix_not_substring(self):
        self.assertFalse(d.server_allowed("My MinimServer", "MinimServer"))

    def _desc(self, name):
        return f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
<friendlyName>{name}</friendlyName><UDN>uuid:srv-{abs(hash(name))}</UDN>
<serviceList><service>
<serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType>
<controlURL>/cd/control</controlURL></service></serviceList>
</device></root>""".encode()

    def _register(self, name, allow):
        reg = ServerRegistry()
        resp = mock.MagicMock()
        resp.read.return_value = self._desc(name)
        resp.__enter__.return_value = resp
        with mock.patch.dict(os.environ, {"UPNP_SERVER_ALLOW": allow}), \
             mock.patch("urllib.request.urlopen", return_value=resp), \
             mock.patch.object(d, "DEVICE_ROLES"), \
             mock.patch("dlna_providers.bind_provider"):
            d._fetch_device("http://192.168.1.50:9790/desc.xml", reg,
                            d.RendererRegistry(), "uuid:gw")
        return [s.name for s in reg.all()]

    def test_discovery_honours_the_allowlist(self):
        self.assertEqual(self._register("MinimServer[mini]", "MinimServer"),
                         ["MinimServer[mini]"])
        self.assertEqual(self._register("[TV] LG webOS", "MinimServer"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
