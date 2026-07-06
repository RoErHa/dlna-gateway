#!/usr/bin/env python3
"""
tests/test_video_db.py — Phase V1a: the `videos` + `geocode_cache` tables and
their LibraryDB methods. Throw-away temp DB; no network, no ffmpeg.

Run: python3 -m unittest tests.test_video_db -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB

UDN = "uuid:localfs-movies"


def _vrow(vid, title, created, **kw):
    row = {
        "id": vid, "udn": UDN, "url": f"http://h/localfs/video/{vid}",
        "title": title, "file_path": f"/m/{vid}.mov", "folder": "2026",
        "duration": 12.5, "width": 1920, "height": 1080,
        "vcodec": "hevc", "acodec": "aac", "container": "mov",
        "mime": "video/quicktime", "size": 1000, "mtime": 1.0,
        "created": created, "location": "+52.37+004.90/",
        "location_name": "Amsterdam", "poster": None,
    }
    row.update(kw)
    return row


class TestVideoTable(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_upsert_and_fetch(self):
        n = self.db.upsert_videos(UDN, [
            _vrow("a", "Old", "2024-01-01T10:00:00Z"),
            _vrow("b", "New", "2026-06-14T14:30:00Z"),
        ])
        self.assertEqual(n, 2)
        vids = self.db.all_videos(UDN)
        self.assertEqual([v["title"] for v in vids], ["New", "Old"])  # newest first
        self.assertEqual(vids[0]["vcodec"], "hevc")
        self.assertEqual(vids[0]["location_name"], "Amsterdam")

    def test_video_by_id(self):
        self.db.upsert_videos(UDN, [_vrow("x", "Clip", "2026-06-14T14:30:00Z")])
        self.assertEqual(self.db.video_by_id("x")["title"], "Clip")
        self.assertIsNone(self.db.video_by_id("nope"))

    def test_upsert_is_replace_by_id(self):
        self.db.upsert_videos(UDN, [_vrow("x", "First", "2026-01-01T00:00:00Z")])
        self.db.upsert_videos(UDN, [_vrow("x", "Second", "2026-01-01T00:00:00Z")])
        vids = self.db.all_videos(UDN)
        self.assertEqual(len(vids), 1)
        self.assertEqual(vids[0]["title"], "Second")

    def test_clear_videos(self):
        self.db.upsert_videos(UDN, [_vrow("a", "A", "2026-01-01T00:00:00Z"),
                                    _vrow("b", "B", "2026-01-02T00:00:00Z")])
        self.assertEqual(self.db.clear_videos(UDN), 2)
        self.assertEqual(self.db.all_videos(UDN), [])

    def test_videos_independent_of_audio_clear(self):
        # clear(udn) wipes tracks, NOT videos.
        self.db.upsert_videos(UDN, [_vrow("a", "A", "2026-01-01T00:00:00Z")])
        self.db.clear(UDN)
        self.assertEqual(len(self.db.all_videos(UDN)), 1)


class TestGeocodeCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_miss_then_hit(self):
        self.assertEqual(self.db.geocode_get(52.3676, 4.9041),
                         (None, None, False))
        self.db.geocode_put(52.3676, 4.9041, "Amsterdam", "NL")
        self.assertEqual(self.db.geocode_get(52.3676, 4.9041),
                         ("Amsterdam", "NL", True))

    def test_sticky_negative(self):
        # A looked-up-but-no-name result is cached as '' and counts as a HIT.
        self.db.geocode_put(10.0, 20.0, "", "")
        self.assertEqual(self.db.geocode_get(10.0, 20.0), ("", "", True))

    def test_legacy_rows_gain_country_column_as_null(self):
        """Migration: a pre-country geocode_cache row reads back with
        country=None (the upgrade-me marker)."""
        with self.db._pool.write() as conn:
            conn.execute(
                "INSERT INTO geocode_cache (lat_key, lon_key, place, "
                "fetched_at) VALUES (?, ?, 'Old Town', 1)",
                self.db._geo_key(1.0, 1.0))
        self.assertEqual(self.db.geocode_get(1.0, 1.0),
                         ("Old Town", None, True))

    def test_rounding_collapses_nearby_coords(self):
        self.db.geocode_put(52.36761, 4.90412, "Amsterdam", "NL")
        # within ~1 m → same rounded key (3 dp)
        self.assertEqual(self.db.geocode_get(52.36759, 4.90408),
                         ("Amsterdam", "NL", True))

    def test_survives_audio_clear(self):
        self.db.geocode_put(1.0, 2.0, "Somewhere")
        self.db.clear(UDN)
        self.assertEqual(self.db.geocode_get(1.0, 2.0),
                         ("Somewhere", None, True))


if __name__ == "__main__":
    unittest.main()
