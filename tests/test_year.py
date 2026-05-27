#!/usr/bin/env python3
"""
tests/test_year.py — tests for the year feature (Phase 1 of 2026-05-26).

Three concerns:
  1. DIDL-Lite parser extracts year from <dc:date> /
     <upnp:originalTrackDate> into items dict.
  2. LibraryDB schema has tracks.year + metadata_overrides.year;
     upsert_tracks stores year; metadata_override_set stores
     override year separately from track year.
  3. _renderNpYear display logic (replicated in Python for testing):
     prefer override year, append "(remastered)" when file year - override
     year >= 3.

Run standalone:
    python3 -m unittest tests.test_year -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
from dlna_content import _parse_didl


# Minimal DIDL-Lite response shell with one audio item.
def _didl(date_xml: str) -> str:
    return f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
<s:Body><u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">
<Result>&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
xmlns:dc="http://purl.org/dc/elements/1.1/"
xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;
&lt;item id="t1" parentID="c1"&gt;
&lt;dc:title&gt;Track A&lt;/dc:title&gt;
&lt;upnp:artist&gt;Artist X&lt;/upnp:artist&gt;
&lt;upnp:album&gt;Album Y&lt;/upnp:album&gt;
{date_xml}
&lt;upnp:class&gt;object.item.audioItem.musicTrack&lt;/upnp:class&gt;
&lt;res protocolInfo="http-get:*:audio/flac:*" duration="0:03:21"&gt;http://x/y.flac&lt;/res&gt;
&lt;/item&gt;&lt;/DIDL-Lite&gt;</Result>
<NumberReturned>1</NumberReturned><TotalMatches>1</TotalMatches>
</u:BrowseResponse></s:Body></s:Envelope>"""


class TestDidlYearParsing(unittest.TestCase):

    def _year(self, date_xml: str):
        items = _parse_didl(_didl(date_xml))["items"]
        return items[0].get("year") if items else None

    def test_dc_date_full_iso(self):
        self.assertEqual(self._year("&lt;dc:date&gt;1987-08-31&lt;/dc:date&gt;"),
                         1987)

    def test_dc_date_year_only(self):
        self.assertEqual(self._year("&lt;dc:date&gt;1973&lt;/dc:date&gt;"), 1973)

    def test_dc_date_year_and_month(self):
        self.assertEqual(self._year("&lt;dc:date&gt;2001-09&lt;/dc:date&gt;"),
                         2001)

    def test_upnp_originaltrackdate_fallback(self):
        self.assertEqual(self._year(
            "&lt;upnp:originalTrackDate&gt;1969-07-04&lt;/upnp:originalTrackDate&gt;"),
            1969)

    def test_missing_date(self):
        self.assertIsNone(self._year(""))

    def test_garbage_date_returns_none(self):
        for bad in ("&lt;dc:date&gt;not-a-year&lt;/dc:date&gt;",
                    "&lt;dc:date&gt;&lt;/dc:date&gt;",
                    "&lt;dc:date&gt;abcd&lt;/dc:date&gt;"):
            with self.subTest(bad=bad):
                self.assertIsNone(self._year(bad))

    def test_out_of_range_year_rejected(self):
        # Defensive — 1899 / 2200 are implausible for music files.
        self.assertIsNone(self._year("&lt;dc:date&gt;1899-01-01&lt;/dc:date&gt;"))
        self.assertIsNone(self._year("&lt;dc:date&gt;2200-12-31&lt;/dc:date&gt;"))


class TestSchemaYearColumns(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)

    def test_tracks_has_year_column(self):
        with self.db._pool.read() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
        self.assertIn("year", cols)

    def test_metadata_overrides_has_year_column(self):
        with self.db._pool.read() as conn:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(metadata_overrides)")}
        self.assertIn("year", cols)

    def test_upsert_tracks_stores_year(self):
        self.db.upsert_tracks("uuid:test", [
            {"id": "1", "url": "http://x/a.flac", "title": "T", "artist": "A",
             "album": "AL", "year": 1987}])
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT year FROM tracks WHERE url='http://x/a.flac'"
            ).fetchone()
        self.assertEqual(row["year"], 1987)

    def test_upsert_tracks_missing_year_stores_null(self):
        self.db.upsert_tracks("uuid:test", [
            {"id": "1", "url": "http://x/b.flac", "title": "T", "artist": "A",
             "album": "AL"}])
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT year FROM tracks WHERE url='http://x/b.flac'"
            ).fetchone()
        self.assertIsNone(row["year"])

    def test_metadata_override_set_stores_year(self):
        self.db.upsert_tracks("uuid:test", [
            {"id": "1", "url": "http://x/a.flac", "title": "T", "artist": "A",
             "album": "AL", "year": 2001}])
        self.db.metadata_override_set(
            "http://x/a.flac", source="acoustid",
            artist="A", album="AL", title="T", year=1987)
        with self.db._pool.read() as conn:
            ov = conn.execute(
                "SELECT year FROM metadata_overrides WHERE url='http://x/a.flac'"
            ).fetchone()
            tr = conn.execute(
                "SELECT year FROM tracks WHERE url='http://x/a.flac'"
            ).fetchone()
        # Override year is the original (1987), tracks.year is the
        # file-tag/edition year (2001) — they stay separate.
        self.assertEqual(ov["year"], 1987)
        self.assertEqual(tr["year"], 2001,
                         "tracks.year must NOT be overwritten by override year")

    def test_track_meta_by_url_returns_both_years(self):
        self.db.upsert_tracks("uuid:test", [
            {"id": "1", "url": "http://x/a.flac", "title": "T",
             "artist": "A", "album": "AL", "year": 2001}])
        self.db.metadata_override_set(
            "http://x/a.flac", source="acoustid",
            artist="A", album="AL", title="T", year=1987)
        meta = self.db.track_meta_by_url("http://x/a.flac")
        self.assertEqual(meta["year"], 2001)
        self.assertEqual(meta["year_original"], 1987)

    def test_track_meta_year_original_null_when_no_override(self):
        self.db.upsert_tracks("uuid:test", [
            {"id": "1", "url": "http://x/a.flac", "title": "T",
             "artist": "A", "album": "AL", "year": 1973}])
        meta = self.db.track_meta_by_url("http://x/a.flac")
        self.assertEqual(meta["year"], 1973)
        self.assertIsNone(meta["year_original"])


# ── Display-logic mirror (Python copy of _renderNpYear) ───────────

def _np_year_display(year_file, year_original):
    """Mirrors _renderNpYear in static/app.js — testing in Python.
    Uses MIN(file, mb) when both present; annotates '(remastered)'
    when file is 3+ years later than the earlier year."""
    if year_original and year_file:
        earlier = min(year_original, year_file)
        s = str(earlier)
        if year_file - earlier >= 3:
            s += " (remastered)"
        return s
    if year_original:
        return str(year_original)
    if year_file:
        return str(year_file)
    return ""


class TestDisplayLogic(unittest.TestCase):

    def test_original_only_simple(self):
        self.assertEqual(_np_year_display(None, 1987), "1987")

    def test_both_match_no_annotation(self):
        # Same year — no remaster annotation.
        self.assertEqual(_np_year_display(1987, 1987), "1987")

    def test_two_year_gap_no_annotation(self):
        # 1987 vs 1989 — only 2 years, below threshold.
        self.assertEqual(_np_year_display(1989, 1987), "1987")

    def test_three_year_gap_annotates(self):
        self.assertEqual(_np_year_display(1990, 1987), "1987 (remastered)")

    def test_big_gap_annotates(self):
        self.assertEqual(_np_year_display(2001, 1987), "1987 (remastered)")

    def test_file_year_only_no_annotation(self):
        # File year known but no original — show file year alone.
        self.assertEqual(_np_year_display(2015, None), "2015")

    def test_both_none(self):
        self.assertEqual(_np_year_display(None, None), "")

    def test_file_earlier_than_orig_uses_file(self):
        # File=1985, orig=1987 — under the MIN rule we trust the
        # earlier (file) year and don't annotate.
        self.assertEqual(_np_year_display(1985, 1987), "1985")

    def test_acoustid_matched_later_edition_uses_file(self):
        # File=1972, AcoustID matched a 2021 anniversary recording.
        # MIN rule → display 1972, no remaster annotation (the file
        # year IS the earlier year; nothing newer than itself).
        # Regression for the Demons-and-Wizards case.
        self.assertEqual(_np_year_display(1972, 2021), "1972")


if __name__ == "__main__":
    unittest.main()
