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

    def test_albums_lists_all(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "albums", "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn("Alpha — Alice", xml)
        self.assertIn("Beta — Bob", xml)

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

    def test_albums_pagination(self):
        # Page 1: first album only.
        xml1, n1, total1 = api_upnp._gw_browse(
            "albums", "BrowseDirectChildren", 0, 1)
        self.assertEqual((n1, total1), (1, 2))
        # Page 2: the second.
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

    def test_unknown_ids_return_empty(self):
        for oid in ("gartist:" + api_upnp._b64e("Nobody"),
                    api_upnp._encode_lib_album_id("X", "Y", "Z/none"),
                    "ggenre:" + api_upnp._b64e("Polka")):
            xml, n_ret, total = api_upnp._gw_browse(
                oid, "BrowseDirectChildren", 0, 0)
            self.assertEqual((n_ret, total), (0, 0), oid)


if __name__ == "__main__":
    unittest.main()
