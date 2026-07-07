#!/usr/bin/env python3
"""
tests/test_video_people.py — Plan B: the `video_people` table + LibraryDB
methods (persons recognised by Immich, synced by tools/immich_people_sync.py).
Throw-away temp DB; no network.

Contract:
  * video_people_replace(person, …) has SYNC semantics — it replaces that
    person's whole row set, so a re-sync drops videos Immich no longer
    lists for the person.
  * Rows survive clear_videos() (force rescan) like the location
    overrides; browse joins simply show nothing while the index rebuilds.
  * Browse methods only count/list videos that exist for the udn.

Run: python3 -m unittest tests.test_video_people -v
"""
import os
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB

UDN = "uuid:localfs-movies"


def _vrow(vid, title, created):
    return {
        "id": vid, "udn": UDN, "url": f"http://h/localfs/video/{vid}",
        "title": title, "file_path": f"/m/{vid}.mov", "folder": "",
        "duration": 12.5, "width": 1920, "height": 1080,
        "vcodec": "hevc", "acodec": "aac", "container": "mov",
        "mime": "video/quicktime", "size": 1000, "mtime": 1.0,
        "created": created, "location": None, "location_name": None,
        "country": None, "poster": None,
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        self.db.upsert_videos(UDN, [
            _vrow("v1", "clip1", "2026-06-01T10:00:00Z"),
            _vrow("v2", "clip2", "2026-06-02T10:00:00Z"),
            _vrow("v3", "clip3", "2026-06-03T10:00:00Z"),
        ])

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)


class TestVideoPeople(_Base):
    def test_replace_and_list(self):
        n = self.db.video_people_replace("Anna", "p-1", ["v1", "v2"])
        self.assertEqual(n, 2)
        self.db.video_people_replace("Bob", "p-2", ["v2"])
        rows = self.db.video_people_list(UDN)
        self.assertEqual([(r["person"], r["count"]) for r in rows],
                         [("Anna", 2), ("Bob", 1)])

    def test_replace_is_a_sync(self):
        self.db.video_people_replace("Anna", "p-1", ["v1", "v2"])
        self.db.video_people_replace("Anna", "p-1", ["v3"])   # re-sync
        vids = self.db.videos_by_person(UDN, "Anna")
        self.assertEqual([v["id"] for v in vids], ["v3"])

    def test_replace_empty_removes_person(self):
        self.db.video_people_replace("Anna", "p-1", ["v1"])
        self.db.video_people_replace("Anna", "p-1", [])
        self.assertEqual(self.db.video_people_list(UDN), [])

    def test_videos_by_person_newest_first(self):
        self.db.video_people_replace("Anna", "p-1", ["v1", "v3", "v2"])
        vids = self.db.videos_by_person(UDN, "Anna")
        self.assertEqual([v["id"] for v in vids], ["v3", "v2", "v1"])

    def test_unknown_video_ids_dont_count_in_browse(self):
        self.db.video_people_replace("Anna", "p-1", ["v1", "ghost"])
        rows = self.db.video_people_list(UDN)
        self.assertEqual([(r["person"], r["count"]) for r in rows],
                         [("Anna", 1)])
        self.assertEqual(
            [v["id"] for v in self.db.videos_by_person(UDN, "Anna")],
            ["v1"])

    def test_people_map_for_payload(self):
        self.db.video_people_replace("Anna", "p-1", ["v1", "v2"])
        self.db.video_people_replace("Bob", "p-2", ["v2"])
        m = self.db.video_people_map(UDN)
        self.assertEqual(m["v1"], ["Anna"])
        self.assertEqual(m["v2"], ["Anna", "Bob"])     # A-Z within a video
        self.assertNotIn("v3", m)

    def test_survives_clear_videos(self):
        self.db.video_people_replace("Anna", "p-1", ["v1"])
        self.db.clear_videos(UDN)
        # browse shows nothing while the index is empty…
        self.assertEqual(self.db.video_people_list(UDN), [])
        # …but the rows are still there once the video row returns
        self.db.upsert_videos(UDN, [_vrow("v1", "clip1",
                                          "2026-06-01T10:00:00Z")])
        self.assertEqual(self.db.video_people_list(UDN),
                         [{"person": "Anna", "count": 1}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
