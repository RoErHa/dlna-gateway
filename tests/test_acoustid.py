#!/usr/bin/env python3
"""
tests/test_acoustid.py — AcoustIDFetcher unit tests against a temp SQLite.

Focus: the data invariants — what bare_metadata_tracks returns, how
fpcalc / AcoustID responses parse, sticky-negative blocking on
'notfound', confidence-threshold gating, clear(udn) survival, and the
trigger / stop / fpcalc-missing guardrails. All subprocess and HTTP
calls are mocked so the suite stays under a second.

Run standalone:
    python3 -m unittest tests.test_acoustid -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
from dlna_acoustid import (AcoustIDFetcher, ACOUSTID_CONFIDENCE_THRESHOLD,
                           _parse_fpcalc_output, _extract_best_match,
                           _reconstruct_artist)


# ── Pure-function parser tests ────────────────────────────────────

class TestFpcalcParser(unittest.TestCase):

    def test_real_fpcalc_json(self):
        out = '{"duration": 240.0, "fingerprint": "AQADtEqYREkSJUmS"}'
        fp, dur = _parse_fpcalc_output(out)
        self.assertEqual(fp, "AQADtEqYREkSJUmS")
        self.assertEqual(dur, 240)

    def test_float_duration_rounds(self):
        out = '{"duration": 239.6, "fingerprint": "ABC"}'
        fp, dur = _parse_fpcalc_output(out)
        self.assertEqual(dur, 240)

    def test_integer_duration_passes_through(self):
        out = '{"duration": 180, "fingerprint": "ABC"}'
        self.assertEqual(_parse_fpcalc_output(out)[1], 180)

    def test_empty_output_returns_none(self):
        self.assertEqual(_parse_fpcalc_output(""), (None, None))

    def test_garbled_json_returns_none(self):
        for bad in ("not json", "{incomplete", "{}", '{"duration": 240}',
                    '{"fingerprint": ""}', '{"fingerprint": "x", "duration": 0}',
                    '{"fingerprint": "x", "duration": "bad"}'):
            with self.subTest(bad=bad):
                self.assertEqual(_parse_fpcalc_output(bad), (None, None))


class TestArtistReconstruction(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(_reconstruct_artist([]), "")

    def test_single_artist(self):
        self.assertEqual(
            _reconstruct_artist([{"name": "Pink Floyd"}]), "Pink Floyd")

    def test_two_with_joinphrase(self):
        a = [{"name": "Eric Clapton", "joinphrase": " feat. "},
             {"name": "B.B. King"}]
        self.assertEqual(_reconstruct_artist(a), "Eric Clapton feat. B.B. King")

    def test_two_without_joinphrase_falls_back_to_ampersand(self):
        a = [{"name": "Simon"}, {"name": "Garfunkel"}]
        self.assertEqual(_reconstruct_artist(a), "Simon & Garfunkel")

    def test_blank_names_skipped(self):
        a = [{"name": "  "}, {"name": "Real Artist"}]
        self.assertEqual(_reconstruct_artist(a), "Real Artist")


class TestExtractBestMatch(unittest.TestCase):

    def _resp(self, score, title="Money", artist="Pink Floyd",
             album="The Dark Side of the Moon", album_type="Album"):
        return {
            "status": "ok",
            "results": [{
                "id": "fake-id",
                "score": score,
                "recordings": [{
                    "id": "rec-id",
                    "title": title,
                    "artists": [{"name": artist}],
                    "releasegroups": [
                        {"id": "rg-id", "title": album, "type": album_type}
                    ],
                }],
            }],
        }

    def test_high_score_hit_extracts_fields(self):
        m = _extract_best_match(self._resp(0.95))
        self.assertEqual(m["title"], "Money")
        self.assertEqual(m["artist"], "Pink Floyd")
        self.assertEqual(m["album"], "The Dark Side of the Moon")
        self.assertAlmostEqual(m["score"], 0.95)

    def test_below_threshold_returns_none(self):
        # 0.80 < 0.85 default → reject
        self.assertIsNone(_extract_best_match(self._resp(0.80)))

    def test_at_threshold_accepts(self):
        # >= threshold is a hit
        self.assertIsNotNone(_extract_best_match(self._resp(
            ACOUSTID_CONFIDENCE_THRESHOLD)))

    def test_below_custom_threshold(self):
        # An explicit higher threshold rejects a high-but-not-perfect score
        self.assertIsNone(_extract_best_match(self._resp(0.90),
                                              threshold=0.95))

    def test_empty_results_returns_none(self):
        self.assertIsNone(_extract_best_match(
            {"status": "ok", "results": []}))

    def test_failed_status_returns_none(self):
        self.assertIsNone(_extract_best_match(
            {"status": "error", "results": [{"score": 0.99}]}))

    def test_garbage_returns_none(self):
        for bad in (None, "string", 42, [], {}, {"status": "ok"}):
            with self.subTest(bad=bad):
                self.assertIsNone(_extract_best_match(bad))

    def test_recording_without_metadata_returns_none(self):
        # AcoustID returned a fingerprint match but no recording info attached.
        bare = {
            "status": "ok",
            "results": [{
                "score": 0.99,
                "recordings": [{"id": "x"}],  # no title, no artists, no rgs
            }],
        }
        self.assertIsNone(_extract_best_match(bare))

    def test_picks_album_over_single_releasegroup(self):
        resp = {
            "status": "ok",
            "results": [{
                "score": 0.99,
                "recordings": [
                    {"title": "Money",
                     "artists": [{"name": "Pink Floyd"}],
                     "releasegroups": [{"title": "Hits", "type": "Single"}]},
                    {"title": "Money",
                     "artists": [{"name": "Pink Floyd"}],
                     "releasegroups": [
                         {"title": "The Dark Side of the Moon",
                          "type": "Album"}]},
                ],
            }],
        }
        m = _extract_best_match(resp)
        self.assertEqual(m["album"], "The Dark Side of the Moon")

    def test_takes_highest_score_across_results(self):
        resp = {
            "status": "ok",
            "results": [
                {"score": 0.86, "recordings": [
                    {"title": "Wrong", "artists": [{"name": "Wrong A"}],
                     "releasegroups": [{"title": "Wrong Album",
                                        "type": "Album"}]}]},
                {"score": 0.99, "recordings": [
                    {"title": "Right", "artists": [{"name": "Right A"}],
                     "releasegroups": [{"title": "Right Album",
                                        "type": "Album"}]}]},
            ],
        }
        m = _extract_best_match(resp)
        self.assertEqual(m["title"], "Right")


# ── DB-level worker tests ─────────────────────────────────────────

class TestAcoustIDFetcher(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db  = LibraryDB(db_file=self._path)
        self.udn = "uuid:test"
        self.fetcher = AcoustIDFetcher(self.db, api_key="test-key")
        with self.db._pool.write() as conn:
            for i in range(5):
                conn.execute(
                    "INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
                    "VALUES (?,?,?,?,?,?)",
                    (self.udn, f"obj{i}", f"http://t/{i}", f"T{i}",
                     "A", "Album"))

    def tearDown(self):
        try:
            self.fetcher.stop()
        finally:
            os.unlink(self._path)

    # ── bare_metadata_tracks invariants ──

    def test_bare_returns_all_unprocessed(self):
        bare = self.fetcher.bare_tracks()
        self.assertEqual(len(bare), 5)
        for b in bare:
            self.assertEqual(len(b), 1)
            self.assertTrue(b[0].startswith("http://t/"))

    def test_bare_excludes_acoustid_overrides(self):
        # Worker has already enriched two tracks
        self.db.metadata_override_set(
            "http://t/0", source="acoustid",
            artist="X", album="Y", title="Z")
        self.db.metadata_override_set(
            "http://t/1", source="acoustid", title="W")
        urls = [b[0] for b in self.fetcher.bare_tracks()]
        self.assertNotIn("http://t/0", urls)
        self.assertNotIn("http://t/1", urls)
        self.assertEqual(len(urls), 3)

    def test_bare_excludes_manual_edits(self):
        # User-edited tracks must NOT be re-fingerprinted (the worker
        # would overwrite a deliberate user choice)
        with self.db._pool.write() as conn:
            conn.execute("INSERT INTO metadata_overrides "
                         "(url, artist, album, title, genre, source) "
                         "VALUES (?,?,?,?,?,'manual')",
                         ("http://t/0", "U", "U", "U", ""))
        urls = [b[0] for b in self.fetcher.bare_tracks()]
        self.assertNotIn("http://t/0", urls)

    def test_bare_excludes_sticky_notfound(self):
        # Same convention as album_art.source='notfound' — sticky.
        self.db.metadata_override_mark_notfound("http://t/0")
        urls = [b[0] for b in self.fetcher.bare_tracks()]
        self.assertNotIn("http://t/0", urls)

    def test_bare_excludes_empty_urls(self):
        with self.db._pool.write() as conn:
            conn.execute(
                "INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
                "VALUES (?,?,?,?,?,?)",
                ("uuid:test", "objE", "", "Empty", "A", "Album"))
        # File-path-only entries can't be fingerprinted via HTTP, so the
        # worker must skip them rather than handing fpcalc an empty URL.
        urls = [b[0] for b in self.fetcher.bare_tracks()]
        self.assertNotIn("", urls)

    def test_clear_does_not_delete_overrides(self):
        # Metadata enrichment survives clear(udn) — same invariant as
        # album_art / play_counts / lyrics. Re-indexing must not cost
        # the user the AcoustID work.
        self.db.metadata_override_set(
            "http://t/0", source="acoustid", artist="X", title="Y")
        self.db.metadata_override_mark_notfound("http://t/1")
        self.db.clear(self.udn)
        with self.db._pool.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM metadata_overrides").fetchone()["n"]
        self.assertEqual(n, 2,
                         "metadata_overrides rows must survive clear(udn)")

    # ── run_once writes correct rows ──

    def test_run_once_writes_acoustid_on_hit(self):
        # Every fingerprint succeeds; every lookup returns a confident match.
        def fake_fp(url):
            return ("FP_" + url[-1], 200)

        def fake_lookup(fp, dur):
            return {"artist": "Pink Floyd", "album": "DSOTM",
                    "title": f"Track-{fp[-1]}", "score": 0.95}

        with patch.object(AcoustIDFetcher, "_fingerprint",
                          side_effect=fake_fp), \
             patch.object(AcoustIDFetcher, "_lookup", side_effect=fake_lookup), \
             patch("dlna_acoustid._AC_RATE_LIMIT_SEC", 0.0):
            stats = self.fetcher.run_once()

        self.assertEqual(stats["hits"], 5)
        self.assertEqual(stats["notfound"], 0)
        with self.db._pool.read() as conn:
            rows = {r["url"]: r for r in
                    conn.execute(
                        "SELECT * FROM metadata_overrides").fetchall()}
        self.assertEqual(len(rows), 5)
        for r in rows.values():
            self.assertEqual(r["source"], "acoustid")
            self.assertEqual(r["artist"], "Pink Floyd")
            self.assertEqual(r["album"], "DSOTM")
        # And `tracks` was updated live (the COALESCE-on-reindex pass
        # would do this eventually, but the worker pushes it now).
        with self.db._pool.read() as conn:
            t = conn.execute(
                "SELECT artist, album, title FROM tracks WHERE url=?",
                ("http://t/0",)).fetchone()
        self.assertEqual(t["artist"], "Pink Floyd")
        self.assertEqual(t["album"], "DSOTM")
        self.assertEqual(t["title"], "Track-0")

    def test_run_once_writes_notfound_on_miss(self):
        # Lookup returns None (no confident match)
        with patch.object(AcoustIDFetcher, "_fingerprint",
                          return_value=("FP", 200)), \
             patch.object(AcoustIDFetcher, "_lookup", return_value=None), \
             patch("dlna_acoustid._AC_RATE_LIMIT_SEC", 0.0):
            stats = self.fetcher.run_once()

        self.assertEqual(stats["hits"], 0)
        self.assertEqual(stats["notfound"], 5)
        with self.db._pool.read() as conn:
            rows = conn.execute(
                "SELECT source FROM metadata_overrides").fetchall()
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["source"] == "notfound" for r in rows))

    def test_run_once_records_fingerprint_failure_as_notfound(self):
        # Fingerprint failure (e.g. AssetUPnP 404, corrupt source) →
        # sticky negative so we don't retry forever.
        with patch.object(AcoustIDFetcher, "_fingerprint",
                          return_value=(None, None)), \
             patch("dlna_acoustid._AC_RATE_LIMIT_SEC", 0.0):
            stats = self.fetcher.run_once()
        self.assertEqual(stats["errors"], 5)
        with self.db._pool.read() as conn:
            n = conn.execute("SELECT COUNT(*) FROM metadata_overrides "
                             "WHERE source='notfound'").fetchone()[0]
        self.assertEqual(n, 5)

    def test_run_once_no_api_key_is_noop(self):
        # Unset API key → run is a no-op with a single WARN; the worker
        # must not iterate, must not write anything.
        no_key = AcoustIDFetcher(self.db, api_key=None)
        with patch.object(AcoustIDFetcher, "_fingerprint") as mock_fp:
            stats = no_key.run_once()
        mock_fp.assert_not_called()
        self.assertEqual(stats, {"total": 0, "hits": 0, "notfound": 0, "errors": 0})

    def test_run_once_fpcalc_missing_does_not_poison_cache(self):
        # Regression guard mirrored from LoudnessScanner: when fpcalc is
        # missing, run_once must bail BEFORE iterating — otherwise every
        # track gets sticky-cached as notfound and the scan permanently
        # can't recover after Chromaprint is installed.
        import dlna_acoustid
        with patch.object(dlna_acoustid, "_FPCALC_PATH", None):
            stats = self.fetcher.run_once()
        self.assertEqual(stats["total"], 0,
                         "fetcher must not iterate when fpcalc is missing")
        with self.db._pool.read() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM metadata_overrides").fetchone()[0]
        self.assertEqual(n, 0,
                         "no rows should be written when fpcalc is missing")

    def test_run_once_fpcalc_disappears_midrun_does_not_poison(self):
        # Mirror the LoudnessScanner regression: if fpcalc vanishes mid-run
        # (Homebrew updating the symlink), the worker must bail WITHOUT
        # caching the in-flight track as a sticky negative.
        call_count = {"n": 0}

        def flaky_fp(url):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return ("FP", 200)
            raise FileNotFoundError("[Errno 2] No such file: 'fpcalc'")

        with patch.object(AcoustIDFetcher, "_fingerprint",
                          side_effect=flaky_fp), \
             patch.object(AcoustIDFetcher, "_lookup",
                          return_value={"artist": "A", "album": "B",
                                        "title": "T", "score": 0.99}), \
             patch("dlna_acoustid._AC_RATE_LIMIT_SEC", 0.0):
            self.fetcher.run_once()

        with self.db._pool.read() as conn:
            ok = conn.execute(
                "SELECT COUNT(*) FROM metadata_overrides "
                "WHERE source='acoustid'").fetchone()[0]
            notfound = conn.execute(
                "SELECT COUNT(*) FROM metadata_overrides "
                "WHERE source='notfound'").fetchone()[0]
        self.assertEqual(ok, 2, "two pre-failure tracks should be cached")
        self.assertEqual(notfound, 0,
                         "fpcalc-vanish must NOT poison the cache — next "
                         "trigger will re-try the remaining tracks")

    def test_trigger_idempotent_while_running(self):
        # Block one run; verify a second trigger() is a no-op.
        block = [True]

        def slow_fp(url):
            while block[0]:
                time.sleep(0.01)
            return (None, None)

        with patch.object(AcoustIDFetcher, "_fingerprint",
                          side_effect=slow_fp):
            self.fetcher.trigger()
            time.sleep(0.1)
            first = self.fetcher._thread
            self.fetcher.trigger()  # should be no-op
            self.assertIs(self.fetcher._thread, first,
                          "second trigger() must not spawn a second thread")
            block[0] = False
            self.fetcher.stop()

    def test_stop_halts_between_batches(self):
        # Stop() set before run_once starts → no iteration happens.
        self.fetcher.stop()
        with patch.object(AcoustIDFetcher, "_fingerprint") as mock_fp:
            self.fetcher._stop.set()
            self.fetcher.run_once()
        mock_fp.assert_not_called()

    def test_start_initial_scan_skipped_when_disabled(self):
        no_key = AcoustIDFetcher(self.db, api_key=None)
        no_key.start_initial_scan(delay=0.0)
        # No thread should have been started
        self.assertIsNone(no_key._thread)

    def test_status_shape(self):
        s = self.fetcher.status()
        for k in ("enabled", "fpcalc", "in_progress", "processed",
                  "threshold", "last_match", "last_url"):
            self.assertIn(k, s)
        self.assertTrue(s["enabled"])
        self.assertFalse(s["in_progress"])
        self.assertEqual(s["processed"], 0)
        self.assertEqual(s["threshold"], ACOUSTID_CONFIDENCE_THRESHOLD)

    def test_status_disabled_when_no_key(self):
        no_key = AcoustIDFetcher(self.db, api_key=None)
        self.assertFalse(no_key.status()["enabled"])

    def test_partial_match_only_overwrites_provided_fields(self):
        # AcoustID returned a title but no album. The album column on
        # `tracks` should be untouched (not blanked).
        with patch.object(AcoustIDFetcher, "_fingerprint",
                          return_value=("FP", 200)), \
             patch.object(AcoustIDFetcher, "_lookup",
                          return_value={"artist": "", "album": "",
                                        "title": "Real Title", "score": 0.99}), \
             patch("dlna_acoustid._AC_RATE_LIMIT_SEC", 0.0):
            self.fetcher.run_once()
        with self.db._pool.read() as conn:
            t = conn.execute(
                "SELECT artist, album, title FROM tracks WHERE url=?",
                ("http://t/0",)).fetchone()
        self.assertEqual(t["title"], "Real Title")
        # Original artist/album from setUp remain
        self.assertEqual(t["artist"], "A")
        self.assertEqual(t["album"], "Album")


# ── LibraryDB-level method tests ──────────────────────────────────

class TestLibraryDBMetadataMethods(unittest.TestCase):

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        with self.db._pool.write() as conn:
            conn.execute(
                "INSERT INTO tracks (udn, obj_id, url, title, artist, album) "
                "VALUES (?,?,?,?,?,?)",
                ("uuid:test", "obj0", "http://x", "Old Title",
                 "Old Artist", "Old Album"))

    def tearDown(self):
        os.unlink(self._path)

    def test_metadata_override_set_creates_row_and_updates_tracks(self):
        self.db.metadata_override_set(
            "http://x", source="acoustid",
            artist="New Artist", title="New Title")
        with self.db._pool.read() as conn:
            ov = conn.execute(
                "SELECT artist, title, album, source "
                "FROM metadata_overrides WHERE url=?",
                ("http://x",)).fetchone()
            tr = conn.execute(
                "SELECT artist, title, album FROM tracks WHERE url=?",
                ("http://x",)).fetchone()
        self.assertEqual(ov["artist"], "New Artist")
        self.assertEqual(ov["title"], "New Title")
        # Album was not supplied; it should fall through from `tracks`
        self.assertEqual(ov["album"], "Old Album")
        self.assertEqual(ov["source"], "acoustid")
        self.assertEqual(tr["artist"], "New Artist")
        self.assertEqual(tr["title"], "New Title")
        self.assertEqual(tr["album"], "Old Album")  # untouched

    def test_metadata_override_set_merges_with_existing_row(self):
        # First write title only
        self.db.metadata_override_set(
            "http://x", source="acoustid", title="T1")
        # Then write album only — title from the first write must persist
        self.db.metadata_override_set(
            "http://x", source="acoustid", album="A1")
        with self.db._pool.read() as conn:
            ov = conn.execute(
                "SELECT title, album FROM metadata_overrides WHERE url=?",
                ("http://x",)).fetchone()
        self.assertEqual(ov["title"], "T1")
        self.assertEqual(ov["album"], "A1")

    def test_mark_notfound_is_sticky(self):
        first = self.db.metadata_override_mark_notfound("http://x")
        self.assertTrue(first)
        # Second call → INSERT OR IGNORE no-ops; rowcount=0 → returns False
        second = self.db.metadata_override_mark_notfound("http://x")
        self.assertFalse(second)
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT artist, album, title, source "
                "FROM metadata_overrides WHERE url=?",
                ("http://x",)).fetchone()
        self.assertIsNone(row["artist"])
        self.assertIsNone(row["album"])
        self.assertIsNone(row["title"])
        self.assertEqual(row["source"], "notfound")

    def test_mark_notfound_does_not_overwrite_real_override(self):
        # A user-set 'manual' row must never be overwritten by a worker
        # 'notfound' sentinel (would silently throw away the user's edit).
        self.db.metadata_override_set(
            "http://x", source="manual", artist="User Artist")
        self.db.metadata_override_mark_notfound("http://x")
        with self.db._pool.read() as conn:
            row = conn.execute(
                "SELECT artist, source FROM metadata_overrides WHERE url=?",
                ("http://x",)).fetchone()
        self.assertEqual(row["artist"], "User Artist")
        self.assertEqual(row["source"], "manual")

    def test_empty_url_rejected(self):
        self.assertFalse(self.db.metadata_override_set(
            "", source="acoustid", title="X"))
        self.assertFalse(self.db.metadata_override_mark_notfound(""))


if __name__ == "__main__":
    unittest.main()
