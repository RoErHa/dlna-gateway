#!/usr/bin/env python3
"""
tests/test_video_scan.py — Phase V1b/V1c: dlna_geocode (reverse-geocode +
cache) and dlna_video_index (scan GWMovies → videos). Temp DB + temp dir;
ffprobe/ffmpeg and the network are mocked — nothing real is invoked.

Run: python3 -m unittest tests.test_video_scan -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
import dlna_geocode
import dlna_video_index as vix

UDN = "uuid:localfs-movies"


# ── geocode ───────────────────────────────────────────────────────

class TestPlaceFromNominatim(unittest.TestCase):
    def test_city_wins(self):
        self.assertEqual(dlna_geocode._place_from_nominatim(
            {"address": {"city": "Amsterdam", "state": "NH"}}), "Amsterdam")

    def test_town_fallback(self):
        self.assertEqual(dlna_geocode._place_from_nominatim(
            {"address": {"town": "Zandvoort"}}), "Zandvoort")

    def test_display_name_fallback(self):
        self.assertEqual(dlna_geocode._place_from_nominatim(
            {"display_name": "Foo, Bar, NL"}), "Foo")

    def test_empty(self):
        self.assertEqual(dlna_geocode._place_from_nominatim({}), "")


class TestCountryFromNominatim(unittest.TestCase):
    def test_country_code_uppercased(self):
        self.assertEqual(dlna_geocode._country_from_nominatim(
            {"address": {"city": "Amsterdam", "country_code": "nl"}}), "NL")

    def test_missing_country_is_empty(self):
        self.assertEqual(dlna_geocode._country_from_nominatim(
            {"address": {"city": "X"}}), "")
        self.assertEqual(dlna_geocode._country_from_nominatim({}), "")


class TestReverseGeocode(unittest.TestCase):
    def _resp(self, payload):
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return cm

    def test_success_returns_place_and_country(self):
        with mock.patch.object(dlna_geocode.urllib.request, "urlopen",
                               return_value=self._resp(
                                   {"address": {"city": "Amsterdam",
                                                "country_code": "nl"}})):
            self.assertEqual(dlna_geocode.reverse_geocode(52.37, 4.90),
                             ("Amsterdam", "NL"))

    def test_no_name_returns_empty_pair(self):
        with mock.patch.object(dlna_geocode.urllib.request, "urlopen",
                               return_value=self._resp({})):
            self.assertEqual(dlna_geocode.reverse_geocode(0, 0), ("", ""))

    def test_network_error_returns_none(self):
        with mock.patch.object(dlna_geocode.urllib.request, "urlopen",
                               side_effect=OSError("down")):
            self.assertIsNone(dlna_geocode.reverse_geocode(52.37, 4.90))


class TestPlaceFor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_cache_hit_skips_network(self):
        self.db.geocode_put(52.37, 4.90, "Amsterdam", "NL")
        with mock.patch.object(dlna_geocode, "reverse_geocode") as rg:
            self.assertEqual(dlna_geocode.place_for(self.db, 52.37, 4.90),
                             ("Amsterdam", "NL"))
            rg.assert_not_called()

    def test_miss_fetches_and_caches(self):
        with mock.patch.object(dlna_geocode, "reverse_geocode",
                               return_value=("Berlin", "DE")) as rg:
            self.assertEqual(dlna_geocode.place_for(self.db, 52.52, 13.40),
                             ("Berlin", "DE"))
            rg.assert_called_once()
        # second call is a cache hit (no network)
        with mock.patch.object(dlna_geocode, "reverse_geocode") as rg2:
            self.assertEqual(dlna_geocode.place_for(self.db, 52.52, 13.40),
                             ("Berlin", "DE"))
            rg2.assert_not_called()

    def test_transient_not_cached(self):
        with mock.patch.object(dlna_geocode, "reverse_geocode", return_value=None):
            self.assertIsNone(dlna_geocode.place_for(self.db, 1.0, 2.0))
        self.assertEqual(self.db.geocode_get(1.0, 2.0), (None, None, False))

    def test_legacy_row_without_country_upgrades_once(self):
        """A pre-country cache row (country NULL) triggers ONE re-fetch that
        fills the country in; after that it's a plain hit."""
        with self.db._pool.write() as conn:      # simulate a legacy row
            conn.execute(
                "INSERT INTO geocode_cache (lat_key, lon_key, place, country, "
                "fetched_at) VALUES (?, ?, 'Utrecht', NULL, 1)",
                self.db._geo_key(52.09, 5.12))
        with mock.patch.object(dlna_geocode, "reverse_geocode",
                               return_value=("Utrecht", "NL")) as rg:
            self.assertEqual(dlna_geocode.place_for(self.db, 52.09, 5.12),
                             ("Utrecht", "NL"))
            rg.assert_called_once()
        with mock.patch.object(dlna_geocode, "reverse_geocode") as rg2:
            self.assertEqual(dlna_geocode.place_for(self.db, 52.09, 5.12),
                             ("Utrecht", "NL"))
            rg2.assert_not_called()

    def test_legacy_row_upgrade_transient_keeps_place(self):
        """If the country upgrade fetch fails transiently, keep serving the
        cached place with no country — and do NOT mark the row upgraded."""
        with self.db._pool.write() as conn:
            conn.execute(
                "INSERT INTO geocode_cache (lat_key, lon_key, place, country, "
                "fetched_at) VALUES (?, ?, 'Utrecht', NULL, 1)",
                self.db._geo_key(52.09, 5.12))
        with mock.patch.object(dlna_geocode, "reverse_geocode",
                               return_value=None):
            self.assertEqual(dlna_geocode.place_for(self.db, 52.09, 5.12),
                             ("Utrecht", ""))
        place, country, hit = self.db.geocode_get(52.09, 5.12)
        self.assertTrue(hit)
        self.assertIsNone(country)      # still NULL → retried next time


# ── scan ──────────────────────────────────────────────────────────

class TestScan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        self.root = tempfile.mkdtemp()
        self.posters = tempfile.mkdtemp()

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def _mkfile(self, rel, content=b"x"):
        p = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(content)
        return p

    def _scan(self, **kw):
        # No real ffmpeg/geocode: probe returns fixed metadata; no poster.
        meta = {"duration": 12.0, "width": 1920, "height": 1080,
                "vcodec": "hevc", "acodec": "aac", "container": "mov",
                "created": "2026-06-14T14:30:00Z",
                "location": "+52.3676+004.9041/", "title": None}
        with mock.patch.object(vix.ff, "probe", return_value=meta), \
             mock.patch.object(vix.ff, "extract_poster", return_value=False), \
             mock.patch.object(vix.dlna_geocode, "place_for",
                               return_value=("Amsterdam", "NL")):
            return vix.scan_videos(self.root, UDN, self.db,
                                   "http://h:8200", poster_dir=self.posters, **kw)

    def test_video_id_deterministic(self):
        self.assertEqual(vix.video_id("a/b.mov"), vix.video_id("a/b.mov"))
        self.assertNotEqual(vix.video_id("a.mov"), vix.video_id("b.mov"))

    def test_scan_inserts_with_constructed_title(self):
        self._mkfile("2026/clip.mov")
        st = self._scan()
        self.assertEqual((st["scanned"], st["added"]), (1, 1))
        v = self.db.all_videos(UDN)[0]
        self.assertEqual(v["title"], "NL_Amsterdam_20260614_1430.mov")
        self.assertEqual(v["vcodec"], "hevc")
        self.assertEqual(v["location_name"], "Amsterdam")
        self.assertEqual(v["folder"], "2026")
        self.assertTrue(v["url"].endswith("/localfs/video/" + v["id"]))
        self.assertEqual(v["mime"], "video/quicktime")

    def test_skips_non_video(self):
        self._mkfile("song.mp3")
        self._mkfile("clip.mp4")
        st = self._scan()
        self.assertEqual(st["scanned"], 1)   # only the .mp4

    def test_incremental_skip_then_prune(self):
        self._mkfile("a.mov")
        self._mkfile("b.mov")
        self._scan()
        # second scan, unchanged → both skipped
        st = self._scan()
        self.assertEqual((st["added"], st["skipped"]), (0, 2))
        # remove one file → next scan prunes it
        os.unlink(os.path.join(self.root, "b.mov"))
        st = self._scan()
        self.assertEqual(st["pruned"], 1)
        self.assertEqual(len(self.db.all_videos(UDN)), 1)

    def test_force_clears_first(self):
        self._mkfile("a.mov")
        self._scan()
        st = self._scan(force=True)
        self.assertEqual(st["added"], 1)     # re-added after clear

    def test_short_clip_poster_seek_clamped(self):
        # A short clip must not seek past its end (real bug: 1.4s clip, -ss 3 →
        # no frame). 1.0s → seek "0"; long clip → "3".
        self._mkfile("short.mov")
        meta = {"duration": 1.0, "width": 100, "height": 100, "vcodec": "h264",
                "acodec": "aac", "container": "mov",
                "created": "2026-06-14T14:30:00Z", "location": None, "title": None}
        with mock.patch.object(vix.ff, "probe", return_value=meta), \
             mock.patch.object(vix.ff, "extract_poster", return_value=False) as ep, \
             mock.patch.object(vix.dlna_geocode, "place_for", return_value=None):
            vix.scan_videos(self.root, UDN, self.db, "http://h:8200",
                            poster_dir=self.posters)
        self.assertEqual(ep.call_args.kwargs.get("when"), "0")

    def test_missing_root(self):
        st = vix.scan_videos("/no/such/dir", UDN, self.db, "http://h:8200")
        self.assertTrue(st["missing_root"])


if __name__ == "__main__":
    unittest.main()
