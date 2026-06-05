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


class TestDecadeSearchNativePorts(unittest.TestCase):
    _PATHS = ("/api/decades", "/api/decade_albums", "/api/decade_tracks",
              "/api/search", "/api/browse_letter")

    def test_registered_exactly_once(self):
        for path in self._PATHS:
            n = sum(1 for r in dlna_asgi.app.routes
                    if getattr(r, "path", None) == path)
            self.assertEqual(n, 1, path)

    def test_decade_validation(self):
        import json
        # missing decade
        r = asyncio.run(dlna_asgi.decade_albums(udn="u"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(json.loads(bytes(r.body)), {"error": "Missing udn or decade"})
        # non-integer decade
        r = asyncio.run(dlna_asgi.decade_albums(udn="u", decade="abc"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(json.loads(bytes(r.body)),
                         {"error": "decade must be an integer"})

    def test_decade_happy_parses_int(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.DB, "decade_albums",
                               return_value=[{"a": 1}]) as m:
            self.assertEqual(asyncio.run(
                dlna_asgi.decade_albums(udn="u", decade="1980")), [{"a": 1}])
            m.assert_called_once_with("u", 1980)

    def test_search_validation_order(self):
        import json
        # no q AND no udn → "Missing q" first (matches legacy order)
        r = asyncio.run(dlna_asgi.search())
        self.assertEqual(json.loads(bytes(r.body)), {"error": "Missing q"})
        r = asyncio.run(dlna_asgi.search(q="x"))
        self.assertEqual(json.loads(bytes(r.body)), {"error": "Missing udn"})

    def test_search_indexing_guard(self):
        # IndexState.status is a read-only property — drive it via update().
        from unittest import mock
        self.addCleanup(lambda: dlna_asgi.INDEXER.state.update(status="idle"))
        dlna_asgi.INDEXER.state.update(status="running")
        with mock.patch.object(dlna_asgi.DB, "track_count", return_value=0):
            out = asyncio.run(dlna_asgi.search(udn="u", q="love"))
            self.assertEqual(out.get("info"), "Indexing — please wait")
            self.assertEqual(out["tracks"], [])

    def test_search_happy_touches(self):
        from unittest import mock
        self.addCleanup(lambda: dlna_asgi.INDEXER.state.update(status="idle"))
        dlna_asgi.INDEXER.state.update(status="idle")
        with mock.patch.object(dlna_asgi.DB, "search",
                               return_value={"tracks": [1], "albums": [], "artists": []}) as ms, \
             mock.patch.object(dlna_asgi.SERVERS, "touch") as mt:
            out = asyncio.run(dlna_asgi.search(udn="u", q=" love "))
            ms.assert_called_once_with("u", "love")   # stripped
            mt.assert_called_once_with("u")
            self.assertEqual(out["tracks"], [1])

    def test_browse_letter_uppercases_and_delegates(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.DB, "browse_letter",
                               return_value={"items": []}) as m:
            asyncio.run(dlna_asgi.browse_letter(udn="u", mode="albums",
                                                letter="b", offset=5, limit=20))
            m.assert_called_once_with("u", "albums", "B", 5, 20)


class TestStatusPlaylistFavNativePorts(unittest.TestCase):
    _PATHS = ("/api/index/status", "/api/track_meta", "/api/playlists",
              "/api/playlist", "/api/album_favourites",
              "/api/album_favourites/check", "/api/radio/favourites")

    def test_registered_exactly_once(self):
        for path in self._PATHS:
            n = sum(1 for r in dlna_asgi.app.routes
                    if getattr(r, "path", None) == path)
            self.assertEqual(n, 1, path)

    def test_index_status_shape(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.DB, "track_count", return_value=42):
            out = asyncio.run(dlna_asgi.index_status(udn="u"))
            self.assertEqual(out["db_tracks"], 42)
            self.assertIn("status", out)            # merged INDEXER.state.get()

    def test_track_meta_errors_and_happy(self):
        import json
        from unittest import mock
        r = asyncio.run(dlna_asgi.track_meta())          # no url
        self.assertEqual((r.status_code, json.loads(bytes(r.body))),
                         (400, {"error": "missing url"}))
        with mock.patch.object(dlna_asgi.DB, "track_meta_by_url",
                               return_value=None):
            r = asyncio.run(dlna_asgi.track_meta(url="x"))
            self.assertEqual(r.status_code, 404)
        with mock.patch.object(dlna_asgi.DB, "track_meta_by_url",
                               return_value={"title": "T"}):
            self.assertEqual(asyncio.run(dlna_asgi.track_meta(url="x")),
                             {"title": "T"})

    def test_playlist_404(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.DB, "pl_get", return_value=None):
            r = asyncio.run(dlna_asgi.playlist(id="nope"))
            self.assertEqual(r.status_code, 404)

    def test_album_fav_check_validation_and_happy(self):
        from unittest import mock
        r = asyncio.run(dlna_asgi.album_favourite_check())   # no album/key
        self.assertEqual(r.status_code, 400)
        with mock.patch.object(dlna_asgi.DB, "album_fav_is",
                               return_value=True) as m:
            self.assertEqual(
                asyncio.run(dlna_asgi.album_favourite_check(
                    artist="A", album="B")), {"is_favourite": True})
            m.assert_called_once_with("A", "B", "")

    def test_radio_favourites_shape(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.DB, "radio_fav_list",
                               return_value=[{"name": "S"}]):
            out = asyncio.run(dlna_asgi.radio_favourites())
            self.assertEqual(out["stations"], [{"name": "S"}])
            self.assertEqual(out["limit"], dlna_asgi.DB.RADIO_FAV_MAX)


class TestPostBridge(unittest.TestCase):
    """The write API runs under Hypercorn via the bridge (POST handlers get
    the raw body). Device /gw/* POSTs stay on the legacy LAN server."""

    def _post_paths(self):
        out = set()
        for r in dlna_asgi.app.routes:
            if "POST" in (getattr(r, "methods", None) or set()):
                out.add(getattr(r, "path", None))
        return out

    def test_post_routes_bridged(self):
        p = self._post_paths()
        for path in ("/api/render_queue", "/api/render", "/api/control",
                     "/api/edit_track", "/api/client_log",
                     "/api/radio/favourites/add",
                     "/api/radio/favourites/remove",
                     "/api/radio/favourites/reorder"):
            self.assertIn(path, p, path)

    def test_device_post_not_bridged(self):
        self.assertNotIn("/gw/cd/control", self._post_paths())

    def test_bridge_runs_post_handler_with_body(self):
        # The shim passes the raw POST body to the legacy (h, body) handler.
        seen = {}

        def fake_post(h, body):
            seen["body"] = body
            h._json(200, {"ok": True})
        code, body, _ = run_legacy_sync(fake_post, '{"x":1}', command="POST")
        self.assertEqual(seen["body"], '{"x":1}')
        self.assertEqual((code, json.loads(body)), (200, {"ok": True}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
