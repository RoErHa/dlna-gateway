"""On-disk cover-art byte cache (dlna_art_cache) + the art_fetch_cached wrapper.

The cache amortises Subsonic getCoverArt / PWA /art fetches so a client library
sync (Amperfy pulls every cover) and gateway restarts don't re-hit
coverartarchive or re-decode embedded art each time.
"""
import io
import os
import tempfile
import time
import unittest
from unittest import mock

import dlna_art_cache as ac
import api_playback


class _CacheBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Point the module at a throwaway dir + tight knobs for this test.
        self._saved = (ac.CACHE_DIR, ac.TTL_SEC, ac.MAX_BYTES, ac._put_count)
        ac.CACHE_DIR = self._tmp.name
        ac.TTL_SEC = 14 * 24 * 3600
        ac.MAX_BYTES = 1024 * 1024 * 1024
        ac._put_count = 0

    def tearDown(self):
        ac.CACHE_DIR, ac.TTL_SEC, ac.MAX_BYTES, ac._put_count = self._saved
        self._tmp.cleanup()


class TestDiskCache(_CacheBase):
    def test_put_then_get_round_trip(self):
        ac.put("http://x/1", "image/png", b"\x89PNGdata")
        got = ac.get("http://x/1")
        self.assertEqual(got, ("image/png", b"\x89PNGdata"))

    def test_missing_is_none(self):
        self.assertIsNone(ac.get("http://x/never"))

    def test_empty_url_or_body_not_stored(self):
        ac.put("", "image/png", b"data")
        ac.put("http://x/empty", "image/png", b"")
        self.assertIsNone(ac.get(""))
        self.assertIsNone(ac.get("http://x/empty"))

    def test_distinct_urls_distinct_entries(self):
        ac.put("http://x/a", "image/jpeg", b"AAA")
        ac.put("http://x/b", "image/jpeg", b"BBB")
        self.assertEqual(ac.get("http://x/a"), ("image/jpeg", b"AAA"))
        self.assertEqual(ac.get("http://x/b"), ("image/jpeg", b"BBB"))

    def test_ttl_expiry_drops_entry(self):
        ac.put("http://x/old", "image/jpeg", b"OLD")
        # Backdate the file mtime beyond the TTL.
        p = ac._path("http://x/old")
        old = time.time() - (ac.TTL_SEC + 60)
        os.utime(p, (old, old))
        self.assertIsNone(ac.get("http://x/old"))
        self.assertFalse(os.path.exists(p), "stale entry should be removed")

    def test_ttl_zero_never_expires(self):
        ac.TTL_SEC = 0
        ac.put("http://x/forever", "image/jpeg", b"F")
        p = ac._path("http://x/forever")
        old = time.time() - (10 * 365 * 24 * 3600)
        os.utime(p, (old, old))
        self.assertEqual(ac.get("http://x/forever"), ("image/jpeg", b"F"))

    def test_corrupt_entry_is_miss(self):
        p = ac._path("http://x/corrupt")
        os.makedirs(ac.CACHE_DIR, exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"no-newline-no-body")        # missing the ctype\n header
        self.assertIsNone(ac.get("http://x/corrupt"))

    def test_ctype_newlines_stripped_on_store(self):
        ac.put("http://x/h", "image/jpeg\r\nInjected: bad", b"B")
        self.assertEqual(ac.get("http://x/h"), ("image/jpeg", b"B"))

    def test_eviction_when_over_cap(self):
        ac.MAX_BYTES = 300
        ac._EVICT_EVERY_saved = ac._EVICT_EVERY
        try:
            ac._EVICT_EVERY = 1                    # evict on every put
            ac.put("http://x/1", "image/jpeg", b"a" * 200)
            time.sleep(0.01)
            ac.put("http://x/2", "image/jpeg", b"b" * 200)  # now 400 > 300 → evict oldest
            self.assertIsNone(ac.get("http://x/1"), "oldest should be evicted")
            self.assertIsNotNone(ac.get("http://x/2"), "newest should survive")
        finally:
            ac._EVICT_EVERY = ac._EVICT_EVERY_saved

    def test_clear_removes_all(self):
        ac.put("http://x/1", "image/jpeg", b"1")
        ac.put("http://x/2", "image/jpeg", b"2")
        self.assertEqual(ac.clear(), 2)
        self.assertIsNone(ac.get("http://x/1"))

    def test_stats_shape(self):
        ac.put("http://x/1", "image/jpeg", b"12345")
        s = ac.stats()
        self.assertEqual(s["entries"], 1)
        self.assertGreaterEqual(s["bytes"], 5)
        self.assertIn("ttl_sec", s)


class TestArtFetchCached(_CacheBase):
    def test_hit_short_circuits_fetch(self):
        import api_playback
        ac.put("http://x/cov", "image/png", b"CACHED")
        with mock.patch.object(api_playback, "art_fetch") as m:
            code, ctype, body = api_playback.art_fetch_cached("http://x/cov")
        m.assert_not_called()                      # served from disk, no fetch
        self.assertEqual((code, ctype, body), (200, "image/png", b"CACHED"))

    def test_miss_fetches_and_caches(self):
        import api_playback
        with mock.patch.object(api_playback, "art_fetch",
                               return_value=(200, "image/jpeg", b"FRESH")) as m:
            r1 = api_playback.art_fetch_cached("http://x/new")
            r2 = api_playback.art_fetch_cached("http://x/new")
        self.assertEqual(r1, (200, "image/jpeg", b"FRESH"))
        self.assertEqual(r2, (200, "image/jpeg", b"FRESH"))
        m.assert_called_once()                     # second call hit the cache
        self.assertEqual(ac.get("http://x/new"), ("image/jpeg", b"FRESH"))

    def test_non_200_not_cached(self):
        import api_playback
        with mock.patch.object(api_playback, "art_fetch",
                               return_value=(502, "Upstream 404", b"")) as m:
            api_playback.art_fetch_cached("http://x/bad")
            api_playback.art_fetch_cached("http://x/bad")
        self.assertEqual(m.call_count, 2)          # nothing cached → re-fetched
        self.assertIsNone(ac.get("http://x/bad"))


class TestCacheVariants(_CacheBase):
    """Size-scaled copies live under a distinct key (the `variant`); the
    original (empty variant) keeps the pre-variant key so existing on-disk
    entries are untouched."""

    def test_empty_variant_matches_legacy_key(self):
        # A no-variant put must land on the same key the pre-variant code used
        # (bare sha1(url)) so covers already cached on disk aren't orphaned.
        self.assertEqual(ac._key("http://x/c"), ac._key("http://x/c", ""))

    def test_variant_is_distinct_entry(self):
        ac.put("http://x/c", "image/jpeg", b"ORIG")
        ac.put("http://x/c", "image/jpeg", b"SMALL", variant="s256")
        self.assertEqual(ac.get("http://x/c"), ("image/jpeg", b"ORIG"))
        self.assertEqual(ac.get("http://x/c", "s256"), ("image/jpeg", b"SMALL"))

    def test_variants_isolated_from_each_other(self):
        ac.put("http://x/c", "image/jpeg", b"A", variant="s96")
        ac.put("http://x/c", "image/jpeg", b"B", variant="s512")
        self.assertEqual(ac.get("http://x/c", "s96"),  ("image/jpeg", b"A"))
        self.assertEqual(ac.get("http://x/c", "s512"), ("image/jpeg", b"B"))
        self.assertIsNone(ac.get("http://x/c"))        # no original stored


class TestSizeBucket(unittest.TestCase):
    def test_ladder(self):
        b = api_playback._size_bucket
        self.assertEqual(b(0),    0)     # no size → original
        self.assertEqual(b(-5),   0)
        self.assertEqual(b(50),   96)
        self.assertEqual(b(96),   96)
        self.assertEqual(b(97),   256)
        self.assertEqual(b(256),  256)
        self.assertEqual(b(300),  512)
        self.assertEqual(b(1024), 1024)
        self.assertEqual(b(2000), 0)     # above top bucket → serve original


def _jpeg(w, h, colour=(20, 120, 200)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, format="JPEG", quality=95)
    return buf.getvalue()


@unittest.skipUnless(api_playback._PILImage is not None, "Pillow not installed")
class TestArtFetchScaled(_CacheBase):
    """getCoverArt?size=N path: downscale the cover to the size bucket, cache
    the scaled copy per bucket, and never re-hit the network for a size the
    original is already cached for."""

    def _dims(self, body):
        from PIL import Image
        return Image.open(io.BytesIO(body)).size

    def test_size_zero_is_original_passthrough(self):
        orig = _jpeg(1500, 1500)
        with mock.patch.object(api_playback, "art_fetch",
                               return_value=(200, "image/jpeg", orig)):
            code, ctype, body = api_playback.art_fetch_scaled("http://x/c", 0)
        self.assertEqual(code, 200)
        self.assertEqual(body, orig)                    # unmodified

    def test_scales_down_to_bucket(self):
        orig = _jpeg(1500, 1500)
        with mock.patch.object(api_playback, "art_fetch",
                               return_value=(200, "image/jpeg", orig)):
            code, ctype, body = api_playback.art_fetch_scaled("http://x/c", 200)
        self.assertEqual(code, 200)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(max(self._dims(body)), 256)    # 200 → bucket 256
        self.assertLess(len(body), len(orig))

    def test_second_request_served_from_variant_cache(self):
        orig = _jpeg(1500, 1500)
        with mock.patch.object(api_playback, "art_fetch",
                               return_value=(200, "image/jpeg", orig)) as m:
            b1 = api_playback.art_fetch_scaled("http://x/c", 200)[2]
            b2 = api_playback.art_fetch_scaled("http://x/c", 200)[2]
        self.assertEqual(b1, b2)
        m.assert_called_once()                          # variant cache hit

    def test_new_bucket_reuses_original_cache_no_refetch(self):
        orig = _jpeg(1500, 1500)
        with mock.patch.object(api_playback, "art_fetch",
                               return_value=(200, "image/jpeg", orig)) as m:
            api_playback.art_fetch_scaled("http://x/c", 200)   # bucket 256
            b512 = api_playback.art_fetch_scaled("http://x/c", 500)[2]  # bucket 512
        self.assertEqual(max(self._dims(b512)), 512)
        m.assert_called_once()      # original fetched once; 2nd bucket re-scaled it

    def test_already_small_image_passthrough(self):
        orig = _jpeg(120, 120)
        with mock.patch.object(api_playback, "art_fetch",
                               return_value=(200, "image/jpeg", orig)):
            body = api_playback.art_fetch_scaled("http://x/c", 256)[2]
        self.assertEqual(body, orig)    # already within box → original bytes

    def test_non_200_not_scaled_or_cached(self):
        with mock.patch.object(api_playback, "art_fetch",
                               return_value=(404, "not found", b"")) as m:
            code, _ct, body = api_playback.art_fetch_scaled("http://x/bad", 200)
        self.assertEqual(code, 404)
        self.assertEqual(body, b"")
        self.assertIsNone(ac.get("http://x/bad", "s256"))


if __name__ == "__main__":
    unittest.main()
