#!/usr/bin/env python3
"""
tests/test_localfs_watch.py — a root that is not there YET.

The bug this file exists for (2026-09-11): the machine rebooted,
launchd started the gateway at 20:06:36, and at 20:06:50 macOS had not
finished mounting the external music drive. The independent-root wiring
did exactly what it promises — music and video disabled themselves,
audiobooks came up — and then the drive mounted seconds later and
NOTHING re-asked. Seventeen hours of a file server whose allowed_roots
held only the books, while `tracks` still listed 26k music rows: every
album browsed, every play 403'd.

So these tests ask three things of `RootWatch`: does a volume that
appears later get wired without a restart, does it get wired exactly
once, and is a person TOLD which drive to go and mount — because the
only evidence last time was one WARNING nobody was looking at.

No network, no file server, no gateway: the activation callback is a
recorder, so what is asserted is the decision, not the plumbing.
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

import dlna_config          # noqa: F401  (loads .env before anything reads it)
import dlna_localfs_watch as watch


class _WatchCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.w = watch.RootWatch()
        self.calls = []
        self._saved = os.environ.get("LOCALFS_ROOT_RECHECK_SEC")

    def tearDown(self):
        self._tmp.cleanup()
        if self._saved is None:
            os.environ.pop("LOCALFS_ROOT_RECHECK_SEC", None)
        else:
            os.environ["LOCALFS_ROOT_RECHECK_SEC"] = self._saved

    def activate(self, path):
        self.calls.append(path)

    def mkroot(self, name):
        p = self.tmp / name
        p.mkdir()
        return str(p)


class TestResolveRoot(unittest.TestCase):
    """The pure half — a root is present, absent, or not configured."""

    def test_unconfigured_is_none(self):
        self.assertIsNone(watch.resolve_root("Music", ""))

    def test_present_root_resolves(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(watch.resolve_root("Music", d), Path(d))

    def test_missing_root_is_none(self):
        self.assertIsNone(watch.resolve_root("Audiobooks", "/nope/not/here"))

    def test_user_home_is_expanded(self):
        self.assertIsNone(watch.resolve_root("Music", "~/definitely-absent"))

    def test_a_volume_caught_mid_unmount_is_absent_not_a_crash(self):
        """`exists()` RAISES on a volume being torn down. Answering
        'absent' keeps the re-check loop alive; propagating would kill
        the thread and nothing would ever be re-checked again."""
        with mock.patch.object(Path, "exists",
                               side_effect=OSError("Device not configured")):
            self.assertIsNone(watch.resolve_root("Music", "/Volumes/GONE"))


class TestRegister(_WatchCase):

    def test_present_root_activates_immediately(self):
        root = self.mkroot("Music")
        self.assertTrue(self.w.register("Music", root, self.activate))
        self.assertEqual(self.calls, [Path(root)])
        self.assertEqual(self.w.waiting(), [])

    def test_missing_root_waits_instead_of_being_written_off(self):
        missing = str(self.tmp / "not-mounted")
        with self.assertLogs("dlna.localfs.watch", "WARNING"):
            self.assertFalse(self.w.register("Music", missing, self.activate))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.w.waiting(), ["Music"])

    def test_the_warning_names_the_volume_to_go_and_mount(self):
        missing = str(self.tmp / "SAMDATA-Music")
        with self.assertLogs("dlna.localfs.watch", "WARNING") as cm:
            self.w.register("Music", missing, self.activate)
        joined = "\n".join(cm.output)
        self.assertIn("Music", joined)
        self.assertIn(missing, joined)
        self.assertIn("no restart needed", joined)

    def test_an_unconfigured_root_is_not_tracked_at_all(self):
        """Off is not the same as waiting. Telling someone the Video
        library is unavailable when they never configured one is noise,
        and noise is what buried the last outage."""
        self.assertFalse(self.w.register("Video", "", self.activate))
        self.assertEqual(self.w.waiting(), [])
        self.assertEqual(self.w.snapshot(), [])


class TestTheVolumeAppears(_WatchCase):

    def test_a_root_that_appears_later_is_wired_with_no_restart(self):
        """THE regression, in one test."""
        root = self.tmp / "Music"
        self.w.register("Music", str(root), self.activate)
        self.assertEqual(self.calls, [])

        root.mkdir()                       # the drive gets mounted
        self.w._activate("Music")

        self.assertEqual(self.calls, [root])
        self.assertEqual(self.w.waiting(), [])

    def test_a_still_missing_root_keeps_waiting(self):
        self.w.register("Music", str(self.tmp / "nope"), self.activate)
        self.w._activate("Music")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.w.waiting(), ["Music"])

    def test_activation_happens_exactly_once(self):
        """The callback starts a file server, binds a provider and
        registers a device. Twice would publish the library twice."""
        root = self.mkroot("Music")
        self.w.register("Music", root, self.activate)
        for _ in range(3):
            self.w._activate("Music")
        self.assertEqual(len(self.calls), 1)

    def test_roots_are_still_independent(self):
        books = self.mkroot("Audio_Books")
        self.w.register("Music", str(self.tmp / "not-mounted"), self.activate)
        self.w.register("Audiobooks", books, self.activate)

        self.assertEqual(self.calls, [Path(books)])
        self.assertEqual(self.w.waiting(), ["Music"])

    def test_a_failed_activation_is_not_retried_forever(self):
        """The root IS there; what failed is our handling of it. Retrying
        every 30 s would repeat the failure forever instead of reporting
        it once — and would re-run whatever half of it succeeded."""
        root = self.mkroot("Music")
        boom = mock.Mock(side_effect=RuntimeError("port in use"))
        with self.assertLogs("dlna.localfs.watch", "ERROR"):
            self.w.register("Music", root, boom)
        self.w._activate("Music")

        self.assertEqual(boom.call_count, 1)
        self.assertEqual(self.w.waiting(), [])
        self.assertIn("port in use", self.w.snapshot()[0]["error"])


class TestWhatTheUiIsTold(_WatchCase):

    def test_a_waiting_root_names_the_volume_in_its_message(self):
        missing = str(self.tmp / "SAMDATA" / "Music")
        self.w.register("Music", missing, self.activate)
        row = self.w.snapshot()[0]
        self.assertEqual(row["state"], watch.WAITING)
        self.assertIn(missing, row["message"])
        self.assertIn("no restart needed", row["message"])

    def test_an_active_root_carries_no_message(self):
        self.w.register("Music", self.mkroot("Music"), self.activate)
        row = self.w.snapshot()[0]
        self.assertEqual(row["state"], watch.ACTIVE)
        self.assertNotIn("message", row)

    def test_the_callback_never_leaks_into_the_json(self):
        """snapshot() is serialised straight to the PWA — a function in
        it is a 500, and every waiting root would take the page with it."""
        self.w.register("Music", str(self.tmp / "nope"), self.activate)
        row = self.w.snapshot()[0]
        self.assertNotIn("activate", row)
        import json
        json.dumps(row)              # must not raise

    def test_rows_are_ordered_so_the_bar_does_not_reshuffle(self):
        self.w.register("Video", str(self.tmp / "v"), self.activate)
        self.w.register("Music", str(self.tmp / "m"), self.activate)
        self.assertEqual([r["label"] for r in self.w.snapshot()],
                         ["Music", "Video"])


class TestEvents(_WatchCase):
    """The PWA must not have to wait for a poll to hear about a drive."""

    def _published(self, fn):
        seen = []
        with mock.patch("dlna_events.EVENTS.publish", seen.append):
            fn()
        return seen

    def test_a_root_going_live_announces_itself_and_the_new_source(self):
        root = self.tmp / "Music"
        self.w.register("Music", str(root), self.activate)
        root.mkdir()

        seen = self._published(lambda: self.w._activate("Music"))
        kinds = [e["type"] for e in seen]
        self.assertIn("libraries", kinds)
        # …and `devices`, because the source picker just gained an entry.
        self.assertIn("devices", kinds)
        lib = next(e for e in seen if e["type"] == "libraries")
        self.assertEqual((lib["label"], lib["state"]), ("Music", watch.ACTIVE))

    def test_a_missing_root_announces_itself_too(self):
        seen = self._published(
            lambda: self.w.register("Music", str(self.tmp / "nope"),
                                    self.activate))
        lib = next(e for e in seen if e["type"] == "libraries")
        self.assertEqual(lib["state"], watch.WAITING)

    def test_a_dead_event_bus_never_breaks_wiring(self):
        """Registration runs at boot, BEFORE the ASGI lifespan binds the
        loop. A publish that throws must not cost the library."""
        root = self.mkroot("Music")
        with mock.patch("dlna_events.EVENTS.publish",
                        side_effect=RuntimeError("no loop")):
            self.assertTrue(self.w.register("Music", root, self.activate))
        self.assertEqual(self.calls, [Path(root)])


class TestTheRecheckThread(_WatchCase):

    def test_nothing_waiting_starts_no_thread(self):
        """A fully-mounted machine must not carry an idle thread."""
        self.w.register("Music", self.mkroot("Music"), self.activate)
        with mock.patch.object(watch.threading, "Thread") as T:
            self.w.start()
        T.assert_not_called()

    def test_a_waiting_root_starts_one_thread(self):
        self.w.register("Music", str(self.tmp / "nope"), self.activate)
        with mock.patch.object(watch.threading, "Thread") as T:
            self.w.start()
        self.assertEqual(T.call_count, 1)
        self.assertEqual(T.call_args.kwargs["name"], "localfs-root-watch")
        self.assertTrue(T.call_args.kwargs["daemon"])

    def test_the_loop_wires_the_root_then_retires(self):
        """It re-checks until everything is up, then exits — the thread
        is for waiting, and there is nothing left to wait for."""
        root = self.tmp / "Music"
        self.w.register("Music", str(root), self.activate)

        def _mount(_sec):
            root.mkdir(exist_ok=True)
        with mock.patch.object(watch.time, "sleep", _mount):
            self.w._loop()               # returns = thread retires

        self.assertEqual(self.calls, [root])
        self.assertEqual(self.w.waiting(), [])

    def test_recheck_interval_is_configurable_and_floored(self):
        os.environ["LOCALFS_ROOT_RECHECK_SEC"] = "120"
        self.assertEqual(watch._recheck_sec(), 120)
        os.environ["LOCALFS_ROOT_RECHECK_SEC"] = "1"
        self.assertEqual(watch._recheck_sec(), 5, "a 1s stat storm is not ok")
        os.environ["LOCALFS_ROOT_RECHECK_SEC"] = "garbage"
        self.assertEqual(watch._recheck_sec(), watch._DEFAULT_RECHECK_SEC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
