"""Unit tests for tools/fill_localfs_album_art.py — over a throw-away SQLite.

Never hits the network: dlna_art_fetcher._mb_lookup_cover is patched.
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tools.fill_localfs_album_art as fill


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row          # the helper uses row["artist"] etc.
    c.executescript("""
        CREATE TABLE tracks(
          id INTEGER PRIMARY KEY, url TEXT, artist TEXT, album TEXT,
          album_key TEXT, art TEXT DEFAULT '');
        CREATE TABLE album_art(
          artist TEXT NOT NULL, album TEXT NOT NULL, art_url TEXT NOT NULL,
          source TEXT DEFAULT 'sibling',
          updated_at TEXT DEFAULT (datetime('now')),
          PRIMARY KEY (artist, album));
    """)
    return path, c


def _add(c, url, artist, album, ak, art=""):
    c.execute("INSERT INTO tracks(url, artist, album, album_key, art) "
              "VALUES (?,?,?,?,?)", (url, artist, album, ak, art))


class TestCleanAlbum(unittest.TestCase):
    def test_strips_edition_format_disc_noise(self):
        cases = {
            "The Snow Goose (SHM-CD)": "The Snow Goose",
            "Playback- Spoiled & Mistreated-CD2": "Playback- Spoiled & Mistreated",
            "Born to Run [30th Anniversary Edition] Disc 3": "Born to Run",
            "The Very Best of Paul Anka [RCA US]": "The Very Best of Paul Anka",
            "Old School Soul Party Disc 1": "Old School Soul Party",
        }
        for raw, want in cases.items():
            self.assertEqual(fill.clean_album(raw), want, raw)

    def test_plain_name_unchanged(self):
        self.assertEqual(fill.clean_album("Fragile"), "Fragile")

    def test_all_noise_returns_empty(self):
        self.assertEqual(fill.clean_album("(Deluxe Edition)"), "")


class TestArtlessSelection(unittest.TestCase):
    def setUp(self):
        self.path, self.c = _make_db()
        # Album A: folder, NO art on any track → candidate.
        _add(self.c, "u1", "Camel", "Mirage", "Camel/Mirage", "")
        _add(self.c, "u2", "Camel", "Mirage", "Camel/Mirage", "")
        # Album B: folder, one track HAS art → NOT art-less.
        _add(self.c, "u3", "Yes", "Fragile", "Yes/Fragile", "")
        _add(self.c, "u4", "Yes", "Fragile", "Yes/Fragile",
             "http://localfs/art/abc")
        # Album C: art-less but no usable artist/album (Various/blank).
        _add(self.c, "u5", "", "", "Comp/Unknown", "")
        # Album D: Various-Artists compilation (3 distinct artists, none
        # dominant) → must be SKIPPED (never wrong-arted).
        _add(self.c, "u6", "Artist A", "Top Hits", "VA/TopHits", "")
        _add(self.c, "u7", "Artist B", "Top Hits", "VA/TopHits", "")
        _add(self.c, "u8", "Artist C", "Top Hits", "VA/TopHits", "")
        # Album E: one artist dominates (4 of 5) + a guest → KEPT.
        for i in range(9, 13):
            _add(self.c, f"u{i}", "Main", "LP", "Main/LP", "")
        _add(self.c, "u13", "Guest", "LP", "Main/LP", "")
        self.c.commit()

    def tearDown(self):
        self.c.close()
        os.unlink(self.path)

    def test_only_artless_folder_albums_selected(self):
        rows = fill.artless_folder_albums(self.c)
        keys = {r[0] for r in rows}
        self.assertIn("Camel/Mirage", keys)        # no art anywhere
        self.assertIn("Comp/Unknown", keys)        # art-less (but no metadata)
        self.assertNotIn("Yes/Fragile", keys)      # has art on a track

    def test_representative_artist_album(self):
        rows = {r[0]: (r[1], r[2]) for r in fill.artless_folder_albums(self.c)}
        self.assertEqual(rows["Camel/Mirage"], ("Camel", "Mirage"))
        self.assertEqual(rows["Comp/Unknown"], ("", ""))   # no usable metadata

    def test_compilation_skipped_dominant_artist_kept(self):
        rows = {r[0]: (r[1], r[2]) for r in fill.artless_folder_albums(self.c)}
        # Various-Artists comp → skipped (no single artist to look up).
        self.assertEqual(rows["VA/TopHits"], ("", ""))
        # One artist owns 4/5 → kept for lookup.
        self.assertEqual(rows["Main/LP"], ("Main", "LP"))


class TestApply(unittest.TestCase):
    def setUp(self):
        self.path, self.c = _make_db()
        _add(self.c, "u1", "Camel", "Mirage", "Camel/Mirage", "")
        _add(self.c, "u2", "Camel", "Mirage", "Camel/Mirage", "")
        _add(self.c, "u3", "Gong", "Camembert", "Gong/Camembert", "")
        _add(self.c, "u5", "", "", "Comp/Unknown", "")  # skipped (no metadata)
        self.c.commit()
        self.c.close()

    def tearDown(self):
        os.unlink(self.path)

    def _run(self, argv, lookup):
        with mock.patch("dlna_art_fetcher._mb_lookup_cover", side_effect=lookup), \
             mock.patch("dlna_art_fetcher._MB_RATE_LIMIT_SEC", 0), \
             mock.patch.object(sys, "argv",
                               ["fill", "--db", self.path, "--no-backup"] + argv):
            return fill.main()

    def test_hit_writes_art_onto_all_album_tracks(self):
        def lookup(artist, album):
            return "https://coverartarchive.org/release-group/X/front-500" \
                if artist == "Camel" else None
        rc = self._run(["--apply"], lookup)
        self.assertEqual(rc, 0)
        c = sqlite3.connect(self.path)
        # Camel got the cover on BOTH tracks.
        arts = [r[0] for r in c.execute(
            "SELECT art FROM tracks WHERE album_key='Camel/Mirage'")]
        self.assertTrue(all("coverartarchive" in a for a in arts))
        # Gong missed → still empty + a sticky notfound row.
        self.assertEqual(
            c.execute("SELECT art FROM tracks WHERE album_key='Gong/Camembert'")
            .fetchone()[0], "")
        nf = c.execute("SELECT source FROM album_art WHERE artist='Gong'").fetchone()
        self.assertEqual(nf[0], "notfound")
        c.close()

    def test_cached_notfound_skips_lookup(self):
        # Pre-seed Camel as notfound → lookup must NOT be called.
        c = sqlite3.connect(self.path)
        c.execute("INSERT INTO album_art VALUES('Camel','Mirage','','notfound',1)")
        c.execute("INSERT INTO album_art VALUES('Gong','Camembert','','notfound',1)")
        c.commit(); c.close()
        called = []
        def lookup(artist, album):
            called.append(artist)
            return None
        self._run(["--apply"], lookup)
        self.assertEqual(called, [])   # both cached notfound → no MB calls

    def test_cached_hit_reused_without_lookup(self):
        c = sqlite3.connect(self.path)
        c.execute("INSERT INTO album_art VALUES('Camel','Mirage',"
                  "'https://coverartarchive.org/release-group/Y/front-500',"
                  "'musicbrainz',1)")
        c.execute("INSERT INTO album_art VALUES('Gong','Camembert','','notfound',1)")
        c.commit(); c.close()
        called = []
        self._run(["--apply"], lambda a, b: called.append(a))
        self.assertEqual(called, [])   # Camel reused from cache, Gong notfound
        c = sqlite3.connect(self.path)
        art = c.execute("SELECT art FROM tracks WHERE album_key='Camel/Mirage' "
                        "LIMIT 1").fetchone()[0]
        self.assertIn("coverartarchive", art)
        c.close()

    def test_retry_notfound_requeries_and_can_fill(self):
        # Camel cached notfound; with --retry-notfound MB is re-asked and now hits.
        c = sqlite3.connect(self.path)
        c.execute("INSERT INTO album_art VALUES('Camel','Mirage','','notfound',1)")
        c.execute("INSERT INTO album_art VALUES('Gong','Camembert','','notfound',1)")
        c.commit(); c.close()
        called = []
        def lookup(artist, album):
            called.append(artist)
            return "https://coverartarchive.org/release-group/Z/front-500" \
                if artist == "Camel" else None
        self._run(["--apply", "--retry-notfound"], lookup)
        self.assertIn("Camel", called)          # re-queried despite notfound
        c = sqlite3.connect(self.path)
        art = c.execute("SELECT art FROM tracks WHERE album_key='Camel/Mirage' "
                        "LIMIT 1").fetchone()[0]
        self.assertIn("coverartarchive", art)   # filled on retry
        # cache refreshed to musicbrainz hit
        src = c.execute("SELECT source FROM album_art WHERE artist='Camel'").fetchone()[0]
        self.assertEqual(src, "musicbrainz")
        c.close()

    def test_dry_run_makes_no_writes(self):
        rc = self._run([], lambda a, b: "x")   # no --apply
        self.assertEqual(rc, 0)
        c = sqlite3.connect(self.path)
        arts = [r[0] for r in c.execute("SELECT art FROM tracks")]
        self.assertTrue(all(a == "" for a in arts))   # untouched
        self.assertEqual(
            c.execute("SELECT COUNT(*) FROM album_art").fetchone()[0], 0)
        c.close()


if __name__ == "__main__":
    unittest.main()
