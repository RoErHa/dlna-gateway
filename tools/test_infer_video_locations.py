#!/usr/bin/env python3
"""Tests for tools/infer_video_locations.py — temporal-neighbor location
inference for GPS-less videos (Plan A, 2026-07-07).

Covers the pure inference core (injected device lookup — no ffprobe, no
network, no DB) plus the override-writing step over a throw-away temp DB.

Run: python3 -m unittest tools.test_infer_video_locations -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tools.infer_video_locations import infer_all, write_overrides
from dlna_library import LibraryDB

UDN = "uuid:localfs-movies"
DEV = ("Apple", "iPhone 14")


def _v(vid, created, *, location=None, location_name=None, country=None):
    return {"id": vid, "created": created, "location": location,
            "location_name": location_name, "country": country,
            "file_path": f"/m/{vid}.mov", "udn": UDN}


def _gps(vid, created, loc, cc):
    """A located-by-real-GPS row — inference evidence."""
    return _v(vid, created, location="+52.09+005.12/",
              location_name=loc, country=cc)


class TestTiers(unittest.TestCase):
    def _run(self, videos, **kw):
        kw.setdefault("device_of", lambda v: DEV)
        return {d["id"]: d for d in infer_all(videos, **kw)}

    def test_same_day_unanimous(self):
        out = self._run([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-10T09:00:00Z", "Utrecht", "NL"),
            _gps("b", "2026-06-10T20:00:00Z", "Utrecht", "NL"),
        ])
        d = out["x"]
        self.assertEqual(d["tier"], "same_day")
        self.assertEqual(d["source"], "inferred_same_day")
        self.assertEqual((d["location_name"], d["country"]),
                         ("Utrecht", "NL"))
        self.assertEqual(d["neighbors"], 2)

    def test_plus_minus_one_day(self):
        out = self._run([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-11T09:00:00Z", "Faro", "PT"),
        ])
        d = out["x"]
        self.assertEqual(d["tier"], "day1")
        self.assertEqual(d["source"], "inferred_window")
        self.assertEqual(d["location_name"], "Faro")

    def test_window_tier(self):
        out = self._run([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-13T09:00:00Z", "Faro", "PT"),
        ])
        d = out["x"]
        self.assertEqual(d["tier"], "window")
        self.assertEqual(d["source"], "inferred_window")

    def test_window_is_tunable(self):
        vids = [
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-15T09:00:00Z", "Faro", "PT"),
        ]
        self.assertIsNone(self._run(vids)["x"]["tier"])          # default 3
        self.assertEqual(self._run(vids, window=7)["x"]["tier"], "window")

    def test_country_only_when_cities_differ(self):
        out = self._run([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-09T09:00:00Z", "Faro", "PT"),
            _gps("b", "2026-06-11T09:00:00Z", "Lagos", "PT"),
        ])
        d = out["x"]
        self.assertEqual(d["tier"], "country")
        self.assertEqual(d["source"], "inferred_country")
        self.assertEqual(d["location_name"], "")
        self.assertEqual(d["country"], "PT")

    def test_same_day_disagreement_falls_to_country(self):
        out = self._run([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-10T09:00:00Z", "Faro", "PT"),
            _gps("b", "2026-06-10T20:00:00Z", "Lagos", "PT"),
        ])
        self.assertEqual(out["x"]["tier"], "country")

    def test_country_disagreement_blocks(self):
        out = self._run([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-09T09:00:00Z", "Faro", "PT"),
            _gps("b", "2026-06-11T09:00:00Z", "Utrecht", "NL"),
        ])
        d = out["x"]
        self.assertIsNone(d["tier"])
        self.assertEqual(d["reason"], "disagree")

    def test_no_neighbors_in_window(self):
        out = self._run([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2025-01-01T09:00:00Z", "Faro", "PT"),
        ])
        self.assertEqual(out["x"]["reason"], "no_neighbors")

    def test_no_created_date_blocks(self):
        out = self._run([
            _v("x", ""),
            _gps("a", "2026-06-10T09:00:00Z", "Faro", "PT"),
        ])
        self.assertEqual(out["x"]["reason"], "no_date")

    def test_inferred_rows_are_not_evidence(self):
        """No chaining: a row located by a previous inference (location_name
        set, but NO raw GPS) must not count as a neighbor."""
        out = self._run([
            _v("x", "2026-06-10T14:00:00Z"),
            _v("prev", "2026-06-10T09:00:00Z",
               location_name="Utrecht", country="NL"),   # no `location`!
        ])
        self.assertEqual(out["x"]["reason"], "no_neighbors")
        # `prev` itself stays a TARGET (re-inference is self-correcting
        # when neighbors change) — it just can't be evidence for others.
        self.assertIn("prev", out)

    def test_gps_but_ungecoded_rows_are_not_evidence_or_targets(self):
        """GPS-but-geocode-empty rows are the --retry-geocode class: not
        inference targets (they have GPS) and not evidence (no name)."""
        out = self._run([
            _v("g", "2026-06-10T09:00:00Z", location="+52+005/"),
            _v("x", "2026-06-10T14:00:00Z"),
        ])
        self.assertNotIn("g", out)
        self.assertEqual(out["x"]["reason"], "no_neighbors")


class TestDeviceCheck(unittest.TestCase):
    def test_device_mismatch_excludes_neighbor(self):
        devs = {"x": DEV, "a": ("Apple", "iPhone 6")}
        out = {d["id"]: d for d in infer_all([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-10T09:00:00Z", "Utrecht", "NL"),
        ], device_of=lambda v: devs.get(v["id"]))}
        self.assertIsNone(out["x"]["tier"])
        self.assertEqual(out["x"]["reason"], "no_neighbors")

    def test_video_without_device_tags_is_blocked(self):
        devs = {"a": DEV}                     # x: no make/model (WhatsApp)
        out = {d["id"]: d for d in infer_all([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-10T09:00:00Z", "Utrecht", "NL"),
        ], device_of=lambda v: devs.get(v["id"]))}
        self.assertIsNone(out["x"]["tier"])
        self.assertEqual(out["x"]["reason"], "no_device")

    def test_no_device_check_relaxes_both(self):
        devs = {"a": DEV}
        out = {d["id"]: d for d in infer_all([
            _v("x", "2026-06-10T14:00:00Z"),
            _gps("a", "2026-06-10T09:00:00Z", "Utrecht", "NL"),
        ], device_check=False, device_of=lambda v: devs.get(v["id"]))}
        self.assertEqual(out["x"]["tier"], "same_day")


class TestWriteOverrides(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_writes_decided_overrides_with_source(self):
        decisions = [
            {"id": "v1", "tier": "same_day", "source": "inferred_same_day",
             "location_name": "Utrecht", "country": "NL", "neighbors": 2,
             "reason": ""},
            {"id": "v2", "tier": "country", "source": "inferred_country",
             "location_name": "", "country": "PT", "neighbors": 3,
             "reason": ""},
            {"id": "v3", "tier": None, "source": "", "location_name": "",
             "country": "", "neighbors": 0, "reason": "disagree"},
        ]
        n = write_overrides(self.db, decisions)
        self.assertEqual(n, 2)
        rows = {r["video_id"]: r for r in self.db.video_loc_override_list()}
        self.assertEqual(set(rows), {"v1", "v2"})
        self.assertEqual(rows["v1"]["source"], "inferred_same_day")
        self.assertEqual(rows["v2"]["location_name"], "")

    def test_manual_override_is_kept(self):
        self.db.video_loc_override_set("v1", "Utrecht", "NL", "manual")
        n = write_overrides(self.db, [
            {"id": "v1", "tier": "same_day", "source": "inferred_same_day",
             "location_name": "Faro", "country": "PT", "neighbors": 1,
             "reason": ""}])
        self.assertEqual(n, 0)
        r = self.db.video_loc_override_list()[0]
        self.assertEqual(r["location_name"], "Utrecht")


if __name__ == "__main__":
    unittest.main(verbosity=2)
