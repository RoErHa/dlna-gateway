#!/usr/bin/env python3
"""
tests/test_backup_library.py — backup-library.sh, run for real in a sandbox.

Hardening #2 was closed on 2026-11-08 by Time Machine, not by the script:
the owner pointed Time Machine at a new disk, which copies library.db and
~/dlna-gateway-backups off the SSD. What Time Machine does not do is SAY
when it stops — an unplugged or full disk shows only in System Settings —
so the weekly job now checks the age of Time Machine's newest backup after
its own snapshot, and fails (→ the EXIT trap's backup_failed alert) when
that is older than BACKUP_TM_MAX_AGE_DAYS. It reads Time Machine's
preferences file, not `tmutil latestbackup`: under launchd that needs Full
Disk Access, and the first live run of this check stalled 26 s and failed
on exactly that.

The two properties that matter most:
  * the local snapshot is taken and kept whatever Time Machine says;
  * a Time Machine problem reaches the owner — it is never just a log line.

Until this file the script had no test at all; the snapshot-failure alert
is pinned here too.

The sandbox: a copy of the script with its own .env, the REAL sqlite3 on a
throwaway database, a Time Machine preferences file written with plistlib
(pointed at by TM_PREFS) and a fake `dlna_alerts.py`
that records the alert it was asked to send. Runs under /bin/bash, the
3.2 that launchd uses.

    python3 -m unittest tests.test_backup_library -v
"""
import datetime
import glob
import os
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("sqlite3"), "sqlite3 not installed")
@unittest.skipUnless(os.path.exists("/bin/bash"), "needs /bin/bash")
class TestBackupLibrary(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="backup-lib-")
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(os.path.join(PROJECT, "backup-library.sh"), self.dir)
        con = sqlite3.connect(os.path.join(self.dir, "library.db"))
        con.execute("CREATE TABLE tracks(id INTEGER PRIMARY KEY, title TEXT)")
        con.execute("INSERT INTO tracks(title) VALUES ('one')")
        con.commit()
        con.close()
        self.dest = os.path.join(self.dir, "backups")
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.alerts = os.path.join(self.dir, "alerts")
        with open(os.path.join(self.dir, "dlna_alerts.py"), "w") as fh:
            fh.write("import sys\n"
                     f"open({self.alerts!r}, 'a').write('\\t'.join(sys.argv[1:]) + '\\n')\n")
        self.prefs_path = os.path.join(self.dir, "com.apple.TimeMachine.plist")
        self.prefs()    # default: one destination, a backup five minutes old

    def prefs(self, snapshots=(0.0,), destination=True):
        """Time Machine's preferences file, as macOS writes it."""
        now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
        dests = []
        if destination:
            dests.append({"DestinationID": "8563DECB",
                          "LastKnownVolumeName": "WD-SERVER",
                          "SnapshotDates": [now - datetime.timedelta(days=d, minutes=5)
                                            for d in snapshots]})
        with open(self.prefs_path, "wb") as fh:
            plistlib.dump({"AutoBackup": True, "Destinations": dests}, fh)

    def run_script(self, dotenv="", **env):
        with open(os.path.join(self.dir, ".env"), "w") as fh:
            fh.write(dotenv)
        base = {"PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": self.dir, "BACKUP_DIR": self.dest,
                "TM_PREFS": self.prefs_path}
        base.update(env)
        return subprocess.run(["/bin/bash", os.path.join(self.dir, "backup-library.sh")],
                              cwd=self.dir, env=base, capture_output=True, text=True)

    def snapshots(self):
        return glob.glob(os.path.join(self.dest, "library-*.db"))

    def sent(self):
        if not os.path.exists(self.alerts):
            return []
        with open(self.alerts) as fh:
            return [line.split("\t") for line in fh.read().splitlines()]

    # ── the local snapshot ────────────────────────────────────────────

    def test_fresh_time_machine_passes_quietly(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.snapshots()), 1)
        self.assertIn("Time Machine: newest backup", r.stdout)
        self.assertEqual(self.sent(), [])

    def test_missing_database_alerts(self):
        os.remove(os.path.join(self.dir, "library.db"))
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        (alert,) = self.sent()
        self.assertEqual(alert[0], "backup_failed")
        self.assertIn("not found", alert[2])

    def test_an_alert_that_was_not_sent_is_logged(self):
        # The number is scrubbed by dlna_alerts_sms before the CLI prints
        # anything (tests/test_alerts.py::TestSms).
        os.remove(os.path.join(self.dir, "library.db"))
        with open(os.path.join(self.dir, "dlna_alerts.py"), "w") as fh:
            fh.write("import sys\n"
                     "print('Messages refused: participant <number> (-1743)')\n"
                     "sys.exit(1)\n")
        self.run_script()
        with open(os.path.join(self.dir, "library-backup.log")) as fh:
            log = fh.read()
        self.assertIn("alert 'backup_failed' was NOT sent (exit 1)", log)
        self.assertIn("-1743", log)

    def test_alerts_off_is_not_reported_as_a_failure(self):
        os.remove(os.path.join(self.dir, "library.db"))
        with open(os.path.join(self.dir, "dlna_alerts.py"), "w") as fh:
            fh.write("import sys\nprint('alerts are off')\nsys.exit(3)\n")
        self.run_script()
        with open(os.path.join(self.dir, "library-backup.log")) as fh:
            self.assertNotIn("NOT sent", fh.read())

    # ── Time Machine ──────────────────────────────────────────────────

    def test_stale_time_machine_alerts_but_keeps_the_snapshot(self):
        self.prefs(snapshots=(10, 12))
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.snapshots()), 1, "the local snapshot must survive")
        (alert,) = self.sent()
        self.assertEqual(alert[0], "backup_failed")
        self.assertIn("local snapshot OK", alert[2])
        self.assertIn("10 days old", alert[2])

    def test_the_newest_snapshot_counts_whatever_the_order(self):
        self.prefs(snapshots=(20, 1, 9))
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("(1 d old", r.stdout)

    def test_no_backup_on_record_alerts(self):
        self.prefs(snapshots=())
        r = self.run_script()
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.snapshots()), 1)
        (alert,) = self.sent()
        self.assertIn("no completed backup on record", alert[2])

    def test_no_destination_is_skipped_not_alerted(self):
        self.prefs(destination=False)
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("no destination configured", r.stdout)
        self.assertEqual(self.sent(), [])

    def test_no_prefs_file_is_skipped(self):
        os.remove(self.prefs_path)
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Time Machine", r.stdout)

    def test_limit_comes_from_dotenv(self):
        self.prefs(snapshots=(5,))
        self.assertEqual(self.run_script().returncode, 1)          # default 3
        self.assertEqual(
            self.run_script(dotenv="BACKUP_TM_MAX_AGE_DAYS=7\n").returncode, 0)

    def test_real_environment_wins_over_dotenv(self):
        self.prefs(snapshots=(5,))
        r = self.run_script(dotenv="BACKUP_TM_MAX_AGE_DAYS=7\n",
                            BACKUP_TM_MAX_AGE_DAYS="2")
        self.assertEqual(r.returncode, 1)

    def test_zero_turns_the_check_off(self):
        self.prefs(snapshots=(30,))
        r = self.run_script(dotenv="BACKUP_TM_MAX_AGE_DAYS=0\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Time Machine", r.stdout)

    def test_garbage_limit_fails_loudly(self):
        r = self.run_script(dotenv="BACKUP_TM_MAX_AGE_DAYS=three\n")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.snapshots()), 1)
        (alert,) = self.sent()
        self.assertIn("not a whole number", alert[2])

    def test_dotenv_is_read_not_sourced(self):
        canary = os.path.join(self.dir, "ran")
        r = self.run_script(dotenv=f"GW_NAME=x touch {canary}\n"
                                   "BACKUP_TM_MAX_AGE_DAYS=3\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(canary))


if __name__ == "__main__":
    unittest.main()
