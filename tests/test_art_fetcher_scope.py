#!/usr/bin/env python3
"""
test_art_fetcher_scope.py — WHAT the cover fetcher should ask about.

A live pass over 1,497 "bare" albums returned 32 covers. Investigating
why turned up three things that are wrong regardless of hit rate — the
fetcher was spending a rate-limited budget on questions that cannot
have an answer:

  * **169 were AUDIOBOOKS.** The very first query of the run was
    `artist='Patrick Rothfuss' album='The Kingkiller Chronicle Book 2'`
    — asked of a music database. `bare_albums()` had no udn filter, so
    a second LocalFs root was swept along with the music.

  * **90 were one- or two-track strays**: a loose file sitting in its
    own folder. It has no cover because it is not an album, and every
    one is a guaranteed miss.

  * **`multi_artist` never reached `art_queries`**, so the title-only
    query — the only form that can find a compilation, and the one
    `tests/test_art_query.py` covers — never ran in production.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_art_fetcher import AlbumArtFetcher                     # noqa: E402
from dlna_library import LibraryDB                               # noqa: E402

MUSIC = "uuid:localfs-music"
BOOKS = "uuid:localfs-books"


class _Base(unittest.TestCase):
    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.f = AlbumArtFetcher(self.db)

    def tearDown(self):
        os.unlink(self._path)

    def _album(self, udn, artist, album, n=6, performers=1):
        rows = []
        for i in range(n):
            a = artist if performers == 1 else f"{artist}{i % performers}"
            rows.append({"id": f"{album}-{i}", "url": f"http://x/{album}/{i}",
                         "title": f"T{i}", "artist": a, "album": album,
                         "duration": "0:03:00", "mime": "audio/flac",
                         "art": "", "album_key": f"{artist}/{album}"})
        self.db.upsert_tracks(udn, rows)


class TestScope(_Base):
    def test_audiobooks_are_never_asked_about(self):
        """A music database cannot answer, and the row it caches is a
        `notfound` against a book."""
        self._album(MUSIC, "Elbow", "Leaders of the Free World")
        self._album(BOOKS, "Patrick Rothfuss", "The Kingkiller Chronicle")
        got = [a for _, a, _ in self.f.bare_albums(skip_udns=(BOOKS,))]
        self.assertIn("Leaders of the Free World", got)
        self.assertNotIn("The Kingkiller Chronicle", got)

    def test_a_one_track_stray_is_not_an_album(self):
        """90 of the live failures were these. No source can find a
        cover for something that was never released as an album."""
        self._album(MUSIC, "Somebody", "A Real Album", n=6)
        self._album(MUSIC, "Stray", "Loose Single", n=1)
        got = [a for _, a, _ in self.f.bare_albums(min_tracks=3)]
        self.assertIn("A Real Album", got)
        self.assertNotIn("Loose Single", got)

    def test_the_default_keeps_the_old_behaviour(self):
        """Callers that pass nothing must see what they always saw —
        the filters are opt-in, so nothing silently narrows."""
        self._album(MUSIC, "Stray", "Loose Single", n=1)
        self.assertIn("Loose Single", [a for _, a, _ in self.f.bare_albums()])

    def test_a_multi_performer_folder_is_reported_as_such(self):
        """This is what feeds `multi_artist` into art_queries, so the
        title-only fallback can finally fire for a compilation."""
        self._album(MUSIC, "Comp", "Big Compilation", n=8, performers=5)
        rows = self.f.bare_albums(with_performers=True)
        row = [r for r in rows if r[1] == "Big Compilation"][0]
        self.assertGreater(row[3], 1)

    def test_a_single_artist_album_reports_one_performer(self):
        self._album(MUSIC, "Elbow", "Asleep in the Back", n=8)
        rows = self.f.bare_albums(with_performers=True)
        row = [r for r in rows if r[1] == "Asleep in the Back"][0]
        self.assertEqual(row[3], 1)

    def test_an_album_that_already_has_art_is_skipped(self):
        """Unchanged contract — the filters must not disturb it."""
        self.db.upsert_tracks(MUSIC, [{
            "id": "1", "url": "http://x/1", "title": "T", "artist": "A",
            "album": "Has Art", "duration": "0:03:00", "mime": "audio/flac",
            "art": "http://x/art.jpg", "album_key": "A/Has Art"}])
        self.assertNotIn("Has Art", [a for _, a, _ in self.f.bare_albums()])


if __name__ == "__main__":
    unittest.main(verbosity=2)
