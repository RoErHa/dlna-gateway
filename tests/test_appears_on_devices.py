#!/usr/bin/env python3
"""
tests/test_appears_on_devices.py — "an artist's own records first" on the
two device surfaces.

The PWA folds compilations behind a disclosure. Neither device can do
that literally, so each gets the honest equivalent of the same idea:

  * UPnP has containers but no dividers → ONE "Appears on (N)" container.
  * Subsonic's getArtist has neither → order, plus a name that says how
    little of the record is theirs.

Both read the same `own` flag from `artist_albums`, so a change to the
rule moves all three together.

Run standalone:
    python3 -m unittest tests.test_appears_on_devices -v
"""
import os
import re
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB

LF = "uuid:localfs-test"


def _row(tid, key, artist, album, title):
    return {"id": tid, "url": f"http://h/{tid}", "title": title,
            "artist": artist, "album": album, "album_key": key,
            "file_path": f"/m/{key}/{tid}.flac", "mime": "audio/flac"}


def _seed(db):
    db.upsert_tracks(LF, [
        _row("o1", "Band/First", "The Band", "Their First", "One"),
        _row("o2", "Band/First", "The Band", "Their First", "Two"),
    ])
    db.upsert_tracks(LF, [
        _row(f"c{i}", "VA/Big", f"Artist {i}", "Big Comp", f"T{i}")
        for i in range(1, 9)
    ] + [_row("c9", "VA/Big", "The Band", "Big Comp", "Guest Spot")])


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        _seed(self.db)

    def tearDown(self):
        os.unlink(self._p)


class TestNaimTree(_Base):

    def _browse(self, oid):
        """`api_upnp_ids.DB` is the process-wide LIVE singleton, so it
        must be swapped for this test's database — patching only
        `primary_udn` would point a real library at a udn it has never
        heard of and quietly return nothing."""
        import api_upnp_browse as ub
        import api_upnp_ids as ids
        real_db = ids.DB
        ids.DB = self.db
        real_udn = self.db.primary_udn
        self.db.primary_udn = lambda: LF
        try:
            return ub._gw_browse(oid, "BrowseDirectChildren", 0, 0)[0]
        finally:
            ids.DB = real_db
            self.db.primary_udn = real_udn

    def _titles(self, xml):
        return [m.group(2) for m in re.finditer(
            r'<(container|item) id="[^"]+"[^>]*>.*?<dc:title>([^<]*)',
            xml, re.S)]

    def test_the_artist_lists_their_album_then_one_container(self):
        from api_upnp_ids import _b64e
        titles = self._titles(self._browse("gartist:" + _b64e("The Band")))
        self.assertEqual(len(titles), 2)
        self.assertIn("Their First", titles[0])
        self.assertEqual(titles[1], "Appears on (1)")

    def test_the_compilation_is_not_at_the_top_level(self):
        """The whole point — it must not sit beside their real album."""
        from api_upnp_ids import _b64e
        titles = self._titles(self._browse("gartist:" + _b64e("The Band")))
        self.assertFalse(any("Big Comp" in t for t in titles))

    def test_opening_the_container_lists_the_compilations(self):
        from api_upnp_ids import _b64e
        titles = self._titles(self._browse("gappears:" + _b64e("The Band")))
        self.assertTrue(any("Big Comp" in t for t in titles))

    def test_a_compilation_still_resolves_to_only_their_track(self):
        from api_upnp_ids import _b64e
        xml = self._browse("gappears:" + _b64e("The Band"))
        cid = re.search(r'<container id="([^"]+)"', xml).group(1)
        titles = self._titles(self._browse(cid))
        self.assertEqual(titles, ["Guest Spot"])

    def test_an_artist_with_only_appearances_gets_them_directly(self):
        """`Artist 1` exists solely on the compilation. Folding their
        only music behind a container would give them a page holding one
        row and nothing else — the fold exists to stop compilations
        burying real albums, and here there are none to bury."""
        from api_upnp_ids import _b64e
        titles = self._titles(self._browse("gartist:" + _b64e("Artist 1")))
        self.assertFalse(any("Appears on" in t for t in titles))
        self.assertTrue(any("Big Comp" in t for t in titles))

    def test_a_garbled_id_is_an_empty_container_not_a_500(self):
        titles = self._titles(self._browse("gappears:!!!not-base64!!!"))
        self.assertEqual(titles, [])

    def test_the_prefix_cannot_be_swallowed_by_gartist(self):
        """`gappears:` must not be routed to `_br_gartist` — the tables
        are disjoint by construction and this pins it."""
        self.assertFalse("gappears:x".startswith("gartist:"))


class TestCarPlayOrder(_Base):

    def _albums(self):
        rows = self.db.artist_albums(LF, "The Band")
        own = [a for a in rows if a.get("own", True)]
        app = [a for a in rows if not a.get("own", True)]
        return own, app

    def test_their_own_records_come_first(self):
        own, app = self._albums()
        self.assertEqual([a["album"] for a in own], ["Their First"])
        self.assertEqual([a["album"] for a in app], ["Big Comp"])

    def test_an_appearance_carries_its_share_for_the_name(self):
        _, app = self._albums()
        self.assertEqual(app[0]["track_count"], 1)
        self.assertEqual(app[0]["folder_tracks"], 9)

    def test_the_album_id_is_unchanged_by_the_marker(self):
        """The marker goes in the display name only. Changing the id
        would invalidate every cached album in a client."""
        from api_subsonic_ids import _album_id, _album_id_decode, _so_album
        _, app = self._albums()
        so = _so_album(app[0])
        self.assertEqual(so["id"],
                         _album_id(app[0]["artist"], app[0]["album"],
                                   app[0]["album_key"]))
        self.assertEqual(_album_id_decode(so["id"])[0], "The Band")


if __name__ == "__main__":
    unittest.main()
