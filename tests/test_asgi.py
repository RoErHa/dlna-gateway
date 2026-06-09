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
import types
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dlna_asgi
import dlna_events
import dlna_gateway
import api_subsonic
from dlna_asgi_bridge import run_legacy_sync, run_subsonic_sync
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

    def test_streams_native_and_device_routes_absent(self):
        # /art + /stream + /radio_stream are all native byte relays now;
        # only the /gw/* UPnP device routes stay on the legacy LAN server.
        p = self._paths()
        for native in ("/art", "/stream", "/radio_stream"):
            self.assertIn(native, p, native)
        self.assertNotIn("/gw/device.xml", p)

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


class TestArtProxy(unittest.TestCase):
    """The /art image proxy ported native (shares api_playback.art_fetch)."""

    def test_registered_once_native(self):
        n = sum(1 for r in dlna_asgi.app.routes
                if getattr(r, "path", None) == "/art")
        self.assertEqual(n, 1)

    def test_art_fetch_validation(self):
        import api_playback
        self.assertEqual(api_playback.art_fetch("")[0], 400)        # missing
        self.assertEqual(api_playback.art_fetch("ftp://x/y")[0], 400)  # scheme
        self.assertEqual(api_playback.art_fetch("no-scheme")[0], 400)

    def test_art_route_maps_fetch_result(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.api_playback, "art_fetch",
                               return_value=(400, "Missing url", b"")):
            r = asyncio.run(dlna_asgi.art(url=""))
            self.assertEqual(r.status_code, 400)
        with mock.patch.object(dlna_asgi.api_playback, "art_fetch",
                               return_value=(200, "image/png", b"PNGDATA")):
            r = asyncio.run(dlna_asgi.art(url="x"))
            self.assertEqual((r.status_code, r.media_type), (200, "image/png"))
            self.assertEqual(bytes(r.body), b"PNGDATA")
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")


class _FakeResp:
    def __init__(self, status, headers, chunks):
        self.status = status
        self._h = headers
        self._chunks = list(chunks)

    def getheader(self, name):
        for k, v in self._h.items():
            if k.lower() == name.lower():
                return v
        return None

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""


class _FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class TestStreamProxy(unittest.TestCase):
    """The /stream Range relay ported native as a StreamingResponse."""

    def test_registered_once(self):
        n = sum(1 for r in dlna_asgi.app.routes
                if getattr(r, "path", None) == "/stream")
        self.assertEqual(n, 1)

    def test_normalize_ctype(self):
        import dlna_stream_proxy as p
        self.assertEqual(p.normalize_audio_ctype("audio/x-flac"), "audio/flac")
        self.assertEqual(p.normalize_audio_ctype("audio/x-m4a"), "audio/mp4")
        self.assertEqual(p.normalize_audio_ctype("audio/mpeg"), "audio/mpeg")
        self.assertEqual(p.normalize_audio_ctype(""), "application/octet-stream")

    def test_missing_url_400(self):
        req = types.SimpleNamespace(headers={})
        r = asyncio.run(dlna_asgi.stream(req, url=""))
        self.assertEqual(r.status_code, 400)

    def test_upstream_unreachable_502(self):
        from unittest import mock
        req = types.SimpleNamespace(headers={"range": ""})
        with mock.patch.object(dlna_asgi.dlna_stream_proxy,
                               "open_stream_upstream",
                               return_value=(None, None)):
            r = asyncio.run(dlna_asgi.stream(req, url="http://x/a.flac"))
        self.assertEqual(r.status_code, 502)

    def test_relays_206_headers_body_and_closes_conn(self):
        from unittest import mock
        conn = _FakeConn()
        resp = _FakeResp(206, {"Content-Type": "audio/x-flac",
                               "Content-Range": "bytes 0-9/100",
                               "Content-Length": "10",
                               "Accept-Ranges": "bytes"}, [b"0123456789"])
        req = types.SimpleNamespace(headers={"range": "bytes=0-9"})
        with mock.patch.object(dlna_asgi.dlna_stream_proxy,
                               "open_stream_upstream",
                               return_value=(conn, resp)):
            r = asyncio.run(dlna_asgi.stream(req, url="http://x/a.flac"))
        self.assertEqual(r.status_code, 206)
        self.assertEqual(r.media_type, "audio/flac")            # normalized
        self.assertEqual(r.headers.get("content-range"), "bytes 0-9/100")
        self.assertEqual(r.headers.get("accept-ranges"), "bytes")

        body = b""

        async def _consume():
            nonlocal body
            async for c in r.body_iterator:
                body += c
        asyncio.run(_consume())
        self.assertEqual(body, b"0123456789")
        self.assertTrue(conn.closed)            # generator closed the upstream


class TestRadioStreamProxy(unittest.TestCase):
    """The /radio_stream ICY relay ported native as a StreamingResponse."""

    def test_registered_once(self):
        n = sum(1 for r in dlna_asgi.app.routes
                if getattr(r, "path", None) == "/radio_stream")
        self.assertEqual(n, 1)

    def test_in_streaming_set_not_bridged(self):
        self.assertIn("/radio_stream", dlna_asgi._STREAMING)
        self.assertFalse(dlna_asgi._bridgeable("/radio_stream"))

    def test_missing_url_400(self):
        req = types.SimpleNamespace(headers={})
        r = asyncio.run(dlna_asgi.radio_stream(req, url=""))
        self.assertEqual(r.status_code, 400)

    def test_upstream_unreachable_502(self):
        from unittest import mock
        req = types.SimpleNamespace(headers={})
        with mock.patch.object(dlna_asgi.dlna_stream_proxy,
                               "open_radio_upstream",
                               return_value=(None, None, 0, "")):
            r = asyncio.run(dlna_asgi.radio_stream(req, url="http://x/s"))
        self.assertEqual(r.status_code, 502)

    def test_plain_relay_no_metaint(self):
        from unittest import mock
        conn = _FakeConn()
        resp = _FakeResp(200, {"Content-Type": "audio/mpeg"},
                         [b"aaa", b"bbb"])
        req = types.SimpleNamespace(headers={})
        with mock.patch.object(dlna_asgi.dlna_stream_proxy,
                               "open_radio_upstream",
                               return_value=(conn, resp, 0, "audio/mpeg")):
            r = asyncio.run(dlna_asgi.radio_stream(req, url="http://x/s"))
        self.assertEqual(r.media_type, "audio/mpeg")

        body = b""

        async def _consume():
            nonlocal body
            async for c in r.body_iterator:
                body += c
        asyncio.run(_consume())
        self.assertEqual(body, b"aaabbb")
        self.assertTrue(conn.closed)

    def test_deinterleaves_and_parks_title(self):
        """metaint=4: [4 audio][len byte][meta] repeating. Audio is relayed
        clean; StreamTitle is parked via _icy_set for nowplaying."""
        import dlna_stream_proxy as p
        from unittest import mock
        meta = b"StreamTitle='Foo - Bar';"
        # pad meta to a 16-byte multiple, length byte = blocks
        pad = (-len(meta)) % 16
        block = meta + b"\x00" * pad
        lenbyte = bytes([len(block) // 16])
        stream = [b"AAAA", lenbyte, block, b"BBBB", b"\x00", b""]
        conn = _FakeConn()
        resp = _FakeResp(200, {"Content-Type": "audio/aac"}, stream)
        url = "http://x/icy"
        with mock.patch.object(dlna_asgi.dlna_stream_proxy,
                               "open_radio_upstream",
                               return_value=(conn, resp, 4, "audio/aac")):
            req = types.SimpleNamespace(headers={})
            r = asyncio.run(dlna_asgi.radio_stream(req, url=url))

        body = b""

        async def _consume():
            nonlocal body
            async for c in r.body_iterator:
                body += c
        asyncio.run(_consume())
        self.assertEqual(body, b"AAAABBBB")        # metadata stripped
        self.assertEqual((p.icy_now(url) or {}).get("title"), "Foo - Bar")
        self.assertTrue(conn.closed)


class TestSubsonicAsgi(unittest.TestCase):
    """The Subsonic /rest/* surface under ASGI: JSON/XML methods bridged via
    run_subsonic_sync, byte methods (stream/download/getCoverArt) native."""

    def setUp(self):
        self._prev = api_subsonic.SUBSONIC_PASSWORD_OVERRIDE
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "pw"

    def tearDown(self):
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = self._prev

    @staticmethod
    def _req(query, method="GET", headers=None):
        import urllib.parse
        if isinstance(query, dict):
            query = urllib.parse.urlencode(query)
        return types.SimpleNamespace(
            url=types.SimpleNamespace(query=query),
            method=method, headers=headers or {})

    def _call(self, rest_path, query, **kw):
        return asyncio.run(dlna_asgi.subsonic(self._req(query, **kw),
                                              rest_path=rest_path))

    def test_route_registered_once(self):
        n = sum(1 for r in dlna_asgi.app.routes
                if getattr(r, "path", None) == "/rest/{rest_path:path}")
        self.assertEqual(n, 1)

    def test_ping_bridged_json(self):
        r = self._call("ping", {"u": "user", "p": "pw", "f": "json", "c": "t"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.media_type, "application/json")
        self.assertEqual(json.loads(r.body)["subsonic-response"]["status"], "ok")

    def test_ping_bridged_xml_default(self):
        r = self._call("ping", {"u": "user", "p": "pw"})
        self.assertEqual(r.media_type, "text/xml")
        self.assertIn(b"subsonic-response", r.body)

    def test_ping_view_suffix_routes(self):
        r = self._call("ping.view", {"u": "user", "p": "pw", "f": "json"})
        self.assertEqual(json.loads(r.body)["subsonic-response"]["status"], "ok")

    def test_bad_auth_failed(self):
        r = self._call("ping", {"u": "user", "p": "WRONG", "f": "json"})
        self.assertEqual(json.loads(r.body)["subsonic-response"]["status"],
                         "failed")

    def test_byte_method_password_unset_503(self):
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = ""
        tid = api_subsonic._track_id("http://x/a.flac")
        r = self._call("stream", {"u": "user", "p": "pw", "id": tid,
                                  "f": "json"})
        self.assertEqual(r.status_code, 503)

    def test_stream_happy_relays(self):
        from unittest import mock
        tid = api_subsonic._track_id("http://x/a.flac")
        conn = _FakeConn()
        resp = _FakeResp(200, {"Content-Type": "audio/flac",
                               "Content-Length": "4"}, [b"FLAC"])
        with mock.patch.object(dlna_asgi.dlna_stream_proxy,
                               "open_stream_upstream",
                               return_value=(conn, resp)):
            r = self._call("stream", {"u": "user", "p": "pw", "id": tid})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.media_type, "audio/flac")
        body = b""

        async def _consume():
            nonlocal body
            async for c in r.body_iterator:
                body += c
        asyncio.run(_consume())
        self.assertEqual(body, b"FLAC")
        self.assertTrue(conn.closed)

    def test_download_aliases_stream(self):
        from unittest import mock
        tid = api_subsonic._track_id("http://x/a.flac")
        with mock.patch.object(dlna_asgi.dlna_stream_proxy,
                               "open_stream_upstream",
                               return_value=(_FakeConn(),
                                             _FakeResp(200, {}, [b"X"]))):
            r = self._call("download", {"u": "user", "p": "pw", "id": tid})
        self.assertEqual(r.status_code, 200)

    def test_stream_bad_id_not_found(self):
        r = self._call("stream", {"u": "user", "p": "pw", "id": "garbage",
                                  "f": "json"})
        self.assertEqual(json.loads(r.body)["subsonic-response"]["status"],
                         "failed")

    def test_cover_art_happy(self):
        from unittest import mock
        # Native getCoverArt now serves via _resolve_cover (tries each candidate
        # art URL until one fetches 200).
        with mock.patch.object(dlna_asgi.api_subsonic, "_resolve_cover",
                               return_value=(200, "image/jpeg", b"JPG")):
            r = self._call("getCoverArt", {"u": "user", "p": "pw",
                                           "id": "al:whatever"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.media_type, "image/jpeg")
        self.assertEqual(r.body, b"JPG")

    def test_cover_art_missing_404(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.api_subsonic, "_resolve_cover",
                               return_value=(404, "no art", b"")):
            r = self._call("getCoverArt", {"u": "user", "p": "pw", "id": "al:x"})
        self.assertEqual(r.status_code, 404)


class TestEntrypointLifespan(unittest.TestCase):
    """The ASGI lifespan boots the gateway's background services (so
    `hypercorn dlna_asgi:app` runs standalone) via the same
    dlna_gateway.start_background_services() the stdlib main() uses."""

    def setUp(self):
        self._env = {k: os.environ.get(k)
                     for k in ("GATEWAY_NO_SERVICES", "GATEWAY_PORT",
                               "GATEWAY_DEBUG")}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _drive_lifespan(self):
        async def _run():
            async with dlna_asgi.app.router.lifespan_context(dlna_asgi.app):
                pass
        asyncio.run(_run())

    def test_extracted_function_exists(self):
        self.assertTrue(callable(dlna_gateway.start_background_services))

    def test_lifespan_registered(self):
        self.assertIsNotNone(dlna_asgi.app.router.lifespan_context)

    def test_disabled_by_env(self):
        os.environ["GATEWAY_NO_SERVICES"] = "1"
        with mock.patch.object(dlna_gateway, "start_background_services") as sbs, \
             mock.patch.object(dlna_asgi.dlna_server,
                               "start_device_server") as dev, \
             mock.patch.object(dlna_gateway, "gw_ssdp_byebye") as bye:
            self._drive_lifespan()
        self.assertEqual(sbs.call_count, 0)
        self.assertEqual(dev.call_count, 0)   # no device server either
        self.assertEqual(bye.call_count, 0)   # nothing started → no byebye

    def test_enabled_starts_services_and_device_server(self):
        os.environ.pop("GATEWAY_NO_SERVICES", None)
        os.environ["GATEWAY_PORT"] = "8770"
        dev_srv = mock.MagicMock()
        with mock.patch.object(dlna_gateway, "start_background_services") as sbs, \
             mock.patch.object(dlna_asgi.dlna_server, "start_device_server",
                               return_value=dev_srv) as dev, \
             mock.patch.object(dlna_gateway, "setup_logging"), \
             mock.patch.object(dlna_gateway, "get_lan_ip",
                               return_value="10.0.0.5"), \
             mock.patch.object(dlna_gateway, "gw_ssdp_byebye") as bye:
            self._drive_lifespan()
        # SSDP advert + device server both on GATEWAY_PORT (the device port)
        sbs.assert_called_once_with("10.0.0.5", 8770)
        dev.assert_called_once_with("0.0.0.0", 8770)
        dev_srv.shutdown.assert_called_once()   # device server torn down
        self.assertEqual(bye.call_count, 1)      # graceful byebye on shutdown

    def test_shutdown_failures_swallowed(self):
        os.environ.pop("GATEWAY_NO_SERVICES", None)
        dev_srv = mock.MagicMock()
        dev_srv.shutdown.side_effect = RuntimeError("dev boom")
        with mock.patch.object(dlna_gateway, "start_background_services"), \
             mock.patch.object(dlna_asgi.dlna_server, "start_device_server",
                               return_value=dev_srv), \
             mock.patch.object(dlna_gateway, "setup_logging"), \
             mock.patch.object(dlna_gateway, "get_lan_ip", return_value="x"), \
             mock.patch.object(dlna_gateway, "gw_ssdp_byebye",
                               side_effect=RuntimeError("boom")):
            self._drive_lifespan()   # must not raise


class _SseReq:
    """Fake Request whose is_disconnected() returns True after N calls."""
    def __init__(self, disconnect_after=1_000_000):
        self._n = 0
        self._after = disconnect_after

    async def is_disconnected(self):
        self._n += 1
        return self._n > self._after


class TestSSE(unittest.TestCase):
    """R2 Server-Sent Events: the dlna_events bus + the /api/events stream."""

    def tearDown(self):
        dlna_events.EVENTS.bind_loop(None)   # don't leak a closed test loop

    # ── the bus (no endpoint) ────────────────────────────────────────
    def test_sse_format_shape(self):
        f = dlna_events.sse_format({"type": "state", "x": 1})
        self.assertTrue(f.startswith("event: state\n"))
        self.assertIn('"x": 1', f)
        self.assertTrue(f.endswith("\n\n"))

    def test_sse_format_defaults_type_message(self):
        self.assertTrue(dlna_events.sse_format({"a": 1}).startswith(
            "event: message\n"))

    def test_publish_without_loop_is_noop(self):
        dlna_events.EventBus().publish({"type": "x"})   # must not raise

    def test_bus_publish_subscribe_unsubscribe(self):
        async def scenario():
            bus = dlna_events.EventBus()
            bus.bind_loop(asyncio.get_running_loop())
            q = bus.subscribe()
            self.assertEqual(bus.subscriber_count, 1)
            bus.publish({"type": "x", "n": 1})
            ev = await asyncio.wait_for(q.get(), 1)
            bus.unsubscribe(q)
            self.assertEqual(bus.subscriber_count, 0)
            return ev
        self.assertEqual(asyncio.run(scenario())["n"], 1)

    def test_bus_drops_when_subscriber_full(self):
        async def scenario():
            bus = dlna_events.EventBus(max_queue=1)
            bus.bind_loop(asyncio.get_running_loop())
            q = bus.subscribe()
            bus.publish({"type": "a"})
            bus.publish({"type": "b"})       # queue full → dropped
            await asyncio.sleep(0)           # let call_soon callbacks run
            return q.qsize()
        self.assertEqual(asyncio.run(scenario()), 1)

    # ── the /api/events endpoint ─────────────────────────────────────
    def test_route_registered_once(self):
        n = sum(1 for r in dlna_asgi.app.routes
                if getattr(r, "path", None) == "/api/events")
        self.assertEqual(n, 1)

    def test_endpoint_hello_then_published_event(self):
        async def scenario():
            dlna_events.EVENTS.bind_loop(asyncio.get_running_loop())
            r = await dlna_asgi.events(_SseReq())
            self.assertEqual(r.media_type, "text/event-stream")
            it = r.body_iterator
            first = await it.__anext__()
            dlna_events.EVENTS.publish({"type": "now_playing", "title": "S"})
            second = await it.__anext__()
            await it.aclose()
            return first, second
        first, second = asyncio.run(scenario())
        self.assertIn("event: hello", first)
        self.assertIn("event: now_playing", second)
        self.assertIn('"title": "S"', second)

    def test_endpoint_heartbeat_on_idle(self):
        prev = dlna_asgi._SSE_HEARTBEAT_SEC
        dlna_asgi._SSE_HEARTBEAT_SEC = 0.02
        try:
            async def scenario():
                dlna_events.EVENTS.bind_loop(asyncio.get_running_loop())
                r = await dlna_asgi.events(_SseReq())
                it = r.body_iterator
                await it.__anext__()                 # hello
                frame = await it.__anext__()         # no event → keepalive
                await it.aclose()
                return frame
            self.assertTrue(asyncio.run(scenario()).startswith(":"))
        finally:
            dlna_asgi._SSE_HEARTBEAT_SEC = prev

    def test_endpoint_unsubscribes_on_disconnect(self):
        async def scenario():
            dlna_events.EVENTS.bind_loop(asyncio.get_running_loop())
            before = dlna_events.EVENTS.subscriber_count
            r = await dlna_asgi.events(_SseReq(disconnect_after=0))
            it = r.body_iterator
            await it.__anext__()                     # hello, then disconnect
            with self.assertRaises(StopAsyncIteration):
                await it.__anext__()
            return before, dlna_events.EVENTS.subscriber_count
        before, after = asyncio.run(scenario())
        self.assertEqual(after, before)   # subscriber cleaned up

    def test_lifespan_binds_event_loop(self):
        os.environ["GATEWAY_NO_SERVICES"] = "1"
        try:
            async def _run():
                async with dlna_asgi.app.router.lifespan_context(dlna_asgi.app):
                    # inside the lifespan the bus is bound → publish reaches a sub
                    q = dlna_events.EVENTS.subscribe()
                    dlna_events.EVENTS.publish({"type": "tick"})
                    ev = await asyncio.wait_for(q.get(), 1)
                    dlna_events.EVENTS.unsubscribe(q)
                    return ev
            self.assertEqual(asyncio.run(_run())["type"], "tick")
        finally:
            os.environ.pop("GATEWAY_NO_SERVICES", None)


class TestLastNativePorts(unittest.TestCase):
    """The final bridged reads ported native via the *_payload pattern:
    /api/browse, /api/radio, /api/radio/search, /api/radio/nowplaying,
    /api/lyrics. Phase 2 fully off the bridge for reads."""

    _PATHS = ("/api/browse", "/api/radio", "/api/radio/search",
              "/api/radio/nowplaying", "/api/lyrics")

    def test_registered_once_native_not_bridged(self):
        paths = [getattr(r, "path", None) for r in dlna_asgi.app.routes]
        for p in self._PATHS:
            self.assertEqual(paths.count(p), 1, p)
            self.assertIn(p, dlna_asgi._NATIVE, p)
            self.assertFalse(dlna_asgi._bridgeable(p), p)

    @staticmethod
    def _call(route, **qp):
        req = types.SimpleNamespace(query_params=qp)
        return asyncio.run(route(req))

    def test_validation_bodies_match_legacy(self):
        r = self._call(dlna_asgi.radio_route)               # missing udn
        self.assertEqual(r.status_code, 400)
        self.assertEqual(json.loads(r.body)["error"], "Missing udn")

        r = self._call(dlna_asgi.radio_search_route)        # no q/country/tag
        self.assertEqual(r.status_code, 400)
        self.assertIn("need one of", json.loads(r.body)["error"])

        r = self._call(dlna_asgi.radio_nowplaying_route)    # no stream/udn
        self.assertEqual(r.status_code, 400)

        r = self._call(dlna_asgi.lyrics_route)              # no url
        self.assertEqual(r.status_code, 400)

        r = self._call(dlna_asgi.browse_route, udn="nope")  # unknown server
        self.assertEqual(r.status_code, 404)

    def test_nowplaying_icy_path_delegates(self):
        from unittest import mock
        with mock.patch.object(dlna_asgi.api_radio.dlna_stream_proxy,
                               "icy_now", return_value={"title": "Foo - Bar"}):
            r = self._call(dlna_asgi.radio_nowplaying_route, stream="http://s")
        self.assertEqual(r.status_code, 200)
        body = json.loads(r.body)
        self.assertEqual(body["title"], "Foo - Bar")
        self.assertEqual(body["source"], "icy")


class TestLoopExceptionHandler(unittest.TestCase):
    """Benign client-transport teardown (TimeoutError/ETIMEDOUT a client left)
    is swallowed; real errors pass through to the default handler."""

    def test_swallows_benign_client_teardown(self):
        loop = mock.MagicMock()
        ctx = {"message": "Unhandled exception in client_connected_cb",
               "exception": TimeoutError("[Errno 60] Operation timed out")}
        dlna_asgi._loop_exception_handler(loop, ctx)
        loop.default_exception_handler.assert_not_called()

    def test_passes_through_real_errors(self):
        loop = mock.MagicMock()
        ctx = {"message": "Task exception was never retrieved",
               "exception": ValueError("boom")}
        dlna_asgi._loop_exception_handler(loop, ctx)
        loop.default_exception_handler.assert_called_once_with(ctx)


class TestFdMonitor(unittest.TestCase):
    """The FD watchdog (dlna_fdmon) — diagnostic for the EMFILE crash."""

    def test_fd_count_and_limit_positive(self):
        import dlna_fdmon
        self.assertGreater(dlna_fdmon.fd_count(), 0)
        self.assertGreater(dlna_fdmon.soft_limit(), 0)

    def test_lsof_breakdown_returns_summary(self):
        import dlna_fdmon
        import os as _os
        bd = dlna_fdmon.lsof_breakdown(_os.getpid())
        self.assertIsInstance(bd, str)
        self.assertTrue("types=" in bd or "unavailable" in bd, bd)

    def test_start_fd_monitor_spawns_named_daemon(self):
        import dlna_fdmon
        import threading as _t
        before = {x.name for x in _t.enumerate()}
        dlna_fdmon.start_fd_monitor(interval=9999)   # won't tick during the test
        names = {x.name for x in _t.enumerate()}
        self.assertIn("fd-monitor", names)
        mon = next(x for x in _t.enumerate() if x.name == "fd-monitor")
        self.assertTrue(mon.daemon)


class TestSSEPublishers(unittest.TestCase):
    """R2 slice 2: backend publishers (index status, device discovery). The
    RendererQueue 'state' publisher is covered in test_player."""

    def test_index_state_publishes_only_on_status_change(self):
        from dlna_indexer import IndexState
        with mock.patch.object(dlna_events.EVENTS, "publish") as pub:
            st = IndexState()
            st.update(progress=1)                  # idle→idle: no publish
            st.update(status="running")            # idle→running: publish
            st.update(progress=2, total=9)         # running→running: no publish
            st.update(status="done", tracks=9)     # running→done: publish
        statuses = [c.args[0]["status"] for c in pub.call_args_list]
        self.assertEqual(statuses, ["running", "done"])
        self.assertTrue(all(c.args[0]["type"] == "index"
                            for c in pub.call_args_list))

    def test_on_server_found_publishes_devices(self):
        import dlna_gateway
        fake = types.SimpleNamespace(udn="uuid:x", name="X")
        with mock.patch.object(dlna_events.EVENTS, "publish") as pub, \
             mock.patch("dlna_discovery.RENDERERS") as R, \
             mock.patch.object(dlna_gateway.INDEXER, "start") as start:
            R.get.return_value = True              # combined device → early return
            dlna_gateway._on_server_found(fake)
        self.assertTrue(any(c.args and c.args[0].get("type") == "devices"
                            for c in pub.call_args_list))
        start.assert_not_called()


class TestFdLimit(unittest.TestCase):
    """raise_fd_limit() lifts the open-file soft limit so the gateway doesn't
    hit macOS's 256 default → EMFILE → sqlite 'unable to open database file'."""

    def test_raises_low_soft_limit(self):
        import resource
        import dlna_config
        soft0, hard0 = resource.getrlimit(resource.RLIMIT_NOFILE)
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard0))
            dlna_config.raise_fd_limit(4096)
            soft1, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            want = 4096 if hard0 == resource.RLIM_INFINITY else min(4096, hard0)
            self.assertEqual(soft1, want)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft0, hard0))

    def test_never_lowers(self):
        import resource
        import dlna_config
        soft0, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        dlna_config.raise_fd_limit(1)          # target below current → no-op
        soft1, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.assertEqual(soft1, soft0)

    def test_lifespan_raises_fd_limit(self):
        # GATEWAY_NO_SERVICES=1 → services skipped, but the FD raise still runs
        os.environ["GATEWAY_NO_SERVICES"] = "1"
        try:
            with mock.patch.object(dlna_asgi, "raise_fd_limit") as r:
                async def _run():
                    async with dlna_asgi.app.router.lifespan_context(dlna_asgi.app):
                        pass
                asyncio.run(_run())
            r.assert_called_once()
        finally:
            os.environ.pop("GATEWAY_NO_SERVICES", None)


class TestDeviceServer(unittest.TestCase):
    """The /gw/*-only device-tier server (dlna_server.DeviceHandler) that runs
    alongside Hypercorn so the Naim reaches the UPnP surface over plain HTTP."""

    def test_devicehandler_subclasses_gateway(self):
        import dlna_server
        self.assertTrue(issubclass(dlna_server.DeviceHandler,
                                   dlna_server.GatewayHandler))

    def test_serves_gw_404s_everything_else(self):
        import dlna_server
        import urllib.request
        import urllib.error
        srv = dlna_server.start_device_server("127.0.0.1", 0)
        try:
            port = srv.server_address[1]
            base = f"http://127.0.0.1:{port}"
            # /gw/device.xml → 200 XML
            with urllib.request.urlopen(base + "/gw/device.xml", timeout=4) as r:
                self.assertEqual(r.status, 200)
                self.assertIn(b"<", r.read(32))
            # non-/gw paths → 404 (API + PWA shell are NOT served here)
            for p in ("/api/version", "/", "/static/app.js", "/rest/ping"):
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(base + p, timeout=4)
                self.assertEqual(cm.exception.code, 404, p)
        finally:
            srv.shutdown()


class TestStaticServing(unittest.TestCase):
    """The PWA static surface served by the ASGI app (so :8768 can load the
    app shell): /, /sw.js, /manifest.json, the generated icons, /static/*."""

    def _paths(self):
        return {getattr(r, "path", None) for r in dlna_asgi.app.routes}

    def test_routes_registered(self):
        p = self._paths()
        for path in ("/", "/index.html", "/sw.js", "/manifest.json",
                     "/icon-192.png", "/icon-512.png", "/static"):
            self.assertIn(path, p, path)

    def test_static_mount_present(self):
        self.assertTrue(any(getattr(r, "name", None) == "static"
                            for r in dlna_asgi.app.routes))

    def test_manifest_shape(self):
        r = asyncio.run(dlna_asgi._manifest())
        self.assertEqual(r.media_type, "application/manifest+json")
        m = json.loads(bytes(r.body))
        self.assertEqual(m["start_url"], "/")
        self.assertEqual(len(m["icons"]), 2)

    def test_icon_is_png(self):
        r = dlna_asgi._icon(192)
        self.assertEqual(r.media_type, "image/png")
        self.assertEqual(bytes(r.body)[:4], b"\x89PNG")

    def test_sw_root_scope_header(self):
        r = asyncio.run(dlna_asgi._service_worker())
        self.assertEqual(r.headers.get("Service-Worker-Allowed"), "/")
        self.assertIn("no-store", r.headers.get("Cache-Control", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
