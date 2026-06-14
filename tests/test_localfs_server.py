#!/usr/bin/env python3
"""
tests/test_localfs_server.py — the in-process file server (Phase 3
of the AssetUPnP migration).

Coverage:

  1. Pure helpers — `_parse_range_header` handles every RFC 7233
     shape we care about (`bytes=N-M`, `bytes=N-`, `bytes=-N`),
     rejects unsatisfiable / malformed inputs.
  2. DLNA-header mapping — content family → DLNA Profile Name.
  3. End-to-end against a real `ThreadingHTTPServer` bound to an
     ephemeral port:
       * Full GET returns 200 + the entire file. `sha256` of bytes
         served equals `sha256` of the source file — the P3
         bit-perfect proof.
       * Range GET (`bytes=0-1023`) returns 206 + `Content-Range:
         bytes 0-1023/<size>` + exactly 1024 bytes.
       * Range past EOF returns 416 with `Content-Range: bytes
         */<size>`.
       * HEAD returns the same headers, zero body.
       * 404 for unknown track id.
       * Content-Type from the DB; DLNA headers present.

Run standalone:
    python3 -m unittest tests.test_localfs_server -v
"""
import hashlib
import http.client
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB
from dlna_localfs_server import (
    _dlna_headers_for_mime,
    _parse_range_header,
    start_server,
)


# ── Pure helpers ─────────────────────────────────────────────────

class TestParseRangeHeader(unittest.TestCase):

    def test_plain_byte_range(self):
        self.assertEqual(_parse_range_header("bytes=0-1023", 100_000),
                         (0, 1023))

    def test_open_ended_range_clamps_to_eof(self):
        self.assertEqual(_parse_range_header("bytes=500-", 1000),
                         (500, 999))

    def test_suffix_range_returns_last_n_bytes(self):
        self.assertEqual(_parse_range_header("bytes=-50", 1000),
                         (950, 999))

    def test_suffix_range_clamped_to_size(self):
        # Asking for the last 100 bytes of a 50-byte file → all 50.
        self.assertEqual(_parse_range_header("bytes=-100", 50),
                         (0, 49))

    def test_end_past_eof_clamps(self):
        self.assertEqual(_parse_range_header("bytes=100-9999", 1000),
                         (100, 999))

    def test_start_past_eof_returns_none(self):
        # 416 territory; caller emits Content-Range: bytes */<size>.
        self.assertIsNone(_parse_range_header("bytes=2000-3000", 1000))

    def test_inverted_range_returns_none(self):
        self.assertIsNone(_parse_range_header("bytes=500-100", 1000))

    def test_garbled_string_returns_none(self):
        self.assertIsNone(_parse_range_header("nonsense", 1000))
        self.assertIsNone(_parse_range_header("bytes=", 1000))
        self.assertIsNone(_parse_range_header("bytes=abc-xyz", 1000))

    def test_multipart_unsupported(self):
        # We deliberately reject multipart — Naim doesn't issue them.
        self.assertIsNone(
            _parse_range_header("bytes=0-100,200-300", 1000))

    def test_empty_returns_none(self):
        self.assertIsNone(_parse_range_header("", 1000))


class TestDlnaHeadersForMime(unittest.TestCase):

    def test_flac_maps_to_flac_pn(self):
        h = _dlna_headers_for_mime("audio/flac")
        self.assertIn("DLNA.ORG_PN=FLAC", h["contentFeatures.dlna.org"])

    def test_legacy_x_flac_also_maps_to_flac(self):
        h = _dlna_headers_for_mime("audio/x-flac")
        self.assertIn("DLNA.ORG_PN=FLAC", h["contentFeatures.dlna.org"])

    def test_mp3_maps_to_mp3_pn(self):
        h = _dlna_headers_for_mime("audio/mpeg")
        self.assertIn("DLNA.ORG_PN=MP3", h["contentFeatures.dlna.org"])

    def test_aac_maps_to_aac_iso(self):
        h = _dlna_headers_for_mime("audio/aac")
        self.assertIn("AAC", h["contentFeatures.dlna.org"])

    def test_wav_maps_to_lpcm(self):
        h = _dlna_headers_for_mime("audio/x-wav")
        self.assertIn("LPCM", h["contentFeatures.dlna.org"])

    def test_dsd_maps_to_dsd(self):
        h = _dlna_headers_for_mime("audio/x-dsf")
        self.assertIn("DSD", h["contentFeatures.dlna.org"])

    def test_op_01_advertises_range_support(self):
        h = _dlna_headers_for_mime("audio/flac")
        self.assertIn("DLNA.ORG_OP=01", h["contentFeatures.dlna.org"])

    def test_transfer_mode_streaming(self):
        h = _dlna_headers_for_mime("audio/flac")
        self.assertEqual(h["transferMode.dlna.org"], "Streaming")


# ── End-to-end against a real HTTP server ────────────────────────

class _Fixture:
    """Spin up a real file server on an ephemeral port + seed
    library.db with one known track. Returns the tuple
    (host, port, track_id, file_bytes, db_path, srv) for the test."""

    @staticmethod
    def setup() -> tuple:
        # Create the file we want to serve.
        body = b"".join(bytes([i % 256]) for i in range(4096))   # 4 KB
        file_dir = tempfile.mkdtemp(prefix="localfs-srv-")
        file_path = Path(file_dir) / "song.flac"
        file_path.write_bytes(body)

        # Seed library.db with a single track row pointing at it.
        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        db = LibraryDB(db_file=db_path)
        track_id = "fixtureFLAC0001"
        with db._pool.write() as conn:
            conn.execute(
                "INSERT INTO tracks (udn, obj_id, url, title, artist, "
                "album, file_path, mime) VALUES (?,?,?,?,?,?,?,?)",
                ("uuid:localfs-test", track_id,
                 f"localfs://uuid:localfs-test/{track_id}",
                 "Smoke", "Tester", "Smokes",
                 str(file_path), "audio/flac"))
        db._pool.close()       # release write lock

        # Start the server on an ephemeral port (bound to 127.0.0.1
        # only — the test runs in-process).
        srv = start_server(db_path, port=0, host="127.0.0.1",
                           allowed_roots=(file_dir,))
        host, port = srv.server_address

        return {
            "host":      host,
            "port":      port,
            "track_id":  track_id,
            "body":      body,
            "file_path": str(file_path),
            "file_dir":  file_dir,
            "db_path":   db_path,
            "srv":       srv,
        }

    @staticmethod
    def teardown(fx: dict):
        fx["srv"].shutdown()
        fx["srv"].server_close()
        try:
            os.unlink(fx["file_path"])
        except OSError:
            pass
        try:
            os.rmdir(fx["file_dir"])
        except OSError:
            pass
        try:
            os.unlink(fx["db_path"])
        except OSError:
            pass


class TestServerEndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = _Fixture.setup()

    @classmethod
    def tearDownClass(cls):
        _Fixture.teardown(cls.fx)

    def _conn(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.fx["host"], self.fx["port"],
                                          timeout=5)

    def test_full_get_returns_200_and_full_bytes(self):
        c = self._conn()
        c.request("GET", f"/localfs/stream/{self.fx['track_id']}")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        body = r.read()
        c.close()
        self.assertEqual(len(body), len(self.fx["body"]))
        self.assertEqual(body, self.fx["body"],
                         "Served bytes must equal source bytes — "
                         "P3 bit-perfect proof")

    def test_full_get_sha256_matches_source(self):
        c = self._conn()
        c.request("GET", f"/localfs/stream/{self.fx['track_id']}")
        r = c.getresponse()
        served = r.read()
        c.close()
        served_h = hashlib.sha256(served).hexdigest()
        source_h = hashlib.sha256(self.fx["body"]).hexdigest()
        self.assertEqual(served_h, source_h)

    def test_range_returns_206_with_content_range(self):
        c = self._conn()
        c.request("GET", f"/localfs/stream/{self.fx['track_id']}",
                  headers={"Range": "bytes=0-1023"})
        r = c.getresponse()
        self.assertEqual(r.status, 206)
        self.assertEqual(r.getheader("Content-Length"), "1024")
        self.assertEqual(r.getheader("Content-Range"),
                         f"bytes 0-1023/{len(self.fx['body'])}")
        body = r.read()
        c.close()
        self.assertEqual(len(body), 1024)
        self.assertEqual(body, self.fx["body"][:1024])

    def test_range_mid_file(self):
        c = self._conn()
        c.request("GET", f"/localfs/stream/{self.fx['track_id']}",
                  headers={"Range": "bytes=2000-2199"})
        r = c.getresponse()
        self.assertEqual(r.status, 206)
        body = r.read()
        c.close()
        self.assertEqual(len(body), 200)
        self.assertEqual(body, self.fx["body"][2000:2200])

    def test_range_open_ended(self):
        c = self._conn()
        c.request("GET", f"/localfs/stream/{self.fx['track_id']}",
                  headers={"Range": "bytes=3072-"})
        r = c.getresponse()
        self.assertEqual(r.status, 206)
        body = r.read()
        c.close()
        self.assertEqual(len(body), 1024)
        self.assertEqual(body, self.fx["body"][3072:])

    def test_range_past_eof_returns_416(self):
        c = self._conn()
        c.request("GET", f"/localfs/stream/{self.fx['track_id']}",
                  headers={"Range": "bytes=9999-99999"})
        r = c.getresponse()
        self.assertEqual(r.status, 416)
        self.assertEqual(r.getheader("Content-Range"),
                         f"bytes */{len(self.fx['body'])}")
        r.read()  # drain
        c.close()

    def test_head_returns_headers_only(self):
        c = self._conn()
        c.request("HEAD", f"/localfs/stream/{self.fx['track_id']}")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Length"),
                         str(len(self.fx["body"])))
        body = r.read()
        c.close()
        self.assertEqual(body, b"")

    def test_unknown_track_id_returns_404(self):
        c = self._conn()
        c.request("GET", "/localfs/stream/doesnotexist1234")
        r = c.getresponse()
        self.assertEqual(r.status, 404)
        r.read()  # drain
        c.close()

    def test_unknown_path_returns_404(self):
        c = self._conn()
        c.request("GET", "/some/other/path")
        r = c.getresponse()
        self.assertEqual(r.status, 404)
        r.read()
        c.close()

    def test_response_advertises_accept_ranges(self):
        c = self._conn()
        c.request("HEAD", f"/localfs/stream/{self.fx['track_id']}")
        r = c.getresponse()
        self.assertEqual(r.getheader("Accept-Ranges"), "bytes")
        r.read()
        c.close()

    def test_response_includes_dlna_headers(self):
        c = self._conn()
        c.request("HEAD", f"/localfs/stream/{self.fx['track_id']}")
        r = c.getresponse()
        cf = r.getheader("contentFeatures.dlna.org") or ""
        tm = r.getheader("transferMode.dlna.org") or ""
        r.read()
        c.close()
        self.assertIn("DLNA.ORG_PN=FLAC", cf)
        self.assertIn("DLNA.ORG_OP=01", cf)
        self.assertEqual(tm, "Streaming")

    def test_response_advertises_content_type_from_db(self):
        c = self._conn()
        c.request("HEAD", f"/localfs/stream/{self.fx['track_id']}")
        r = c.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "audio/flac")
        r.read()
        c.close()


class TestPathTraversalDefence(unittest.TestCase):
    """If file_path in the DB points OUTSIDE the allowed_roots,
    refuse to serve. Defensive layer in case anything downstream
    pollutes the tracks table."""

    def test_path_outside_allowed_roots_blocked(self):
        # Same fixture shape as TestServerEndToEnd, but lying about
        # the roots so the (legitimate) file_path is no longer
        # underneath any of them.
        body = b"x" * 100
        file_dir = tempfile.mkdtemp(prefix="localfs-srv-")
        file_path = Path(file_dir) / "song.flac"
        file_path.write_bytes(body)

        db_fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        db = LibraryDB(db_file=db_path)
        track_id = "tracksrv0001"
        with db._pool.write() as conn:
            conn.execute(
                "INSERT INTO tracks (udn, obj_id, url, title, artist, "
                "album, file_path, mime) VALUES (?,?,?,?,?,?,?,?)",
                ("uuid:localfs-test", track_id,
                 f"localfs://uuid:localfs-test/{track_id}",
                 "x", "x", "x",
                 str(file_path), "audio/flac"))
        db._pool.close()

        # Use a different, unrelated allowed_root.
        bogus_root = tempfile.mkdtemp(prefix="localfs-roots-")
        srv = start_server(db_path, port=0, host="127.0.0.1",
                           allowed_roots=(bogus_root,))
        host, port = srv.server_address
        try:
            c = http.client.HTTPConnection(host, port, timeout=5)
            c.request("GET", f"/localfs/stream/{track_id}")
            r = c.getresponse()
            self.assertEqual(r.status, 403)
            r.read()
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()
            os.unlink(file_path)
            os.rmdir(file_dir)
            os.rmdir(bogus_root)
            os.unlink(db_path)


class TestArtRoute(unittest.TestCase):
    """GET/HEAD /localfs/art/<id> — embedded cover serving. Byte
    extraction is mocked (patching the module symbol the handler thread
    calls) so the test needs no cover-embedded file on disk. The
    `_Fixture` row's file_path is a real file under allowed_roots, so
    the path-traversal check passes and the only variable is what
    `_extract_art_bytes` returns."""

    _PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)   # valid PNG magic + filler

    @classmethod
    def setUpClass(cls):
        cls.fx = _Fixture.setup()

    @classmethod
    def tearDownClass(cls):
        _Fixture.teardown(cls.fx)

    def _conn(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection(self.fx["host"], self.fx["port"],
                                          timeout=5)

    def test_art_returns_image_bytes(self):
        with patch("dlna_localfs_server._extract_art_bytes",
                   return_value=(self._PNG, "image/png")):
            c = self._conn()
            c.request("GET", f"/localfs/art/{self.fx['track_id']}")
            r = c.getresponse()
            body = r.read()
            c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "image/png")
        self.assertEqual(body, self._PNG)
        self.assertEqual(int(r.getheader("Content-Length")), len(self._PNG))

    def test_art_head_has_headers_no_body(self):
        with patch("dlna_localfs_server._extract_art_bytes",
                   return_value=(self._PNG, "image/png")):
            c = self._conn()
            c.request("HEAD", f"/localfs/art/{self.fx['track_id']}")
            r = c.getresponse()
            body = r.read()
            c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "image/png")
        self.assertEqual(body, b"")

    def test_art_unknown_id_404(self):
        with patch("dlna_localfs_server._extract_art_bytes",
                   return_value=(self._PNG, "image/png")):
            c = self._conn()
            c.request("GET", "/localfs/art/deadbeefdeadbeef")
            r = c.getresponse()
            r.read()
            c.close()
        self.assertEqual(r.status, 404)

    def test_art_no_embedded_art_404(self):
        # id resolves to a real file, but the file carries no cover.
        with patch("dlna_localfs_server._extract_art_bytes",
                   return_value=None):
            c = self._conn()
            c.request("GET", f"/localfs/art/{self.fx['track_id']}")
            r = c.getresponse()
            r.read()
            c.close()
        self.assertEqual(r.status, 404)


class TestDlnaHeadersForVideo(unittest.TestCase):
    def test_video_has_no_pn_but_has_op_flags(self):
        h = _dlna_headers_for_mime("video/mp4")
        cf = h["contentFeatures.dlna.org"]
        self.assertNotIn("DLNA.ORG_PN=", cf)     # no wrong codec PN
        self.assertIn("DLNA.ORG_OP=01", cf)       # Range advertised
        self.assertEqual(h["transferMode.dlna.org"], "Streaming")


class TestVideoServeEndToEnd(unittest.TestCase):
    """GET /localfs/video/<id> serves bytes from the `videos` table with the
    same Range machinery as audio (used by the LG TV / PWA video player)."""

    @classmethod
    def setUpClass(cls):
        body = bytes((i % 256) for i in range(4096))
        cls.vdir = tempfile.mkdtemp(prefix="gwmovies-")
        fp = Path(cls.vdir) / "clip.mp4"
        fp.write_bytes(body)
        db_fd, cls.db_path = tempfile.mkstemp(suffix=".db")
        os.close(db_fd)
        db = LibraryDB(db_file=cls.db_path)
        cls.vid = "vid0001"
        db.upsert_videos("uuid:localfs-movies", [{
            "id": cls.vid, "udn": "uuid:localfs-movies",
            "url": f"http://h/localfs/video/{cls.vid}", "title": "Clip",
            "file_path": str(fp), "folder": "", "duration": 1.0,
            "width": 1920, "height": 1080, "vcodec": "h264", "acodec": "aac",
            "container": "mp4", "mime": "video/mp4", "size": len(body),
            "mtime": 1.0, "created": "2026-06-14T14:30:00Z",
            "location": None, "location_name": None, "poster": None,
        }])
        db._pool.close()
        cls.srv = start_server(cls.db_path, port=0, host="127.0.0.1",
                               allowed_roots=(cls.vdir,))
        cls.host, cls.port = cls.srv.server_address
        cls.body = body

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); cls.srv.server_close()

    def _conn(self):
        return http.client.HTTPConnection(self.host, self.port, timeout=5)

    def test_full_get(self):
        c = self._conn(); c.request("GET", f"/localfs/video/{self.vid}")
        r = c.getresponse(); data = r.read(); c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(data, self.body)
        self.assertEqual(r.getheader("Content-Type"), "video/mp4")
        self.assertIn("DLNA.ORG_OP=01", r.getheader("contentFeatures.dlna.org"))

    def test_range(self):
        c = self._conn()
        c.request("GET", f"/localfs/video/{self.vid}", headers={"Range": "bytes=0-1023"})
        r = c.getresponse(); data = r.read(); c.close()
        self.assertEqual(r.status, 206)
        self.assertEqual(len(data), 1024)
        self.assertEqual(r.getheader("Content-Range"),
                         f"bytes 0-1023/{len(self.body)}")

    def test_unknown_video_404(self):
        c = self._conn(); c.request("GET", "/localfs/video/deadbeef")
        r = c.getresponse(); r.read(); c.close()
        self.assertEqual(r.status, 404)


if __name__ == "__main__":
    unittest.main()
