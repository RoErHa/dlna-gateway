#!/usr/bin/env python3
"""
tests/test_lyrics.py — Lyrics cache + lrclib handler tests.

Covers the data contract of the on-demand lyrics feature:
- lyrics table survives clear(udn) (same invariant as album_art)
- get_lyrics / set_lyrics round-trip
- handler short-circuits to cache when row exists
- handler caches sticky 'notfound' on LrclibNotFound
- handler does NOT cache on transient network failure
- handler returns 400 on missing url, 404 on track not in library

Network is fully mocked; suite runs in well under a second.

Run standalone:
    python3 -m unittest tests.test_lyrics -v
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
import dlna_lyrics


class _MockH:
    """Captures the (code, payload) of the last _json call so tests can
    assert on it without spinning up an HTTP server."""
    def __init__(self):
        self.last = None
    def _json(self, code, payload):
        self.last = (code, payload)


def _seed_track(db, url="http://srv/x.flac", title="Wish You Were Here",
                artist="Pink Floyd", album="Wish You Were Here",
                duration="0:05:34.000"):
    with db._pool.write() as c:
        c.execute(
            "INSERT INTO tracks(udn, obj_id, url, title, artist, album, "
            "duration, art, mime, genre, file_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("uuid:test", "1", url, title, artist, album, duration,
             "", "audio/flac", "", ""))
    return url


# ── DB layer ──────────────────────────────────────────────────────

class TestLyricsDB(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_get_returns_none_for_unknown_url(self):
        self.assertIsNone(self.db.get_lyrics("http://nope"))

    def test_set_then_get_roundtrip(self):
        self.db.set_lyrics("u1", "hello\nworld", None, "lrclib")
        row = self.db.get_lyrics("u1")
        self.assertEqual(row["plain"],  "hello\nworld")
        self.assertIsNone(row["synced"])
        self.assertEqual(row["source"], "lrclib")
        self.assertGreater(row["fetched_at"], 0)

    def test_set_overwrites_existing_row(self):
        self.db.set_lyrics("u1", None, None, "notfound")
        self.db.set_lyrics("u1", "now found", None, "lrclib")
        self.assertEqual(self.db.get_lyrics("u1")["source"], "lrclib")
        self.assertEqual(self.db.get_lyrics("u1")["plain"],  "now found")

    def test_lyrics_survive_clear_udn(self):
        # Same invariant as album_art / play_counts
        url = _seed_track(self.db, url="http://srv/a.flac")
        self.db.set_lyrics(url, "lyrics body", None, "lrclib")
        self.db.clear("uuid:test")
        # Track is gone, lyrics remain
        with self.db._pool.read() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
        self.assertEqual(n, 0)
        self.assertEqual(self.db.get_lyrics(url)["plain"], "lyrics body")


# ── Handler ───────────────────────────────────────────────────────

class TestLyricsHandler(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        self._patch = patch("api_playback_state.DB", self.db)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_missing_url_400(self):
        import api_playback
        h = _MockH()
        api_playback.lyrics(h, {})
        code, payload = h.last
        self.assertEqual(code, 400)
        self.assertIn("error", payload)

    def test_unknown_track_404(self):
        import api_playback
        h = _MockH()
        api_playback.lyrics(h, {"url": "http://nope"})
        code, payload = h.last
        self.assertEqual(code, 404)
        self.assertEqual(payload.get("source"), "notfound")

    def test_cache_hit_returns_immediately_no_network(self):
        import api_playback
        url = _seed_track(self.db)
        self.db.set_lyrics(url, "cached body", None, "lrclib")
        with patch("dlna_lyrics.fetch_lrclib") as mock_fetch:
            h = _MockH()
            api_playback.lyrics(h, {"url": url})
            mock_fetch.assert_not_called()
        code, payload = h.last
        self.assertEqual(code, 200)
        self.assertEqual(payload["plain"], "cached body")
        self.assertTrue(payload["cached"])
        self.assertEqual(payload["source"], "lrclib")

    def test_cache_miss_fetches_and_caches(self):
        import api_playback
        url = _seed_track(self.db)
        with patch("dlna_lyrics.fetch_lrclib",
                   return_value={"plain": "fetched body", "synced": None}) as mf:
            h = _MockH()
            api_playback.lyrics(h, {"url": url})
            self.assertEqual(mf.call_count, 1)
            # Second call must hit cache, NOT lrclib
            api_playback.lyrics(h, {"url": url})
            self.assertEqual(mf.call_count, 1)
        code, payload = h.last
        self.assertTrue(payload["cached"])
        self.assertEqual(payload["plain"], "fetched body")

    def test_lrclib_404_caches_notfound_sticky(self):
        import api_playback
        url = _seed_track(self.db)
        with patch("dlna_lyrics.fetch_lrclib",
                   side_effect=dlna_lyrics.LrclibNotFound()) as mf:
            h = _MockH()
            api_playback.lyrics(h, {"url": url})
            self.assertEqual(mf.call_count, 1)
            # Sticky: second tap must NOT re-call lrclib
            api_playback.lyrics(h, {"url": url})
            self.assertEqual(mf.call_count, 1)
        code, payload = h.last
        self.assertEqual(code, 200)
        self.assertEqual(payload["source"], "notfound")
        self.assertIsNone(payload["plain"])
        self.assertTrue(payload["cached"])

    def test_network_error_does_not_cache(self):
        # Transient errors must NOT pollute the cache — next tap retries.
        import api_playback
        url = _seed_track(self.db)
        with patch("dlna_lyrics.fetch_lrclib", return_value=None):
            h = _MockH()
            api_playback.lyrics(h, {"url": url})
        code, payload = h.last
        self.assertEqual(code, 502)
        self.assertIsNone(self.db.get_lyrics(url))


# ── lrclib parser ─────────────────────────────────────────────────

class TestLrclibFetch(unittest.TestCase):
    """We don't hit the real network; we mock urlopen so the parser
    surface and 404 handling are exercised deterministically."""

    def test_404_raises_notfound(self):
        import urllib.error
        e = urllib.error.HTTPError("u", 404, "x", {}, None)
        with patch("urllib.request.urlopen", side_effect=e):
            with self.assertRaises(dlna_lyrics.LrclibNotFound):
                dlna_lyrics.fetch_lrclib("t", "a")

    def test_other_http_error_returns_none(self):
        import urllib.error
        e = urllib.error.HTTPError("u", 503, "x", {}, None)
        with patch("urllib.request.urlopen", side_effect=e):
            self.assertIsNone(dlna_lyrics.fetch_lrclib("t", "a"))

    def test_network_error_returns_none(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionError("x")):
            self.assertIsNone(dlna_lyrics.fetch_lrclib("t", "a"))

    def test_empty_payload_treated_as_notfound(self):
        # lrclib returns 200 with both fields null when "match" exists
        # but lyrics aren't filled in yet
        import io, json
        class _FakeResp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): pass
        body = json.dumps({"plainLyrics": None, "syncedLyrics": None}).encode()
        with patch("urllib.request.urlopen", return_value=_FakeResp(body)):
            with self.assertRaises(dlna_lyrics.LrclibNotFound):
                dlna_lyrics.fetch_lrclib("t", "a")

    def test_missing_track_or_artist_returns_none(self):
        self.assertIsNone(dlna_lyrics.fetch_lrclib("", "a"))
        self.assertIsNone(dlna_lyrics.fetch_lrclib("t", ""))


if __name__ == "__main__":
    unittest.main()
