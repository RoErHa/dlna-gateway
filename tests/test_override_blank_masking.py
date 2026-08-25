#!/usr/bin/env python3
"""
tests/test_override_blank_masking.py — an override may not blank a field
the file tags fill correctly.

`metadata_overrides`' contract is **NULL = no override for this field**,
read back through `COALESCE(override.col, tracks.col)`. An empty string is
not NULL, so it wins that COALESCE. The user-edit writer used to seed a
new row from the track's CURRENT values, which froze whatever the other
fields happened to be at that moment — including blanks.

Live consequence (2026-08-25): 74 rows masked real tags, and 11
perfectly-tagged the band files browsed with no artist and no album. They
were invisible in the "- Unknown Artists -" worklist too, because their
folder could name them, so nothing surfaced the problem at all.

Run standalone:
    python3 -m unittest tests.test_override_blank_masking -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
from dlna_library_overrides import _blank_to_null

LF = "uuid:localfs-test"


def _row(tid, artist, album, title, genre=""):
    return {"id": tid, "url": f"http://h/{tid}", "title": title,
            "artist": artist, "album": album, "genre": genre,
            "album_key": "Band/Record", "file_path": f"/m/{tid}.flac",
            "mime": "audio/flac"}


class TestBlankToNull(unittest.TestCase):

    def test_empty_string_becomes_none(self):
        self.assertIsNone(_blank_to_null(""))

    def test_none_stays_none(self):
        self.assertIsNone(_blank_to_null(None))

    def test_a_real_value_is_untouched(self):
        self.assertEqual(_blank_to_null("R.V.M."), "R.V.M.")

    def test_whitespace_is_a_real_value(self):
        """Only the empty string is 'absent'. A user who typed a space
        made a choice, and guessing otherwise is a different bug."""
        self.assertEqual(_blank_to_null(" "), " ")


class TestEditingOneFieldDoesNotBlankTheOthers(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.db.upsert_tracks(LF, [
            _row("t1", "The Band", "The Record", "Song One", "Rock")])

    def tearDown(self):
        os.unlink(self._p)

    def _override(self):
        with self.db._pool.read() as c:
            return dict(c.execute(
                "SELECT artist, album, title, genre FROM metadata_overrides "
                "WHERE url='http://h/t1'").fetchone())

    def _track(self):
        with self.db._pool.read() as c:
            return dict(c.execute(
                "SELECT artist, album, title FROM tracks "
                "WHERE url='http://h/t1'").fetchone())

    def test_editing_only_the_title_leaves_artist_intact(self):
        self.db.update_track_meta("http://h/t1", title="Song One (Live)")
        self.assertEqual(self._track()["artist"], "The Band")

    def test_untouched_fields_are_stored_as_null_not_empty(self):
        """The whole bug in one assertion: '' here masks the file tag on
        every future re-index, NULL does not."""
        self.db.update_track_meta("http://h/t1", title="Song One (Live)")
        o = self._override()
        for k in ("artist", "album", "genre"):
            self.assertNotEqual(o[k], "", f"{k} stored as empty string")

    def test_a_blank_track_field_is_not_frozen_into_the_override(self):
        """The real shape: the track was blank when the edit happened, so
        the row captured '' and kept masking the tags forever after."""
        self.db.upsert_tracks(LF, [_row("t2", "", "", "Untitled")])
        self.db.update_track_meta("http://h/t2", genre="Jazz")
        with self.db._pool.read() as c:
            o = dict(c.execute("SELECT artist, album FROM metadata_overrides "
                               "WHERE url='http://h/t2'").fetchone())
        self.assertIsNone(o["artist"])
        self.assertIsNone(o["album"])

    def test_a_real_edit_still_wins(self):
        self.db.update_track_meta("http://h/t1", artist="Someone Else")
        self.assertEqual(self._track()["artist"], "Someone Else")
        self.assertEqual(self._override()["artist"], "Someone Else")


class TestReindexCannotBeMaskedByAStrayBlank(unittest.TestCase):
    """Second layer: even if a '' reaches the table from somewhere else —
    an old row, a tool, a hand-written UPDATE — a re-index must not let it
    overwrite a real tag."""

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.db = LibraryDB(db_file=self._p)
        self.db.upsert_tracks(LF, [
            _row("t1", "The Band", "The Record", "Song One", "Rock")])
        with self.db._pool.write() as c:
            c.execute(
                "INSERT OR REPLACE INTO metadata_overrides "
                "(url, artist, album, title, genre, source) "
                "VALUES ('http://h/t1', '', '', 'Kept Title', '', 'manual')")

    def tearDown(self):
        os.unlink(self._p)

    def test_reindex_keeps_the_file_tags(self):
        self.db.upsert_tracks(LF, [
            _row("t1", "The Band", "The Record", "Song One", "Rock")])
        with self.db._pool.read() as c:
            t = dict(c.execute("SELECT artist, album, title FROM tracks "
                               "WHERE url='http://h/t1'").fetchone())
        self.assertEqual(t["artist"], "The Band")
        self.assertEqual(t["album"], "The Record")

    def test_a_genuine_override_still_applies(self):
        """The fix must not disable overrides — only the blank ones."""
        self.db.upsert_tracks(LF, [
            _row("t1", "The Band", "The Record", "Song One", "Rock")])
        with self.db._pool.read() as c:
            t = dict(c.execute("SELECT title FROM tracks "
                               "WHERE url='http://h/t1'").fetchone())
        self.assertEqual(t["title"], "Kept Title")


if __name__ == "__main__":
    unittest.main()
