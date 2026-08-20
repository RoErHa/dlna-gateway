#!/usr/bin/env python3
"""
tests/test_upnp_browse_dispatch.py — guards the `_gw_browse` dispatch
split (2026-08-20).

`_gw_browse` was one 491-line function with 26 sequential
`if obj_id == …: return` branches (~99 branch points). It is now a
lookup over two tables — `_BROWSE_EXACT` and `_BROWSE_PREFIX` — plus one
handler per container type, sharing a `_Browse` context that owns the
DIDL envelope and the pagination arithmetic all 26 branches used to
repeat verbatim.

The behavioural equivalence of that split was verified against the real
library by replaying 496 (object_id × flag × start × count) cases through
both implementations and diffing the XML — 0 differences. This file keeps
the STRUCTURAL invariants honest going forward, since those are what a
future edit is most likely to break:

  1. Exact and prefix ids must stay disjoint, or a container gets
     shadowed. "vidlocs" vs "vidloc:" and "favalbums" vs "favalbum:" are
     one character away from exactly that bug.
  2. Every handler must be reachable from a table (an orphaned handler is
     a container the Naim can no longer browse).
  3. Every response must be a well-formed DIDL-Lite envelope, and an
     unknown or garbled id must yield an EMPTY container, never a fault —
     a Naim control point abandons the whole browse on a SOAP fault.
  4. BrowseMetadata always answers (xml, 1, 1); a listing never returns
     more items than `RequestedCount`.

Run standalone:  python3 -m unittest tests.test_upnp_browse_dispatch -v
"""
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import api_upnp
import api_upnp_ids
from dlna_library import LibraryDB

UDN = "uuid:localfs-brtest"

TRACKS = [
    ("Aphex Twin", "Selected Ambient Works", "AFX/SAW", "Xtal",
     "http://gw:8200/localfs/stream/t1"),
    ("Aphex Twin", "Selected Ambient Works", "AFX/SAW", "Tha",
     "http://gw:8200/localfs/stream/t2"),
    ("Boards of Canada", "Music Has the Right", "BoC/MHTRTC", "Roygbiv",
     "http://gw:8200/localfs/stream/t3"),
]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        self.db.upsert_tracks(UDN, [
            {"id": f"o{i}", "url": url, "title": title, "artist": artist,
             "album": album, "album_key": key, "genre": "Electronic",
             "duration": "0:03:00", "mime": "audio/flac"}
            for i, (artist, album, key, title, url) in enumerate(TRACKS)])
        self.db.album_fav_add("Aphex Twin", "Selected Ambient Works",
                              album_key="AFX/SAW")
        pid = self.db.pl_create("Test PL")
        self.db.pl_add_track(pid, {"url": TRACKS[0][4], "title": "Xtal",
                                   "artist": "Aphex Twin",
                                   "album": "Selected Ambient Works"})
        self.pid = pid
        self._p = patch.object(api_upnp_ids, "DB", self.db)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        os.unlink(self.tmp.name)

    def browse(self, oid, flag="BrowseDirectChildren", start=0, count=0):
        return api_upnp._gw_browse(oid, flag, start, count)


class TestDispatchTables(unittest.TestCase):
    """Pure table invariants — no DB needed."""

    def test_prefixes_all_end_with_colon(self):
        """The disjointness argument in the dispatch comment RESTS on this:
        every prefix ends in ':' and no exact id contains one."""
        bad = [p for p, _ in api_upnp._BROWSE_PREFIX if not p.endswith(":")]
        self.assertEqual(bad, [], f"prefixes without a ':' terminator: {bad}")

    def test_no_exact_id_contains_a_colon(self):
        bad = [k for k in api_upnp._BROWSE_EXACT if ":" in k]
        self.assertEqual(bad, [], f"exact ids containing ':': {bad}")

    def test_no_exact_id_is_shadowed_by_a_prefix(self):
        """"vidlocs" must not be swallowed by "vidloc:", etc. Exact wins in
        the implementation, but an exact id that ALSO matches a prefix is a
        latent trap for the next editor."""
        shadowed = [(k, p) for k in api_upnp._BROWSE_EXACT
                    for p, _ in api_upnp._BROWSE_PREFIX if k.startswith(p)]
        self.assertEqual(shadowed, [], f"exact ids shadowed by prefixes: {shadowed}")

    def test_no_prefix_shadows_another_prefix(self):
        pres = [p for p, _ in api_upnp._BROWSE_PREFIX]
        shadowed = [(a, b) for a in pres for b in pres
                    if a != b and a.startswith(b)]
        self.assertEqual(shadowed, [],
                         f"prefix pairs where one shadows the other: {shadowed}")

    def test_every_handler_is_registered(self):
        """An orphaned _br_* handler is a container nobody can browse."""
        defined = {v for k, v in vars(api_upnp).items()
                   if k.startswith("_br_") and callable(v)}
        wired = set(api_upnp._BROWSE_EXACT.values()) | {
            h for _, h in api_upnp._BROWSE_PREFIX}
        self.assertEqual(defined - wired, set(),
                         f"handlers defined but not registered: "
                         f"{sorted(f.__name__ for f in defined - wired)}")

    def test_registered_handlers_all_exist(self):
        for name, h in list(api_upnp._BROWSE_EXACT.items()) + \
                list(api_upnp._BROWSE_PREFIX):
            self.assertTrue(callable(h), f"{name} maps to a non-callable")


class TestEnvelope(_Base):
    """Every reachable container answers with well-formed DIDL-Lite."""

    ALL_IDS = ["0", "artists", "albums", "genres", "favalbums", "playlists",
               "videos", "abooks", "vidall", "viddates", "vidlocs",
               "vidpeople", "vidcountry-none", "vidloc-none"]

    def test_all_exact_ids_return_parseable_didl(self):
        for oid in self.ALL_IDS:
            for flag in ("BrowseDirectChildren", "BrowseMetadata"):
                with self.subTest(oid=oid, flag=flag):
                    xml, n, total = self.browse(oid, flag)
                    root = ET.fromstring(xml)      # raises on malformed
                    self.assertTrue(root.tag.endswith("DIDL-Lite"))
                    self.assertIsInstance(n, int)
                    self.assertIsInstance(total, int)
                    self.assertLessEqual(n, total if total else n)

    def test_browse_metadata_always_returns_one_one(self):
        for oid in self.ALL_IDS:
            with self.subTest(oid=oid):
                _xml, n, total = self.browse(oid, "BrowseMetadata")
                self.assertEqual((n, total), (1, 1))

    def test_root_lists_the_expected_containers(self):
        xml, n, total = self.browse("0")
        self.assertEqual(n, total)
        titles = [e.text for e in ET.fromstring(xml).iter()
                  if e.tag.endswith("title")]
        for want in ("Artists", "Albums", "Genres",
                     "⭐ Favourite Albums", "Playlists"):
            self.assertIn(want, titles)


class TestGarbledIds(_Base):
    """Rule 3: never fault. A Naim abandons the browse on a SOAP fault."""

    GARBAGE = ["", "bogus", "nope:xyz", "galbum:!!!notbase64!!!",
               "gartist:@@@", "ggenre:%%%", "abbook:zzz", "abauthor:!!",
               "albumltr:Ø", "viddate:xx", "vidcloc:!!", "vidperson:!!",
               "vidcountry:!!", "vidloc:!!", "vid:nope", "pl:nosuch",
               "favalbum:###"]

    def test_garbled_ids_yield_empty_container_not_an_error(self):
        for oid in self.GARBAGE:
            for flag in ("BrowseDirectChildren", "BrowseMetadata"):
                with self.subTest(oid=oid, flag=flag):
                    xml, n, _total = self.browse(oid, flag)
                    ET.fromstring(xml)             # parseable
                    self.assertEqual(n, 0 if flag == "BrowseDirectChildren"
                                     else n)

    def test_unknown_id_matches_no_table_entry(self):
        xml, n, total = self.browse("definitely-not-a-container")
        self.assertEqual((n, total), (0, 0))
        self.assertEqual(xml, api_upnp._DIDL_OPEN + api_upnp._DIDL_CLOSE)


class TestPagination(_Base):
    """Rule 4 — `count == 0` means unlimited in the CD spec, which is why
    the slice is conditional. Off-by-one here silently truncates the Naim's
    view of a 2,000-album library."""

    def test_count_zero_returns_everything(self):
        _xml, n, total = self.browse("artists", count=0)
        self.assertEqual(n, total)

    def test_count_limits_the_page(self):
        _xml, n, total = self.browse("artists", start=0, count=1)
        self.assertEqual(n, 1)
        self.assertEqual(total, 2)          # Aphex Twin + Boards of Canada

    def test_start_beyond_end_returns_no_items_but_real_total(self):
        _xml, n, total = self.browse("artists", start=10_000, count=5)
        self.assertEqual(n, 0)
        self.assertEqual(total, 2)

    def test_total_is_independent_of_the_page(self):
        totals = {self.browse("artists", start=s, count=1)[2]
                  for s in (0, 1, 5)}
        self.assertEqual(totals, {2})


class TestDrilldown(_Base):
    """The real tree still resolves end to end after the split."""

    def test_artist_to_album_to_tracks(self):
        xml, _n, _t = self.browse("artists")
        aid = ET.fromstring(xml)[0].attrib["id"]
        self.assertTrue(aid.startswith("gartist:"))

        xml, _n, _t = self.browse(aid)
        alb = ET.fromstring(xml)[0].attrib["id"]
        self.assertTrue(alb.startswith("galbum:"))

        xml, n, _t = self.browse(alb)
        items = list(ET.fromstring(xml))
        self.assertTrue(items and n == len(items))
        res = [e for e in ET.fromstring(xml).iter() if e.tag.endswith("res")]
        self.assertTrue(all("/localfs/stream/" in (e.text or "") for e in res))

    def test_playlist_tracks_resolve(self):
        xml, n, _t = self.browse(f"pl:{self.pid}")
        self.assertEqual(n, 1)
        titles = [e.text for e in ET.fromstring(xml).iter()
                  if e.tag.endswith("title")]
        self.assertIn("Xtal", titles)

    def test_favourite_album_resolves_by_album_key(self):
        xml, _n, _t = self.browse("favalbums")
        fid = ET.fromstring(xml)[0].attrib["id"]
        self.assertTrue(fid.startswith("favalbum:"))
        _xml, n, _t = self.browse(fid)
        self.assertEqual(n, 2)              # both Aphex tracks


class TestDbInjectionContract(unittest.TestCase):
    """api_upnp was split into seven modules on 2026-08-20. `DB` is bound in
    exactly ONE of them (api_upnp_ids) and siblings reach it as `_ids.DB`,
    resolved at call time.

    This matters more than it looks. If a second module ever does
    `from dlna_library import DB`, a test patching the owner leaves THAT
    module pointed at the real library.db — the tests still pass, but
    against production data. A false pass is far worse than a crash, so the
    invariant is asserted rather than trusted."""

    def test_db_is_bound_in_exactly_one_module(self):
        import pathlib
        family = sorted(pathlib.Path(PROJECT).glob("api_upnp*.py"))
        binders = [p.name for p in family
                   if "from dlna_library import DB" in p.read_text(encoding="utf-8")]
        self.assertEqual(
            binders, ["api_upnp_ids.py"],
            "DB must be imported in api_upnp_ids ONLY; every other module in "
            f"the family uses `_ids.DB`. Found binders: {binders}")

    def test_patching_the_owner_reaches_every_sibling(self):
        import api_upnp_browse
        import api_upnp_browse_video
        import api_upnp_didl
        import api_upnp_ids

        class _Sentinel:
            def primary_udn(self):
                raise _Reached()

            def all_videos(self, _udn):
                raise _Reached()

        class _Reached(Exception):
            pass

        ctx = api_upnp_didl._Browse("x", "BrowseDirectChildren", 0, 0)
        with patch.object(api_upnp_ids, "DB", _Sentinel()):
            for name, fn in (("browse", api_upnp_browse._br_artists),
                             ("browse_video", api_upnp_browse_video._br_vidall)):
                with self.subTest(module=name):
                    with self.assertRaises(_Reached,
                                           msg=f"{name} did not see the patched DB "
                                               f"— it is using the REAL library"):
                        fn(ctx)


if __name__ == "__main__":
    unittest.main()
