#!/usr/bin/env python3
"""
tests/test_chaos_stderr.py — chaos.py's silent-thread-death canary.

`tests/chaos.py` fails a run when a daemon thread dies without saying so,
and `/tmp/dlna-gateway.err` (the launchd stderr sink) is where that
traceback lands. The criterion used to be "the file must not grow by a
single byte", which is not the same question: hypercorn logs its
`[INFO] Running on …` banner to that same file on every boot, and
`launchctl kickstart -k` SIGKILLs the old process, which reliably flushes
a `resource_tracker: leaked semaphore objects` UserWarning on the way out
— 18 such warnings had accumulated in the live file, one per restart.

So a restart overlapping a chaos run failed it while nothing had crashed.
`classify_stderr` reads WHAT was appended instead. It is pure so these
shapes are tests, not surprises found 400 iterations into a live run.

chaos.py itself is a live-gateway script and is NOT part of run_all.py;
this file tests only the pure classifier, so it needs no gateway.
"""
import os
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from tests.chaos import classify_stderr  # noqa: E402


# Verbatim from the live /tmp/dlna-gateway.err.
RESTART_NOISE = """[2026-08-26 23:05:57 +0200] [97265] [INFO] Running on https://100.78.51.93:8443 (CTRL + C to quit)
[2026-08-26 23:05:57 +0200] [97265] [INFO] Running on http://127.0.0.1:8765 (CTRL + C to quit)
"""

SEMAPHORE_WARNING = """/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/lib/python3.14/multiprocessing/resource_tracker.py:475: UserWarning: resource_tracker: There appear to be 5 leaked semaphore objects to clean up at shutdown: {'/mp-oyr5_uw7'}
  warnings.warn(
"""

# The shape of the real 2026-08-21 incident this canary exists to catch.
REAL_CRASH = """Exception in thread renderer-queue:
Traceback (most recent call last):
  File "/Users/x/dlna-gateway-2.0/dlna_player.py", line 210, in _monitor
    dur = int(t["duration"])
ValueError: invalid literal for int() with base 10: '0:03:47.000'
"""


class TestBenignGrowthIsNotACrash(unittest.TestCase):

    def test_nothing_appended(self):
        crash, benign = classify_stderr("")
        self.assertEqual(crash, [])
        self.assertEqual(benign, 0)

    def test_hypercorn_boot_banner(self):
        crash, benign = classify_stderr(RESTART_NOISE)
        self.assertEqual(crash, [])
        self.assertEqual(benign, 2)

    def test_sigkill_semaphore_warning(self):
        """`kickstart -k` produces this EVERY time. It is the single most
        likely thing to land in that file and it is not a crash."""
        crash, benign = classify_stderr(SEMAPHORE_WARNING)
        self.assertEqual(crash, [])
        self.assertEqual(benign, 2)

    def test_a_whole_restart_is_benign(self):
        crash, _ = classify_stderr(SEMAPHORE_WARNING + RESTART_NOISE)
        self.assertEqual(crash, [])

    def test_blank_lines_are_not_counted(self):
        _, benign = classify_stderr("\n\n   \n")
        self.assertEqual(benign, 0)


class TestCrashesAreCaught(unittest.TestCase):

    def test_the_real_incident(self):
        crash, _ = classify_stderr(REAL_CRASH)
        self.assertEqual(len(crash), 2)          # thread line + Traceback
        self.assertIn("renderer-queue", crash[0])

    def test_a_traceback_alone(self):
        crash, _ = classify_stderr("Traceback (most recent call last):\n"
                                   "  File \"x.py\", line 1\n")
        self.assertEqual(len(crash), 1)

    def test_fatal_python_error(self):
        crash, _ = classify_stderr("Fatal Python error: Segmentation fault\n")
        self.assertEqual(len(crash), 1)

    def test_a_crash_buried_in_benign_noise_still_fails(self):
        """The case that matters: a restart AND a dead thread in one
        window. Byte-counting caught this; so must reading the content."""
        crash, benign = classify_stderr(
            RESTART_NOISE + REAL_CRASH + SEMAPHORE_WARNING)
        self.assertEqual(len(crash), 2)
        self.assertGreater(benign, 0)

    def test_the_word_error_alone_is_not_a_crash(self):
        """A deliberately short marker list — an INFO line mentioning an
        error, or a 500 the chaos run provoked on purpose, must not wake
        anyone up."""
        crash, benign = classify_stderr(
            "[INFO] handled error path: 500 returned to client\n"
            "[ERROR] hypercorn.error: connection reset\n")
        self.assertEqual(crash, [])
        self.assertEqual(benign, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
