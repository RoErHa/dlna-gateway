#!/usr/bin/env python3
"""
tests/test_photos.py — the 📷 Photos tree for the LG TV (dlna_photos,
dlna_photos_immich, api_upnp_browse_photo, dlna_asgi_photos).

Immich is never contacted: the index takes a fake client, and the tests of
the real client stub its HTTP.
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
import dlna_photos_immich
from api_upnp_ids import _b64e
from dlna_photos import PhotoIndex, build
from dlna_photos_immich import Immich

A1 = "c6fe38ee-8332-4f49-bb1f-ff278cc8cd39"
A2 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
P1 = "11111111-1111-4111-8111-111111111111"
P2 = "22222222-2222-4222-8222-222222222222"
P3 = "33333333-3333-4333-8333-333333333333"
P4 = "44444444-4444-4444-8444-444444444444"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100


def item(i, when, city="", country="", people=(), fav=False, **kw):
    return {"id": i, "localDateTime": when + ".000Z", "isFavorite": fav,
            "exifInfo": {"city": city, "country": country},
            "people": [{"name": n, "isHidden": n == "Hidden"} for n in people], **kw}


# Ron sees P1-P3 and names Benjamin; Marja sees P1, P2, P4, names "Benjamin"
# too (her own Immich person, same name) and favourites P2. P3 is archived
# and only in her album A2.
RON = [item(P1, "2020-09-05T13:04:39", "Bakkum", "Netherlands", ["Benjamin"]),
       item(P2, "2021-01-02T10:00:00", "", "Netherlands"),
       item(P3, "2021-01-03T10:00:00", visibility="archive")]
MARJA = [item(P1, "2020-09-05T13:04:39", "Bakkum", "Netherlands", ["Benjamin"]),
         item(P2, "2021-01-02T10:00:00", "", "Netherlands", ["Benjamin"], fav=True),
         item(P4, "2021-01-01T09:00:00", "Paris", "France", ["", "Hidden"]),
         item("not-a-uuid", "2021-01-01T09:00:00"),
         item(P3, "2021-01-03T10:00:00", visibility="locked")]


class FakeImmich:
    calls = []
    blobs = {}
    data = {"ron": (RON, [], {}),
            "marja": (MARJA, [{"id": A1, "name": "Verjaardag Benjamin 6 jaar"},
                              {"id": A2, "name": "Archief"}],
                      {A1: [MARJA[0], MARJA[1]], A2: [RON[2]]})}

    def __init__(self, url, key):
        self.key = key

    def search(self, albumIds=None):
        FakeImmich.calls.append(("search", self.key, albumIds))
        if self.key not in self.data:
            raise urllib.error.URLError("refused")
        timeline, _, album_items = self.data[self.key]
        return iter(album_items[albumIds[0]] if albumIds else timeline)

    def albums(self):
        return self.data[self.key][1]

    def preview(self, asset_id):
        FakeImmich.calls.append(("preview", self.key, asset_id))
        return FakeImmich.blobs.get(asset_id, JPEG)


def _index(keys=("ron", "marja")):
    FakeImmich.calls, FakeImmich.blobs = [], {}
    idx = PhotoIndex("http://localhost:2283", list(keys), True, client=FakeImmich)
    idx.refresh()
    return idx


class TestConfig(unittest.TestCase):

    def test_immich_must_be_on_this_mac(self):
        lb = dlna_photos_immich.loopback_url
        self.assertEqual(lb("http://localhost:2283/"), "http://localhost:2283")
        self.assertEqual(lb("http://127.0.0.1:2283"), "http://127.0.0.1:2283")
        for bad in ("http://192.168.1.5:2283", "file:///etc", "ftp://localhost", ""):
            self.assertEqual(lb(bad), "", bad)

    def test_every_immich_key_is_tried(self):
        env = {"IMMICH_API_KEY": "ron", "IMMICH_API_KEY_MARJA": "marja",
               "IMMICH_API_KEY_EMPTY": " ", "OTHER": "x"}
        self.assertEqual(dlna_photos.keys_from_env(env), ["ron", "marja"])

    def test_off_unless_switched_on(self):
        self.assertFalse(PhotoIndex("http://localhost:2283", ["k"], False).enabled)
        self.assertFalse(PhotoIndex("", ["k"], True).enabled)
        self.assertTrue(PhotoIndex("http://localhost:2283", ["k"], True).enabled)

    def test_start_configures_the_shared_index_in_place(self):
        """Readers hold dlna_photos.INDEX; a rebind would strand them."""
        before = dlna_photos.INDEX
        env = {"IMMICH_URL": "http://localhost:2283", "IMMICH_API_KEY": "k",
               "PHOTOS_ENABLED": "1"}
        with patch.dict(os.environ, env, clear=False), \
             patch.object(dlna_photos.threading, "Thread") as thread:
            try:
                dlna_photos.start()
                self.assertIs(dlna_photos.INDEX, before)
                self.assertTrue(before.enabled)
                thread.return_value.start.assert_called_once()
            finally:
                before.url, before.keys, before.on = "", [], False


class TestBuild(unittest.TestCase):

    def setUp(self):
        self.lib = _index().lib

    def test_timeline_merges_users_and_drops_hidden_trashed_locked_and_bad_ids(self):
        self.assertEqual(set(self.lib.photos), {P1, P2, P3, P4})   # P3 via its album
        self.assertEqual(sorted(self.lib.months), ["2020-09", "2021-01"])
        self.assertEqual([p.id for p in self.lib.months["2021-01"]], [P4, P2])

    def test_archived_photos_appear_only_in_albums(self):
        self.assertNotIn(P3, [p.id for ps in self.lib.months.values() for p in ps])
        self.assertEqual([p.id for p in self.lib.albums[0].photos], [P3])

    def test_albums_newest_first(self):
        self.assertEqual([a.name for a in self.lib.albums],
                         ["Archief", "Verjaardag Benjamin 6 jaar"])

    def test_one_name_is_one_person_across_users(self):
        self.assertEqual([(n, [p.id for p in ps]) for n, ps in self.lib.people],
                         [("Benjamin", [P1, P2])])

    def test_favourites_are_any_users(self):
        self.assertEqual([p.id for p in self.lib.favourites], [P2])

    def test_places_biggest_country_first_and_no_city_last(self):
        self.assertEqual([(c, [(city, len(ps)) for city, ps in cs]) for c, cs in self.lib.places],
                         [("Netherlands", [("Bakkum", 1), ("", 1)]), ("France", [("Paris", 1)])])

    def test_a_photo_is_owned_by_the_first_key_that_listed_it(self):
        self.assertEqual(self.lib.owner[P1], "ron")
        self.assertEqual(self.lib.owner[P4], "marja")

    def test_build_is_pure(self):
        lib = build([("k", RON[:1])], [])
        self.assertEqual(list(lib.photos), [P1])
        self.assertEqual(lib.albums, [])


class TestRefreshAndPreview(unittest.TestCase):

    def test_a_key_that_fails_is_skipped(self):
        idx = _index(("broken", "marja"))
        self.assertIn(P4, idx.lib.photos)

    def test_no_working_key_keeps_the_previous_index(self):
        idx = _index()
        idx.keys = ["broken"]
        idx.refresh()
        self.assertIn(P1, idx.lib.photos)

    def test_preview_uses_the_owning_key_and_is_cached(self):
        idx = _index()
        self.assertEqual(idx.preview(P4), JPEG)
        self.assertEqual(idx.preview(P4), JPEG)
        self.assertEqual([c for c in FakeImmich.calls if c[0] == "preview"],
                         [("preview", "marja", P4)])

    def test_an_id_not_in_the_index_is_never_fetched(self):
        idx = _index()
        self.assertIsNone(idx.preview("99999999-9999-4999-8999-999999999999"))
        self.assertFalse([c for c in FakeImmich.calls if c[0] == "preview"])

    def test_only_jpeg_goes_out(self):
        idx = _index()
        FakeImmich.blobs[P2] = b"RIFF....WEBPVP8 "
        self.assertIsNone(idx.preview(P2))


class TestImmichClient(unittest.TestCase):

    def test_search_pages_until_the_end(self):
        pages = {1: {"items": [{"id": P1}], "nextPage": "2"},
                 2: {"items": [{"id": P2}], "nextPage": None}}
        bodies = []

        def fake_json(method, path, body=None):
            bodies.append(body)
            return {"assets": pages[body["page"]]}
        c = Immich("http://localhost:2283", "k")
        with patch.object(c, "json", side_effect=fake_json):
            got = [it["id"] for it in c.search(albumIds=[A1])]
        self.assertEqual(got, [P1, P2])
        self.assertEqual({(b["type"], b["withPeople"], tuple(b["albumIds"])) for b in bodies},
                         {("IMAGE", True, (A1,))})

    def test_albums_owned_and_shared_once_each(self):
        answers = {"/api/albums": [{"id": A1, "albumName": "Feest"}, {"id": "bad"}],
                   "/api/albums?shared=true": [{"id": A1, "albumName": "Feest"},
                                               {"id": A2, "albumName": " "}]}
        c = Immich("http://localhost:2283", "k")
        with patch.object(c, "json", side_effect=lambda m, p, b=None: answers[p]):
            self.assertEqual(c.albums(), [{"id": A1, "name": "Feest"},
                                          {"id": A2, "name": "(album)"}])


class TestBrowse(unittest.TestCase):

    def setUp(self):
        fd, self._db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        from dlna_library import LibraryDB
        self._p = [patch.object(api_upnp_ids, "DB", LibraryDB(self._db)),
                   patch.object(api_upnp_ids, "_get_lan_ip", return_value="192.168.1.125"),
                   patch.object(dlna_photos, "INDEX", _index())]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        os.unlink(self._db)

    def browse(self, oid, flag="BrowseDirectChildren"):
        return api_upnp._gw_browse(oid, flag, 0, 0)

    def kids(self, oid):
        root = ET.fromstring(self.browse(oid)[0])
        return [(e.get("id"), e.find("{http://purl.org/dc/elements/1.1/}title").text)
                for e in root]

    def test_no_photos_folder_while_the_index_is_empty(self):
        with patch.object(dlna_photos, "INDEX", PhotoIndex()):
            self.assertNotIn("photos", [i for i, _ in self.kids("0")])

    def test_root_counts_the_photos_folder(self):
        n = self.browse("0")[1]
        self.assertIn("photos", [i for i, _ in self.kids("0")])
        meta = ET.fromstring(self.browse("0", "BrowseMetadata")[0])
        self.assertEqual(meta[0].get("childCount"), str(n))

    def test_the_five_branches(self):
        self.assertEqual([i for i, _ in self.kids("photos")],
                         ["phdates", "phalbums", "phpeople", "phplaces", "phfavs"])

    def test_by_date(self):
        self.assertEqual(self.kids("phdates"), [("phyear:2021", "2021"), ("phyear:2020", "2020")])
        self.assertEqual(self.kids("phyear:2021"), [("phmonth:2021-01", "January 2021")])
        self.assertEqual([i for i, _ in self.kids("phmonth:2021-01")], [f"ph:{P4}", f"ph:{P2}"])

    def test_albums_people_favourites(self):
        self.assertEqual(self.kids("phalbums")[1], (f"phalbum:{A1}", "Verjaardag Benjamin 6 jaar"))
        self.assertEqual(len(self.kids(f"phalbum:{A1}")), 2)
        self.assertEqual(self.kids("phpeople"), [(f"phperson:{_b64e('Benjamin')}", "Benjamin")])
        self.assertEqual(len(self.kids(f"phperson:{_b64e('Benjamin')}")), 2)
        self.assertEqual([i for i, _ in self.kids("phfavs")], [f"ph:{P2}"])

    def test_places(self):
        nl = f"phcountry:{_b64e('Netherlands')}"
        self.assertEqual(self.kids("phplaces")[0], (nl, "Netherlands"))
        self.assertEqual([t for _, t in self.kids(nl)], ["Bakkum", "(no city)"])
        no_city = self.kids(nl)[1][0]
        self.assertEqual([i for i, _ in self.kids(no_city)], [f"ph:{P2}"])

    def test_a_photo_item(self):
        xml, n, total = self.browse(f"phalbum:{A1}")
        self.assertIn("<upnp:class>object.item.imageItem.photo</upnp:class>", xml)
        self.assertIn("http-get:*:image/jpeg:DLNA.ORG_PN=JPEG_LRG", xml)
        self.assertIn(f">http://192.168.1.125:8765/gw/photo/{P1}.jpg</res>", xml)
        xml, n, total = self.browse(f"ph:{P1}", "BrowseMetadata")
        self.assertEqual((n, total), (1, 1))
        self.assertIn('parentID="phmonth:2020-09"', xml)

    def test_every_container_describes_itself(self):
        for oid in ("photos", "phdates", "phyear:2020", "phmonth:2020-09", "phalbums",
                    f"phalbum:{A1}", "phpeople", "phplaces", "phfavs",
                    f"phcountry:{_b64e('France')}"):
            self.assertEqual(self.browse(oid, "BrowseMetadata")[1:], (1, 1), oid)

    def test_garbled_ids_are_empty_never_an_error(self):
        for oid in ("phalbum:nope", "ph:nope", "phalbum:", "ph:", "phyear:1800",
                    "phmonth:2021-13", "phmonth:", "phperson:!!", "phperson:",
                    "phcountry:@@", "phcity:%%", f"phcity:{_b64e('Nowhere')}"):
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
        self.assertEqual((r.status_code, r.body, r.media_type), (200, JPEG, "image/jpeg"))
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
            for name in (f"{P2}.jpg", "99999999-9999-4999-8999-999999999999.jpg",
                         f"{P1}.png", "../../etc/passwd", f"{P1}.jpg.jpg"):
                r = self.get(name)
                self.assertEqual((r.status_code, r.body),
                                 (502, b'{"error":"photo unavailable"}'), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
