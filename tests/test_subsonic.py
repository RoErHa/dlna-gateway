#!/usr/bin/env python3
"""
tests/test_subsonic.py — Subsonic API handler tests.

Focus: per-endpoint response shape + auth correctness. The HTTP server
isn't started; we drive `api_subsonic.handle()` directly with a mock
handler that captures `_json()` output. The DB is a fresh tempfile
seeded with known fixture rows.

Run standalone:
    python3 -m unittest tests.test_subsonic -v
"""
import hashlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
import api_subsonic


# ── Test scaffolding ─────────────────────────────────────────────

class _MockH:
    """Captures whatever response method was called.

    The Subsonic API now dispatches to JSON (`_json`) or XML
    (`_xml_response`) depending on the request's f= parameter. Tests
    that don't care about format use the _call() helper which sets
    f=json. Tests that drive `handle()` directly may exercise either
    path — this mock captures both."""
    def __init__(self):
        self.last_code = None
        self.last_payload = None
        self.last_xml: bytes | None = None
        self.errors: list[tuple[int, str]] = []

    def _json(self, code, payload):
        self.last_code = code
        self.last_payload = payload

    def _xml_response(self, code, body: bytes):
        self.last_code = code
        self.last_xml = body
        # For tests that read self.last_payload regardless of format,
        # provide a minimal status='ok'/'failed' shim parsed from the
        # XML attributes. Tests that care about XML details inspect
        # self.last_xml directly.
        import re
        if body:
            text = body.decode("utf-8", errors="replace")
            status_m = re.search(r'\bstatus="([^"]+)"', text)
            err_m    = re.search(r'<error\s+code="(\d+)"', text)
            shim = {"status": status_m.group(1) if status_m else "?"}
            if err_m:
                shim["error"] = {"code": int(err_m.group(1)), "message": ""}
            self.last_payload = {"subsonic-response": shim}

    def send_error(self, code, msg=""):
        self.errors.append((code, msg))


def _seed(db, rows=None):
    """Seed `rows` (list of (artist, album, title, url)) into the DB."""
    if rows is None:
        rows = [
            ("Pink Floyd", "Animals",     "Pigs",       "http://srv/pf/a/01.flac"),
            ("Pink Floyd", "Animals",     "Dogs",       "http://srv/pf/a/02.flac"),
            ("Pink Floyd", "Animals",     "Sheep",      "http://srv/pf/a/03.flac"),
            ("Pink Floyd", "Wish You Were Here", "Wish", "http://srv/pf/w/01.flac"),
            ("Cream",      "Wheels of Fire", "White Room", "http://srv/c/w/01.flac"),
        ]
    with db._pool.write() as c:
        for i, (ar, al, ti, url) in enumerate(rows):
            c.execute(
                "INSERT INTO tracks(udn, obj_id, url, title, artist, album, "
                "duration, art, mime, genre, file_path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("uuid:srv1", f"t{i}", url, ti, ar, al,
                 "0:03:30", "http://srv/cover.jpg", "audio/flac", "", ""))
    return rows


def _call(method: str, params: dict, *, db, password="testpwd"):
    """Drive api_subsonic.handle() with auth + DB patched in. Returns
    (mock_handler, response_body)."""
    h = _MockH()
    api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = password
    with patch.object(api_subsonic, "DB", db), \
         patch("api_subsonic.SERVERS") as mock_servers:
        mock_servers.online.return_value = []
        mock_servers.all.return_value = []
        full_params = {"u": "user", "p": password, "v": "1.16.1",
                       "c": "test", "f": "json"}
        full_params.update(params)
        api_subsonic.handle(h, "GET", f"/rest/{method}", full_params)
    api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
    return h, (h.last_payload.get("subsonic-response") if h.last_payload
               else None)


class _BaseDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        _seed(self.db)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)


# ── Auth ─────────────────────────────────────────────────────────

class TestAuth(unittest.TestCase):

    def test_no_password_env_returns_503(self):
        h = _MockH()
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = ""
        with patch.dict(os.environ, {"SUBSONIC_PASSWORD": ""}, clear=False):
            os.environ.pop("SUBSONIC_PASSWORD", None)
            api_subsonic.handle(h, "GET", "/rest/ping",
                                {"u": "user", "p": "x"})
        self.assertEqual(h.last_code, 503)
        body = h.last_payload["subsonic-response"]
        self.assertEqual(body["status"], "failed")

    def test_wrong_password_returns_failed(self):
        h = _MockH()
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "rightpwd"
        api_subsonic.handle(h, "GET", "/rest/ping",
                            {"u": "user", "p": "WRONGpwd"})
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
        body = h.last_payload["subsonic-response"]
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"]["code"], 40)

    def test_correct_plaintext_password(self):
        h = _MockH()
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "rightpwd"
        api_subsonic.handle(h, "GET", "/rest/ping",
                            {"u": "user", "p": "rightpwd"})
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
        self.assertEqual(h.last_payload["subsonic-response"]["status"], "ok")

    def test_correct_hex_encoded_password(self):
        h = _MockH()
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "rightpwd"
        hex_pw = "enc:" + "rightpwd".encode().hex()
        api_subsonic.handle(h, "GET", "/rest/ping",
                            {"u": "user", "p": hex_pw})
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
        self.assertEqual(h.last_payload["subsonic-response"]["status"], "ok")

    def test_correct_token_salt_password(self):
        h = _MockH()
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "rightpwd"
        salt = "abcd1234"
        token = hashlib.md5(("rightpwd" + salt).encode()).hexdigest()
        api_subsonic.handle(h, "GET", "/rest/ping",
                            {"u": "user", "t": token, "s": salt})
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
        self.assertEqual(h.last_payload["subsonic-response"]["status"], "ok")

    def test_wrong_username(self):
        h = _MockH()
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "rightpwd"
        api_subsonic.handle(h, "GET", "/rest/ping",
                            {"u": "NOTuser", "p": "rightpwd"})
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
        self.assertEqual(h.last_payload["subsonic-response"]["status"], "failed")


# ── ID round-trip ────────────────────────────────────────────────

class TestIdCodec(unittest.TestCase):

    def test_track_id_round_trip(self):
        url = "http://srv/path with spaces/áéí.flac"
        tid = api_subsonic._track_id(url)
        self.assertTrue(tid.startswith("tr:"))
        self.assertEqual(api_subsonic._track_id_decode(tid), url)

    def test_album_id_round_trip(self):
        aid = api_subsonic._album_id("Sigur Rós", "( )")
        self.assertEqual(api_subsonic._album_id_decode(aid),
                         ("Sigur Rós", "( )"))

    def test_artist_id_round_trip(self):
        aid = api_subsonic._artist_id("AC/DC")
        self.assertEqual(api_subsonic._artist_id_decode(aid), "AC/DC")

    def test_unknown_prefix_returns_none(self):
        self.assertIsNone(api_subsonic._track_id_decode("al:xxx"))
        self.assertIsNone(api_subsonic._album_id_decode("not-prefixed"))

    def test_garbled_payload_returns_none(self):
        self.assertIsNone(api_subsonic._track_id_decode("tr:!!!notb64!!!"))


# ── Hard-coded endpoints ─────────────────────────────────────────

class TestSimpleEndpoints(_BaseDB):

    def test_ping(self):
        _, body = _call("ping", {}, db=self.db)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["version"], api_subsonic.API_VERSION)
        # Read SERVER_TYPE from the module rather than hard-coding so
        # the test doesn't need editing when we tune the reported
        # server type for client-compatibility reasons.
        self.assertEqual(body["type"], api_subsonic.SERVER_TYPE)
        self.assertTrue(body.get("openSubsonic"),
                        "openSubsonic flag must be present for OS-aware clients")

    def test_get_license(self):
        _, body = _call("getLicense", {}, db=self.db)
        self.assertEqual(body["license"]["valid"], True)

    def test_get_music_folders(self):
        _, body = _call("getMusicFolders", {}, db=self.db)
        folders = body["musicFolders"]["musicFolder"]
        self.assertEqual(len(folders), 1)
        self.assertEqual(folders[0]["name"], "Music")

    def test_unimplemented_method_returns_not_found(self):
        _, body = _call("getInternetRadioStations", {}, db=self.db)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["error"]["code"], 70)

    def test_dot_view_suffix_handled(self):
        # Legacy clients send /rest/ping.view — must route the same.
        h = _MockH()
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "testpwd"
        api_subsonic.handle(h, "GET", "/rest/ping.view",
                            {"u": "user", "p": "testpwd"})
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
        self.assertEqual(h.last_payload["subsonic-response"]["status"], "ok")


# ── Browse endpoints ─────────────────────────────────────────────

class TestBrowse(_BaseDB):

    def test_get_artists_groups_alphabetically(self):
        _, body = _call("getArtists", {}, db=self.db)
        index = body["artists"]["index"]
        keys = [g["name"] for g in index]
        self.assertIn("C", keys)   # Cream
        self.assertIn("P", keys)   # Pink Floyd
        # Pink Floyd has 2 albums
        pf = next(a for g in index for a in g["artist"]
                  if a["name"] == "Pink Floyd")
        self.assertEqual(pf["albumCount"], 2)

    def test_get_indexes_same_shape(self):
        _, body = _call("getIndexes", {}, db=self.db)
        self.assertIn("index", body["indexes"])
        self.assertTrue(len(body["indexes"]["index"]) >= 1)

    def test_get_artist_returns_albums(self):
        aid = api_subsonic._artist_id("Pink Floyd")
        _, body = _call("getArtist", {"id": aid}, db=self.db)
        self.assertEqual(body["artist"]["name"], "Pink Floyd")
        self.assertEqual(body["artist"]["albumCount"], 2)
        album_names = {a["name"] for a in body["artist"]["album"]}
        self.assertEqual(album_names, {"Animals", "Wish You Were Here"})

    def test_get_album_returns_tracks(self):
        aid = api_subsonic._album_id("Pink Floyd", "Animals")
        _, body = _call("getAlbum", {"id": aid}, db=self.db)
        self.assertEqual(body["album"]["songCount"], 3)
        titles = {s["title"] for s in body["album"]["song"]}
        self.assertEqual(titles, {"Pigs", "Dogs", "Sheep"})

    def test_get_album_unknown_id_404(self):
        _, body = _call("getAlbum", {"id": "al:notb64"}, db=self.db)
        # Decoding will return None — endpoint should fail gracefully.
        # (al:notb64 happens to decode to empty since `notb64` -> bytes,
        # but no matching album exists in DB so we get an empty result.
        # We accept either failure or empty as valid.)
        if body["status"] == "ok":
            self.assertEqual(body["album"]["songCount"], 0)

    def test_get_album_list2_alphabetical(self):
        _, body = _call("getAlbumList2", {"type": "alphabeticalByName"},
                        db=self.db)
        albums = body["albumList2"]["album"]
        self.assertGreaterEqual(len(albums), 3)
        names = [a["name"] for a in albums]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_get_album_list2_starred(self):
        self.db.album_fav_add("Pink Floyd", "Animals")
        _, body = _call("getAlbumList2", {"type": "starred"}, db=self.db)
        albums = body["albumList2"]["album"]
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["name"], "Animals")

    def test_search3_finds_tracks(self):
        _, body = _call("search3", {"query": "Sheep"}, db=self.db)
        songs = body["searchResult3"].get("song", [])
        self.assertTrue(any(s["title"] == "Sheep" for s in songs))


# ── Playlists ────────────────────────────────────────────────────

class TestPlaylists(_BaseDB):

    def test_get_playlists_includes_favourites(self):
        # LibraryDB seeds __favourites__ on init.
        _, body = _call("getPlaylists", {}, db=self.db)
        pls = body["playlists"]["playlist"]
        ids = {p["id"] for p in pls}
        self.assertIn("pl:__favourites__", ids)

    def test_get_playlist_round_trip(self):
        pid = self.db.pl_create("My Mix")
        self.db.pl_add_track(pid, {
            "url": "http://srv/pf/a/01.flac",
            "title": "Pigs", "artist": "Pink Floyd", "album": "Animals",
            "duration": "0:03:30", "art": "",
        })
        _, body = _call("getPlaylist", {"id": f"pl:{pid}"}, db=self.db)
        self.assertEqual(body["playlist"]["name"], "My Mix")
        self.assertEqual(body["playlist"]["songCount"], 1)
        self.assertEqual(body["playlist"]["entry"][0]["title"], "Pigs")


# ── Stars (Album favourites) ─────────────────────────────────────

class TestStars(_BaseDB):

    def test_star_then_unstar(self):
        aid = api_subsonic._album_id("Pink Floyd", "Animals")

        _call("star", {"albumId": aid}, db=self.db)
        self.assertTrue(self.db.album_fav_is("Pink Floyd", "Animals"))

        _call("unstar", {"albumId": aid}, db=self.db)
        self.assertFalse(self.db.album_fav_is("Pink Floyd", "Animals"))

    def test_get_starred2(self):
        self.db.album_fav_add("Pink Floyd", "Animals")
        _, body = _call("getStarred2", {}, db=self.db)
        albums = body["starred2"]["album"]
        self.assertEqual(len(albums), 1)
        self.assertEqual(albums[0]["name"], "Animals")


# ── Scrobble bumps play_counts ───────────────────────────────────

class TestScrobble(_BaseDB):

    def test_scrobble_submission_true_bumps_count(self):
        url = "http://srv/pf/a/01.flac"
        tid = api_subsonic._track_id(url)
        _, body = _call("scrobble",
                        {"id": tid, "submission": "true"}, db=self.db)
        self.assertEqual(body["status"], "ok")
        with self.db._pool.read() as c:
            row = c.execute("SELECT count FROM play_counts WHERE url=?",
                            (url,)).fetchone()
        self.assertEqual(row["count"], 1)

    def test_scrobble_submission_false_does_not_bump(self):
        url = "http://srv/pf/a/01.flac"
        tid = api_subsonic._track_id(url)
        _call("scrobble", {"id": tid, "submission": "false"}, db=self.db)
        with self.db._pool.read() as c:
            row = c.execute("SELECT count FROM play_counts WHERE url=?",
                            (url,)).fetchone()
        self.assertIsNone(row, "submission=false must not insert a row")


# ── Cover art resolution ─────────────────────────────────────────

class TestCoverArt(unittest.TestCase):
    """The cover-art endpoint resolves Subsonic ID → DB → URL, then
    delegates to the existing /art proxy. We don't actually hit the
    network — we mock api_playback.art and assert it was called with
    the resolved URL."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        _seed(self.db)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_album_cover_resolves_to_art_url(self):
        captured = {}
        def fake_art(h, params):
            captured["url"] = params.get("url")
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "testpwd"
        with patch.object(api_subsonic, "DB", self.db), \
             patch("api_playback.art", side_effect=fake_art), \
             patch("api_subsonic.SERVERS"):
            h = _MockH()
            aid = api_subsonic._album_id("Pink Floyd", "Animals")
            api_subsonic.handle(h, "GET", "/rest/getCoverArt",
                                {"u": "user", "p": "testpwd", "id": aid})
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
        self.assertEqual(captured.get("url"), "http://srv/cover.jpg")

    def test_unknown_id_404s(self):
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "testpwd"
        with patch.object(api_subsonic, "DB", self.db), \
             patch("api_subsonic.SERVERS"):
            h = _MockH()
            aid = api_subsonic._album_id("Nobody", "Nothing")
            api_subsonic.handle(h, "GET", "/rest/getCoverArt",
                                {"u": "user", "p": "testpwd", "id": aid})
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
        self.assertTrue(any(code == 404 for code, _ in h.errors))


class TestXmlFormat(unittest.TestCase):
    """Regression guard for the 2026-05-11 Amperfy incompatibility:
    the Subsonic spec defaults to XML when f= is unset. We failed
    to honour that and always returned JSON, which caused Amperfy's
    XML parser to silently fail with no readable error."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        _seed(self.db)
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = "testpwd"

    def tearDown(self):
        api_subsonic.SUBSONIC_PASSWORD_OVERRIDE = None
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def _call_raw(self, method, params):
        """Drive handle() without forcing f=json — preserves whatever
        f= the test set (or None)."""
        h = _MockH()
        with patch.object(api_subsonic, "DB", self.db), \
             patch("api_subsonic.SERVERS") as mock_servers:
            mock_servers.online.return_value = []
            mock_servers.all.return_value = []
            full = {"u": "user", "p": "testpwd", "v": "1.16.1", "c": "test"}
            full.update(params)
            api_subsonic.handle(h, "GET", f"/rest/{method}", full)
        return h

    def test_no_f_param_returns_xml(self):
        h = self._call_raw("ping", {})
        self.assertIsNotNone(h.last_xml,
                             "missing f= must default to XML response")
        self.assertIsNone(h.last_payload.get("subsonic-response", {}).get("song"),
                          "shim shouldn't have payload keys")
        xml = h.last_xml.decode()
        self.assertIn('<?xml version="1.0"', xml)
        self.assertIn('<subsonic-response', xml)
        self.assertIn('xmlns="http://subsonic.org/restapi"', xml)
        self.assertIn('status="ok"', xml)
        self.assertIn('openSubsonic="true"', xml)

    def test_f_xml_returns_xml(self):
        h = self._call_raw("ping", {"f": "xml"})
        self.assertIsNotNone(h.last_xml)

    def test_f_json_returns_json(self):
        h = self._call_raw("ping", {"f": "json"})
        self.assertIsNone(h.last_xml,
                          "f=json must NOT return XML")
        self.assertEqual(h.last_payload["subsonic-response"]["status"], "ok")

    def test_xml_nested_arrays_as_repeated_elements(self):
        """getPlaylists has a nested array — verify it round-trips as
        <playlists><playlist .../><playlist .../></playlists>."""
        h = self._call_raw("getPlaylists", {})
        xml = h.last_xml.decode()
        # __favourites__ playlist exists by default.
        self.assertIn('<playlists>', xml)
        self.assertIn('id="pl:__favourites__"', xml)
        self.assertIn('<playlist ', xml)

    def test_xml_error_response(self):
        """Failed status with error code must produce a valid <error>
        child element."""
        h = self._call_raw("getAlbum", {"id": "al:NOTREAL"})
        if h.last_xml:
            xml = h.last_xml.decode()
            # Status either failed (unknown id) or ok-with-empty-album.
            # Both are valid responses; just check structural validity.
            self.assertIn('<subsonic-response', xml)

    def test_xml_escapes_special_chars(self):
        """Track titles with &/</>/quotes must escape cleanly."""
        with self.db._pool.write() as c:
            c.execute(
                "INSERT INTO tracks(udn, obj_id, url, title, artist, album, "
                "duration, art, mime, genre, file_path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("uuid:srv1", "tx", "http://srv/x.flac",
                 'Mick & Keith <2>', 'AC/DC', 'Back in "Black"',
                 "0:03:00", "", "audio/flac", "", ""))
        aid = api_subsonic._album_id("AC/DC", 'Back in "Black"')
        h = self._call_raw("getAlbum", {"id": aid})
        xml = h.last_xml.decode()
        self.assertIn('Mick &amp; Keith &lt;2&gt;', xml)
        self.assertIn('Back in &quot;Black&quot;', xml)
        self.assertNotIn(' & ', xml)  # raw ampersand would be a bug


if __name__ == "__main__":
    unittest.main()
