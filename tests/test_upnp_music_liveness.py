#!/usr/bin/env python3
"""
tests/test_upnp_music_liveness.py — the Naim's music tree follows what
is SERVING, not what is merely indexed.

`tracks` outlives its files. When the music volume is unmounted the rows
stay in the index, so `DB.primary_udn()` — "the udn with the most rows" —
happily backed a full Artists/Albums/Genres tree in which nothing could
play: browsing worked, pressing play 404'd. The PWA and Subsonic both
degraded cleanly (they read the server registry); the UPnP tree was the
one surface that did not.

`api_upnp_ids.music_udn()` now answers "which music library is actually
serving?" — a registered `MediaServer` being the evidence — and the root
container offers Artists/Albums/Genres only while one is.

Run standalone:  python3 -m unittest tests.test_upnp_music_liveness -v
"""
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import api_upnp
import api_upnp_ids
import dlna_discovery
import dlna_localfs_wiring
from dlna_library import LibraryDB

MUSIC_UDN = "uuid:localfs-musictest"
AB_UDN    = "uuid:localfs-abtest"
VIDEO_UDN = "uuid:localfs-movies"


class _FakeServer:
    def __init__(self, udn):
        self.udn = udn


class _FakeRegistry:
    """Stands in for dlna_discovery.SERVERS — only .all() is consulted."""

    def __init__(self, *udns):
        self._servers = [_FakeServer(u) for u in udns]

    def all(self):
        return list(self._servers)


class _Base(unittest.TestCase):
    """A library holding BOTH music and audiobook rows, as the real one
    does — the music rows are what must stop being offered."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        with self.db._pool.write() as conn:
            for i in range(6):          # music is the BIGGEST library
                conn.execute(
                    "INSERT INTO tracks(udn, obj_id, url, title, artist, "
                    "album, genre) VALUES (?,?,?,?,?,?,?)",
                    (MUSIC_UDN, f"m{i}", f"http://gw:8200/localfs/stream/m{i}",
                     f"Song {i}", "Bad Company", "Straight Shooter", "Rock"))
            for i in range(2):
                conn.execute(
                    "INSERT INTO tracks(udn, obj_id, url, title, artist, "
                    "album, album_key) VALUES (?,?,?,?,?,?,?)",
                    (AB_UDN, f"b{i}", f"http://gw:8200/localfs/stream/b{i}",
                     f"Chapter {i}", "Iain M Banks", "Use of Weapons",
                     "Culture/03"))
        self._p1 = patch.object(api_upnp_ids, "DB", self.db)
        self._p1.start()
        self._p2 = patch.object(dlna_localfs_wiring, "AUDIOBOOKS_UDN", AB_UDN)
        self._p2.start()

    def tearDown(self):
        self._p1.stop()
        self._p2.stop()
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def registry(self, *udns):
        return patch.object(dlna_discovery, "SERVERS", _FakeRegistry(*udns))

    def browse(self, oid, flag="BrowseDirectChildren", start=0, count=0):
        return api_upnp._gw_browse(oid, flag, start, count)


class TestMusicUdnFollowsWhatIsServing(_Base):

    def test_music_server_registered_backs_the_tree(self):
        with self.registry(MUSIC_UDN, AB_UDN):
            self.assertEqual(api_upnp_ids.music_udn(), MUSIC_UDN)

    def test_unmounted_music_volume_is_not_offered(self):
        """THE regression: books came up, music did not. Its rows are
        still the majority of the index and must NOT win."""
        with self.registry(AB_UDN):
            self.assertEqual(api_upnp_ids.music_udn(), "")

    def test_video_udn_is_never_the_music_library(self):
        with self.registry(VIDEO_UDN):
            self.assertEqual(api_upnp_ids.music_udn(), "")

    def test_empty_registry_falls_back_to_the_index(self):
        """The SSDP announcer starts BEFORE maybe_start_localfs, so an
        empty registry means 'mid-boot', not 'nothing is live' — and a
        client that caches an empty tree is worse than a stale one."""
        with self.registry():
            self.assertEqual(api_upnp_ids.music_udn(), MUSIC_UDN)

    def test_biggest_LIVE_library_wins_not_biggest_overall(self):
        other = "uuid:upnp-minim"
        with self.db._pool.write() as conn:
            for i in range(3):          # smaller than music, bigger than books
                conn.execute(
                    "INSERT INTO tracks(udn, obj_id, url, title, artist, album)"
                    " VALUES (?,?,?,?,?,?)",
                    (other, f"o{i}", f"http://minim/{i}", f"T{i}", "A", "B"))
        with self.registry(other, AB_UDN):        # music NOT serving
            self.assertEqual(api_upnp_ids.music_udn(), other)
        with self.registry(other, MUSIC_UDN, AB_UDN):
            self.assertEqual(api_upnp_ids.music_udn(), MUSIC_UDN)

    def test_registry_failure_falls_back_rather_than_raising(self):
        boom = patch.object(dlna_discovery, "SERVERS", property(
            lambda self: (_ for _ in ()).throw(RuntimeError("mid-init"))))
        with boom:
            self.assertIsInstance(api_upnp_ids.music_udn(), str)


class TestRootContainerFollowsSuit(_Base):

    def _ids_in(self, xml):
        return re.findall(r'id="([^"]+)"', xml)

    def test_music_containers_present_while_serving(self):
        with self.registry(MUSIC_UDN, AB_UDN):
            xml, n, total = self.browse("0")
        ids = self._ids_in(xml)
        for want in ("artists", "albums", "genres"):
            self.assertIn(want, ids)
        self.assertEqual(total, 6)      # 3 music + favalbums + playlists + books

    def test_music_containers_gone_when_not_serving(self):
        with self.registry(AB_UDN):
            xml, n, total = self.browse("0")
        ids = self._ids_in(xml)
        for gone in ("artists", "albums", "genres"):
            self.assertNotIn(gone, ids)
        self.assertIn("abooks", ids)    # books still browsable
        self.assertIn("playlists", ids)
        self.assertEqual(total, 3)

    def test_meta_childcount_matches_the_listing(self):
        """A childCount that disagrees with the listing makes strict
        control points give up on the container."""
        for regs in ((MUSIC_UDN, AB_UDN), (AB_UDN,)):
            with self.subTest(registered=regs):
                with self.registry(*regs):
                    meta, _, _ = self.browse("0", flag="BrowseMetadata")
                    _xml, _n, total = self.browse("0")
                got = int(re.search(r'childCount="(\d+)"', meta).group(1))
                self.assertEqual(got, total)

    def test_drilling_into_music_when_absent_is_empty_not_a_fault(self):
        """A stale bookmark on the Naim must not 500 the browse."""
        with self.registry(AB_UDN):
            for oid in ("artists", "albums", "genres"):
                with self.subTest(oid=oid):
                    xml, n, total = self.browse(oid)
                    self.assertEqual((n, total), (0, 0))
                    self.assertIn("DIDL-Lite", xml)

    def test_audiobook_tree_is_untouched_by_music_being_down(self):
        with self.registry(AB_UDN):
            xml, _, total = self.browse("abooks")
        self.assertEqual(total, 1)
        self.assertIn("Iain M Banks", xml)


class TestPrimaryUdnAmong(_Base):
    """The SQL half — `among` is a restriction, and empty means none."""

    def test_none_is_unrestricted(self):
        self.assertEqual(self.db.primary_udn(), MUSIC_UDN)
        self.assertEqual(self.db.primary_udn(among=None), MUSIC_UDN)

    def test_empty_list_means_nothing_is_serving(self):
        self.assertEqual(self.db.primary_udn(among=[]), "")

    def test_restricts_to_the_given_set(self):
        self.assertEqual(self.db.primary_udn(among=[AB_UDN]), AB_UDN)

    def test_a_udn_with_no_rows_yields_empty(self):
        self.assertEqual(self.db.primary_udn(among=["uuid:nothing"]), "")

    def test_accepts_any_iterable(self):
        self.assertEqual(self.db.primary_udn(among={AB_UDN}), AB_UDN)
        self.assertEqual(self.db.primary_udn(among=(AB_UDN,)), AB_UDN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
