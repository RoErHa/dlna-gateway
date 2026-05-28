#!/usr/bin/env python3
"""
tools/test_audit_override_mismatches.py — unit tests over a throw-away DB.

Run standalone:
    python3 -m unittest tools.test_audit_override_mismatches -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

THIS = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS))

import audit_override_mismatches as A  # noqa: E402


def _make_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            udn TEXT, obj_id TEXT, url TEXT NOT NULL,
            title TEXT, artist TEXT
        );
        CREATE TABLE metadata_overrides (
            url TEXT PRIMARY KEY,
            artist TEXT, album TEXT, title TEXT,
            genre TEXT, year INTEGER, updated_at TEXT,
            source TEXT NOT NULL DEFAULT 'manual'
        );
    """)
    return conn


def _row(conn, url, t_a, t_t, m_a, m_t, src='acoustid'):
    conn.execute("INSERT INTO tracks (udn, obj_id, url, artist, title) "
                 "VALUES ('u', ?, ?, ?, ?)", (url, url, t_a, t_t))
    conn.execute("INSERT INTO metadata_overrides "
                 "(url, artist, title, source) VALUES (?,?,?,?)",
                 (url, m_a, m_t, src))
    conn.commit()


class TestSuspectDetection(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def test_exact_match_not_suspect(self):
        _row(self.conn, "u1", "Pink Floyd", "Hey You",
                                 "Pink Floyd", "Hey You")
        self.assertEqual(A.find_suspects(self.conn), [])

    def test_punctuation_difference_not_suspect(self):
        # Apostrophe variants, unicode hyphens, etc. — same song.
        _row(self.conn, "u1", "Bob Dylan", "Hard Rain's A-Gonna Fall",
                                 "Bob Dylan", "Hard Rain’s A‐Gonna Fall")
        self.assertEqual(A.find_suspects(self.conn), [])

    def test_collaboration_credit_difference_not_suspect(self):
        # "Bill Evans Trio; Stan Getz" vs "Bill Evans Trio feat. Stan Getz"
        _row(self.conn, "u1",
             "Bill Evans Trio; Stan Getz",   "Funkallero",
             "Bill Evans Trio feat. Stan Getz", "Funkallero")
        self.assertEqual(A.find_suspects(self.conn), [])

    def test_bracketed_qualifier_not_suspect(self):
        # 'Hey You' vs 'Hey You (Live, Wembley 1990)' — should NOT
        # be flagged because the bracketed annotation is stripped.
        _row(self.conn, "u1",
             "Pink Floyd", "Hey You (Live, Wembley 1990)",
             "Pink Floyd", "Hey You")
        self.assertEqual(A.find_suspects(self.conn), [])

    def test_completely_different_song_suspect(self):
        # Override claims Bob Dylan / Tambourine Man,
        # track is actually Ottawan / Crazy Music — mis-attached.
        _row(self.conn, "u1",
             "Ottawan",   "Crazy Music",
             "Bob Dylan", "Mr. Tambourine Man (Album Version)")
        suspects = A.find_suspects(self.conn)
        self.assertEqual(len(suspects), 1)
        self.assertEqual(suspects[0]["url"], "u1")

    def test_same_album_different_song_suspect(self):
        # 3 Doors Down — Kryptonite vs Down Poison — TRUE d-id
        # collision case from real data. Track is Down Poison;
        # override claims Kryptonite.
        _row(self.conn, "u1",
             "3 Doors Down", "Down Poison",
             "3 Doors Down", "Kryptonite")
        # Artist matches → ar_score=1.0; title score is low.
        # By design we require BOTH to be low; this should NOT flag
        # under the default floor — same-album-collisions are accepted
        # as "the artist is right, only the song id drifted, and the
        # next AcoustID pass will catch it anyway via fingerprint."
        suspects = A.find_suspects(self.conn)
        self.assertEqual(suspects, [],
                         "Same-artist-different-title is currently NOT "
                         "treated as suspect (conservative).")

    def test_manual_override_never_suspect(self):
        # User-edited override stays. Even if the metadata wildly
        # disagrees with the track, the user knows best.
        _row(self.conn, "u1",
             "Bob Dylan", "Mr. Tambourine Man",
             "Ottawan",   "Crazy Music",
             src='manual')
        self.assertEqual(A.find_suspects(self.conn), [])


class TestDelete(unittest.TestCase):

    def setUp(self):
        self._fd, self._p = tempfile.mkstemp(suffix=".db")
        os.close(self._fd)
        self.conn = _make_db(Path(self._p))

    def tearDown(self):
        self.conn.close()
        os.unlink(self._p)

    def test_delete_removes_suspect_rows(self):
        _row(self.conn, "u_good", "PF", "Hey You", "PF", "Hey You")
        _row(self.conn, "u_bad",  "Ottawan", "Crazy Music",
                                  "Bob Dylan", "Mr. Tambourine Man")
        suspects = A.find_suspects(self.conn)
        self.assertEqual(len(suspects), 1)
        n = A.delete_suspects(self.conn, suspects)
        self.conn.commit()
        self.assertEqual(n, 1)
        # u_good is still there; u_bad is gone.
        rows = {r[0] for r in self.conn.execute(
            "SELECT url FROM metadata_overrides")}
        self.assertEqual(rows, {"u_good"})

    def test_delete_leaves_manual_alone_even_if_in_list(self):
        # If somehow a manual override is in the suspect list (shouldn't
        # happen because find_suspects filters them out), the DELETE
        # WHERE source='acoustid' guard prevents collateral damage.
        _row(self.conn, "u1", "Track", "T", "Override", "O", src='manual')
        suspects = [{"url": "u1"}]
        A.delete_suspects(self.conn, suspects)
        self.conn.commit()
        n = self.conn.execute(
            "SELECT COUNT(*) FROM metadata_overrides WHERE source='manual'"
        ).fetchone()[0]
        self.assertEqual(n, 1, "manual override must survive delete pass")


if __name__ == "__main__":
    unittest.main()
