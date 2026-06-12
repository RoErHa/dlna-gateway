#!/usr/bin/env python3
"""
tests/test_upnp_album_favourites.py — UPnP ContentDirectory exposure
of the album-favourites feature.

Naim Uniti (and any other UPnP control point) browses the gateway as
a MediaServer. The contract:
  * Root container "0" lists five children: "Artists", "Albums", "Genres"
    (the full library), then "⭐ Favourite Albums" and "Playlists".
  * Browsing "artists"/"albums"/"genres" pages the library via LibraryDB on
    DB.primary_udn(); album containers (galbum:*) resolve their tracks with
    the LocalFs album_key so folder-albums work.
  * Browsing "favalbums" returns one container per favourited album.
  * Browsing a "favalbum:..." container returns the album's tracks
    (resolved against the udn stored on the favourite row).

Album ObjectID encoding round-trips arbitrary unicode in artist /
album names through XML and back.

Run standalone:
    python3 -m unittest tests.test_upnp_album_favourites -v
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
import api_upnp


class TestAlbumIdCodec(unittest.TestCase):

    def test_round_trip_simple(self):
        a, b = "Pink Floyd", "Animals"
        oid = api_upnp._encode_album_id(a, b)
        self.assertTrue(oid.startswith("favalbum:"))
        self.assertEqual(api_upnp._decode_album_id(oid), (a, b, ""))

    def test_round_trip_unicode_and_special_chars(self):
        a, b = "Sigur Rós", "( ) — 'parens & quotes' / slashes \\ nulls"
        oid = api_upnp._encode_album_id(a, b)
        self.assertEqual(api_upnp._decode_album_id(oid), (a, b, ""))

    def test_round_trip_compilation_blank_artist(self):
        # Compilations use empty artist; codec must still round-trip.
        oid = api_upnp._encode_album_id("", "Now That's What I Call Music!")
        self.assertEqual(api_upnp._decode_album_id(oid),
                         ("", "Now That's What I Call Music!", ""))

    def test_round_trip_album_key(self):
        # LocalFs folder identity round-trips as the third field.
        a, b, k = "Various Artists", "Hits", "VA/Hits (2024)/CD1"
        oid = api_upnp._encode_album_id(a, b, k)
        self.assertEqual(api_upnp._decode_album_id(oid), (a, b, k))

    def test_decode_legacy_two_field_id(self):
        # An id minted before album_key existed decodes with key=''.
        import base64
        raw = "Queen\x00A Night".encode("utf-8")
        legacy = "favalbum:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")
        self.assertEqual(api_upnp._decode_album_id(legacy),
                         ("Queen", "A Night", ""))

    def test_decode_garbled_returns_empty(self):
        self.assertEqual(api_upnp._decode_album_id("favalbum:!!!not-base64!!!"),
                         ("", "", ""))
        self.assertEqual(api_upnp._decode_album_id("not-an-id"),
                         ("", "", ""))


class TestBrowse(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        # Seed two albums in the library and favourite one of them.
        with self.db._pool.write() as c:
            for i in range(3):
                c.execute(
                    "INSERT INTO tracks(udn, obj_id, url, title, artist, "
                    "album, duration, art, mime, genre, file_path) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("uuid:srv1", f"t{i}", f"http://srv/track{i}.flac",
                     f"Track {i}", "Pink Floyd", "Animals",
                     "0:04:00", "", "audio/flac", "", ""))
        self.db.album_fav_add("Pink Floyd", "Animals")
        self._patch = patch.object(api_upnp, "DB", self.db)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_root_lists_library_then_favalbums_then_playlists(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "0", "BrowseDirectChildren", 0, 0)
        # Root now exposes the full library (Artists/Albums/Genres) plus the
        # Favourite Albums + Playlists convenience trees — 5 containers.
        self.assertEqual((n_ret, total), (5, 5))
        for cid in ("artists", "albums", "genres", "favalbums", "playlists"):
            self.assertIn(f'id="{cid}"', xml, f"missing root container {cid}")
        # Library tree comes before the favourites/playlists trees; favalbums
        # still precedes playlists (preserves the prior relative order).
        self.assertLess(xml.find('id="artists"'), xml.find('id="favalbums"'))
        self.assertLess(xml.find('id="favalbums"'), xml.find('id="playlists"'))
        self.assertIn("⭐ Favourite Albums", xml)

    def test_favalbums_lists_each_favourite(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "favalbums", "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (1, 1))
        # The container's id is the encoded album id.
        oid = api_upnp._encode_album_id("Pink Floyd", "Animals")
        self.assertIn(f'id="{oid}"', xml)
        # Title shows "album — artist" so Naim's display is informative.
        self.assertIn("Animals — Pink Floyd", xml)

    def test_favalbum_browse_returns_tracks(self):
        oid = api_upnp._encode_album_id("Pink Floyd", "Animals")
        xml, n_ret, total = api_upnp._gw_browse(
            oid, "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (3, 3))
        # Three <item> elements, each a musicTrack.
        self.assertEqual(xml.count("<item "), 3)
        self.assertIn("object.item.audioItem.musicTrack", xml)
        # And the track URLs from the seed are present.
        for i in range(3):
            self.assertIn(f"http://srv/track{i}.flac", xml)

    def test_unknown_favalbum_returns_empty_container(self):
        # Album exists in favourites but not in tracks — return empty
        # rather than 500. (User added a favourite for a server that's
        # since gone away.)
        self.db.album_fav_add("Ghost", "Lost Tapes")
        oid = api_upnp._encode_album_id("Ghost", "Lost Tapes")
        xml, n_ret, total = api_upnp._gw_browse(
            oid, "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (0, 0))

    def test_browse_metadata_for_favalbums(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "favalbums", "BrowseMetadata", 0, 0)
        self.assertEqual((n_ret, total), (1, 1))
        self.assertIn('id="favalbums"', xml)
        self.assertIn('parentID="0"', xml)


class TestBrowseByAlbumKey(unittest.TestCase):
    """A LocalFs compilation favourited by FOLDER exposes via UPnP as one
    album and resolves the whole folder's tracks (A3)."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        udn = "uuid:localfs-x"
        self.db.upsert_tracks(udn, [
            {"id": "c1", "url": "http://h/c1", "title": "Song A",
             "artist": "Alice", "album": "Orig A", "album_key": "VA/Comp",
             "file_path": "/m/VA/Comp/01.flac", "mime": "audio/flac"},
            {"id": "c2", "url": "http://h/c2", "title": "Song B",
             "artist": "Bob", "album": "Orig B", "album_key": "VA/Comp",
             "file_path": "/m/VA/Comp/02.flac", "mime": "audio/flac"},
        ])
        self.db.album_fav_add("Various Artists", "Comp", album_key="VA/Comp")
        self._patch = patch.object(api_upnp, "DB", self.db)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_favalbums_container_encodes_album_key(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "favalbums", "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (1, 1))
        oid = api_upnp._encode_album_id("Various Artists", "Comp", "VA/Comp")
        self.assertIn(f'id="{oid}"', xml)

    def test_browse_favalbum_resolves_whole_folder(self):
        oid = api_upnp._encode_album_id("Various Artists", "Comp", "VA/Comp")
        xml, n_ret, total = api_upnp._gw_browse(
            oid, "BrowseDirectChildren", 0, 0)
        # Both tracks resolve via album_key, regardless of per-track artist.
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn("Song A", xml)
        self.assertIn("Song B", xml)


class TestLibAlbumIdCodec(unittest.TestCase):
    """galbum:* / gartist / ggenre id round-trips (full-library tree)."""

    def test_galbum_round_trip(self):
        a, b, k = "Sigur Rós", "( ) & 'x' / y", "VA/Comp 2024/CD1"
        oid = api_upnp._encode_lib_album_id(a, b, k)
        self.assertTrue(oid.startswith("galbum:"))
        self.assertEqual(api_upnp._decode_lib_album_id(oid), (a, b, k))

    def test_galbum_garbled_returns_empty(self):
        self.assertEqual(api_upnp._decode_lib_album_id("galbum:!!!"), ("", "", ""))

    def test_b64_round_trip(self):
        for s in ("Pink Floyd", "AC/DC & Co", "Sigur Rós", ""):
            self.assertEqual(api_upnp._b64d(api_upnp._b64e(s)), s)

    def test_b64_garbled_returns_empty(self):
        self.assertEqual(api_upnp._b64d("!!!not-base64!!!"), "")


class TestFullLibraryBrowse(unittest.TestCase):
    """The gateway-as-MediaServer exposes the whole library (Artists / Albums /
    Genres) to the Naim, backed by LibraryDB on the primary (LocalFs) udn."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        self.udn = "uuid:localfs-lib"
        self.db.upsert_tracks(self.udn, [
            {"id": "a1", "url": "http://h/a1", "title": "Alpha One",
             "artist": "Alice", "album": "Alpha", "album_key": "Alice/Alpha",
             "genre": "Rock", "file_path": "/m/Alice/Alpha/01.flac",
             "mime": "audio/flac", "duration": "0:03:00"},
            {"id": "a2", "url": "http://h/a2", "title": "Alpha Two",
             "artist": "Alice", "album": "Alpha", "album_key": "Alice/Alpha",
             "genre": "Rock", "file_path": "/m/Alice/Alpha/02.flac",
             "mime": "audio/flac", "duration": "0:03:30"},
            {"id": "b1", "url": "http://h/b1", "title": "Beta One",
             "artist": "Bob", "album": "Beta", "album_key": "Bob/Beta",
             "genre": "Jazz", "file_path": "/m/Bob/Beta/01.flac",
             "mime": "audio/flac", "duration": "0:04:00"},
        ])
        self._patch = patch.object(api_upnp, "DB", self.db)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_primary_udn_picks_the_library(self):
        self.assertEqual(self.db.primary_udn(), self.udn)

    def test_artists_lists_all(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "artists", "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn("gartist:" + api_upnp._b64e("Alice"), xml)
        self.assertIn("gartist:" + api_upnp._b64e("Bob"), xml)
        self.assertIn("<dc:title>Alice</dc:title>", xml)

    def test_artist_albums(self):
        oid = "gartist:" + api_upnp._b64e("Alice")
        xml, n_ret, total = api_upnp._gw_browse(
            oid, "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (1, 1))
        gid = api_upnp._encode_lib_album_id("Alice", "Alpha", "Alice/Alpha")
        self.assertIn(f'id="{gid}"', xml)

    def test_albums_is_letter_index(self):
        # "Albums" is a #-0-A..Z index now; Alpha→A, Beta→B = two buckets.
        xml, n_ret, total = api_upnp._gw_browse(
            "albums", "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn('id="albumltr:A"', xml)
        self.assertIn('id="albumltr:B"', xml)
        self.assertIn("<dc:title>A</dc:title>", xml)

    def test_album_letter_lists_albums(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "albumltr:A", "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (1, 1))
        self.assertIn("Alpha — Alice", xml)
        gid = api_upnp._encode_lib_album_id("Alice", "Alpha", "Alice/Alpha")
        self.assertIn(f'id="{gid}"', xml)

    def test_album_tracks_have_res_urls(self):
        gid = api_upnp._encode_lib_album_id("Alice", "Alpha", "Alice/Alpha")
        xml, n_ret, total = api_upnp._gw_browse(
            gid, "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (2, 2))
        self.assertEqual(xml.count("<item "), 2)
        self.assertIn("object.item.audioItem.musicTrack", xml)
        self.assertIn("http://h/a1", xml)
        self.assertIn("http://h/a2", xml)
        self.assertIn("<res ", xml)

    def test_genres_lists_all(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "genres", "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn("ggenre:" + api_upnp._b64e("Rock"), xml)
        self.assertIn("ggenre:" + api_upnp._b64e("Jazz"), xml)

    def test_genre_albums(self):
        oid = "ggenre:" + api_upnp._b64e("Rock")
        xml, n_ret, total = api_upnp._gw_browse(
            oid, "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (1, 1))
        self.assertIn("Alpha — Alice", xml)

    def test_albums_letter_pagination(self):
        # Two letter buckets (A, B); page through them one at a time.
        xml1, n1, total1 = api_upnp._gw_browse(
            "albums", "BrowseDirectChildren", 0, 1)
        self.assertEqual((n1, total1), (1, 2))
        xml2, n2, total2 = api_upnp._gw_browse(
            "albums", "BrowseDirectChildren", 1, 1)
        self.assertEqual((n2, total2), (1, 2))
        self.assertNotEqual(xml1, xml2)

    def test_browse_metadata_for_artists(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "artists", "BrowseMetadata", 0, 0)
        self.assertEqual((n_ret, total), (1, 1))
        self.assertIn('id="artists"', xml)
        self.assertIn('parentID="0"', xml)

    def _insert(self, ak, artist, album, genre="Rock"):
        with self.db._pool.write() as c:
            c.execute(
                "INSERT INTO tracks(udn,obj_id,url,title,artist,album,"
                "album_key,duration,art,mime,genre,file_path) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.udn, ak, f"http://h/{ak}", "T", artist, album, ak,
                 "0:01:00", "", "audio/flac", genre, f"/m/{ak}/1.flac"))

    def test_blank_and_junk_albums_hidden(self):
        # A blank album name and a "NN. Title" album name are untagged junk —
        # they must NOT create a '#' or '0' letter bucket nor show up.
        self._insert("jk1", "Solo Act", "")                 # blank → "#"
        self._insert("jk2", "Solo Act", "10. Some Title")   # track-no → "0"
        xml, _, _ = api_upnp._gw_browse("albums", "BrowseDirectChildren", 0, 0)
        self.assertNotIn('id="albumltr:#"', xml)
        self.assertNotIn('id="albumltr:0"', xml)
        self.assertNotIn("10. Some Title", xml)

    def test_junk_artists_hidden(self):
        self._insert("jk3", "07", "Junk Album One")
        self._insert("jk4", "10. Mad About The Boy", "Junk Album Two")
        xml, _, total = api_upnp._gw_browse("artists", "BrowseDirectChildren", 0, 0)
        # Only Alice + Bob survive; the two filename-derived artists are gone.
        self.assertEqual(total, 2)
        self.assertNotIn("Mad About The Boy", xml)
        self.assertNotIn("gartist:" + api_upnp._b64e("07"), xml)

    def test_is_junk_name_cases(self):
        for junk in ("", "  ", "07", "10. Mad About The Boy", "1) Intro",
                     "3 - Track"):
            self.assertTrue(api_upnp._is_junk_name(junk), junk)
        for ok in ("Pink Floyd", "100 Proof Aged in Soul", "311",
                   "*NSYNC", "U2", "98 Degrees"):
            self.assertFalse(api_upnp._is_junk_name(ok), ok)

    def test_letter_of_buckets(self):
        self.assertEqual(api_upnp._letter_of("Animals"), "A")
        self.assertEqual(api_upnp._letter_of("9 to 5"), "0")
        self.assertEqual(api_upnp._letter_of("(parens)"), "#")
        self.assertEqual(api_upnp._letter_of(""), "#")

    def test_unknown_ids_return_empty(self):
        for oid in ("gartist:" + api_upnp._b64e("Nobody"),
                    api_upnp._encode_lib_album_id("X", "Y", "Z/none"),
                    "ggenre:" + api_upnp._b64e("Polka")):
            xml, n_ret, total = api_upnp._gw_browse(
                oid, "BrowseDirectChildren", 0, 0)
            self.assertEqual((n_ret, total), (0, 0), oid)


class TestContentDirectorySCPD(unittest.TestCase):
    """The ContentDirectory SCPD must be valid UPnP — action/argument/state
    names in <name> tags (a stray <n> made NaimUPnP reject the service and
    never browse). Regression guard for that 2026-06-12 incident."""

    def test_scpd_uses_name_not_n_and_is_well_formed(self):
        import xml.etree.ElementTree as ET
        scpd = api_upnp._gw_cd_desc_xml()
        self.assertNotIn("<n>", scpd, "SCPD must use <name>, not <n>")
        self.assertIn("<name>Browse</name>", scpd)
        root = ET.fromstring(scpd)              # must be well-formed XML
        NS = "{urn:schemas-upnp-org:service-1-0}"
        names = [e.text for e in root.iter(NS + "name")]
        # The Browse action + its key arguments + the DLNA handshake actions
        # must be discoverable by name.
        for n in ("Browse", "ObjectID", "BrowseFlag", "Result", "TotalMatches",
                  "GetSearchCapabilities", "GetSortCapabilities",
                  "GetSystemUpdateID"):
            self.assertIn(n, names, n)


class TestContentDirectoryActions(unittest.TestCase):
    """The DLNA pre-browse handshake — GetSearchCapabilities / GetSortCapabilities
    / GetSystemUpdateID (+ optional GetSortExtensionCapabilities / GetFeatureList
    / Search) — must return 200 empty-but-valid SOAP, or NaimUPnP 400s and drops
    the server (2026-06-12 incident)."""

    def _soap(self, action):
        return (f'<?xml version="1.0"?><s:Envelope '
                f'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                f'<u:{action} xmlns:u="{api_upnp._CD_NS}"></u:{action}>'
                f'</s:Body></s:Envelope>').encode()

    def test_handshake_actions_return_200_valid_soap(self):
        import xml.etree.ElementTree as ET
        for action, needle in [
            ("GetSearchCapabilities", b"<SearchCaps>"),
            ("GetSortCapabilities", b"<SortCaps>"),
            ("GetSortExtensionCapabilities", b"<SortExtensionCaps>"),
            ("GetSystemUpdateID", b"<Id>1</Id>"),
            ("GetFeatureList", b"FeatureList"),
        ]:
            st, ct, body = api_upnp.cd_control_soap(self._soap(action))
            self.assertEqual(st, 200, action)
            self.assertIn("xml", ct)
            ET.fromstring(body)                       # well-formed
            self.assertIn(f"{action}Response".encode(), body)
            self.assertIn(needle, body)

    def test_search_returns_empty_result(self):
        st, ct, body = api_upnp.cd_control_soap(self._soap("Search"))
        self.assertEqual(st, 200)
        self.assertIn(b"SearchResponse", body)
        self.assertIn(b"<TotalMatches>0</TotalMatches>", body)

    def test_unknown_action_400(self):
        st, ct, body = api_upnp.cd_control_soap(self._soap("Frobnicate"))
        self.assertEqual(st, 400)

    def test_device_xml_advertises_dlna_dms(self):
        import xml.etree.ElementTree as ET
        dx = api_upnp._gw_device_xml("10.0.0.5", 8765)
        ET.fromstring(dx)                              # well-formed
        self.assertIn("X_DLNADOC", dx)
        self.assertIn("DMS-1.50", dx)


class TestConnectionManager(unittest.TestCase):
    """A DLNA Media Server MUST expose ConnectionManager alongside
    ContentDirectory, or strict clients (LG TV, Naim) reject the device and
    never browse (2026-06-13 incident — both clients fetched device.xml + the
    CD SCPD then quit)."""

    def _soap(self, action):
        return (f'<?xml version="1.0"?><s:Envelope '
                f'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                f'<u:{action} xmlns:u="{api_upnp._CM_NS}"></u:{action}>'
                f'</s:Body></s:Envelope>').encode()

    def test_device_xml_lists_both_services(self):
        import xml.etree.ElementTree as ET
        dx = api_upnp._gw_device_xml("10.0.0.5", 8765)
        root = ET.fromstring(dx)
        svc = [e.text for e in
               root.iter("{urn:schemas-upnp-org:device-1-0}serviceType")]
        self.assertIn("urn:schemas-upnp-org:service:ContentDirectory:1", svc)
        self.assertIn("urn:schemas-upnp-org:service:ConnectionManager:1", svc)

    def test_cm_scpd_valid_with_actions(self):
        import xml.etree.ElementTree as ET
        scpd = api_upnp._gw_cm_desc_xml()
        self.assertNotIn("<n>", scpd)
        root = ET.fromstring(scpd)
        names = [e.text for e in
                 root.iter("{urn:schemas-upnp-org:service-1-0}name")]
        for a in ("GetProtocolInfo", "GetCurrentConnectionIDs",
                  "GetCurrentConnectionInfo", "SourceProtocolInfo"):
            self.assertIn(a, names, a)

    def test_get_protocol_info_advertises_audio(self):
        import xml.etree.ElementTree as ET
        st, ct, body = api_upnp.cm_control_soap(self._soap("GetProtocolInfo"))
        self.assertEqual(st, 200)
        ET.fromstring(body)
        self.assertIn(b"GetProtocolInfoResponse", body)
        self.assertIn(b"<Source>", body)
        self.assertIn(b"audio/flac", body)
        self.assertIn(b"<Sink></Sink>", body)

    def test_connection_ids_and_info(self):
        st, _, body = api_upnp.cm_control_soap(self._soap("GetCurrentConnectionIDs"))
        self.assertEqual(st, 200)
        self.assertIn(b"<ConnectionIDs>0</ConnectionIDs>", body)
        st2, _, body2 = api_upnp.cm_control_soap(self._soap("GetCurrentConnectionInfo"))
        self.assertEqual(st2, 200)
        self.assertIn(b"<Status>OK</Status>", body2)

    def test_unknown_cm_action_400(self):
        st, _, _ = api_upnp.cm_control_soap(self._soap("Frobnicate"))
        self.assertEqual(st, 400)


class TestGenaEvents(unittest.TestCase):
    """A GENA SUBSCRIBE must get a valid SID + TIMEOUT (the old stub returned a
    bare 200, so dLeyna/GUPnP on the Naim aborted device setup and never
    browsed)."""

    def test_new_subscription_gets_sid_timeout_callback(self):
        hdrs, cb, sid = api_upnp.gw_event_subscribe(
            {"CALLBACK": "<http://10.0.0.9:49152/evt>", "NT": "upnp:event",
             "TIMEOUT": "Second-1800"})
        self.assertTrue(sid.startswith("uuid:"))
        self.assertEqual(hdrs["SID"], sid)
        self.assertEqual(hdrs["TIMEOUT"], "Second-1800")
        self.assertEqual(cb, "http://10.0.0.9:49152/evt")

    def test_renewal_echoes_sid_no_callback(self):
        hdrs, cb, sid = api_upnp.gw_event_subscribe({"SID": "uuid:abc-123"})
        self.assertEqual(hdrs["SID"], "uuid:abc-123")
        self.assertEqual(sid, "uuid:abc-123")
        self.assertEqual(cb, "")

    def test_parse_callback(self):
        self.assertEqual(api_upnp._parse_callback("<http://a/1><http://b/2>"),
                         "http://a/1")
        self.assertEqual(api_upnp._parse_callback(""), "")
        self.assertEqual(api_upnp._parse_callback("garbage"), "")


class TestMSearchResponder(unittest.TestCase):
    """The gateway answers SSDP M-SEARCH so the Naim's ACTIVE discovery finds
    'DLNA Gateway (IINA)' (not only via the passive 60s NOTIFY)."""

    LOC = "http://10.0.0.5:8765/gw/device.xml"

    def _msearch(self, st):
        return ("\r\n".join([
            "M-SEARCH * HTTP/1.1", "HOST: 239.255.255.250:1900",
            'MAN: "ssdp:discover"', "MX: 2", f"ST: {st}", "", ""]).encode())

    def test_mediaserver_search_answered(self):
        rs = api_upnp._gw_msearch_replies(
            self._msearch("urn:schemas-upnp-org:device:MediaServer:1"), self.LOC)
        self.assertEqual(len(rs), 1)
        st, usn, raw = rs[0]
        self.assertEqual(st, "urn:schemas-upnp-org:device:MediaServer:1")
        self.assertIn(api_upnp.GW_UDN, usn)
        self.assertIn(b"HTTP/1.1 200 OK", raw)
        self.assertIn(self.LOC.encode(), raw)
        self.assertIn(b"ST: urn:schemas-upnp-org:device:MediaServer:1", raw)

    def test_ssdp_all_answers_every_entry(self):
        rs = api_upnp._gw_msearch_replies(self._msearch("ssdp:all"), self.LOC)
        self.assertEqual(len(rs), 4)
        sts = {st for st, _, _ in rs}
        self.assertIn("upnp:rootdevice", sts)
        self.assertIn("urn:schemas-upnp-org:service:ContentDirectory:1", sts)
        self.assertIn(api_upnp.GW_UDN, sts)

    def test_rootdevice_search_answered(self):
        rs = api_upnp._gw_msearch_replies(self._msearch("upnp:rootdevice"), self.LOC)
        self.assertEqual(len(rs), 1)
        self.assertEqual(rs[0][0], "upnp:rootdevice")

    def test_unrelated_search_ignored(self):
        rs = api_upnp._gw_msearch_replies(
            self._msearch("urn:schemas-upnp-org:device:MediaRenderer:1"), self.LOC)
        self.assertEqual(rs, [])

    def test_notify_is_not_answered(self):
        notify = (b"NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
                  b"NT: upnp:rootdevice\r\nNTS: ssdp:alive\r\n\r\n")
        self.assertEqual(api_upnp._gw_msearch_replies(notify, self.LOC), [])

    def test_msearch_without_discover_ignored(self):
        bad = (b"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\n"
               b"ST: ssdp:all\r\n\r\n")   # no MAN: "ssdp:discover"
        self.assertEqual(api_upnp._gw_msearch_replies(bad, self.LOC), [])

    def test_garbage_datagram_ignored(self):
        self.assertEqual(api_upnp._gw_msearch_replies(b"\x00\x01\x02", self.LOC), [])


if __name__ == "__main__":
    unittest.main()
