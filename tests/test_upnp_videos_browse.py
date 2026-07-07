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
    # (id, title, created, location_name, country)
    ("v1", "NL_Utrecht_20260610_0952.mov", "2026-06-10T09:52:44.000000Z",
     "Utrecht", "NL"),
    ("v2", "NL_Utrecht_20260609_1200.mov", "2026-06-09T12:00:00.000000Z",
     "Utrecht", "NL"),
    ("v3", "NL_Amsterdam_20260501_0800.mov", "2026-05-01T08:00:00.000000Z",
     "Amsterdam", "NL"),
    ("v4", "NL_Amsterdam_20210816_1448.mov", "2021-08-16T14:48:35.000000Z",
     "Amsterdam", "NL"),
    ("v5", "20210101_0000.mp4", "2021-01-01T00:00:00.000000Z", "", ""),
    # live data has NULL (not '') for un-geocoded videos — must land in the
    # same "(no location)" bucket, sorted last (the 2026-07-06 live bug:
    # NULL sorted FIRST and the bucket resolved empty)
    ("v6", "20210102_0000.mp4", "2021-01-02T00:00:00.000000Z", None, None),
    ("v7", "PT_Faro_20250701_1000.mov", "2025-07-01T10:00:00.000000Z",
     "Faro", "PT"),
    # located but country unknown ('' = fetched-no-country) → "(no country)"
    ("v8", "Atlantis_20240101_0000.mov", "2024-01-01T00:00:00.000000Z",
     "Atlantis", ""),
]


def _loc_id(name: str) -> str:
    if not name:
        return "vidloc-none"       # sentinel — see api_upnp vidlocs branch
    return ("vidloc:" +
            base64.urlsafe_b64encode(name.encode()).decode().rstrip("="))


def _cloc_id(cc: str, name: str) -> str:
    payload = cc + "\x00" + name
    return ("vidcloc:" +
            base64.urlsafe_b64encode(payload.encode()).decode().rstrip("="))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        with self.db._pool.write() as conn:
            for vid, title, created, loc, cc in VIDS:
                conn.execute(
                    "INSERT INTO videos (id, udn, url, title, file_path, "
                    "created, location_name, country, added_at) "
                    "VALUES (?,?,?,?,?,?,?,?,1)",
                    (vid, UDN, f"http://h/localfs/video/{vid}", title,
                     f"/m/{vid}.mov", created, loc, cc))
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
        self.assertEqual((n_ret, total), (8, 8))
        self.assertIn('id="vid:v1"', xml)

    def test_vidall_pages(self):
        xml, n_ret, total = self.browse("vidall", start=1, count=2)
        self.assertEqual((n_ret, total), (2, 8))


class TestByDate(_Base):
    def test_years_newest_first_with_counts(self):
        xml, n_ret, total = self.browse("viddates")
        self.assertEqual((n_ret, total), (4, 4))
        self.assertIn('id="viddate:2026"', xml)
        self.assertIn('id="viddate:2021"', xml)
        self.assertLess(xml.find("2026"), xml.find("2021"))
        self.assertIn('childCount="3"', xml)   # 2026 has 3 videos
        self.assertIn('childCount="3"', xml)   # 2021 has 3

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
    """2026-07-06 v2: vidlocs is COUNTRY blocks first (A-Z by ISO code),
    then "(no country)" for located-but-unknown-country, then the
    "(no location)" bucket last. Each country drills to its locations."""

    def test_country_blocks_alpha_specials_last(self):
        xml, n_ret, total = self.browse("vidlocs")
        self.assertEqual((n_ret, total), (4, 4))
        self.assertIn('id="vidcountry:NL"', xml)
        self.assertIn('id="vidcountry:PT"', xml)
        self.assertIn('id="vidcountry-none"', xml)
        self.assertIn('id="vidloc-none"', xml)
        self.assertIn("(no country)", xml)
        self.assertIn("(no location)", xml)
        self.assertLess(xml.find('id="vidcountry:NL"'),
                        xml.find('id="vidcountry:PT"'))
        self.assertLess(xml.find('id="vidcountry:PT"'),
                        xml.find('id="vidcountry-none"'))
        self.assertLess(xml.find('id="vidcountry-none"'),
                        xml.find('id="vidloc-none"'))
        self.assertIn('childCount="4"', xml)    # NL has 4 videos

    def test_country_lists_locations_alpha(self):
        xml, n_ret, total = self.browse("vidcountry:NL")
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn(f'id="{_cloc_id("NL", "Amsterdam")}"', xml)
        self.assertIn(f'id="{_cloc_id("NL", "Utrecht")}"', xml)
        self.assertLess(xml.find("Amsterdam"), xml.find("Utrecht"))

    def test_country_location_lists_items_newest_first(self):
        xml, n_ret, total = self.browse(_cloc_id("NL", "Amsterdam"))
        self.assertEqual((n_ret, total), (2, 2))
        self.assertLess(xml.find('id="vid:v3"'), xml.find('id="vid:v4"'))

    def test_no_country_block_holds_its_locations(self):
        xml, n_ret, total = self.browse("vidcountry-none")
        self.assertEqual((n_ret, total), (1, 1))
        self.assertIn(f'id="{_cloc_id("", "Atlantis")}"', xml)

    def test_no_location_bucket_resolves(self):
        # '' and NULL location_name both land here (items directly)
        xml, n_ret, total = self.browse(_loc_id(""))
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn('id="vid:v5"', xml)
        self.assertIn('id="vid:v6"', xml)

    def test_legacy_vidloc_id_still_resolves(self):
        # pre-country ids keep working (cross-country by location name)
        xml, n_ret, total = self.browse(_loc_id("Amsterdam"))
        self.assertEqual((n_ret, total), (2, 2))

    def test_garbled_ids_empty_not_500(self):
        for oid in ("vidloc:!!!not-base64!!!", "vidcloc:!!!garbage!!!"):
            xml, n_ret, total = self.browse(oid)
            self.assertEqual((n_ret, total), (0, 0), oid)


class TestNoCityBucket(_Base):
    """Plan A (2026-07-07): a country-only video (country inferred, no
    city) lives INSIDE its country block as a "(no city)" bucket — not in
    the top-level "(no location)" bucket."""

    def setUp(self):
        super().setUp()
        with self.db._pool.write() as conn:
            conn.execute(
                "INSERT INTO videos (id, udn, url, title, file_path, "
                "created, location_name, country, added_at) "
                "VALUES (?,?,?,?,?,?,?,?,1)",
                ("v9", UDN, "http://h/localfs/video/v9",
                 "PT_20250702_1100.mov", "/m/v9.mov",
                 "2025-07-02T11:00:00.000000Z", None, "PT"))

    def test_country_count_includes_country_only(self):
        xml, n_ret, total = self.browse("vidlocs")
        # PT block now counts Faro's v7 + country-only v9
        pt = xml.find('id="vidcountry:PT"')
        self.assertIn('childCount="2"', xml[pt:pt + 200])

    def test_country_drill_lists_no_city_last(self):
        xml, n_ret, total = self.browse("vidcountry:PT")
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn(f'id="{_cloc_id("PT", "Faro")}"', xml)
        self.assertIn(f'id="{_cloc_id("PT", "")}"', xml)
        self.assertIn("(no city)", xml)
        self.assertLess(xml.find("Faro"), xml.find("(no city)"))

    def test_no_city_bucket_resolves_to_items(self):
        xml, n_ret, total = self.browse(_cloc_id("PT", ""))
        self.assertEqual((n_ret, total), (1, 1))
        self.assertIn('id="vid:v9"', xml)

    def test_no_city_browse_metadata_titled(self):
        xml, n_ret, total = self.browse(_cloc_id("PT", ""),
                                        flag="BrowseMetadata")
        self.assertEqual(n_ret, 1)
        self.assertIn("(no city)", xml)

    def test_country_only_absent_from_no_location_bucket(self):
        xml, n_ret, total = self.browse(_loc_id(""))
        self.assertEqual((n_ret, total), (2, 2))   # v5 + v6 only
        self.assertNotIn('id="vid:v9"', xml)


class TestByPerson(_Base):
    """Plan B (2026-07-07): Immich person tags (video_people, synced by
    tools/immich_people_sync.py) surface as a "👤 By person" container —
    present only when persons exist (TestVideosRoot proves the 3-child
    root when there are none)."""

    def setUp(self):
        super().setUp()
        self.db.video_people_replace("Anna", "p-1", ["v1", "v3"])
        self.db.video_people_replace("Bob", "p-2", ["v3"])

    def _person_id(self, name):
        return ("vidperson:" +
                base64.urlsafe_b64encode(name.encode()).decode().rstrip("="))

    def test_videos_root_gains_people_container(self):
        xml, n_ret, total = self.browse("videos")
        self.assertEqual((n_ret, total), (4, 4))
        self.assertIn('id="vidpeople"', xml)
        self.assertIn("By person", xml)
        # between "By location" and the flat list
        self.assertLess(xml.find('id="vidlocs"'), xml.find('id="vidpeople"'))
        self.assertLess(xml.find('id="vidpeople"'), xml.find('id="vidall"'))

    def test_vidpeople_lists_persons_with_counts(self):
        xml, n_ret, total = self.browse("vidpeople")
        self.assertEqual((n_ret, total), (2, 2))
        self.assertIn(f'id="{self._person_id("Anna")}"', xml)
        self.assertIn(f'id="{self._person_id("Bob")}"', xml)
        anna = xml.find(f'id="{self._person_id("Anna")}"')
        self.assertIn('childCount="2"', xml[anna:anna + 200])
        self.assertLess(xml.find("Anna"), xml.find("Bob"))   # A-Z

    def test_person_resolves_items_newest_first(self):
        xml, n_ret, total = self.browse(self._person_id("Anna"))
        self.assertEqual((n_ret, total), (2, 2))
        self.assertLess(xml.find('id="vid:v1"'), xml.find('id="vid:v3"'))

    def test_person_browse_metadata(self):
        xml, n_ret, total = self.browse(self._person_id("Bob"),
                                        flag="BrowseMetadata")
        self.assertEqual(n_ret, 1)
        self.assertIn("Bob", xml)

    def test_garbled_person_id_empty_not_500(self):
        xml, n_ret, total = self.browse("vidperson:!!!garbage!!!")
        self.assertEqual((n_ret, total), (0, 0))

    def test_unknown_person_empty(self):
        xml, n_ret, total = self.browse(self._person_id("Nobody"))
        self.assertEqual((n_ret, total), (0, 0))


class TestMetadataAndPaging(_Base):
    def test_browse_metadata_on_containers(self):
        for oid in ("videos", "viddates", "vidlocs", "vidall",
                    "viddate:2026", "vidcountry:NL",
                    _cloc_id("NL", "Utrecht")):
            xml, n_ret, total = self.browse(oid, flag="BrowseMetadata")
            self.assertEqual(n_ret, 1, f"BrowseMetadata failed for {oid}")
            self.assertIn(f'id="{oid}"', xml)

    def test_month_items_page(self):
        xml, n_ret, total = self.browse("viddate:2026-06", start=1, count=1)
        self.assertEqual((n_ret, total), (1, 2))
        self.assertIn('id="vid:v2"', xml)


if __name__ == "__main__":
    unittest.main()
