#!/usr/bin/env python3
"""
tests/test_photos.py — the 📷 Photos folder for the LG TV (dlna_photos,
api_upnp_browse_photo, dlna_asgi_photos).

Immich is never contacted: the index takes a fake client, and the one test
of the real client stubs its HTTP.
"""
import asyncio
import os
import sys
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import api_upnp
import api_upnp_ids
import dlna_asgi_photos
import dlna_photos
from dlna_photos import Album, Immich, PhotoIndex

A1 = "c6fe38ee-8332-4f49-bb1f-ff278cc8cd39"
P1 = "11111111-1111-4111-8111-111111111111"
P2 = "22222222-2222-4222-8222-222222222222"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100


class FakeImmich:
    """Album A1 is visible to key 'marja' only; previews per `blobs`."""
    calls = []
    blobs = {}

    def __init__(self, url, key):
        self.key = key

    def album(self, album_id):
        FakeImmich.calls.append(("album", self.key, album_id))
        if self.key != "marja" or album_id != A1:
            raise urllib.error.HTTPError("u", 400, "no access", {}, None)
        return Album(A1, "Verjaardag Benjamin 6 jaar", [
            {"id": P1, "title": "2026-05-10 14:00 · Utrecht", "date": "2026-05-10T14:00:00"},
            {"id": P2, "title": "2026-05-10 14:05", "date": "2026-05-10T14:05:00"}])

    def preview(self, asset_id):
        FakeImmich.calls.append(("preview", self.key, asset_id))
        return FakeImmich.blobs.get(asset_id, JPEG)


def _index():
    FakeImmich.calls, FakeImmich.blobs = [], {}
    idx = PhotoIndex("http://localhost:2283", ["ron", "marja"], [A1], client=FakeImmich)
    idx.refresh()
    return idx


class TestConfig(unittest.TestCase):

    def test_immich_must_be_on_this_mac(self):
        self.assertEqual(dlna_photos.loopback_url("http://localhost:2283/"), "http://localhost:2283")
        self.assertEqual(dlna_photos.loopback_url("http://127.0.0.1:2283"), "http://127.0.0.1:2283")
        for bad in ("http://192.168.1.5:2283", "file:///etc", "ftp://localhost", ""):
            self.assertEqual(dlna_photos.loopback_url(bad), "", bad)

    def test_every_immich_key_is_tried(self):
        env = {"IMMICH_API_KEY": "ron", "IMMICH_API_KEY_MARJA": "marja",
               "IMMICH_API_KEY_EMPTY": " ", "OTHER": "x"}
        self.assertEqual(dlna_photos.keys_from_env(env), ["ron", "marja"])

    def test_album_ids_must_be_uuids(self):
        env = {"PHOTOS_ALBUMS": f" {A1.upper()} , nonsense,, ../etc"}
        self.assertEqual(dlna_photos.album_ids_from_env(env), [A1])

    def test_off_without_all_three(self):
        self.assertFalse(PhotoIndex("http://localhost:2283", ["k"], []).enabled)
        self.assertFalse(PhotoIndex("", ["k"], [A1]).enabled)
        self.assertTrue(PhotoIndex("http://localhost:2283", ["k"], [A1]).enabled)

    def test_start_configures_the_shared_index_in_place(self):
        """Readers hold dlna_photos.INDEX; a rebind would strand them."""
        before = dlna_photos.INDEX
        env = {"IMMICH_URL": "http://localhost:2283", "IMMICH_API_KEY": "k",
               "PHOTOS_ALBUMS": A1}
        with patch.dict(os.environ, env, clear=False), \
             patch.object(dlna_photos.threading, "Thread") as thread:
            try:
                dlna_photos.start()
                self.assertIs(dlna_photos.INDEX, before)
                self.assertEqual(before.album_ids, [A1])
                thread.return_value.start.assert_called_once()
            finally:
                before.url, before.keys, before.album_ids = "", [], []


class TestIndex(unittest.TestCase):

    def test_an_album_is_read_with_the_first_key_that_may_see_it(self):
        idx = _index()
        self.assertEqual([a.name for a in idx.albums], ["Verjaardag Benjamin 6 jaar"])
        self.assertEqual([c[1] for c in FakeImmich.calls], ["ron", "marja"])

    def test_preview_uses_the_key_that_listed_it_and_is_cached(self):
        idx = _index()
        self.assertEqual(idx.preview(P1), JPEG)
        self.assertEqual(idx.preview(P1), JPEG)
        self.assertEqual([c for c in FakeImmich.calls if c[0] == "preview"],
                         [("preview", "marja", P1)])

    def test_an_id_not_in_the_index_is_never_fetched(self):
        idx = _index()
        self.assertIsNone(idx.preview("33333333-3333-4333-8333-333333333333"))
        self.assertFalse([c for c in FakeImmich.calls if c[0] == "preview"])

    def test_only_jpeg_goes_out(self):
        idx = _index()
        FakeImmich.blobs[P2] = b"RIFF....WEBPVP8 "
        self.assertIsNone(idx.preview(P2))

    def test_an_unreadable_album_leaves_the_index_empty_not_broken(self):
        idx = PhotoIndex("http://localhost:2283", ["ron"], [A1], client=FakeImmich)
        idx.refresh()
        self.assertEqual(idx.albums, [])
        self.assertIsNone(idx.preview(P1))


class TestImmichClient(unittest.TestCase):

    def test_pages_and_drops_hidden_trashed_and_bad_ids(self):
        def item(i, **kw):
            return {"id": i, "localDateTime": f"2026-05-10T14:0{len(kw)}:00.000Z",
                    "exifInfo": {"city": "Utrecht"}, **kw}
        pages = {1: {"items": [item(P2), item(P1, isTrashed=True)], "nextPage": "2"},
                 2: {"items": [item(P1, visibility="locked"), item("not-a-uuid"),
                               item("33333333-3333-4333-8333-333333333333",
                                    visibility="archive")], "nextPage": None}}

        def fake_json(method, path, body=None):
            if method == "GET":
                return {"albumName": "Feest"}
            self.assertEqual(body["type"], "IMAGE")
            return {"assets": pages[body["page"]]}
        c = Immich("http://localhost:2283", "k")
        with patch.object(c, "json", side_effect=fake_json):
            album = c.album(A1)
        self.assertEqual(album.name, "Feest")
        self.assertEqual([p["id"] for p in album.photos],
                         [P2, "33333333-3333-4333-8333-333333333333"])
        self.assertEqual(album.photos[0]["title"], "2026-05-10 14:00 · Utrecht")


class TestBrowse(unittest.TestCase):

    def setUp(self):
        fd, self._db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from dlna_library import LibraryDB
        self._p = [patch.object(api_upnp_ids, "DB", LibraryDB(self._db)),
                   patch.object(api_upnp_ids, "_get_lan_ip", return_value="192.168.1.125")]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        os.unlink(self._db)

    def browse(self, oid, flag="BrowseDirectChildren"):
        return api_upnp._gw_browse(oid, flag, 0, 0)

    def titles(self, xml):
        return [e.text for e in ET.fromstring(xml).iter() if e.tag.endswith("title")]

    def test_no_photos_folder_while_the_index_is_empty(self):
        with patch.object(dlna_photos, "INDEX", PhotoIndex()):
            self.assertNotIn("\U0001F4F7 Photos", self.titles(self.browse("0")[0]))

    def test_root_album_photo(self):
        with patch.object(dlna_photos, "INDEX", _index()):
            xml, n, total = self.browse("0")
            self.assertIn("\U0001F4F7 Photos", self.titles(xml))
            meta = ET.fromstring(self.browse("0", "BrowseMetadata")[0])
            self.assertEqual(meta[0].get("childCount"), str(n))   # root counts it

            xml, n, _ = self.browse("photos")
            self.assertEqual(n, 1)
            self.assertIn(f'id="phalbum:{A1}"', xml)

            xml, n, total = self.browse(f"phalbum:{A1}")
            self.assertEqual((n, total), (2, 2))
            self.assertIn("<upnp:class>object.item.imageItem.photo</upnp:class>", xml)
            self.assertIn("http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG", xml)
            self.assertIn(f">http://192.168.1.125:8765/gw/photo/{P1}.jpg</res>", xml)

            xml, n, total = self.browse(f"ph:{P1}", "BrowseMetadata")
            self.assertEqual((n, total), (1, 1))
            self.assertIn(f'parentID="phalbum:{A1}"', xml)

    def test_garbled_ids_are_empty_never_an_error(self):
        with patch.object(dlna_photos, "INDEX", _index()):
            for oid in ("phalbum:nope", "ph:nope", "phalbum:", "ph:"):
                self.assertEqual(self.browse(oid)[1:], (0, 0), oid)
            self.assertEqual(self.browse(f"ph:{P1}")[1:], (0, 0))


class TestRoute(unittest.TestCase):

    def get(self, name, method="GET"):
        req = MagicMock()
        req.method = method
        return asyncio.run(dlna_asgi_photos.gw_photo(name, req))

    def test_serves_an_indexed_photo_as_jpeg(self):
        with patch.object(dlna_photos, "INDEX", _index()):
            r = self.get(f"{P1}.jpg")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.body, JPEG)
        self.assertEqual(r.media_type, "image/jpeg")
        self.assertIn("DLNA.ORG_PN=JPEG_LRG", r.headers["contentFeatures.dlna.org"])

    def test_head_has_the_length_and_no_body(self):
        with patch.object(dlna_photos, "INDEX", _index()):
            r = self.get(f"{P1}.jpg", "HEAD")
        self.assertEqual((r.status_code, r.body), (200, b""))
        self.assertEqual(r.headers["content-length"], str(len(JPEG)))

    def test_every_failure_is_the_same_opaque_502(self):
        idx = _index()
        FakeImmich.blobs[P2] = b"not a jpeg"
        with patch.object(dlna_photos, "INDEX", idx):
            for name in (f"{P2}.jpg", "33333333-3333-4333-8333-333333333333.jpg",
                         f"{P1}.png", "../../etc/passwd", f"{P1}.jpg.jpg"):
                r = self.get(name)
                self.assertEqual((r.status_code, r.body),
                                 (502, b'{"error":"photo unavailable"}'), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
