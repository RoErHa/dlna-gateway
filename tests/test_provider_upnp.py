#!/usr/bin/env python3
"""
tests/test_provider_upnp.py — Phase 1 UpnpProvider tests.

Three concerns:
  1. Wire-level delegation: cd_browse / cd_search / browse_all forward
     to dlna_content with the provider's control_url. No silent
     argument mangling.
  2. Protocol conformance: UpnpProvider satisfies LibraryProvider
     structurally; stream_url is the near-identity required for UPnP.
  3. probe(): True on a 200 response, False on dlna_content raising or
     returning an error dict.

The high-level Protocol methods (list_artists / list_albums /
list_tracks / get_track / search / watch_changes) are explicitly
stubbed with NotImplementedError in this phase. Tests pin that
contract so future code can rely on `try / NotImplementedError /
fallback` patterns.

Run standalone:
    python3 -m unittest tests.test_provider_upnp -v
"""
import os
import sys
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_providers import LibraryProvider
from dlna_providers.upnp import UpnpProvider


class _FakeServer:
    """Minimum surface UpnpProvider reads from a MediaServer-like."""
    def __init__(self, udn="uuid:test", name="TestSrv",
                 control_url="http://srv:1234/cd/control",
                 base_url="http://srv:1234/"):
        self.udn         = udn
        self.name        = name
        self.control_url = control_url
        self.base_url    = base_url


# ── Wire-level delegation ────────────────────────────────────────

class TestWireDelegation(unittest.TestCase):

    def setUp(self):
        self.srv = _FakeServer()
        self.p   = UpnpProvider(self.srv)

    def test_cd_browse_forwards_control_url_and_args(self):
        with patch("dlna_content.cd_browse",
                   return_value={"containers": [], "items": []}) as m:
            self.p.cd_browse("c42", start=10, count=25)
        m.assert_called_once_with(self.srv.control_url, "c42",
                                  start=10, count=25)

    def test_cd_browse_default_root_object(self):
        with patch("dlna_content.cd_browse",
                   return_value={"containers": []}) as m:
            self.p.cd_browse()
        # The default object_id and count
        args, kwargs = m.call_args
        self.assertEqual(args, (self.srv.control_url, "0"))

    def test_cd_search_forwards(self):
        with patch("dlna_content.cd_search",
                   return_value={"items": []}) as m:
            self.p.cd_search("pink floyd", count=42)
        m.assert_called_once_with(self.srv.control_url, "pink floyd",
                                  count=42)

    def test_browse_all_forwards(self):
        with patch("dlna_content.browse_all",
                   return_value=([], [])) as m:
            self.p.browse_all("c7", max_items=999)
        m.assert_called_once_with(self.srv.control_url, "c7",
                                  max_items=999)

    def test_browse_all_returns_dlna_content_tuple_verbatim(self):
        with patch("dlna_content.browse_all",
                   return_value=(["sub"], ["item"])):
            self.assertEqual(self.p.browse_all("c1"), (["sub"], ["item"]))


# ── Protocol conformance ─────────────────────────────────────────

class TestProtocolConformance(unittest.TestCase):

    def test_upnp_is_library_provider(self):
        # @runtime_checkable Protocol — structural check
        p = UpnpProvider(_FakeServer())
        self.assertIsInstance(p, LibraryProvider)

    def test_name_is_upnp(self):
        self.assertEqual(UpnpProvider(_FakeServer()).name, "upnp")

    def test_udn_passed_through(self):
        srv = _FakeServer(udn="uuid:custom-udn")
        self.assertEqual(UpnpProvider(srv).udn, "uuid:custom-udn")


# ── stream_url ───────────────────────────────────────────────────

class TestStreamUrl(unittest.TestCase):

    def test_stream_url_is_identity(self):
        # For UPnP, the track_id IS the renderer-fetchable URL — the
        # indexer stored it on tracks.url. Keep that contract.
        p = UpnpProvider(_FakeServer())
        u = "http://srv:1234/content/c2/b16/f44100/d123-coABCDE.flac"
        self.assertEqual(p.stream_url(u), u)


# ── probe() ──────────────────────────────────────────────────────

class TestProbe(unittest.TestCase):

    def setUp(self):
        self.p = UpnpProvider(_FakeServer())

    def test_probe_returns_true_on_normal_response(self):
        with patch("dlna_content.cd_browse",
                   return_value={"containers": [], "items": []}):
            self.assertTrue(self.p.probe())

    def test_probe_returns_false_on_error_dict(self):
        # cd_browse signals SOAP failure by returning a dict with an
        # 'error' key (no exception raised — matches existing
        # behaviour in api_browse.browse()).
        with patch("dlna_content.cd_browse",
                   return_value={"error": "HTTP 500"}):
            self.assertFalse(self.p.probe())

    def test_probe_returns_false_on_exception(self):
        # Network blowups (refused, timeout, …) — defensive.
        with patch("dlna_content.cd_browse",
                   side_effect=OSError("connection refused")):
            self.assertFalse(self.p.probe())


# ── Stubbed Protocol methods (P1 contract) ───────────────────────

class TestStubbedProtocolMethods(unittest.TestCase):

    def setUp(self):
        self.p = UpnpProvider(_FakeServer())

    def test_list_artists_raises_not_implemented(self):
        # Iterator is built lazily — materialise to trigger.
        with self.assertRaises(NotImplementedError):
            list(self.p.list_artists())

    def test_list_albums_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            list(self.p.list_albums("a1"))

    def test_list_tracks_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            list(self.p.list_tracks("al1"))

    def test_get_track_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.p.get_track("t1")

    def test_search_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            list(self.p.search("anything"))

    def test_watch_changes_raises_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            self.p.watch_changes(lambda: None)


# ── Registry hookup ──────────────────────────────────────────────

class TestRegistryHookup(unittest.TestCase):

    def test_upnp_class_registered_under_name_upnp(self):
        # The @register_provider("upnp") decorator should have run at
        # import time.
        from dlna_providers import get_provider_class
        self.assertIs(get_provider_class("upnp"), UpnpProvider)


if __name__ == "__main__":
    unittest.main()
