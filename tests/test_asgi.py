#!/usr/bin/env python3
"""
tests/test_asgi.py — 2.0 ASGI skeleton (dlna_asgi).

Verifies the FastAPI app exists, the /api/version route is registered, and
the handler returns the same payload as the legacy stdlib handler. No HTTP
client / network — calls the route coroutine directly.

    python3 -m unittest tests.test_asgi -v
"""
import asyncio
import json
import os
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dlna_asgi
from dlna_asgi_bridge import run_legacy_sync
from dlna_config import VERSION


class TestAsgiSkeleton(unittest.TestCase):
    def test_app_exists(self):
        self.assertIsNotNone(dlna_asgi.app)

    def test_version_route_registered(self):
        paths = {getattr(r, "path", None) for r in dlna_asgi.app.routes}
        self.assertIn("/api/version", paths)

    def test_version_handler_returns_version(self):
        # Same shape as api_playback.version, so the PWA badge is identical.
        self.assertEqual(asyncio.run(dlna_asgi.version()), {"version": VERSION})


class TestLegacyBridge(unittest.TestCase):
    """The shim runs a legacy (h, params) handler and captures its response."""

    def test_captures_json(self):
        def fake(h, params):
            h._json(200, {"ok": True, "echo": params.get("x")})
        code, body, ctype = run_legacy_sync(fake, {"x": "hi"})
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "application/json")
        self.assertEqual(json.loads(body), {"ok": True, "echo": "hi"})

    def test_captures_send_error(self):
        def fake(h, params):
            h.send_error(404, "nope")
        code, body, ctype = run_legacy_sync(fake, {})
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(body), {"error": "nope"})

    def test_captures_xml(self):
        def fake(h, params):
            h._xml_response(200, b"<x/>")
        code, body, ctype = run_legacy_sync(fake, {})
        self.assertEqual((code, body, ctype), (200, b"<x/>", "text/xml"))

    def test_post_body_passed_through(self):
        seen = {}

        def fake(h, body):
            seen["body"] = body
            h._json(200, {"len": len(body)})
        code, body, _ = run_legacy_sync(fake, '{"a":1}', command="POST")
        self.assertEqual(seen["body"], '{"a":1}')
        self.assertEqual(json.loads(body), {"len": 7})


class TestBridgeWiring(unittest.TestCase):
    def _route_count(self, path):
        return sum(1 for r in dlna_asgi.app.routes
                   if getattr(r, "path", None) == path)

    def _paths(self):
        return {getattr(r, "path", None) for r in dlna_asgi.app.routes}

    def test_still_bridged_routes_present(self):
        p = self._paths()
        self.assertIn("/api/playlists", p)
        self.assertIn("/api/album_tracks", p)

    def test_streaming_and_device_routes_not_bridged(self):
        p = self._paths()
        for excluded in ("/stream", "/art", "/radio_stream",
                         "/gw/device.xml"):
            self.assertNotIn(excluded, p, excluded)

    def test_native_routes_registered_exactly_once(self):
        # ported natively → present, and NOT also bridged (no duplicate route)
        for native in ("/api/version", "/api/servers", "/api/renderers"):
            self.assertEqual(self._route_count(native), 1, native)


class TestNativePorts(unittest.TestCase):
    """Native routes call the SAME payload fns the legacy handlers use, so
    there's zero divergence between the stdlib and ASGI servers."""

    def test_servers_route_matches_payload(self):
        import api_browse
        self.assertEqual(asyncio.run(dlna_asgi.servers()),
                         api_browse.servers_payload())

    def test_renderers_route_matches_payload(self):
        import api_browse
        self.assertEqual(asyncio.run(dlna_asgi.renderers()),
                         api_browse.renderers_payload())


class TestBrowseNativePorts(unittest.TestCase):
    """Browse-navigation reads ported native — validation matches the legacy
    400 bodies, and the happy path delegates to the shared DB methods."""

    _BROWSE = ("/api/artists", "/api/albums", "/api/genres",
               "/api/artist_albums", "/api/artist_tracks",
               "/api/genre_albums", "/api/genre_tracks", "/api/album_tracks")

    def test_registered_exactly_once(self):
        for path in self._BROWSE:
            n = sum(1 for r in dlna_asgi.app.routes
                    if getattr(r, "path", None) == path)
            self.assertEqual(n, 1, path)

    def test_validation_400_bodies_match_legacy(self):
        import json
        cases = [
            (dlna_asgi.artists(), "Missing udn"),
            (dlna_asgi.artist_albums(udn="u"), "Missing udn or artist"),
            (dlna_asgi.genre_albums(udn="u"), "Missing udn or genre"),
            (dlna_asgi.album_tracks(udn="u"), "Missing udn or album/album_key"),
        ]
        for coro, msg in cases:
            resp = asyncio.run(coro)
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(json.loads(bytes(resp.body)), {"error": msg})

    def test_happy_path_delegates_to_db(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.DB, "all_artists",
                               return_value=[{"artist": "A"}]) as m:
            self.assertEqual(asyncio.run(dlna_asgi.artists(udn="u")),
                             [{"artist": "A"}])
            m.assert_called_once_with("u")

    def test_album_tracks_wraps_and_touches(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.DB, "album_tracks",
                               return_value=[{"t": 1}]) as mt, \
             mock.patch.object(dlna_asgi.SERVERS, "touch") as mtouch:
            out = asyncio.run(dlna_asgi.album_tracks(udn="u", album="X"))
            self.assertEqual(out, {"tracks": [{"t": 1}]})
            mtouch.assert_called_once_with("u")


if __name__ == "__main__":
    unittest.main(verbosity=2)
