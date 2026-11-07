#!/usr/bin/env python3
"""
tests/test_immich_import_job.py — immich-import.sh, run for real in a sandbox.

An unattended job that fails silently is worse than none: videos would just stop
appearing on the TV. The sandbox: a copy of the script, with a fake
`.venv/bin/python` that records its arguments and exits as told.

    python3 -m unittest tests.test_immich_import_job -v
"""
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestImmichImportJob(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="immich-job-")
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(os.path.join(PROJECT, "immich-import.sh"), self.dir)
        os.makedirs(os.path.join(self.dir, ".venv/bin"))
        open(os.path.join(self.dir, "dlna_alerts.py"), "w").close()
        self.calls = os.path.join(self.dir, "calls")

    def _run(self, importer_exit):
        fake = os.path.join(self.dir, ".venv/bin/python")
        with open(fake, "w") as fh:
            fh.write(f'#!/bin/bash\necho "$*" >> "{self.calls}"\n'
                     'case "$1" in\n'
                     '  tools/immich_import.py) echo "importer output: why it stopped"; '
                     f'exit {importer_exit};;\n'
                     'esac\n')
        os.chmod(fake, 0o755)
        # From another folder and with launchd's bare environment.
        return subprocess.run(["/bin/bash", os.path.join(self.dir, "immich-import.sh")],
                              cwd="/", env={"PATH": "/usr/bin:/bin"},
                              capture_output=True, text=True, timeout=30)

    def _called(self):
        with open(self.calls) as fh:
            return fh.read()

    def _log(self):
        with open(os.path.join(self.dir, "immich-import.log")) as fh:
            return fh.read()

    def test_success_applies_logs_and_stays_quiet(self):
        r = self._run(0)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._called().strip(), "tools/immich_import.py --apply")
        self.assertIn("importer output", self._log())

    def test_failure_alerts_with_the_reason_and_keeps_the_exit_code(self):
        for code in (2, 3):
            with self.subTest(code=code):
                os.path.exists(self.calls) and os.remove(self.calls)
                r = self._run(code)
                self.assertEqual(r.returncode, code)
                called = self._called()
                self.assertIn("dlna_alerts.py immich_import_failed", called)
                self.assertIn("why it stopped", called)
                self.assertIn("--priority high", called)

    def test_the_log_is_not_tracked_by_git(self):
        r = subprocess.run(["git", "check-ignore", "-q", "immich-import.log"],
                           cwd=PROJECT, capture_output=True)
        self.assertEqual(r.returncode, 0)


class TestLaunchAgentTemplate(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(PROJECT, "com.roha.dlna-immich-import.plist"), "rb") as fh:
            text = fh.read()
        # The template opens with a comment, which plistlib's format sniffing rejects.
        self.plist = plistlib.loads(text[text.index(b"<plist"):], fmt=plistlib.FMT_XML)

    def test_runs_the_script_nightly(self):
        self.assertEqual(self.plist["ProgramArguments"],
                         ["/path/to/dlna-gateway/immich-import.sh"])
        self.assertEqual(self.plist["StartCalendarInterval"], {"Hour": 2, "Minute": 45})

    def test_path_reaches_homebrew_ffprobe(self):
        self.assertTrue(re.search(r"(^|:)/opt/homebrew/bin(:|$)",
                                  self.plist["EnvironmentVariables"]["PATH"]))


if __name__ == "__main__":
    unittest.main()
