#!/usr/bin/env python3
"""
tests/test_localfs_traversal.py — attacking the LocalFs file server's
containment check.

WHY A SECOND FILE. `tests/test_localfs_server.py` already has a
`TestPathTraversalDefence`, but it asks the easy question: a file under a
completely unrelated root is refused. That passes against a check with a real
hole in it. The 2026-08-20 audit left this surface unexamined; the
public-release plan (Track B1) calls for the adversarial version, because the
file server is reachable by any unauthenticated peer on the LAN — it has to
be, since renderers fetch bytes from it directly.

The one that mattered: containment was `str(resolved).startswith(root)`, a
STRING prefix test, so a sibling directory whose name merely begins with a
root's name was inside. With the deployed roots that is
`/Volumes/SAMDATA/Music` admitting `/Volumes/SAMDATA/Music Videos`, and
`/Volumes/SAMDATA-1TB/Audio_Books` admitting `/Volumes/SAMDATA-1TB/Audio_Books_private`.
Containment is a question about path COMPONENTS, and is now asked as one.

The URL half is separately safe by construction and stays tested here so it
stays that way: the id from the URL is only ever a SQL parameter, and the
path served comes from the database — so `../` in a URL cannot name a file.
"""
import http.client
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_library import LibraryDB          # noqa: E402
from dlna_localfs_server import start_server  # noqa: E402


class _Server:
    """A real file server over a throw-away tree + DB.

    `rows` is {track_id: file_path}; `video_rows` the same for the videos
    table. `roots` is what the server will accept.
    """

    def __init__(self, rows: dict, roots: tuple, video_rows: dict | None = None):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db = LibraryDB(db_file=self.db_path)
        with db._pool.write() as conn:
            for tid, path in rows.items():
                conn.execute(
                    "INSERT INTO tracks (udn, obj_id, url, title, artist, "
                    "album, file_path, mime) VALUES (?,?,?,?,?,?,?,?)",
                    ("uuid:localfs-test", tid,
                     f"localfs://uuid:localfs-test/{tid}",
                     "t", "a", "al", str(path), "audio/flac"))
            for vid, path in (video_rows or {}).items():
                conn.execute(
                    "INSERT INTO videos (id, udn, url, title, file_path, "
                    "mime, added_at) VALUES (?,?,?,?,?,?,?)",
                    (vid, "uuid:localfs-movies",
                     f"localfs://movies/{vid}", "v", str(path),
                     "video/mp4", 0))
        db._pool.close()
        self.srv = start_server(self.db_path, port=0, host="127.0.0.1",
                                allowed_roots=roots)
        self.host, self.port = self.srv.server_address

    def get(self, path: str) -> tuple[int, bytes]:
        c = http.client.HTTPConnection(self.host, self.port, timeout=5)
        # Send the raw path: http.client would otherwise leave it alone, but
        # be explicit that no normalisation happens on our side either.
        c.putrequest("GET", path, skip_host=False, skip_accept_encoding=True)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        status = r.status
        c.close()
        return status, body

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.unlink(self.db_path)


class TestSiblingDirectoryIsNotInsideTheRoot(unittest.TestCase):
    """A directory whose NAME merely starts with the root's name is outside
    it. This is the finding: it used to be served."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="localfs-sib-")).resolve()
        self.root = self.base / "Music"
        self.sibling = self.base / "Music Videos"      # not under Music/
        self.root.mkdir()
        self.sibling.mkdir()
        (self.root / "ok.flac").write_bytes(b"legitimate")
        (self.sibling / "leak.flac").write_bytes(b"SHOULD NOT BE SERVED")
        self.srv = _Server(
            rows={"idok": self.root / "ok.flac",
                  "idleak": self.sibling / "leak.flac"},
            roots=(str(self.root),))

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_the_sibling_file_is_refused(self):
        status, body = self.srv.get("/localfs/stream/idleak")
        self.assertEqual(status, 403, "a sibling directory is not the root")
        self.assertNotIn(b"SHOULD NOT BE SERVED", body)

    def test_the_real_file_still_serves(self):
        """The half that makes a containment fix worth having: it must not
        break the files it exists to serve."""
        status, body = self.srv.get("/localfs/stream/idok")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"legitimate")

    def test_the_art_route_refuses_it_too(self):
        """The check was copy-pasted into the art route, so the hole was
        too. Both now go through one helper."""
        status, _ = self.srv.get("/localfs/art/idleak")
        self.assertEqual(status, 403)


class TestSymlinkCannotEscape(unittest.TestCase):
    """A symlink is followed BEFORE the containment test, so a link planted
    inside the root cannot be used to read outside it."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="localfs-lnk-")).resolve()
        self.root = self.base / "music"
        self.outside = self.base / "outside"
        self.root.mkdir()
        self.outside.mkdir()
        (self.outside / "secret.flac").write_bytes(b"SECRET")
        (self.root / "escape.flac").symlink_to(self.outside / "secret.flac")
        (self.root / "real.flac").write_bytes(b"inside")
        (self.root / "inside-link.flac").symlink_to(self.root / "real.flac")
        self.srv = _Server(
            rows={"idesc": self.root / "escape.flac",
                  "idin": self.root / "inside-link.flac"},
            roots=(str(self.root),))

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_symlink_pointing_outside_is_refused(self):
        status, body = self.srv.get("/localfs/stream/idesc")
        self.assertEqual(status, 403)
        self.assertNotIn(b"SECRET", body)

    def test_symlink_pointing_inside_still_serves(self):
        status, body = self.srv.get("/localfs/stream/idin")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"inside")


class TestTheUrlCannotNameAFile(unittest.TestCase):
    """The id in the URL is only ever a SQL parameter; the path served comes
    from the database. So traversal in the URL resolves to nothing at all —
    it must stay 404, and must never be 200 or 500."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="localfs-url-")).resolve()
        (self.base / "ok.flac").write_bytes(b"ok")
        self.srv = _Server(rows={"idok": self.base / "ok.flac"},
                           roots=(str(self.base),))

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_dot_dot_segments(self):
        for p in ("/localfs/stream/../../../../etc/passwd",
                  "/localfs/stream/..%2f..%2f..%2fetc%2fpasswd",
                  "/localfs/stream/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                  "/localfs/stream/....//....//etc/passwd"):
            with self.subTest(path=p):
                status, body = self.srv.get(p)
                self.assertEqual(status, 404)
                self.assertNotIn(b"root:", body)

    def test_absolute_path_as_id(self):
        for p in ("/localfs/stream//etc/passwd",
                  "/localfs/stream/%2fetc%2fpasswd"):
            with self.subTest(path=p):
                status, _ = self.srv.get(p)
                self.assertEqual(status, 404)

    def test_hostile_bytes_in_the_id_do_not_500(self):
        """A NUL or a newline must be a miss, not a traceback."""
        for p in ("/localfs/stream/%00",
                  "/localfs/stream/id%00.flac",
                  "/localfs/stream/%0d%0aX-Injected:%20yes",
                  "/localfs/stream/" + "A" * 5000):
            with self.subTest(path=p):
                status, _ = self.srv.get(p)
                self.assertLess(status, 500, f"{p} produced {status}")

    def test_poster_route_cannot_escape_its_directory(self):
        """`/localfs/poster/<id>` builds a filesystem path from the id, so it
        is the one route where the URL DOES reach the filesystem."""
        for p in ("/localfs/poster/../../../../etc/passwd",
                  "/localfs/poster/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                  "/localfs/poster/..",
                  "/localfs/poster/"):
            with self.subTest(path=p):
                status, body = self.srv.get(p)
                self.assertEqual(status, 404)
                self.assertNotIn(b"root:", body)


class TestVideoRouteIsContainedToo(unittest.TestCase):
    """/localfs/video/<id> resolves against a different table but shares the
    containment check — so it must share the fix."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="localfs-vid-")).resolve()
        self.root = self.base / "Movies"
        self.sibling = self.base / "Movies Private"
        self.root.mkdir()
        self.sibling.mkdir()
        (self.sibling / "leak.mp4").write_bytes(b"SHOULD NOT BE SERVED")
        self.srv = _Server(rows={}, roots=(str(self.root),),
                           video_rows={"vleak": self.sibling / "leak.mp4"})

    def tearDown(self):
        self.srv.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def test_video_outside_the_root_is_refused(self):
        status, body = self.srv.get("/localfs/video/vleak")
        self.assertEqual(status, 403)
        self.assertNotIn(b"SHOULD NOT BE SERVED", body)


class TestVideoLinkException(unittest.TestCase):
    """GWMovies may link to Immich's originals. This is the only hole in the
    containment rule, so every way of abusing it is attacked here."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="localfs-link-")).resolve()
        self.root = self.base / "GWMovies"
        self.lib = self.base / "PHOTOS-ALL"
        self.sibling = self.base / "PHOTOS-ALL-private"
        self.other = self.base / "Elsewhere"
        for d in (self.root, self.lib, self.sibling, self.other):
            d.mkdir()
        (self.lib / "clip.mov").write_bytes(b"VIDEO BYTES")
        (self.lib / "photo.jpg").write_bytes(b"PHOTO BYTES")
        (self.lib / "CLIP2.MOV").write_bytes(b"UPPER CASE")
        (self.sibling / "clip.mov").write_bytes(b"SIBLING SECRET")
        (self.other / "clip.mov").write_bytes(b"ELSEWHERE SECRET")
        links = {"good": self.lib / "clip.mov", "photo": self.lib / "photo.jpg",
                 "upper": self.lib / "CLIP2.MOV",
                 "sib": self.sibling / "clip.mov", "else": self.other / "clip.mov"}
        for name, target in links.items():
            (self.root / f"{name}.mov").symlink_to(target)
        (self.other / "outside-link.mov").symlink_to(self.lib / "clip.mov")
        (self.root / "hop.mov").symlink_to(self.root / "good.mov")
        self.srv = _Server(
            rows={"aud": self.root / "good.mov"},
            roots=(str(self.root),),
            video_rows={**{n: self.root / f"{n}.mov" for n in (*links, "hop")},
                        "direct": self.lib / "clip.mov",
                        "outlink": self.other / "outside-link.mov"})
        self._old = os.environ.get("LOCALFS_VIDEO_LINK_ROOTS")
        os.environ["LOCALFS_VIDEO_LINK_ROOTS"] = str(self.lib)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LOCALFS_VIDEO_LINK_ROOTS", None)
        else:
            os.environ["LOCALFS_VIDEO_LINK_ROOTS"] = self._old
        self.srv.close()
        shutil.rmtree(self.base, ignore_errors=True)

    def video(self, vid):
        return self.srv.get(f"/localfs/video/{vid}")

    def test_a_link_to_a_video_in_a_listed_folder_plays(self):
        self.assertEqual(self.video("good"), (200, b"VIDEO BYTES"))

    def test_extension_check_ignores_case(self):
        self.assertEqual(self.video("upper"), (200, b"UPPER CASE"))

    def test_a_chain_of_links_ending_in_a_listed_folder_plays(self):
        self.assertEqual(self.video("hop"), (200, b"VIDEO BYTES"))

    def test_without_the_setting_nothing_changes(self):
        del os.environ["LOCALFS_VIDEO_LINK_ROOTS"]
        status, body = self.video("good")
        self.assertEqual(status, 403)
        self.assertNotIn(b"VIDEO", body)

    def test_a_photo_in_a_listed_folder_stays_refused(self):
        status, body = self.video("photo")
        self.assertEqual(status, 403)
        self.assertNotIn(b"PHOTO", body)

    def test_a_folder_merely_starting_with_a_listed_name_is_refused(self):
        status, body = self.video("sib")
        self.assertEqual(status, 403)
        self.assertNotIn(b"SECRET", body)

    def test_a_link_to_an_unlisted_folder_is_refused(self):
        status, body = self.video("else")
        self.assertEqual(status, 403)
        self.assertNotIn(b"SECRET", body)

    def test_a_row_naming_the_outside_file_directly_is_refused(self):
        status, body = self.video("direct")
        self.assertEqual(status, 403)
        self.assertNotIn(b"VIDEO", body)

    def test_a_link_living_outside_the_video_root_is_refused(self):
        status, body = self.video("outlink")
        self.assertEqual(status, 403)
        self.assertNotIn(b"VIDEO", body)

    def test_the_audio_route_gets_no_exception(self):
        status, body = self.srv.get("/localfs/stream/aud")
        self.assertEqual(status, 403)
        self.assertNotIn(b"VIDEO", body)

    def test_extension_list_matches_the_scanner(self):
        import dlna_localfs_http
        import dlna_video_index
        self.assertEqual(dlna_localfs_http._LINKABLE_VIDEO_EXTS, dlna_video_index.VIDEO_EXTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
