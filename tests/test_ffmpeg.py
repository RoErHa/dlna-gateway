#!/usr/bin/env python3
"""
tests/test_ffmpeg.py — Phase V0 of video support: the optional ffmpeg/ffprobe
helpers (dlna_ffmpeg) + the LOCALFS_VIDEO_ROOT resolver. Pure/mocked — never
invokes a real ffmpeg/ffprobe or touches the network.

Run: python3 -m unittest tests.test_ffmpeg -v
"""
import json
import os
import sys
import unittest
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dlna_ffmpeg as ff
import dlna_localfs_wiring as wiring


class TestBinaryDiscovery(unittest.TestCase):
    def test_which_wins(self):
        with mock.patch.object(ff.shutil, "which", return_value="/x/ffprobe"):
            self.assertEqual(ff.find_ffprobe(), "/x/ffprobe")

    def test_homebrew_fallback(self):
        def isfile(p): return p == "/opt/homebrew/bin/ffmpeg"
        def access(p, _): return p == "/opt/homebrew/bin/ffmpeg"
        with mock.patch.object(ff.shutil, "which", return_value=None), \
             mock.patch.object(ff.os.path, "isfile", side_effect=isfile), \
             mock.patch.object(ff.os, "access", side_effect=access):
            self.assertEqual(ff.find_ffmpeg(), "/opt/homebrew/bin/ffmpeg")

    def test_absent_returns_none(self):
        with mock.patch.object(ff.shutil, "which", return_value=None), \
             mock.patch.object(ff.os.path, "isfile", return_value=False):
            self.assertIsNone(ff.find_ffprobe())


class TestIso6709(unittest.TestCase):
    def test_lat_lon(self):
        self.assertEqual(ff.parse_iso6709("+52.3676+004.9041/"),
                         (52.3676, 4.9041))

    def test_negative_lon(self):
        self.assertEqual(ff.parse_iso6709("+52.3676-004.9041/"),
                         (52.3676, -4.9041))

    def test_with_altitude_ignored(self):
        self.assertEqual(ff.parse_iso6709("+52.3676+004.9041+012.3/"),
                         (52.3676, 4.9041))

    def test_garbage_and_empty(self):
        self.assertIsNone(ff.parse_iso6709("not coords"))
        self.assertIsNone(ff.parse_iso6709(""))
        self.assertIsNone(ff.parse_iso6709(None))


class TestParseProbe(unittest.TestCase):
    def _doc(self, vtags=None, vcodec="h264", acodec="aac"):
        return {
            "format": {
                "duration": "12.500",
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "tags": vtags or {},
            },
            "streams": [
                {"codec_type": "video", "codec_name": vcodec,
                 "width": 1920, "height": 1080},
                {"codec_type": "audio", "codec_name": acodec},
            ],
        }

    def test_basic_h264(self):
        m = ff._parse_probe(self._doc(), "x.mov")
        self.assertEqual(m["duration"], 12.5)
        self.assertEqual((m["width"], m["height"]), (1920, 1080))
        self.assertEqual(m["vcodec"], "h264")
        self.assertEqual(m["acodec"], "aac")
        self.assertEqual(m["container"], "mov")

    def test_hevc(self):
        m = ff._parse_probe(self._doc(vcodec="hevc"))
        self.assertEqual(m["vcodec"], "hevc")

    def test_creation_title_and_location_tags(self):
        m = ff._parse_probe(self._doc({
            "creation_time": "2026-06-14T14:30:00.000000Z",
            "title": "Birthday",
            "location": "+52.3676+004.9041/",
        }))
        self.assertEqual(m["created"], "2026-06-14T14:30:00.000000Z")
        self.assertEqual(m["title"], "Birthday")
        self.assertEqual(m["location"], "+52.3676+004.9041/")

    def test_apple_quicktime_location_tag(self):
        m = ff._parse_probe(self._doc({
            "com.apple.quicktime.location.ISO6709": "+52.3676+004.9041/"}))
        self.assertEqual(m["location"], "+52.3676+004.9041/")

    def test_missing_streams_and_tags(self):
        m = ff._parse_probe({"format": {}})
        self.assertIsNone(m["vcodec"])
        self.assertIsNone(m["title"])
        self.assertIsNone(m["location"])


class TestProbe(unittest.TestCase):
    def test_no_ffprobe_returns_none(self):
        with mock.patch.object(ff, "find_ffprobe", return_value=None):
            self.assertIsNone(ff.probe("/x.mov"))

    def test_success(self):
        doc = {"format": {"duration": "3.0", "format_name": "matroska,webm"},
               "streams": [{"codec_type": "video", "codec_name": "vp9",
                            "width": 640, "height": 480}]}
        fake = mock.Mock(returncode=0, stdout=json.dumps(doc))
        with mock.patch.object(ff, "find_ffprobe", return_value="/ffprobe"), \
             mock.patch.object(ff.subprocess, "run", return_value=fake):
            m = ff.probe("/x.mkv")
        self.assertEqual(m["vcodec"], "vp9")
        self.assertEqual(m["container"], "matroska")

    def test_nonzero_exit_returns_none(self):
        fake = mock.Mock(returncode=1, stdout="")
        with mock.patch.object(ff, "find_ffprobe", return_value="/ffprobe"), \
             mock.patch.object(ff.subprocess, "run", return_value=fake):
            self.assertIsNone(ff.probe("/x.mov"))

    def test_garbled_json_returns_none(self):
        fake = mock.Mock(returncode=0, stdout="{not json")
        with mock.patch.object(ff, "find_ffprobe", return_value="/ffprobe"), \
             mock.patch.object(ff.subprocess, "run", return_value=fake):
            self.assertIsNone(ff.probe("/x.mov"))


class TestCmdBuilders(unittest.TestCase):
    def test_transcode_cmd(self):
        cmd = ff.transcode_cmd("/m/clip.mov", ffmpeg="/ff")
        self.assertEqual(cmd[0], "/ff")
        self.assertIn("/m/clip.mov", cmd)
        self.assertIn("libx264", cmd)
        self.assertIn("aac", cmd)
        self.assertIn("pipe:1", cmd)
        joined = " ".join(cmd)
        self.assertIn("frag_keyframe", joined)

    def test_poster_cmd(self):
        cmd = ff.poster_cmd("/m/clip.mov", "/tmp/p.jpg", "00:00:05", "/ff")
        self.assertIn("/m/clip.mov", cmd)
        self.assertIn("/tmp/p.jpg", cmd)
        self.assertIn("00:00:05", cmd)

    def test_extract_poster_no_ffmpeg(self):
        with mock.patch.object(ff, "find_ffmpeg", return_value=None):
            self.assertFalse(ff.extract_poster("/x.mov", "/tmp/p.jpg"))


class TestDisplayTitle(unittest.TestCase):
    def test_embedded_title_wins(self):
        self.assertEqual(
            ff.build_display_title("Birthday", "2026-06-14T14:30:00Z",
                                   "Amsterdam", "(52,4)", "mov"),
            "Birthday")

    def test_constructed_with_place(self):
        self.assertEqual(
            ff.build_display_title(None, "2026-06-14T14:30:00Z",
                                   "Amsterdam", None, ".mov"),
            "Amsterdam_20260614_1430.mov")

    def test_no_location_uses_datetime(self):
        self.assertEqual(
            ff.build_display_title("", "2026-06-14T14:30:00Z", None, None, "mp4"),
            "20260614_1430.mp4")

    def test_coords_when_no_place(self):
        self.assertEqual(
            ff.build_display_title(None, "2026-06-14T14:30:00Z", None,
                                   "52.37,4.90", "mov"),
            "52.37,4.90_20260614_1430.mov")

    def test_nothing_known(self):
        self.assertEqual(ff.build_display_title(None, None, None, None, ""),
                         "video")

    def test_fmt_dt_garbage(self):
        self.assertEqual(ff._fmt_dt("nonsense"), "")
        self.assertEqual(ff._fmt_dt(None), "")


class TestVideoRootResolver(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("LOCALFS_VIDEO_ROOT")
        os.environ.pop("LOCALFS_VIDEO_ROOT", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("LOCALFS_VIDEO_ROOT", None)
        else:
            os.environ["LOCALFS_VIDEO_ROOT"] = self._saved

    def test_env_wins(self):
        os.environ["LOCALFS_VIDEO_ROOT"] = "/Volumes/SAMDATA/GWMovies"
        self.assertEqual(wiring.video_root(), "/Volumes/SAMDATA/GWMovies")

    def test_config_fallback(self):
        # video_root() imports load_config from dlna_config at call time.
        with mock.patch("dlna_config.load_config",
                        return_value={"localfs": {"video_root": "/v/movies"}}):
            self.assertEqual(wiring.video_root(), "/v/movies")

    def test_unset_returns_empty(self):
        with mock.patch("dlna_config.load_config", return_value={}):
            self.assertEqual(wiring.video_root(), "")

    def test_video_udn_distinct(self):
        self.assertEqual(wiring.VIDEO_UDN, "uuid:localfs-movies")


if __name__ == "__main__":
    unittest.main()
