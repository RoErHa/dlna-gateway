#!/usr/bin/env python3
"""DLNA Videos sub-browse for the LG TV (2026-07-06).

The 📹 Videos folder used to be ONE flat list (~3,000 items — unbrowsable
with a TV remote). Contract now:

  * "videos" lists three child containers:
      viddates  "📅 By date"      (count = #years)
      vidlocs   "📍 By location"  (count = #locations, incl. "(no location)")
      vidall    "🎞 All videos"   (count = total; the old flat list)
  * "viddates"           → year containers  viddate:YYYY   (newest first)
  * "viddate:YYYY"       → month containers viddate:YYYY-MM (newest first)
  * "viddate:YYYY-MM"    → video items, created DESC
  * "vidlocs"            → vidloc:<b64(name)> containers A-Z (case-insensitive),
                           "(no location)" bucket LAST when it exists
  * "vidloc:<b64>"       → that location's items, created DESC
  * garbled/unknown ids  → empty container, never a 500

Run standalone:  python3 -m unittest tests.test_upnp_videos_browse -v
"""
import base64
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

UDN = api_upnp._VIDEO_UDN

VIDS = [
    # (id, title, created, location_name)
    ("v1", "Utrecht_20260610_0952.mov", "2026-06-10T09:52:44.000000Z", "Utrecht"),
    ("v2", "Utrecht_20260609_1200.mov", "2026-06-09T12:00:00.000000Z", "Utrecht"),
    ("v3", "Amsterdam_20260501_0800.mov", "2026-05-01T08:00:00.000000Z", "Amsterdam"),
    ("v4", "Amsterdam_20210816_1448.mov", "2021-08-16T14:48:35.000000Z", "Amsterdam"),
    ("v5", "20210101_0000.mp4", "2021-01-01T00:00:00.000000Z", ""),
]


def _loc_id(name: str) -> str:
    if not name:
        return "vidloc-none"       # sentinel — see api_upnp vidlocs branch
    return ("vidloc:" +
            base64.urlsafe_b64encode(name.encode()).decode().rstrip("="))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        with self.db._pool.write() as conn:
            for vid, title, created, loc in VIDS:
                conn.execute(
                    "INSERT INTO videos (id, udn, url, title, file_path, "
                    "created, location_name, added_at) "
                    "VALUES (?,?,?,?,?,?,?,1)",
                    (vid, UDN, f"http://h/localfs/video/{vid}", title,
                     f"/m/{vid}.mov", created, loc))
        self._patch = patch.object(api_upnp, "DB", self.db)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def browse(self, oid, flag="BrowseDirectChildren", start=0, count=0):
        return api_upnp._gw_browse(oid, flag, start, count)


class TestVideosRoot(_Base):
    def test_videos_lists_three_containers(self):
        xml, n_ret, total = self.browse("videos")
        self.assertEqual((n_ret, total), (3, 3))
        for cid, title in (("viddates", "By date"),
                           ("vidlocs", "By location"),
                           ("vidall", "All videos")):
            self.assertIn(f'id="{cid}"', xml, f"missing {cid}")
            self.assertIn(title, xml)
        # date browse first, flat list last
        self.assertLess(xml.find('id="viddates"'), xml.find('id="vidlocs"'))
        self.assertLess(xml.find('id="vidlocs"'), xml.find('id="vidall"'))

    def test_vidall_is_the_flat_list(self):
        xml, n_ret, total = self.browse("vidall")
        self.assertEqual((n_ret, total), (5, 5))
        self.assertIn('id="vid:v1"', xml)

    def test_vidall_pages(self):
        xml, n_ret, total = self.browse("vidall", start=1, count=2)
        self.assertEqual((n_ret, total), (2, 5))


class TestByDate(_Base):
    def test_years_newest_first_with_counts(self):
        xml, n_ret, total = self.browse("viddates")
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn('id="viddate:2026"', xml)
        self.assertIn('id="viddate:2021"', xml)
        self.assertLess(xml.find("2026"), xml.find("2021"))
        self.assertIn('childCount="3"', xml)   # 2026 has 3 videos
        self.assertIn('childCount="2"', xml)   # 2021 has 2

    def test_year_lists_months_newest_first(self):
        xml, n_ret, total = self.browse("viddate:2026")
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn('id="viddate:2026-06"', xml)
        self.assertIn('id="viddate:2026-05"', xml)
        self.assertLess(xml.find("2026-06"), xml.find("2026-05"))

    def test_month_lists_items_newest_first(self):
        xml, n_ret, total = self.browse("viddate:2026-06")
        self.assertEqual((n_ret, total), (2, 2))
        self.assertLess(xml.find('id="vid:v1"'), xml.find('id="vid:v2"'))

    def test_unknown_year_is_empty_not_500(self):
        xml, n_ret, total = self.browse("viddate:1999")
        self.assertEqual((n_ret, total), (0, 0))


class TestByLocation(_Base):
    def test_locations_alpha_with_no_location_last(self):
        xml, n_ret, total = self.browse("vidlocs")
        self.assertEqual((n_ret, total), (3, 3))
        self.assertIn(f'id="{_loc_id("Amsterdam")}"', xml)
        self.assertIn(f'id="{_loc_id("Utrecht")}"', xml)
        self.assertIn(f'id="{_loc_id("")}"', xml)
        self.assertIn("(no location)", xml)
        self.assertLess(xml.find("Amsterdam"), xml.find("Utrecht"))
        self.assertLess(xml.find("Utrecht"), xml.find("(no location)"))

    def test_location_lists_items_newest_first(self):
        xml, n_ret, total = self.browse(_loc_id("Amsterdam"))
        self.assertEqual((n_ret, total), (2, 2))
        self.assertLess(xml.find('id="vid:v3"'), xml.find('id="vid:v4"'))

    def test_no_location_bucket_resolves(self):
        xml, n_ret, total = self.browse(_loc_id(""))
        self.assertEqual((n_ret, total), (1, 1))
        self.assertIn('id="vid:v5"', xml)

    def test_garbled_location_id_is_empty_not_500(self):
        xml, n_ret, total = self.browse("vidloc:!!!not-base64!!!")
        self.assertEqual((n_ret, total), (0, 0))


class TestMetadataAndPaging(_Base):
    def test_browse_metadata_on_containers(self):
        for oid in ("videos", "viddates", "vidlocs", "vidall",
                    "viddate:2026", _loc_id("Utrecht")):
            xml, n_ret, total = self.browse(oid, flag="BrowseMetadata")
            self.assertEqual(n_ret, 1, f"BrowseMetadata failed for {oid}")
            self.assertIn(f'id="{oid}"', xml)

    def test_month_items_page(self):
        xml, n_ret, total = self.browse("viddate:2026-06", start=1, count=1)
        self.assertEqual((n_ret, total), (1, 2))
        self.assertIn('id="vid:v2"', xml)


if __name__ == "__main__":
    unittest.main()
