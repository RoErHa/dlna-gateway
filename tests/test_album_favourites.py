#!/usr/bin/env python3
"""
tests/test_album_favourites.py — Album-favourites DB + handler tests.

Covers the data contract of whole-album bookmarking (distinct from the
track-level "⭐ Favourites" playlist):
- album_fav_add is idempotent (re-add doesn't duplicate or bump added_at)
- album_fav_is reflects current state
- album_fav_list joins art + track_count + udn correctly
- album_favourites table survives clear(udn) (same invariant as
  album_art / play_counts / lyrics)
- HTTP handlers route through DB and reject missing params

Run standalone:
    python3 -m unittest tests.test_album_favourites -v
"""
import os
import sys
import tempfile
import time
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


def _seed_track(db, *, udn="uuid:srv1", obj_id="t1",
                url="http://srv/x.flac", title="T",
                artist="Pink Floyd", album="Wish You Were Here",
                art="http://srv/cover.jpg"):
    with db._pool.write() as c:
        c.execute(
            "INSERT INTO tracks(udn, obj_id, url, title, artist, album, "
            "duration, art, mime, genre, file_path) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (udn, obj_id, url, title, artist, album, "0:03:00",
             art, "audio/flac", "", ""))
    return url


# ── DB layer ──────────────────────────────────────────────────────

class TestAlbumFavouritesDB(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_is_false_for_unknown(self):
        self.assertFalse(self.db.album_fav_is("Pink Floyd", "Animals"))

    def test_add_then_is_true(self):
        self.assertTrue(self.db.album_fav_add("Pink Floyd", "Animals"))
        self.assertTrue(self.db.album_fav_is("Pink Floyd", "Animals"))

    def test_add_is_idempotent(self):
        self.assertTrue(self.db.album_fav_add("PF", "WYWH"))
        # Second add returns False (no new row) and doesn't error.
        self.assertFalse(self.db.album_fav_add("PF", "WYWH"))
        self.assertEqual(len(self.db.album_fav_list()), 1)

    def test_remove(self):
        self.db.album_fav_add("PF", "Animals")
        self.assertTrue(self.db.album_fav_remove("PF", "Animals"))
        self.assertFalse(self.db.album_fav_is("PF", "Animals"))
        # Removing a non-favourite returns False, doesn't error.
        self.assertFalse(self.db.album_fav_remove("PF", "Animals"))

    def test_list_empty(self):
        self.assertEqual(self.db.album_fav_list(), [])

    def test_list_returns_art_and_track_count(self):
        # Seed a 3-track album so list() returns track_count=3 + art.
        for i in range(3):
            _seed_track(self.db, obj_id=f"t{i}",
                        url=f"http://srv/track{i}.flac",
                        title=f"Track {i}",
                        artist="Pink Floyd", album="Wish You Were Here",
                        art="http://srv/cover.jpg")
        self.db.album_fav_add("Pink Floyd", "Wish You Were Here")
        rows = self.db.album_fav_list()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["artist"],      "Pink Floyd")
        self.assertEqual(r["album"],       "Wish You Were Here")
        self.assertEqual(r["track_count"], 3)
        self.assertEqual(r["art"],         "http://srv/cover.jpg")
        self.assertEqual(r["udn"],         "uuid:srv1")
        self.assertGreater(r["added_at"],  0)

    def test_list_orders_newest_first(self):
        # Two adds with a 1-second gap; the newer one must come first.
        self.db.album_fav_add("A", "Older")
        # Force an earlier timestamp on the older row by hand to avoid
        # a real sleep.
        with self.db._pool.write() as c:
            c.execute("UPDATE album_favourites SET added_at=? "
                      "WHERE artist='A' AND album='Older'",
                      (int(time.time()) - 100,))
        self.db.album_fav_add("B", "Newer")
        rows = self.db.album_fav_list()
        self.assertEqual([r["album"] for r in rows], ["Newer", "Older"])

    def test_list_includes_orphan_albums(self):
        # If the user favourited an album whose tracks were later
        # removed (server gone, rebuild-index cleared them), the row
        # still appears so the user can prune it manually.
        self.db.album_fav_add("Ghost", "Lost Tapes")
        rows = self.db.album_fav_list()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["track_count"], 0)
        self.assertEqual(rows[0]["udn"],         "")

    def test_survives_clear_udn(self):
        """Same invariant as album_art / play_counts / lyrics."""
        _seed_track(self.db, url="http://srv/a.flac",
                    artist="A", album="X")
        self.db.album_fav_add("A", "X")
        self.db.clear("uuid:srv1")
        # Tracks are gone, but the favourite row survives.
        with self.db._pool.read() as c:
            n_tracks = c.execute(
                "SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
        self.assertEqual(n_tracks, 0)
        self.assertTrue(self.db.album_fav_is("A", "X"))

    def test_empty_album_string_rejected(self):
        # Defensive: an empty album would collide with metadata-less
        # tracks and is meaningless as a favourite.
        self.assertFalse(self.db.album_fav_add("Anyone", ""))
        self.assertEqual(self.db.album_fav_list(), [])


# ── Handlers ──────────────────────────────────────────────────────

class TestAlbumFavouritesHandlers(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        import api_playlists
        self._patch = patch.object(api_playlists, "DB", self.db)
        self._patch.start()
        self.api = api_playlists

    def tearDown(self):
        self._patch.stop()
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def test_check_unfavourited(self):
        h = _MockH()
        self.api.album_favourite_check(h, {"artist": "A", "album": "X"})
        self.assertEqual(h.last, (200, {"is_favourite": False}))

    def test_add_then_check_then_remove(self):
        h = _MockH()
        self.api.album_favourite_add(h, {"artist": "A", "album": "X"})
        self.assertEqual(h.last[0], 200)
        self.assertTrue(h.last[1]["created"])

        # Re-add → ok=True, created=False (idempotent).
        self.api.album_favourite_add(h, {"artist": "A", "album": "X"})
        self.assertFalse(h.last[1]["created"])

        # Check
        self.api.album_favourite_check(h, {"artist": "A", "album": "X"})
        self.assertEqual(h.last, (200, {"is_favourite": True}))

        # Remove
        self.api.album_favourite_remove(h, {"artist": "A", "album": "X"})
        self.assertEqual(h.last, (200, {"ok": True}))

        # Re-check
        self.api.album_favourite_check(h, {"artist": "A", "album": "X"})
        self.assertEqual(h.last, (200, {"is_favourite": False}))

    def test_missing_album_returns_400(self):
        h = _MockH()
        self.api.album_favourite_add(h, {"artist": "A", "album": ""})
        self.assertEqual(h.last[0], 400)
        self.api.album_favourite_check(h, {"artist": "A"})
        self.assertEqual(h.last[0], 400)
        self.api.album_favourite_remove(h, {"album": ""})
        self.assertEqual(h.last[0], 400)

    def test_list_returns_array(self):
        h = _MockH()
        self.db.album_fav_add("A", "X")
        self.db.album_fav_add("B", "Y")
        self.api.album_favourites(h, {})
        self.assertEqual(h.last[0], 200)
        self.assertIsInstance(h.last[1], list)
        self.assertEqual(len(h.last[1]), 2)


# ── album_key (folder-identity favourites, A2 / 2026-05-31) ─────────

import sqlite3   # noqa: E402


class TestAlbumFavByKey(unittest.TestCase):
    """Favouriting a LocalFs album by FOLDER (album_key)."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_add_and_is_by_album_key(self):
        self.assertTrue(self.db.album_fav_add(
            "Various Artists", "Hits", album_key="VA/Hits 1"))
        self.assertTrue(self.db.album_fav_is(
            "Various Artists", "Hits", album_key="VA/Hits 1"))
        # A different folder with the SAME display is NOT favourited.
        self.assertFalse(self.db.album_fav_is(
            "Various Artists", "Hits", album_key="VA/Hits 2"))

    def test_same_display_different_folders_coexist(self):
        # PK includes album_key, so two comps sharing artist+album display
        # both persist (would have collided under the old PK).
        self.assertTrue(self.db.album_fav_add(
            "Various Artists", "Greatest Hits", album_key="A/Greatest Hits"))
        self.assertTrue(self.db.album_fav_add(
            "Various Artists", "Greatest Hits", album_key="B/Greatest Hits"))
        keys = {f["album_key"] for f in self.db.album_fav_list()}
        self.assertEqual(keys, {"A/Greatest Hits", "B/Greatest Hits"})

    def test_remove_by_album_key(self):
        self.db.album_fav_add("VA", "X", album_key="VA/X")
        self.assertTrue(self.db.album_fav_remove("VA", "X", album_key="VA/X"))
        self.assertFalse(self.db.album_fav_is("VA", "X", album_key="VA/X"))

    def test_list_track_count_by_folder(self):
        udn = "uuid:localfs-t"
        self.db.upsert_tracks(udn, [
            {"id": "a", "url": "http://h/a", "title": "A", "artist": "P1",
             "album": "O1", "album_key": "VA/Comp", "mime": "audio/flac"},
            {"id": "b", "url": "http://h/b", "title": "B", "artist": "P2",
             "album": "O2", "album_key": "VA/Comp", "mime": "audio/flac"},
        ])
        self.db.album_fav_add("Various Artists", "Comp", album_key="VA/Comp")
        fav = next(f for f in self.db.album_fav_list()
                   if f["album_key"] == "VA/Comp")
        self.assertEqual(fav["track_count"], 2)

    def test_legacy_artist_album_fav_still_works(self):
        self.db.album_fav_add("Adele", "21")
        self.assertTrue(self.db.album_fav_is("Adele", "21"))
        self.assertFalse(self.db.album_fav_is("Adele", "21", album_key="x/y"))


class TestAlbumFavMigration(unittest.TestCase):
    """The PK-widening migration carries old rows forward with
    album_key=''."""

    def test_migration_preserves_rows_and_adds_column(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        # Build the OLD-schema table by hand, then run the migration.
        conn = sqlite3.connect(tmp.name)
        conn.executescript("""
            CREATE TABLE album_favourites (
                artist TEXT NOT NULL, album TEXT NOT NULL,
                added_at INTEGER NOT NULL, PRIMARY KEY (artist, album));
            INSERT INTO album_favourites VALUES ('Queen','A Night',111);
        """)
        conn.commit()
        db = LibraryDB.__new__(LibraryDB)        # don't run __init__
        db._migrate_album_fav_key(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(album_favourites)")}
        self.assertIn("album_key", cols)
        row = conn.execute(
            "SELECT artist, album, album_key, added_at "
            "FROM album_favourites").fetchone()
        self.assertEqual(row, ("Queen", "A Night", "", 111))
        # Idempotent: a second run is a no-op.
        db._migrate_album_fav_key(conn)
        conn.close()
        os.unlink(tmp.name)


if __name__ == "__main__":
    unittest.main()
