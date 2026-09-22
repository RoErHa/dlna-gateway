#!/usr/bin/env python3
"""
test_album_art_sentinel.py — a marker is not a URL (2026-09-22).

A LocalFs scan writes `localfs-art:<sha1>` into `tracks.art` as a
PLACEHOLDER: the embedded picture's bytes aren't read until serve time,
so at scan time all we have is a hash. `LocalFsProvider._rescan` heals
those markers into real `<base_url>/localfs/art/<obj_id>` URLs once the
base URL is known.

That heal covers `tracks`. It never covered **`album_art`** — and the
Phase-A sibling harvest copies `MIN(art)` straight out of `tracks`, so
a harvest that runs before the heal stores the marker permanently.
`INSERT OR IGNORE` then guarantees it is never replaced.

Measured on the live library: **3,982 album_art rows** holding an
unfetchable `localfs-art:` string. `tracks` was clean, so the PWA mostly
got away with it — the exposure is every surface that reads `album_art`
directly, which is Subsonic/CarPlay cover art and the favourites list.
It surfaced as five Queen albums whose covers 404'd.

Both directions are fixed here, because the harvest's own UPDATE also
pushes `album_art.art_url` back onto art-less tracks — so a stored
marker can travel into `tracks` as well.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_library import LibraryDB                              # noqa: E402

UDN = "uuid:localfs-art"
REAL = "http://192.168.1.125:8200/localfs/art/abc123"


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)

    def tearDown(self):
        os.unlink(self._path)

    def _add(self, artist, album, art, n=2, folder=None):
        folder = folder or f"{artist}/{album}"
        self.db.upsert_tracks(UDN, [{
            "id": f"{folder}-{i}", "url": f"http://x/{folder}/{i}",
            "title": f"T{i}", "artist": artist, "album": album,
            "duration": "0:03:00", "mime": "audio/flac", "art": art,
            "file_path": f"/m/{folder}/{i}.flac", "album_key": folder}
            for i in range(n)])

    def _art_row(self, artist, album):
        with self.db._pool.read() as c:
            r = c.execute("SELECT art_url, source FROM album_art "
                          "WHERE artist=? AND album=?",
                          (artist, album)).fetchone()
        return dict(r) if r else None


class TestTheMarkerIsNeverHarvested(_Base):
    def test_a_marker_does_not_become_an_album_art_row(self):
        """It is a hash, not an address. Storing it caches a cover that
        can never be fetched — and INSERT OR IGNORE makes it permanent."""
        self._add("Queen", "Jazz", "localfs-art:deadbeef")
        self.assertIsNone(self._art_row("Queen", "Jazz"))

    def test_a_real_url_is_still_harvested(self):
        self._add("Queen", "The Game", REAL)
        self.assertEqual(self._art_row("Queen", "The Game")["art_url"], REAL)

    def test_an_album_recovers_once_its_art_is_healed(self):
        """The marker is refused, not remembered — so when the provider
        heals tracks.art the next harvest picks the album up normally."""
        self._add("Queen", "Jazz", "localfs-art:deadbeef")
        self.assertIsNone(self._art_row("Queen", "Jazz"))
        self._add("Queen", "Jazz", REAL)
        self.assertEqual(self._art_row("Queen", "Jazz")["art_url"], REAL)


class TestExistingRowsAreHealed(_Base):
    def _poison(self, artist, album, marker="localfs-art:deadbeef"):
        with self.db._pool.write() as c:
            c.execute("INSERT OR REPLACE INTO album_art "
                      "(artist, album, art_url, source, updated_at) "
                      "VALUES (?,?,?,'sibling',0)", (artist, album, marker))

    def test_a_stored_marker_is_replaced_from_the_tracks_table(self):
        self._poison("Queen", "Jazz")
        self._add("Queen", "Jazz", REAL)
        self.assertEqual(self._art_row("Queen", "Jazz")["art_url"], REAL)

    def test_a_marker_with_no_healed_track_is_left_alone(self):
        """Deleting it would make the album bare and send it to
        MusicBrainz — an hour of rate-limited lookups for 3,982 albums.
        It is inert where it sits; leave it for the next scan."""
        self._poison("Ghost", "Nothing Here", "localfs-art:original")
        self._add("Ghost", "Nothing Here", "localfs-art:stillamarker")
        # Untouched — still the value it was poisoned with, not the
        # track's marker and not NULL.
        self.assertEqual(self._art_row("Ghost", "Nothing Here")["art_url"],
                         "localfs-art:original")

    def test_real_art_is_never_overwritten_by_the_heal(self):
        """Only markers are touched. A MusicBrainz cover outranks the
        embedded one and must survive."""
        caa = "https://coverartarchive.org/release-group/x/front-500"
        with self.db._pool.write() as c:
            c.execute("INSERT OR REPLACE INTO album_art "
                      "(artist, album, art_url, source, updated_at) "
                      "VALUES ('Queen','Jazz',?,'musicbrainz',0)", (caa,))
        self._add("Queen", "Jazz", REAL)
        self.assertEqual(self._art_row("Queen", "Jazz")["art_url"], caa)


class TestTheMarkerCannotTravelIntoTracks(_Base):
    def test_an_art_less_track_is_not_filled_with_a_marker(self):
        """The harvest's second half pushes album_art back onto tracks
        that have none. A stored marker would spread there too."""
        with self.db._pool.write() as c:
            c.execute("INSERT OR REPLACE INTO album_art "
                      "(artist, album, art_url, source, updated_at) "
                      "VALUES ('Queen','Jazz','localfs-art:x','sibling',0)")
        self._add("Queen", "Jazz", "")
        with self.db._pool.read() as c:
            arts = [r["art"] for r in c.execute(
                "SELECT art FROM tracks WHERE artist='Queen'")]
        self.assertTrue(all(not (a or "").startswith("localfs-art:")
                            for a in arts), arts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
