"""tests/fake_renderer.py, driven by the gateway's OWN UPnP client code.

The fake is only useful if the gateway treats it like a real renderer, so
every check here goes through the production functions — `_fetch_device`
for registration, `dlna_avtransport` / `dlna_rendering_control` for
playback — rather than through hand-written SOAP that could drift from them.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dlna_avtransport as av  # noqa: E402
import dlna_discovery  # noqa: E402
import dlna_rendering_control as rc  # noqa: E402
from dlna_registry import RendererRegistry, ServerRegistry  # noqa: E402
from fake_renderer import NAME, UDN, FakeRenderer  # noqa: E402

TRACK = 0.4


class _Running(unittest.TestCase):
    def setUp(self):
        self.fake = FakeRenderer(track_sec=TRACK).start()
        self.addCleanup(self.fake.stop)
        base = self.fake.location.rsplit("/", 1)[0]
        self.av = base + "/AVTransport/control"
        self.rc = base + "/RenderingControl/control"


class TestRegistration(_Running):
    def test_gateway_registers_it_as_a_renderer(self):
        servers, renderers = ServerRegistry(), RendererRegistry()
        with mock.patch.object(dlna_discovery, "DEVICE_ROLES"):
            dlna_discovery._fetch_device(self.fake.location, servers, renderers)
        r = renderers.get(UDN)
        self.assertIsNotNone(r)
        self.assertEqual((r.name, r.av_url, r.rc_url), (NAME, self.av, self.rc))
        self.assertEqual(servers.all(), [])

    def test_udn_survives_the_gateways_sanitiser(self):
        self.assertEqual(dlna_discovery._clean_udn(UDN), UDN)


class TestPlayback(_Running):
    def test_send_plays_then_track_ends_stopped(self):
        """PLAYING → STOPPED is what makes the queue monitor advance."""
        self.assertTrue(av.avtransport_send(self.av, "http://x/1.flac", "One"))
        self.assertEqual(av.avtransport_get_state(self.av), "PLAYING")
        pos = av.avtransport_get_position(self.av)
        self.assertEqual(pos["track_uri"], "http://x/1.flac")
        time.sleep(TRACK + 0.2)
        self.assertEqual(av.avtransport_get_state(self.av), "STOPPED")

    def test_next_uri_is_a_gapless_hand_over(self):
        """TrackURI changes with no STOPPED in between — the shape
        `_gapless_advanced` looks for."""
        av.avtransport_send(self.av, "http://x/1.flac", "One")
        self.assertTrue(av.avtransport_set_next_uri(self.av, "http://x/2.flac"))
        time.sleep(TRACK + 0.1)
        self.assertEqual(av.avtransport_get_state(self.av), "PLAYING")
        self.assertEqual(av.avtransport_get_position(self.av)["track_uri"],
                         "http://x/2.flac")

    def test_pause_toggles_and_holds_the_position(self):
        av.avtransport_send(self.av, "http://x/1.flac", "One")
        self.assertTrue(av.avtransport_pause(self.av))
        self.assertEqual(av.avtransport_get_state(self.av), "PAUSED_PLAYBACK")
        time.sleep(TRACK + 0.1)                  # paused: must not end
        self.assertEqual(av.avtransport_get_state(self.av), "PAUSED_PLAYBACK")
        av.avtransport_pause(self.av)
        self.assertEqual(av.avtransport_get_state(self.av), "PLAYING")

    def test_stop_and_seek(self):
        av.avtransport_send(self.av, "http://x/1.flac", "One")
        self.assertTrue(av.avtransport_seek(self.av, 0))
        self.assertTrue(av.avtransport_stop(self.av))
        self.assertEqual(av.avtransport_get_state(self.av), "STOPPED")

    def test_volume_round_trips(self):
        self.assertTrue(rc.set_volume(self.rc, 22))
        self.assertEqual(rc.get_volume(self.rc), 22)
        self.assertEqual(self.fake.volume, 22)

    def test_unknown_action_is_a_upnp_fault(self):
        code, body = self.fake.handle("/AVTransport/control", "SelfDestruct", "")
        self.assertEqual(code, 500)
        self.assertIn("<errorCode>401</errorCode>", body)

    def test_calls_are_counted(self):
        av.avtransport_send(self.av, "http://x/1.flac", "One")
        self.assertEqual(self.fake.calls["SetAVTransportURI"], 1)
        self.assertEqual(self.fake.calls["Play"], 1)


class TestAnnounce(unittest.TestCase):
    def test_notify_is_unicast_to_loopback_and_passes_the_ssdp_guard(self):
        """Never the multicast group — the LAN's renderers must not hear it —
        and LOCATION's host must be the datagram source, or the gateway drops
        it (`parse_location`, security posture 9)."""
        from dlna_discovery_ssdp import parse_location
        fake = FakeRenderer().start()
        self.addCleanup(fake.stop)
        sent = []

        class _Sock:
            def __init__(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def sendto(self, data, addr): sent.append((data, addr))

        with mock.patch("fake_renderer.socket.socket", _Sock):
            fake.announce()
        (data, addr), = sent
        self.assertEqual(addr, ("127.0.0.1", 1900))
        self.assertEqual(parse_location(data, "127.0.0.1"), fake.location)


if __name__ == "__main__":
    unittest.main()
