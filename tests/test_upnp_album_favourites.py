#!/usr/bin/env python3
"""
tests/test_upnp_album_favourites.py — UPnP ContentDirectory exposure
of the album-favourites feature.

Naim Uniti (and any other UPnP control point) browses the gateway as
a MediaServer. The contract:
  * Root container "0" lists two children: "⭐ Favourite Albums" and
    "Playlists" (in that order — fav albums first per user spec).
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
        self.assertEqual(api_upnp._decode_album_id(oid), (a, b))

    def test_round_trip_unicode_and_special_chars(self):
        a, b = "Sigur Rós", "( ) — 'parens & quotes' / slashes \\ nulls"
        oid = api_upnp._encode_album_id(a, b)
        self.assertEqual(api_upnp._decode_album_id(oid), (a, b))

    def test_round_trip_compilation_blank_artist(self):
        # Compilations use empty artist; codec must still round-trip.
        oid = api_upnp._encode_album_id("", "Now That's What I Call Music!")
        self.assertEqual(api_upnp._decode_album_id(oid),
                         ("", "Now That's What I Call Music!"))

    def test_decode_garbled_returns_empty(self):
        self.assertEqual(api_upnp._decode_album_id("favalbum:!!!not-base64!!!"),
                         ("", ""))
        self.assertEqual(api_upnp._decode_album_id("not-an-id"),
                         ("", ""))


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

    def test_root_lists_favalbums_first(self):
        xml, n_ret, total = api_upnp._gw_browse(
            "0", "BrowseDirectChildren", 0, 0)
        self.assertEqual((n_ret, total), (2, 2))
        # First container is favalbums; second is playlists.
        first_idx  = xml.find('id="favalbums"')
        second_idx = xml.find('id="playlists"')
        self.assertGreater(first_idx, 0,
                           f"favalbums container missing in: {xml!r}")
        self.assertGreater(second_idx, first_idx,
                           "Playlists must come AFTER Favourite Albums")
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


if __name__ == "__main__":
    unittest.main()
