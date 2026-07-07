#!/usr/bin/env python3
"""
tests/test_video_loc_overrides.py — Plan A: the `video_location_overrides`
table, its LibraryDB methods, the "(no city)" browse-query semantics, and the
re-apply hook in dlna_video_index. Throw-away temp DB; no network, no ffmpeg.

Contract under test:
  * Overrides are keyed by the path-stable video id and survive
    clear_videos() (force rescan) — the whole point: the scanner derives
    rows from file metadata and these files have NO GPS, so a rescan
    would wipe inferred locations without the re-apply.
  * source='manual' always wins — an inferred write never overwrites it.
  * apply_location_overrides() never touches a row with real GPS, never
    rewrites an embedded title, and is idempotent.
  * "(no city)": a country-set/location-empty video belongs INSIDE its
    country (vidcountry drill + PWA country block), NOT in the top-level
    "(no location)" bucket.

Run: python3 -m unittest tests.test_video_loc_overrides -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
import dlna_video_index

UDN = "uuid:localfs-movies"


def _vrow(vid, title, created, *, location=None, location_name=None,
          country=None, file_path=None, **kw):
    row = {
        "id": vid, "udn": UDN, "url": f"http://h/localfs/video/{vid}",
        "title": title, "file_path": file_path or f"/m/{vid}.mov",
        "folder": "", "duration": 12.5, "width": 1920, "height": 1080,
        "vcodec": "hevc", "acodec": "aac", "container": "mov",
        "mime": "video/quicktime", "size": 1000, "mtime": 1.0,
        "created": created, "location": location,
        "location_name": location_name, "country": country, "poster": None,
    }
    row.update(kw)
    return row


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)


# ── the table + methods ────────────────────────────────────────────


class TestOverrideTable(_Base):
    def test_set_and_list_roundtrip(self):
        ok = self.db.video_loc_override_set(
            "v1", "Utrecht", "NL", "inferred_same_day")
        self.assertTrue(ok)
        rows = self.db.video_loc_override_list()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["video_id"], "v1")
        self.assertEqual(r["location_name"], "Utrecht")
        self.assertEqual(r["country"], "NL")
        self.assertEqual(r["source"], "inferred_same_day")

    def test_country_only_override(self):
        self.assertTrue(self.db.video_loc_override_set(
            "v1", "", "PT", "inferred_country"))
        r = self.db.video_loc_override_list()[0]
        self.assertEqual(r["location_name"], "")
        self.assertEqual(r["country"], "PT")

    def test_inferred_updates_inferred(self):
        self.db.video_loc_override_set("v1", "Faro", "PT", "inferred_window")
        self.assertTrue(self.db.video_loc_override_set(
            "v1", "Lagos", "PT", "inferred_same_day"))
        r = self.db.video_loc_override_list()[0]
        self.assertEqual(r["location_name"], "Lagos")
        self.assertEqual(r["source"], "inferred_same_day")

    def test_manual_wins_over_inferred(self):
        self.db.video_loc_override_set("v1", "Utrecht", "NL", "manual")
        blocked = self.db.video_loc_override_set(
            "v1", "Faro", "PT", "inferred_same_day")
        self.assertFalse(blocked)
        r = self.db.video_loc_override_list()[0]
        self.assertEqual(r["location_name"], "Utrecht")
        self.assertEqual(r["source"], "manual")

    def test_manual_overwrites_manual(self):
        self.db.video_loc_override_set("v1", "Utrecht", "NL", "manual")
        self.assertTrue(self.db.video_loc_override_set(
            "v1", "Amsterdam", "NL", "manual"))
        self.assertEqual(
            self.db.video_loc_override_list()[0]["location_name"],
            "Amsterdam")

    def test_remove(self):
        self.db.video_loc_override_set("v1", "Utrecht", "NL", "manual")
        self.assertTrue(self.db.video_loc_override_remove("v1"))
        self.assertFalse(self.db.video_loc_override_remove("v1"))
        self.assertEqual(self.db.video_loc_override_list(), [])

    def test_survives_clear_videos(self):
        self.db.upsert_videos(UDN, [_vrow("v1", "t", "2026-01-01T10:00:00Z")])
        self.db.video_loc_override_set("v1", "Utrecht", "NL",
                                       "inferred_window")
        self.db.clear_videos(UDN)
        self.assertEqual(len(self.db.video_loc_override_list()), 1)

    def test_update_video_location(self):
        self.db.upsert_videos(UDN, [_vrow("v1", "old", "2026-01-01T10:00:00Z")])
        self.db.update_video_location("v1", "Utrecht", "NL", "new-title")
        v = self.db.video_by_id("v1")
        self.assertEqual(v["location_name"], "Utrecht")
        self.assertEqual(v["country"], "NL")
        self.assertEqual(v["title"], "new-title")


# ── "(no city)" query semantics ────────────────────────────────────


class TestNoCityQueries(_Base):
    def setUp(self):
        super().setUp()
        self.db.upsert_videos(UDN, [
            # located, in NL
            _vrow("v1", "NL_Utrecht_20260610_0952.mov",
                  "2026-06-10T09:52:00Z", location="+52.09+005.12/",
                  location_name="Utrecht", country="NL"),
            # country-only (inferred): country set, NO location_name
            _vrow("v2", "NL_20260611_1000.mov", "2026-06-11T10:00:00Z",
                  location_name=None, country="NL"),
            # fully unlocated → the top-level "(no location)" bucket
            _vrow("v3", "20210101_0000.mp4", "2021-01-01T00:00:00Z",
                  location_name=None, country=None),
            # located, country unknown → "(no country)"
            _vrow("v4", "Atlantis_20240101_0000.mov", "2024-01-01T00:00:00Z",
                  location_name="Atlantis", country=""),
        ])

    def test_video_countries_includes_country_only_rows(self):
        rows = {r["country"]: r["count"] for r in
                self.db.video_countries(UDN)}
        self.assertEqual(rows.get("NL"), 2)      # v1 located + v2 country-only
        self.assertEqual(rows.get(""), 1)        # v4 located, no country
        # v3 (no location AND no country) is NOT a country entry
        self.assertEqual(sum(rows.values()), 3)

    def test_locations_for_country_appends_no_city_bucket_last(self):
        rows = self.db.video_locations_for_country(UDN, "NL")
        self.assertEqual([r["location_name"] for r in rows], ["Utrecht", ""])
        self.assertEqual(rows[-1]["count"], 1)

    def test_no_city_bucket_absent_when_country_fully_located(self):
        rows = self.db.video_locations_for_country(UDN, "")
        self.assertEqual([r["location_name"] for r in rows], ["Atlantis"])

    def test_videos_by_country_location_empty_loc_matches_null(self):
        vids = self.db.videos_by_country_location(UDN, "NL", "")
        self.assertEqual([v["id"] for v in vids], ["v2"])

    def test_no_location_bucket_excludes_country_only(self):
        rows = {r["location_name"]: r["count"]
                for r in self.db.video_locations(UDN)}
        self.assertEqual(rows.get(""), 1)        # only v3
        vids = self.db.videos_by_location(UDN, "")
        self.assertEqual([v["id"] for v in vids], ["v3"])


# ── the re-apply hook ──────────────────────────────────────────────


class TestApplyOverrides(_Base):
    def test_applies_onto_gps_less_row_and_retitles(self):
        self.db.upsert_videos(UDN, [
            _vrow("v1", "20260611_1000.mov", "2026-06-11T10:00:00Z")])
        self.db.video_loc_override_set("v1", "Utrecht", "NL",
                                       "inferred_same_day")
        n = dlna_video_index.apply_location_overrides(self.db, UDN)
        self.assertEqual(n, 1)
        v = self.db.video_by_id("v1")
        self.assertEqual(v["location_name"], "Utrecht")
        self.assertEqual(v["country"], "NL")
        self.assertEqual(v["title"], "NL_Utrecht_20260611_1000.mov")

    def test_country_only_override_retitles_without_city(self):
        self.db.upsert_videos(UDN, [
            _vrow("v1", "20260611_1000.mov", "2026-06-11T10:00:00Z")])
        self.db.video_loc_override_set("v1", "", "PT", "inferred_country")
        dlna_video_index.apply_location_overrides(self.db, UDN)
        v = self.db.video_by_id("v1")
        self.assertIsNone(v["location_name"])
        self.assertEqual(v["country"], "PT")
        self.assertEqual(v["title"], "PT_20260611_1000.mov")

    def test_never_touches_real_gps_row(self):
        self.db.upsert_videos(UDN, [
            _vrow("v1", "NL_Utrecht_20260610_0952.mov",
                  "2026-06-10T09:52:00Z", location="+52.09+005.12/",
                  location_name="Utrecht", country="NL")])
        self.db.video_loc_override_set("v1", "Faro", "PT", "manual")
        n = dlna_video_index.apply_location_overrides(self.db, UDN)
        self.assertEqual(n, 0)
        v = self.db.video_by_id("v1")
        self.assertEqual(v["location_name"], "Utrecht")
        self.assertEqual(v["title"], "NL_Utrecht_20260610_0952.mov")

    def test_embedded_title_is_preserved(self):
        self.db.upsert_videos(UDN, [
            _vrow("v1", "My Holiday Movie", "2026-06-11T10:00:00Z")])
        self.db.video_loc_override_set("v1", "Utrecht", "NL", "manual")
        n = dlna_video_index.apply_location_overrides(self.db, UDN)
        self.assertEqual(n, 1)
        v = self.db.video_by_id("v1")
        self.assertEqual(v["location_name"], "Utrecht")
        self.assertEqual(v["title"], "My Holiday Movie")   # NOT rewritten

    def test_idempotent(self):
        self.db.upsert_videos(UDN, [
            _vrow("v1", "20260611_1000.mov", "2026-06-11T10:00:00Z")])
        self.db.video_loc_override_set("v1", "Utrecht", "NL", "manual")
        self.assertEqual(
            dlna_video_index.apply_location_overrides(self.db, UDN), 1)
        self.assertEqual(
            dlna_video_index.apply_location_overrides(self.db, UDN), 0)

    def test_missing_video_is_skipped(self):
        self.db.video_loc_override_set("ghost", "Utrecht", "NL", "manual")
        self.assertEqual(
            dlna_video_index.apply_location_overrides(self.db, UDN), 0)


class TestScanReapplies(unittest.TestCase):
    """A FORCE rescan (clear + re-crawl) regenerates the row bare — the
    end-of-scan hook must lay the override back on."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        self.root = tempfile.TemporaryDirectory()
        self.pdir = tempfile.TemporaryDirectory()
        p = os.path.join(self.root.name, "clip.mp4")
        with open(p, "wb") as fh:
            fh.write(b"\x00" * 64)
        os.utime(p, (1750000000, 1750000000))   # deterministic mtime title
        self.vid = dlna_video_index.video_id("clip.mp4")

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)
        self.root.cleanup()
        self.pdir.cleanup()

    def _scan(self, force=False):
        # /nonexistent binaries → probe/poster degrade; geocode off.
        return dlna_video_index.scan_videos(
            self.root.name, UDN, self.db, "http://h", force=force,
            poster_dir=self.pdir.name, geocode=False,
            ffprobe="/nonexistent", ffmpeg="/nonexistent")

    def test_force_rescan_reapplies_override(self):
        self._scan()
        self.db.video_loc_override_set(self.vid, "Utrecht", "NL",
                                       "inferred_window")
        stats = self._scan(force=True)
        self.assertGreaterEqual(stats.get("overrides_applied", 0), 1)
        v = self.db.video_by_id(self.vid)
        self.assertEqual(v["location_name"], "Utrecht")
        self.assertEqual(v["country"], "NL")
        self.assertTrue(v["title"].startswith("NL_Utrecht_"),
                        v["title"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
