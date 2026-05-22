#!/usr/bin/env python3
"""
tests/test_radio.py — Internet-radio favourites DB + handler tests
(Phase 1).

Covers the data contract of the radio_favourites table and the
/api/radio/* handlers:
- radio_fav_add is idempotent and rejects incomplete stations
- the 25-cap (RADIO_FAV_MAX) is enforced server-side; a re-add of an
  already-favourited station is allowed even when full
- radio_fav_reorder persists preset ordering
- radio_favourites survives clear(udn) (same invariant as
  album_favourites / album_art / play_counts / lyrics)
- handlers route through DB, reject bad input, and return 409 on full
- /api/radio/search filters out HLS stations and normalizes records

Run standalone:
    python3 -m unittest tests.test_radio -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB


class _MockH:
    def __init__(self):
        self.last = None
    def _json(self, code, payload):
        self.last = (code, payload)


def _station(uuid="u1", name="BBC Radio 6",
             url="http://stream.example/6music"):
    """A complete station dict in the gateway's normalized shape."""
    return {
        "station_uuid": uuid,
        "name":         name,
        "stream_url":   url,
        "homepage":     "http://example/home",
        "favicon":      "http://example/logo.png",
        "codec":        "MP3",
        "bitrate":      128,
        "country":      "GB",
        "tags":         "indie,alternative",
    }


# ── DB layer ──────────────────────────────────────────────────────

class TestRadioFavouritesDB(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_count_starts_zero(self):
        self.assertEqual(self.db.radio_fav_count(), 0)

    def test_is_false_for_unknown(self):
        self.assertFalse(self.db.radio_fav_is("nope"))
        self.assertFalse(self.db.radio_fav_is(""))

    def test_add_then_is_true(self):
        self.assertEqual(self.db.radio_fav_add(_station()), "ok")
        self.assertTrue(self.db.radio_fav_is("u1"))
        self.assertEqual(self.db.radio_fav_count(), 1)

    def test_add_is_idempotent(self):
        self.assertEqual(self.db.radio_fav_add(_station()), "ok")
        # Second add of the same UUID is a no-op, not an error.
        self.assertEqual(self.db.radio_fav_add(_station()), "exists")
        self.assertEqual(self.db.radio_fav_count(), 1)

    def test_add_rejects_incomplete(self):
        for bad in (
            {"station_uuid": "", "name": "X", "stream_url": "u"},
            {"station_uuid": "x", "name": "",  "stream_url": "u"},
            {"station_uuid": "x", "name": "X", "stream_url": ""},
            {},
        ):
            self.assertEqual(self.db.radio_fav_add(bad), "bad")
        self.assertEqual(self.db.radio_fav_count(), 0)

    def test_cap_enforced_at_25(self):
        for i in range(self.db.RADIO_FAV_MAX):
            self.assertEqual(
                self.db.radio_fav_add(_station(uuid=f"u{i}",
                                                name=f"S{i}")), "ok")
        self.assertEqual(self.db.radio_fav_count(), 25)
        # The 26th distinct station is rejected.
        self.assertEqual(
            self.db.radio_fav_add(_station(uuid="overflow")), "full")
        self.assertEqual(self.db.radio_fav_count(), 25)

    def test_readd_existing_allowed_when_full(self):
        # Re-adding an already-favourited station must NOT be blocked by
        # the cap — it's idempotent and creates no new row.
        for i in range(self.db.RADIO_FAV_MAX):
            self.db.radio_fav_add(_station(uuid=f"u{i}"))
        self.assertEqual(self.db.radio_fav_add(_station(uuid="u0")),
                         "exists")
        self.assertEqual(self.db.radio_fav_count(), 25)

    def test_remove(self):
        self.db.radio_fav_add(_station())
        self.assertTrue(self.db.radio_fav_remove("u1"))
        self.assertFalse(self.db.radio_fav_is("u1"))
        # Removing a non-favourite returns False, doesn't error.
        self.assertFalse(self.db.radio_fav_remove("u1"))

    def test_remove_frees_a_cap_slot(self):
        for i in range(self.db.RADIO_FAV_MAX):
            self.db.radio_fav_add(_station(uuid=f"u{i}"))
        self.assertEqual(self.db.radio_fav_add(_station(uuid="new")),
                         "full")
        self.db.radio_fav_remove("u0")
        self.assertEqual(self.db.radio_fav_add(_station(uuid="new")), "ok")

    def test_list_empty(self):
        self.assertEqual(self.db.radio_fav_list(), [])

    def test_list_returns_all_columns(self):
        self.db.radio_fav_add(_station())
        rows = self.db.radio_fav_list()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["station_uuid"], "u1")
        self.assertEqual(r["name"],         "BBC Radio 6")
        self.assertEqual(r["stream_url"],   "http://stream.example/6music")
        self.assertEqual(r["codec"],        "MP3")
        self.assertEqual(r["bitrate"],      128)
        self.assertEqual(r["country"],      "GB")
        self.assertGreater(r["added_at"],   0)

    def test_list_ordered_by_sort_order(self):
        for i in range(3):
            self.db.radio_fav_add(_station(uuid=f"u{i}", name=f"S{i}"))
        # Insertion order → sort_order 0,1,2.
        self.assertEqual([r["station_uuid"] for r in self.db.radio_fav_list()],
                         ["u0", "u1", "u2"])

    def test_reorder(self):
        for i in range(3):
            self.db.radio_fav_add(_station(uuid=f"u{i}"))
        self.assertTrue(self.db.radio_fav_reorder(["u2", "u0", "u1"]))
        self.assertEqual([r["station_uuid"] for r in self.db.radio_fav_list()],
                         ["u2", "u0", "u1"])

    def test_reorder_empty_is_noop(self):
        self.db.radio_fav_add(_station())
        self.assertFalse(self.db.radio_fav_reorder([]))

    def test_bitrate_garbage_coerced_to_zero(self):
        s = _station()
        s["bitrate"] = "not-a-number"
        self.assertEqual(self.db.radio_fav_add(s), "ok")
        self.assertEqual(self.db.radio_fav_list()[0]["bitrate"], 0)

    def test_survives_clear_udn(self):
        """Same invariant as album_favourites / album_art / lyrics —
        clear(udn) wipes tracks, never radio favourites (radio has no
        udn)."""
        self.db.radio_fav_add(_station())
        self.db.clear("uuid:srv1")
        self.assertTrue(self.db.radio_fav_is("u1"))


# ── Handlers ──────────────────────────────────────────────────────

class TestRadioHandlers(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        import api_radio
        self._patch = patch.object(api_radio, "DB", self.db)
        self._patch.start()
        self.api = api_radio

    def tearDown(self):
        self._patch.stop()
        self.db._pool.close()
        os.unlink(self.tmp.name)

    # favourites list
    def test_favourites_list_shape(self):
        h = _MockH()
        self.db.radio_fav_add(_station())
        self.api.favourites(h, {})
        self.assertEqual(h.last[0], 200)
        self.assertEqual(h.last[1]["limit"], 25)
        self.assertEqual(len(h.last[1]["stations"]), 1)

    # add
    def test_add_then_remove(self):
        h = _MockH()
        self.api.favourite_add(h, json.dumps(_station()).encode())
        self.assertEqual(h.last[0], 200)
        self.assertTrue(h.last[1]["created"])

        # Re-add → created=False (idempotent).
        self.api.favourite_add(h, json.dumps(_station()).encode())
        self.assertEqual(h.last[0], 200)
        self.assertFalse(h.last[1]["created"])

        # Remove
        self.api.favourite_remove(h, json.dumps({"station_uuid": "u1"}).encode())
        self.assertEqual(h.last, (200, {"ok": True}))

    def test_add_bad_json_returns_400(self):
        h = _MockH()
        self.api.favourite_add(h, b"not json")
        self.assertEqual(h.last[0], 400)

    def test_add_incomplete_returns_400(self):
        h = _MockH()
        self.api.favourite_add(h, json.dumps({"name": "X"}).encode())
        self.assertEqual(h.last[0], 400)

    def test_add_full_returns_409(self):
        h = _MockH()
        for i in range(self.db.RADIO_FAV_MAX):
            self.db.radio_fav_add(_station(uuid=f"u{i}"))
        self.api.favourite_add(h, json.dumps(_station(uuid="x")).encode())
        self.assertEqual(h.last[0], 409)
        self.assertEqual(h.last[1]["error"], "favourites_full")
        self.assertEqual(h.last[1]["limit"], 25)

    def test_remove_missing_uuid_returns_400(self):
        h = _MockH()
        self.api.favourite_remove(h, json.dumps({}).encode())
        self.assertEqual(h.last[0], 400)

    # reorder
    def test_reorder(self):
        h = _MockH()
        for i in range(3):
            self.db.radio_fav_add(_station(uuid=f"u{i}"))
        self.api.favourite_reorder(
            h, json.dumps({"order": ["u2", "u1", "u0"]}).encode())
        self.assertEqual(h.last, (200, {"ok": True}))
        self.assertEqual(
            [r["station_uuid"] for r in self.db.radio_fav_list()],
            ["u2", "u1", "u0"])

    def test_reorder_bad_body_returns_400(self):
        h = _MockH()
        self.api.favourite_reorder(h, json.dumps({"order": "nope"}).encode())
        self.assertEqual(h.last[0], 400)

    # search
    def test_search_no_params_returns_400(self):
        h = _MockH()
        self.api.search(h, {})
        self.assertEqual(h.last[0], 400)

    def test_search_filters_hls_and_normalizes(self):
        h = _MockH()
        fake = [
            {"stationuuid": "a", "name": "Plain MP3",
             "url_resolved": "http://x/mp3", "codec": "MP3",
             "bitrate": 128, "countrycode": "GB", "favicon": "f",
             "homepage": "h", "tags": "rock", "hls": 0},
            {"stationuuid": "b", "name": "An HLS stream",
             "url_resolved": "http://x/hls", "codec": "AAC", "hls": 1},
            {"stationuuid": "c", "name": "No URL at all", "hls": 0},
        ]
        with patch.object(self.api, "_radiobrowser_get", return_value=fake):
            self.api.search(h, {"q": "music"})
        self.assertEqual(h.last[0], 200)
        out = h.last[1]
        # HLS station 'b' and URL-less station 'c' are dropped.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["station_uuid"], "a")
        self.assertEqual(out[0]["stream_url"],   "http://x/mp3")
        self.assertEqual(out[0]["country"],      "GB")

    def test_search_directory_unreachable_returns_502(self):
        h = _MockH()
        with patch.object(self.api, "_radiobrowser_get", return_value=None):
            self.api.search(h, {"q": "music"})
        self.assertEqual(h.last[0], 502)


if __name__ == "__main__":
    unittest.main()
