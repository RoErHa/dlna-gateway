#!/usr/bin/env python3
"""
tests/test_localfs_wiring.py — the three LocalFs roots are INDEPENDENT.

The bug this file exists for (2026-09-10): a missing MUSIC root
returned out of `maybe_start_localfs` before the file server started,
so an unmounted music drive also took down the audiobooks living on a
different, healthy disk — and, with no `MediaServer` in `SERVERS`, the
SSRF guard then refused every /stream and /art against :8200 as an
unknown device, which the PWA saw as "every song skips".

No network, no live gateway: the file server, the provider and the
device registry are all stubbed, so these tests assert WIRING
DECISIONS — which libraries came up, and what the one file server was
told it may serve.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# Imported FIRST and deliberately: dlna_config loads `.env` at import
# time, which would re-populate the very LOCALFS_* vars these tests
# pop in setUp if the first import happened mid-test.
import dlna_config          # noqa: F401
import dlna_localfs_wiring as wiring

ROOT_ENVS = ("LOCALFS_MUSIC_ROOT", "LOCALFS_VIDEO_ROOT", "AUDIOBOOKS_ROOT",
             "LOCALFS_BASE_URL", "LOCALFS_BIND", "LOCALFS_PORT")


class _FakeProvider:
    """Stands in for LocalFsProvider — records how it was constructed."""

    def __init__(self, library, root, *, base_url="", id_namespace="",
                 collect_unknown_artists=True):
        self.root = Path(root)
        self.udn = f"uuid:localfs-fake-{id_namespace or 'music'}"
        self.id_namespace = id_namespace
        self.rescanned = False

    def rescan(self, *a, **kw):
        self.rescanned = True
        return {"scanned": 0}


class _WiringCase(unittest.TestCase):
    """Runs maybe_start_localfs() against stubs and exposes what it did."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ROOT_ENVS}
        for k in ROOT_ENVS:
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved_udn = wiring.AUDIOBOOKS_UDN
        wiring.AUDIOBOOKS_UDN = ""

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()
        wiring.AUDIOBOOKS_UDN = self._saved_udn

    def mkroot(self, name):
        p = self.tmp / name
        p.mkdir()
        return str(p)

    def run_wiring(self):
        """Returns (started_servers, bound_providers, added_servers)."""
        started, bound, added = [], [], []

        def fake_start_server(db, port=8200, *, host="0.0.0.0",
                              allowed_roots=()):
            started.append({"port": port, "host": host,
                            "allowed_roots": tuple(allowed_roots)})
            return mock.MagicMock()

        threads = []

        class _FakeThread:
            def __init__(self, target=None, daemon=None, name=None):
                self.target, self.name = target, name
                threads.append(self)

            def start(self):
                pass          # never run the scan/poll loops in a test

        with mock.patch("dlna_localfs_server.start_server", fake_start_server), \
             mock.patch("dlna_providers.localfs.LocalFsProvider",
                        _FakeProvider), \
             mock.patch("dlna_providers.bind_provider",
                        lambda udn, p: bound.append((udn, p))), \
             mock.patch("dlna_discovery.SERVERS") as servers, \
             mock.patch("dlna_config.load_config", return_value={}), \
             mock.patch.object(wiring.threading, "Thread", _FakeThread):
            servers.add.side_effect = lambda s: added.append(s)
            wiring.maybe_start_localfs(lambda: "192.168.1.125")

        self.threads = threads
        return started, bound, added


class TestRootsAreIndependent(_WiringCase):

    def test_missing_music_root_does_not_disable_audiobooks(self):
        """THE regression. Music volume unmounted, audiobooks disk fine:
        the file server must still come up and serve the books."""
        os.environ["LOCALFS_MUSIC_ROOT"] = str(self.tmp / "not-mounted")
        books = self.mkroot("Audio_Books")
        os.environ["AUDIOBOOKS_ROOT"] = books

        started, bound, added = self.run_wiring()

        self.assertEqual(len(started), 1, "file server must still start")
        self.assertEqual(started[0]["allowed_roots"],
                         (str(Path(books).resolve()),))
        names = sorted(s.name for s in added)
        self.assertEqual(names, ["RoHaAudioBooks"])
        self.assertTrue(wiring.AUDIOBOOKS_UDN,
                        "audiobooks UDN must be published for /api/servers")
        self.assertEqual([p.id_namespace for _, p in bound], ["audiobooks"])

    def test_missing_audiobooks_root_does_not_disable_music(self):
        music = self.mkroot("Music")
        os.environ["LOCALFS_MUSIC_ROOT"] = music
        os.environ["AUDIOBOOKS_ROOT"] = str(self.tmp / "not-mounted")

        started, bound, added = self.run_wiring()

        self.assertEqual(started[0]["allowed_roots"],
                         (str(Path(music).resolve()),))
        self.assertEqual([s.name for s in added], ["RoHaLocalFS"])
        self.assertEqual(wiring.AUDIOBOOKS_UDN, "")

    def test_missing_video_root_does_not_disable_the_rest(self):
        music, books = self.mkroot("Music"), self.mkroot("Audio_Books")
        os.environ["LOCALFS_MUSIC_ROOT"] = music
        os.environ["AUDIOBOOKS_ROOT"] = books
        os.environ["LOCALFS_VIDEO_ROOT"] = str(self.tmp / "not-mounted")

        started, _bound, added = self.run_wiring()

        self.assertEqual(sorted(started[0]["allowed_roots"]),
                         sorted([str(Path(music).resolve()),
                                 str(Path(books).resolve())]))
        self.assertEqual(sorted(s.name for s in added),
                         ["RoHaAudioBooks", "RoHaLocalFS"])

    def test_video_only_still_starts_the_file_server(self):
        """Video has no LibraryProvider, but its bytes are served by the
        same :8200 — so a video-only setup still needs the server."""
        video = self.mkroot("GWMovies")
        os.environ["LOCALFS_VIDEO_ROOT"] = video

        started, bound, added = self.run_wiring()

        self.assertEqual(started[0]["allowed_roots"],
                         (str(Path(video).resolve()),))
        self.assertEqual(bound, [])
        self.assertEqual(added, [])

    def test_all_three_present_all_three_served(self):
        music = self.mkroot("Music")
        video = self.mkroot("GWMovies")
        books = self.mkroot("Audio_Books")
        os.environ.update(LOCALFS_MUSIC_ROOT=music,
                          LOCALFS_VIDEO_ROOT=video,
                          AUDIOBOOKS_ROOT=books)

        started, bound, added = self.run_wiring()

        self.assertEqual(sorted(started[0]["allowed_roots"]),
                         sorted(str(Path(p).resolve())
                                for p in (music, video, books)))
        self.assertEqual(len(bound), 2)          # music + audiobooks
        self.assertEqual(sorted(s.name for s in added),
                         ["RoHaAudioBooks", "RoHaLocalFS"])


class TestNothingToServe(_WiringCase):

    def test_no_root_configured_is_a_silent_no_op(self):
        started, bound, added = self.run_wiring()
        self.assertEqual((started, bound, added), ([], [], []))

    def test_every_configured_root_missing_starts_no_server(self):
        """An empty allowed_roots would be a server that refuses
        everything — worse than no server, because it looks alive."""
        os.environ["LOCALFS_MUSIC_ROOT"] = str(self.tmp / "gone-music")
        os.environ["AUDIOBOOKS_ROOT"] = str(self.tmp / "gone-books")

        with self.assertLogs("dlna.localfs.wiring", "WARNING") as cm:
            started, bound, added = self.run_wiring()

        self.assertEqual((started, bound, added), ([], [], []))
        self.assertTrue(any("every configured root is missing" in m
                            for m in cm.output), cm.output)


class TestScanThreads(_WiringCase):

    def test_scan_thread_only_for_libraries_that_came_up(self):
        os.environ["LOCALFS_MUSIC_ROOT"] = str(self.tmp / "not-mounted")
        os.environ["AUDIOBOOKS_ROOT"] = self.mkroot("Audio_Books")

        self.run_wiring()

        names = [t.name for t in self.threads]
        self.assertIn("localfs-initial-scan", names)
        self.assertNotIn("video-scan", names)

    def test_scan_thread_scans_each_live_provider(self):
        os.environ["LOCALFS_MUSIC_ROOT"] = self.mkroot("Music")
        os.environ["AUDIOBOOKS_ROOT"] = self.mkroot("Audio_Books")

        _started, bound, _added = self.run_wiring()
        scan = next(t for t in self.threads
                    if t.name == "localfs-initial-scan")
        scan.target()

        self.assertTrue(all(p.rescanned for _, p in bound))

    def test_one_failing_scan_does_not_cost_the_other_its_index(self):
        os.environ["LOCALFS_MUSIC_ROOT"] = self.mkroot("Music")
        os.environ["AUDIOBOOKS_ROOT"] = self.mkroot("Audio_Books")

        _started, bound, _added = self.run_wiring()
        music = bound[0][1]
        music.rescan = mock.Mock(side_effect=RuntimeError("bad tree"))
        scan = next(t for t in self.threads
                    if t.name == "localfs-initial-scan")
        with self.assertLogs("dlna.localfs.wiring", "ERROR"):
            scan.target()

        self.assertTrue(bound[1][1].rescanned,
                        "audiobooks must still scan after music fails")

    def test_no_scan_thread_when_only_video(self):
        os.environ["LOCALFS_VIDEO_ROOT"] = self.mkroot("GWMovies")
        self.run_wiring()
        names = [t.name for t in self.threads]
        self.assertNotIn("localfs-initial-scan", names)
        self.assertIn("video-scan", names)


class TestResolveRoot(unittest.TestCase):
    """The pure half — a root is present, absent, or not configured."""

    def test_unconfigured_is_none_and_silent(self):
        self.assertIsNone(wiring._resolve_root("Music", ""))

    def test_present_root_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(wiring._resolve_root("Music", d), Path(d))

    def test_missing_root_warns_and_names_the_volume(self):
        with self.assertLogs("dlna.localfs.wiring", "WARNING") as cm:
            got = wiring._resolve_root("Audiobooks", "/nope/not/mounted")
        self.assertIsNone(got)
        joined = "\n".join(cm.output)
        self.assertIn("Audiobooks", joined)
        self.assertIn("/nope/not/mounted", joined)

    def test_user_home_is_expanded(self):
        self.assertIsNone(wiring._resolve_root("Music", "~/definitely-absent"))


class TestMusicRootConfig(unittest.TestCase):
    """music_root() — env beats config, '' when neither (music off)."""

    def setUp(self):
        self._saved = os.environ.get("LOCALFS_MUSIC_ROOT")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("LOCALFS_MUSIC_ROOT", None)
        else:
            os.environ["LOCALFS_MUSIC_ROOT"] = self._saved

    def test_env_var_wins(self):
        os.environ["LOCALFS_MUSIC_ROOT"] = "/Volumes/SAMDATA/Music"
        self.assertEqual(wiring.music_root(), "/Volumes/SAMDATA/Music")

    def test_config_fallback(self):
        os.environ.pop("LOCALFS_MUSIC_ROOT", None)
        with mock.patch("dlna_config.load_config",
                        return_value={"localfs": {"root": "/music"}}):
            self.assertEqual(wiring.music_root(), "/music")

    def test_unset_returns_empty(self):
        os.environ.pop("LOCALFS_MUSIC_ROOT", None)
        with mock.patch("dlna_config.load_config", return_value={}):
            self.assertEqual(wiring.music_root(), "")

    def test_null_config_value_does_not_crash(self):
        os.environ.pop("LOCALFS_MUSIC_ROOT", None)
        with mock.patch("dlna_config.load_config",
                        return_value={"localfs": {"root": None}}):
            self.assertEqual(wiring.music_root(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
