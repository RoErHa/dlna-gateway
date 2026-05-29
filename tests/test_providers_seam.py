#!/usr/bin/env python3
"""
tests/test_providers_seam.py — exercises the dlna_providers seam
(Phase 0 of the AssetUPnP migration).

Covers:
  1. Dataclass shape (Artist, Album, Track) — frozen, defaults.
  2. Class registry — register_provider, get_provider_class,
     list_provider_names, re-registration replacement.
  3. Instance registry — bind_provider, get_provider,
     unbind_provider, list_bound_udns; multiple-UDN isolation;
     bind_provider rejects empty UDN.
  4. Protocol conformance — MockProvider satisfies
     LibraryProvider via @runtime_checkable.
  5. MockProvider semantics — seeded data is returned, search +
     limit honoured, probe state, watch_changes fan-out.

Run standalone:
    python3 -m unittest tests.test_providers_seam -v
"""
import os
import sys
import unittest
from dataclasses import FrozenInstanceError

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_providers import (
    Album,
    Artist,
    LibraryProvider,
    Track,
    bind_provider,
    clear_bindings,
    get_provider,
    get_provider_class,
    list_bound_udns,
    list_provider_names,
    register_provider,
    unbind_provider,
)
from dlna_providers.mock import MockProvider


# ── Dataclasses ──────────────────────────────────────────────────

class TestDataclasses(unittest.TestCase):

    def test_artist_minimal(self):
        a = Artist(id="a1", name="Pink Floyd")
        self.assertEqual(a.id, "a1")
        self.assertEqual(a.name, "Pink Floyd")
        self.assertEqual(a.album_count, 0)

    def test_album_minimal(self):
        al = Album(id="al1", name="The Wall")
        self.assertEqual(al.year, None)
        self.assertEqual(al.artist_id, "")

    def test_track_minimal(self):
        t = Track(id="t1", title="Comfortably Numb")
        self.assertEqual(t.title, "Comfortably Numb")
        self.assertEqual(t.duration_sec, 0.0)
        self.assertIsNone(t.bit_depth)

    def test_artist_is_frozen(self):
        a = Artist(id="a1", name="X")
        with self.assertRaises(FrozenInstanceError):
            a.name = "Y"

    def test_album_is_frozen(self):
        al = Album(id="al1", name="X")
        with self.assertRaises(FrozenInstanceError):
            al.year = 2000

    def test_track_is_frozen(self):
        t = Track(id="t1", title="X")
        with self.assertRaises(FrozenInstanceError):
            t.title = "Y"


# ── Class registry ──────────────────────────────────────────────

class TestClassRegistry(unittest.TestCase):

    def test_mock_class_registered(self):
        # Import of dlna_providers.mock triggered @register_provider
        self.assertIn("mock", list_provider_names())

    def test_get_provider_class(self):
        self.assertIs(get_provider_class("mock"), MockProvider)

    def test_unknown_class_returns_none(self):
        self.assertIsNone(get_provider_class("does-not-exist"))

    def test_re_register_replaces_previous(self):
        @register_provider("test-temp")
        class FirstImpl:
            name = "test-temp"
            udn = ""
        self.assertIs(get_provider_class("test-temp"), FirstImpl)

        @register_provider("test-temp")
        class SecondImpl:
            name = "test-temp"
            udn = ""
        self.assertIs(get_provider_class("test-temp"), SecondImpl)

    def test_list_names_is_sorted(self):
        names = list_provider_names()
        self.assertEqual(names, sorted(names))


# ── Instance registry ──────────────────────────────────────────

class TestInstanceRegistry(unittest.TestCase):

    def tearDown(self):
        clear_bindings()

    def test_bind_get_round_trip(self):
        p = MockProvider(udn="uuid:test1")
        bind_provider("uuid:test1", p)
        self.assertIs(get_provider("uuid:test1"), p)

    def test_unbind_returns_previous_and_removes(self):
        p = MockProvider(udn="uuid:test1")
        bind_provider("uuid:test1", p)
        self.assertIs(unbind_provider("uuid:test1"), p)
        self.assertIsNone(get_provider("uuid:test1"))

    def test_unbind_missing_returns_none(self):
        self.assertIsNone(unbind_provider("uuid:nothing"))

    def test_get_unbound_returns_none(self):
        self.assertIsNone(get_provider("uuid:never-bound"))

    def test_multiple_udns_are_independent(self):
        a = MockProvider(udn="uuid:a")
        b = MockProvider(udn="uuid:b")
        bind_provider("uuid:a", a)
        bind_provider("uuid:b", b)
        self.assertIs(get_provider("uuid:a"), a)
        self.assertIs(get_provider("uuid:b"), b)
        self.assertEqual(sorted(list_bound_udns()), ["uuid:a", "uuid:b"])

    def test_bind_replaces_previous(self):
        a = MockProvider(udn="uuid:x")
        b = MockProvider(udn="uuid:x")
        bind_provider("uuid:x", a)
        bind_provider("uuid:x", b)
        self.assertIs(get_provider("uuid:x"), b)

    def test_empty_udn_rejected(self):
        with self.assertRaises(ValueError):
            bind_provider("", MockProvider(udn="uuid:x"))

    def test_clear_bindings_drops_all(self):
        bind_provider("uuid:a", MockProvider(udn="uuid:a"))
        bind_provider("uuid:b", MockProvider(udn="uuid:b"))
        clear_bindings()
        self.assertEqual(list_bound_udns(), [])
        # Class registrations are NOT cleared
        self.assertIn("mock", list_provider_names())


# ── Protocol conformance (structural typing) ───────────────────

class TestProtocolConformance(unittest.TestCase):

    def test_mock_isinstance_of_library_provider(self):
        p = MockProvider(udn="uuid:test")
        self.assertIsInstance(p, LibraryProvider)


# ── MockProvider semantics ─────────────────────────────────────

class TestMockProviderData(unittest.TestCase):

    def setUp(self):
        self.p = MockProvider(udn="uuid:test")

    def test_seeded_artists_listed(self):
        self.p.seed_artist(Artist(id="a1", name="Pink Floyd"))
        self.p.seed_artist(Artist(id="a2", name="The Beatles"))
        names = sorted(a.name for a in self.p.list_artists())
        self.assertEqual(names, ["Pink Floyd", "The Beatles"])

    def test_list_albums_filters_by_artist(self):
        self.p.seed_album(Album(id="al1", name="The Wall",   artist_id="a1"))
        self.p.seed_album(Album(id="al2", name="Abbey Road", artist_id="a2"))
        names = sorted(al.name for al in self.p.list_albums("a1"))
        self.assertEqual(names, ["The Wall"])

    def test_list_albums_for_unknown_artist_empty(self):
        self.assertEqual(list(self.p.list_albums("nope")), [])

    def test_list_tracks_filters_by_album(self):
        self.p.seed_track(Track(id="t1", title="Comfortably Numb",
                                album_id="al1", track_number=6))
        self.p.seed_track(Track(id="t2", title="Money",
                                album_id="al1", track_number=2))
        self.p.seed_track(Track(id="t3", title="Come Together",
                                album_id="al2", track_number=1))
        titles = [t.title for t in self.p.list_tracks("al1")]
        self.assertEqual(titles, ["Money", "Comfortably Numb"],
                         "Tracks must be ordered by track_number")

    def test_list_tracks_for_unknown_album_empty(self):
        self.assertEqual(list(self.p.list_tracks("nope")), [])

    def test_get_track_round_trip(self):
        t = Track(id="t1", title="Comfortably Numb")
        self.p.seed_track(t)
        self.assertEqual(self.p.get_track("t1"), t)

    def test_get_missing_track_returns_none(self):
        self.assertIsNone(self.p.get_track("nope"))

    def test_stream_url_format(self):
        self.p.seed_track(Track(id="t1", title="x"))
        self.assertEqual(self.p.stream_url("t1"),
                         "mock://uuid:test/track/t1")

    def test_stream_url_for_missing_track_is_empty(self):
        self.assertEqual(self.p.stream_url("nope"), "")

    def test_search_matches_title(self):
        self.p.seed_track(Track(id="t1", title="Comfortably Numb"))
        self.p.seed_track(Track(id="t2", title="Money"))
        hits = [t.id for t in self.p.search("comfortably")]
        self.assertEqual(hits, ["t1"])

    def test_search_matches_artist_name(self):
        self.p.seed_track(Track(id="t1", title="Whatever",
                                artist_name="Pink Floyd"))
        hits = [t.id for t in self.p.search("Floyd")]
        self.assertEqual(hits, ["t1"])

    def test_search_matches_album_name(self):
        self.p.seed_track(Track(id="t1", title="Whatever",
                                album_name="The Wall"))
        hits = [t.id for t in self.p.search("wall")]
        self.assertEqual(hits, ["t1"])

    def test_search_is_case_insensitive(self):
        self.p.seed_track(Track(id="t1", title="MONEY"))
        self.assertEqual([t.id for t in self.p.search("money")], ["t1"])

    def test_search_respects_limit(self):
        for i in range(5):
            self.p.seed_track(Track(id=f"t{i}", title=f"Song {i}"))
        hits = list(self.p.search("song", limit=3))
        self.assertEqual(len(hits), 3)

    def test_search_empty_query_returns_nothing(self):
        self.p.seed_track(Track(id="t1", title="x"))
        self.assertEqual(list(self.p.search("")), [])

    def test_probe_default_reachable(self):
        self.assertTrue(self.p.probe())

    def test_probe_can_be_set_unreachable(self):
        self.p.set_reachable(False)
        self.assertFalse(self.p.probe())

    def test_watch_changes_fires_callback(self):
        fired = []
        self.p.watch_changes(lambda: fired.append(1))
        self.p.fire_change()
        self.assertEqual(len(fired), 1)

    def test_watch_changes_supports_multiple_subscribers(self):
        fired_a = []
        fired_b = []
        self.p.watch_changes(lambda: fired_a.append(1))
        self.p.watch_changes(lambda: fired_b.append(1))
        self.p.fire_change()
        self.assertEqual(len(fired_a), 1)
        self.assertEqual(len(fired_b), 1)


if __name__ == "__main__":
    unittest.main()
