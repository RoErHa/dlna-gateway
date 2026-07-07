#!/usr/bin/env python3
"""Tests for tools/immich_people_sync.py — videos-per-person from the
Immich REST API (Plan B, 2026-07-07).

No network, no live Immich: the HTTP layer is an injected `fetch`
callable; file hashing runs over throw-away tempdirs; DB writes over a
throw-away LibraryDB.

Run: python3 -m unittest tools.test_immich_people_sync -v
"""
import base64
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tools.immich_people_sync import (
    b64_to_hex, fetch_people, fetch_person_video_assets, build_sha1_index,
    build_name_size_index, sync_people)
from dlna_library import LibraryDB
import dlna_video_index

UDN = "uuid:localfs-movies"


def _sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _sha1_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


class TestChecksum(unittest.TestCase):
    def test_b64_to_hex_roundtrip(self):
        self.assertEqual(b64_to_hex(_sha1_b64(b"abc")), _sha1_hex(b"abc"))

    def test_garbage_returns_empty(self):
        self.assertEqual(b64_to_hex("!!!not-base64!!!"), "")
        self.assertEqual(b64_to_hex(""), "")


class TestFetchPeople(unittest.TestCase):
    def test_named_people_only_and_paginated(self):
        pages = {
            1: {"people": [{"id": "p1", "name": "Anna"},
                           {"id": "p2", "name": ""}],       # unnamed
                "hasNextPage": True},
            2: {"people": [{"id": "p3", "name": "Bob"}],
                "hasNextPage": False},
        }
        calls = []

        def fetch(method, path, body=None):
            calls.append(path)
            page = int(path.split("page=")[1].split("&")[0])
            return pages[page]

        people = fetch_people(fetch)
        self.assertEqual([(p["id"], p["name"]) for p in people],
                         [("p1", "Anna"), ("p3", "Bob")])
        self.assertEqual(len(calls), 2)

    def test_plain_list_response_tolerated(self):
        people = fetch_people(
            lambda m, p, body=None: [{"id": "p1", "name": "Anna"}])
        self.assertEqual(people, [{"id": "p1", "name": "Anna"}])


class TestFetchAssets(unittest.TestCase):
    def test_paginates_until_next_page_is_none(self):
        pages = {
            "1": {"assets": {"items": [{"id": "a1", "checksum": "AA=="}],
                             "nextPage": "2"}},
            "2": {"assets": {"items": [{"id": "a2", "checksum": "BB=="}],
                             "nextPage": None}},
        }
        bodies = []

        def fetch(method, path, body=None):
            self.assertEqual(method, "POST")
            bodies.append(body)
            return pages[str(body.get("page", 1))]

        assets = fetch_person_video_assets(fetch, "p1")
        self.assertEqual([a["id"] for a in assets], ["a1", "a2"])
        self.assertEqual(bodies[0]["personIds"], ["p1"])
        self.assertEqual(bodies[0]["type"], "VIDEO")


class TestSha1Index(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.TemporaryDirectory()
        self.cache = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.cache.close()

    def tearDown(self):
        self.dest.cleanup()
        os.unlink(self.cache.name)

    def _write(self, name, data):
        p = Path(self.dest.name) / name
        p.write_bytes(data)
        return p

    def test_index_maps_hex_sha1_to_rel_path(self):
        self._write("a.mp4", b"AAA")
        self._write("b.mov", b"BBB")
        self._write("notes.txt", b"skip me")          # non-video skipped
        conn = sqlite3.connect(self.cache.name)
        idx = build_sha1_index(Path(self.dest.name), conn)
        self.assertEqual(idx[_sha1_hex(b"AAA")], "a.mp4")
        self.assertEqual(idx[_sha1_hex(b"BBB")], "b.mov")
        self.assertEqual(len(idx), 2)

    def test_cache_avoids_rehash(self):
        self._write("a.mp4", b"AAA")
        conn = sqlite3.connect(self.cache.name)
        build_sha1_index(Path(self.dest.name), conn)
        row = conn.execute("SELECT sha1 FROM video_sha1 WHERE path LIKE "
                           "'%a.mp4'").fetchone()
        self.assertEqual(row[0], _sha1_hex(b"AAA"))
        # poison the cached value; unchanged (size, mtime) must be trusted
        conn.execute("UPDATE video_sha1 SET sha1='cached-value'")
        conn.commit()
        idx = build_sha1_index(Path(self.dest.name), conn)
        self.assertIn("cached-value", idx)


class TestNameSizeIndex(unittest.TestCase):
    """Fallback matching (2026-07-07): live data proved Immich's stored
    checksums can differ from the bytes on disk (files metadata-edited in
    place after indexing — same size, different SHA1). (normalized name,
    exact size) is the fallback key; the importer's ' (2)' collision
    suffix is stripped so a renamed copy still matches."""

    def setUp(self):
        self.dest = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dest.cleanup()

    def _write(self, name, data):
        (Path(self.dest.name) / name).write_bytes(data)

    def test_key_is_lowercase_name_plus_size(self):
        self._write("IMG_1.MOV", b"AAAA")
        idx = build_name_size_index(Path(self.dest.name))
        self.assertEqual(idx[("img_1.mov", 4)], "IMG_1.MOV")

    def test_collision_suffix_stripped(self):
        self._write("IMG_1 (2).MOV", b"AAAA")
        idx = build_name_size_index(Path(self.dest.name))
        self.assertEqual(idx[("img_1.mov", 4)], "IMG_1 (2).MOV")

    def test_ambiguous_key_excluded(self):
        self._write("IMG_1.MOV", b"AAAA")
        self._write("IMG_1 (2).MOV", b"BBBBB")   # different size → distinct
        self._write("IMG_1 (3).MOV", b"CCCCCC")
        idx = build_name_size_index(Path(self.dest.name))
        self.assertEqual(len(idx), 3)            # all distinct (sizes differ)
        self._write("IMG_2.MOV", b"XXXX")
        self._write("IMG_2 (2).MOV", b"YYYY")    # same size, same norm name
        idx = build_name_size_index(Path(self.dest.name))
        self.assertNotIn(("img_2.mov", 4), idx)  # ambiguous → dropped
        self.assertEqual(len(idx), 3)            # IMG_1 variants unaffected


class TestSyncPeople(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = LibraryDB(self.tmp.name)
        # two indexed videos whose ids derive from their rel paths
        self.vid_a = dlna_video_index.video_id("a.mp4")
        self.vid_b = dlna_video_index.video_id("b.mov")
        for vid, rel in ((self.vid_a, "a.mp4"), (self.vid_b, "b.mov")):
            self.db.upsert_videos(UDN, [{
                "id": vid, "udn": UDN, "url": f"http://h/{vid}",
                "title": rel, "file_path": f"/gw/{rel}", "folder": "",
                "duration": 1, "width": 1, "height": 1, "vcodec": "h264",
                "acodec": "aac", "container": "mp4", "mime": "video/mp4",
                "size": 3, "mtime": 1.0, "created": "2026-06-01T10:00:00Z",
                "location": None, "location_name": None, "country": None,
                "poster": None}])
        self.people = [{"id": "p1", "name": "Anna"},
                       {"id": "p2", "name": "Bob"}]
        self.sha1_index = {_sha1_hex(b"AAA"): "a.mp4",
                           _sha1_hex(b"BBB"): "b.mov"}
        self.assets = {
            "p1": [{"id": "x1", "checksum": _sha1_b64(b"AAA")},
                   {"id": "x2", "checksum": _sha1_b64(b"BBB")},
                   {"id": "x3", "checksum": _sha1_b64(b"EXTERNAL")}],
            "p2": [{"id": "x2", "checksum": _sha1_b64(b"BBB")}],
        }

    def tearDown(self):
        self.db._pool.close()
        os.unlink(self.tmp.name)

    def _fetch(self, method, path, body=None):
        pid = body["personIds"][0]
        return {"assets": {"items": self.assets[pid], "nextPage": None}}

    def test_dry_run_reports_but_does_not_write(self):
        stats = sync_people(self.db, self._fetch, self.people,
                            self.sha1_index, apply=False)
        self.assertEqual(self.db.video_people_list(UDN), [])
        self.assertEqual(stats["matched"], 3)      # Anna 2 + Bob 1
        self.assertEqual(stats["unmatched"], 1)    # the external-lib asset

    def test_apply_writes_rows(self):
        sync_people(self.db, self._fetch, self.people,
                    self.sha1_index, apply=True)
        rows = self.db.video_people_list(UDN)
        self.assertEqual([(r["person"], r["count"]) for r in rows],
                         [("Anna", 2), ("Bob", 1)])
        self.assertEqual(
            [v["id"] for v in self.db.videos_by_person(UDN, "Bob")],
            [self.vid_b])

    def test_resync_drops_stale_links(self):
        sync_people(self.db, self._fetch, self.people,
                    self.sha1_index, apply=True)
        self.assets["p1"] = [{"id": "x2", "checksum": _sha1_b64(b"BBB")}]
        sync_people(self.db, self._fetch, self.people,
                    self.sha1_index, apply=True)
        self.assertEqual(
            [v["id"] for v in self.db.videos_by_person(UDN, "Anna")],
            [self.vid_b])

    def test_name_size_fallback_when_checksum_stale(self):
        """Checksum misses fall back to (name, size) via a per-asset GET —
        the stale-checksum case proven live 2026-07-07."""
        vid_c = dlna_video_index.video_id("IMG_9 (2).MOV")
        self.db.upsert_videos(UDN, [{
            "id": vid_c, "udn": UDN, "url": "http://h/c",
            "title": "IMG_9 (2).MOV", "file_path": "/gw/IMG_9 (2).MOV",
            "folder": "", "duration": 1, "width": 1, "height": 1,
            "vcodec": "h264", "acodec": "aac", "container": "mov",
            "mime": "video/quicktime", "size": 7, "mtime": 1.0,
            "created": "2026-06-01T10:00:00Z", "location": None,
            "location_name": None, "country": None, "poster": None}])
        self.assets["p2"] = [{"id": "x9", "checksum": _sha1_b64(b"STALE")}]
        name_size = {("img_9.mov", 7): "IMG_9 (2).MOV"}

        def fetch(method, path, body=None):
            if method == "GET" and path == "/api/assets/x9":
                return {"originalFileName": "IMG_9.MOV",
                        "exifInfo": {"fileSizeInByte": 7}}
            return self._fetch(method, path, body)

        stats = sync_people(self.db, fetch, self.people, self.sha1_index,
                            name_size_index=name_size, apply=True)
        self.assertEqual(
            [v["id"] for v in self.db.videos_by_person(UDN, "Bob")],
            [vid_c])
        self.assertEqual(stats["matched_by_name"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
