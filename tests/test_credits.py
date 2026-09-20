#!/usr/bin/env python3
"""
test_credits.py — composer / lyricist, read from the tags already on
disk (step 1 of the metadata-enrichment work, 2026-09-20).

Two halves, and the second is where the bugs hide:

  * `_read_tags` must actually SEE the credit tags. mutagen's
    `easy=True` interface is not uniform — FLAC and MP3 expose
    `composer`/`lyricist` natively, but **EasyMP4 registers neither**,
    so an `.m4a` carrying a real `©wrt` credit reads as blank unless
    `localfs_tags` registers the key at import time. Measured on the
    live library before this was written: `easy=True` returned None for
    an m4a whose raw tag held "Eric Kretz/Robert DeLeo/Scott Weiland".

  * `upsert_tracks` must PERSIST a change to them. Step 2a's UPDATE
    carries a change-guard WHERE clause so an untouched rescan is a
    no-op (no FTS trigger churn). A column added to SET but not to that
    guard silently never updates — the retagged file looks identical
    forever. `test_composer_only_change_is_refreshed` is that trap.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dlna_library import LibraryDB                      # noqa: E402
from dlna_providers.localfs_tags import _read_tags      # noqa: E402


class _FakeInfo:
    length = 180.0
    bits_per_sample = 16
    sample_rate = 44100


class _FakeAudio(dict):
    """Stands in for a mutagen easy-mode file: dict-like `.get`
    returning a list of values, plus `.info`."""
    def __init__(self, **tags):
        super().__init__({k: list(v) if isinstance(v, (list, tuple)) else [v]
                          for k, v in tags.items()})
        self.info = _FakeInfo()


def _tags(**kw):
    base = {"title": "T", "artist": "A", "album": "Alb"}
    base.update(kw)
    with patch("mutagen.File", return_value=_FakeAudio(**base)):
        return _read_tags(Path("/music/x.flac"))


class TestReadTagsCredits(unittest.TestCase):
    """The reader's allowlist must include the credit fields."""

    def test_composer_is_read(self):
        self.assertEqual(_tags(composer="Freddie Mercury")["composer"],
                         "Freddie Mercury")

    def test_lyricist_is_read(self):
        self.assertEqual(_tags(lyricist="Bernie Taupin")["lyricist"],
                         "Bernie Taupin")

    def test_absent_credits_are_empty_not_missing(self):
        """Every consumer does row['composer']; a missing key is a
        KeyError on 68% of this library."""
        row = _tags()
        self.assertEqual(row["composer"], "")
        self.assertEqual(row["lyricist"], "")

    def test_whitespace_is_stripped(self):
        self.assertEqual(_tags(composer="  Carlo Karges \n")["composer"],
                         "Carlo Karges")

    def test_multi_value_takes_the_first(self):
        """A collaborative credit is a multi-value frame; the rest of
        the row is single-valued, so it follows `_first`."""
        self.assertEqual(
            _tags(composer=["Lennon", "McCartney"])["composer"], "Lennon")

    def test_easymp4_composer_key_is_registered(self):
        """Importing localfs_tags must teach EasyMP4 the `©wrt` atom.
        Without it every .m4a/.m4b credit reads blank — silently, since
        a missing tag and an unmapped key are indistinguishable."""
        from mutagen.easymp4 import EasyMP4Tags
        # RegisterTextKey populates Get/Set/Delete (List is for
        # list-valued keys) — Get is the registry the read path asks.
        self.assertIn("composer", EasyMP4Tags.Get)


class TestUpsertCredits(unittest.TestCase):
    """The credits must survive the trip through upsert_tracks — both
    on first insert and on an in-place retag."""

    URL = "http://gw:8200/localfs/stream/abc123"

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.udn = "uuid:localfs-test"

    def tearDown(self):
        os.unlink(self._path)

    def _upsert(self, **kw):
        row = {"id": "abc123", "url": self.URL, "title": "Old Title",
               "artist": "Old Artist", "album": "Old Album",
               "genre": "Rock", "art": "localfs-art:aa", "year": 1999,
               "duration": "0:03:00", "mime": "audio/flac",
               "bit_depth": 16, "sample_rate": 44100,
               "composer": "Old Composer", "lyricist": "Old Lyricist",
               "album_key": "Old Artist/Old Album"}
        row.update(kw)
        return self.db.upsert_tracks(self.udn, [row])

    def _row(self):
        with self.db._pool.read() as conn:
            r = conn.execute("SELECT * FROM tracks WHERE url=?",
                             (self.URL,)).fetchone()
        return dict(r) if r else None

    def test_credits_are_stored_on_insert(self):
        self._upsert()
        row = self._row()
        self.assertEqual(row["composer"], "Old Composer")
        self.assertEqual(row["lyricist"], "Old Lyricist")

    def test_composer_only_change_is_refreshed(self):
        """THE trap. Step 2a's UPDATE only fires when its change-guard
        WHERE clause sees a difference. A `beet parentwork` run writes
        ONLY the composer — every other field is byte-identical — so a
        guard that doesn't mention composer leaves the row stale
        forever, and the rescan reports 0 refreshed."""
        self._upsert()
        self._upsert(composer="Freddie Mercury")
        self.assertEqual(self._row()["composer"], "Freddie Mercury")

    def test_lyricist_only_change_is_refreshed(self):
        self._upsert()
        self._upsert(lyricist="Tim Rice")
        self.assertEqual(self._row()["lyricist"], "Tim Rice")

    def test_incoming_blank_never_blanks_a_stored_credit(self):
        """Same contract as genre/art: a file that lost its tag — or a
        format whose easy interface can't read it — must not erase a
        credit another source supplied. The metadata_overrides '' vs
        NULL lesson, one table over."""
        self._upsert()
        self._upsert(composer="", lyricist="")
        row = self._row()
        self.assertEqual(row["composer"], "Old Composer")
        self.assertEqual(row["lyricist"], "Old Lyricist")

    def test_credits_survive_alongside_a_normal_retag(self):
        self._upsert()
        self._upsert(title="New Title", composer="New Composer")
        row = self._row()
        self.assertEqual(row["title"], "New Title")
        self.assertEqual(row["composer"], "New Composer")
        self.assertEqual(self.db.track_count(self.udn), 1)


class TestTrackMetaExposesCredits(unittest.TestCase):
    """`track_meta_by_url` backs /api/track_meta, which is the ONE
    request the now-playing panel already makes per track. The credits
    ride along on it rather than costing a second round-trip."""

    URL = "http://gw:8200/localfs/stream/zz"

    def setUp(self):
        self._fd, self._path = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._path)
        self.db.upsert_tracks("uuid:localfs-test", [{
            "id": "zz", "url": self.URL, "title": "Bohemian Rhapsody",
            "artist": "Queen", "album": "A Night at the Opera",
            "duration": "0:05:55", "mime": "audio/flac", "year": 1975,
            "composer": "Freddie Mercury", "lyricist": "Freddie Mercury",
            "album_key": "Queen/A Night at the Opera"}])

    def tearDown(self):
        os.unlink(self._path)

    def test_credits_are_returned(self):
        m = self.db.track_meta_by_url(self.URL)
        self.assertEqual(m["composer"], "Freddie Mercury")
        self.assertEqual(m["lyricist"], "Freddie Mercury")

    def test_existing_year_fields_are_untouched(self):
        """The panel's year line reads the same payload — adding
        columns must not disturb it."""
        m = self.db.track_meta_by_url(self.URL)
        self.assertEqual(m["year"], 1975)
        self.assertIn("year_original", m)
        self.assertEqual(m["title"], "Bohemian Rhapsody")

    def test_scene_advert_credits_are_not_served(self):
        """8% of this library's credits are scene adverts
        (`www.t.me/pmedia_music`). track_meta must not hand one to the
        panel — see dlna_credits + tests/test_credit_cleaning.py."""
        url = "http://gw:8200/localfs/stream/spam"
        self.db.upsert_tracks("uuid:localfs-test", [{
            "id": "spam", "url": url, "title": "Under the Lowest",
            "artist": "Nina Simone", "album": "X", "duration": "0:03:00",
            "mime": "audio/flac", "composer": "Nina Simone",
            "lyricist": "www.t.me/pmedia_music", "album_key": "Nina/X"}])
        m = self.db.track_meta_by_url(url)
        self.assertEqual(m["composer"], "Nina Simone")
        self.assertEqual(m["lyricist"], "")

    def test_unknown_url_still_returns_none(self):
        self.assertIsNone(self.db.track_meta_by_url("http://nope/x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
