"""dlna_runtime_watch — restart when the Python we run from leaves the disk.

The 2026-10-31 incident: `brew upgrade` deleted the running gateway's
python@3.15 keg; music died while radio kept working, for 2.5 days.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlna_runtime_watch as rw  # noqa: E402


class _Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.a = self._make("python3.15")
        self.b = self._make("os.py")
        self.exits = []

    def _make(self, name, body="x"):
        p = os.path.join(self.tmp.name, name)
        with open(p, "w") as f:
            f.write(body)
        return p

    def _replace(self, path):
        """What brew does: a new file at the same path → a new inode.
        Keep the old one alive meanwhile so the inode cannot be reused."""
        held = path + ".old"
        os.rename(path, held)
        self._make(os.path.basename(path), "y")

    def watch(self, confirm=2, ready=True):
        return rw.RuntimeWatch([self.a, self.b], confirm=confirm,
                               exit_fn=self.exits.append,
                               ready_fn=lambda: ready)


class TestStaleReason(_Files):
    def test_unchanged_is_not_stale(self):
        fp = rw.fingerprint([self.a, self.b])
        self.assertIsNone(rw.stale_reason(fp, rw.fingerprint([self.a, self.b])))

    def test_deleted_file_is_stale(self):
        fp = rw.fingerprint([self.a])
        os.remove(self.a)
        self.assertIn("is gone", rw.stale_reason(fp, rw.fingerprint([self.a])))

    def test_replaced_in_place_is_stale(self):
        """`brew reinstall` keeps the path and changes the file."""
        fp = rw.fingerprint([self.a])
        self._replace(self.a)
        self.assertIn("was replaced", rw.stale_reason(fp, rw.fingerprint([self.a])))

    def test_path_missing_at_boot_is_never_judged(self):
        ghost = os.path.join(self.tmp.name, "never-there")
        fp = rw.fingerprint([ghost])
        self.assertIsNone(rw.stale_reason(fp, rw.fingerprint([ghost])))

    def test_appearing_later_is_not_stale(self):
        ghost = os.path.join(self.tmp.name, "late")
        fp = rw.fingerprint([ghost])
        self._make("late")
        self.assertIsNone(rw.stale_reason(fp, rw.fingerprint([ghost])))


class TestTick(_Files):
    def test_healthy_never_exits(self):
        w = self.watch()
        for _ in range(5):
            self.assertEqual(w.tick(), "ok")
        self.assertEqual(self.exits, [])

    def test_one_sample_is_not_enough(self):
        w = self.watch(confirm=2)
        os.remove(self.a)
        self.assertEqual(w.tick(), "suspect")
        self.assertEqual(self.exits, [])

    def test_confirmed_stale_exits_nonzero(self):
        """Non-zero, because hypercorn's master respawns a worker that exits
        0 — from its own stale interpreter path."""
        w = self.watch(confirm=2)
        os.remove(self.b)
        w.tick()
        self.assertEqual(w.tick(), "exit")
        self.assertEqual(self.exits, [rw.EXIT_CODE])
        self.assertNotEqual(rw.EXIT_CODE, 0)

    def test_transient_blip_resets(self):
        """brew moves files about mid-upgrade; one bad sample then a good
        one must not count towards the next."""
        w = self.watch(confirm=2)
        held = self.a + ".tmp"
        os.rename(self.a, held)
        self.assertEqual(w.tick(), "suspect")
        os.rename(held, self.a)                 # same inode back
        self.assertEqual(w.tick(), "ok")
        os.rename(self.a, held)
        self.assertEqual(w.tick(), "suspect")
        self.assertEqual(self.exits, [])

    def test_no_replacement_means_wait_not_exit(self):
        """Exiting with no Python to restart into is a launchd respawn loop."""
        w = self.watch(confirm=1, ready=False)
        os.remove(self.a)
        self.assertEqual(w.tick(), "waiting")
        self.assertEqual(w.tick(), "waiting")
        self.assertEqual(self.exits, [])

    def test_exits_once_replacement_arrives(self):
        ready = {"v": False}
        w = rw.RuntimeWatch([self.a], confirm=1, exit_fn=self.exits.append,
                            ready_fn=lambda: ready["v"])
        os.remove(self.a)
        self.assertEqual(w.tick(), "waiting")
        ready["v"] = True
        self.assertEqual(w.tick(), "exit")

    def test_waiting_warns_once(self):
        w = self.watch(confirm=1, ready=False)
        os.remove(self.a)
        with self.assertLogs("dlna.runtime", "WARNING") as cm:
            w.tick()
            w.tick()
            w.tick()
        self.assertEqual(len(cm.output), 1)


class TestReplacementReady(unittest.TestCase):
    def test_real_venv_is_ready(self):
        self.assertTrue(rw.replacement_ready())

    def test_missing_prefix_is_not_ready(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(rw.replacement_ready(d))

    def test_dangling_symlink_is_not_ready(self):
        """The 10-31 shape: a venv python whose target keg was deleted."""
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "bin"))
            os.symlink(os.path.join(d, "deleted-keg", "python3.15"),
                       os.path.join(d, "bin", "python3"))
            self.assertFalse(rw.replacement_ready(d))


class TestRuntimeFiles(unittest.TestCase):
    def test_covers_interpreter_and_stdlib(self):
        files = rw.runtime_files()
        self.assertIn(os.path.realpath(sys.executable), files)
        self.assertIn(os.path.realpath(os.__file__), files)
        for p in files:
            self.assertTrue(os.path.exists(p), p)


class TestStart(unittest.TestCase):
    def test_zero_disables(self):
        self.assertIsNone(rw.start_runtime_watch(0))

    def test_env_zero_disables(self):
        old = os.environ.get("RUNTIME_WATCH_SEC")
        os.environ["RUNTIME_WATCH_SEC"] = "0"
        try:
            self.assertIsNone(rw.start_runtime_watch())
        finally:
            if old is None:
                del os.environ["RUNTIME_WATCH_SEC"]
            else:
                os.environ["RUNTIME_WATCH_SEC"] = old


class TestRealExit(unittest.TestCase):
    """End to end in a SUBPROCESS: a watch thread that sees its file vanish
    must take the whole process down with EXIT_CODE — os._exit from a daemon
    thread, which is the path the gateway worker takes."""

    def test_process_exits_with_code(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = textwrap.dedent(f"""
            import os, sys, tempfile, threading, time
            sys.path.insert(0, {repo!r})
            import dlna_runtime_watch as rw
            fd, p = tempfile.mkstemp(); os.close(fd)
            w = rw.RuntimeWatch([p], confirm=1, ready_fn=lambda: True)
            os.remove(p)
            threading.Thread(target=w.tick, daemon=True).start()
            time.sleep(10)
            sys.exit(0)
        """)
        r = subprocess.run([sys.executable, "-c", script],
                           capture_output=True, timeout=30)
        self.assertEqual(r.returncode, rw.EXIT_CODE, r.stderr.decode())


if __name__ == "__main__":
    unittest.main()
