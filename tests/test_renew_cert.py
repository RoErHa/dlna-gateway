#!/usr/bin/env python3
"""
tests/test_renew_cert.py — renew-cert.sh, run for real in a sandbox.

Two defects found in cert-renewal.log while investigating epub-reader's
2026-08-15 TLS start failure (which turned out to be unrelated):

1. On 2026-07-20 `tailscale cert` answered "Public cert unchanged" (it
   declined to renew with 26 days left) and the script still logged
   "OK renewal complete" and restarted the gateway for nothing. The expiry
   must actually MOVE before anything is called renewed.
2. Only the gateway was restarted, but epub-reader loads the same files.
   After the 2026-09-21 renewal it served the OLD certificate until a
   development restart happened to come along — the last one ~7 hours
   before that certificate expired. Every service named in
   CERT_RELOAD_LABELS is restarted now.

The sandbox: a copy of the script with its own .env, the REAL openssl on
throwaway self-signed certs, and fake `tailscale` / `launchctl` that only
record what they were asked to do.

    python3 -m unittest tests.test_renew_cert -v
"""
import os
import shutil
import subprocess
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = "box.tailtest.ts.net"


def _cert(path_crt, path_key, days):
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-subj", f"/CN={HOST}", "-days", str(days),
                    "-keyout", path_key, "-out", path_crt],
                   check=True, capture_output=True)


@unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
class TestRenewCert(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="renew-cert-")
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copy(os.path.join(PROJECT, "renew-cert.sh"), self.dir)
        self.crt = os.path.join(self.dir, f"{HOST}.crt")
        self.key = os.path.join(self.dir, f"{HOST}.key")
        _cert(self.crt, self.key, days=10)          # due for renewal (<30)
        self.fresh = os.path.join(self.dir, "fresh")
        os.mkdir(self.fresh)
        _cert(os.path.join(self.fresh, "c"), os.path.join(self.fresh, "k"), days=90)
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        self.calls = os.path.join(self.dir, "calls")
        self._fake("launchctl", f'echo "launchctl $*" >> "{self.calls}"\n'
                                'case "$*" in *broken*) exit 3;; esac')

    def _fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/bash\n" + body + "\n")
        os.chmod(path, 0o755)

    def _tailscale(self, renews):
        copy = (f'cp "{self.fresh}/c" "{self.crt}"; cp "{self.fresh}/k" "{self.key}"; '
                'echo "Wrote public cert"') if renews else 'echo "Public cert unchanged"'
        self._fake("tailscale", f'echo "tailscale $*" >> "{self.calls}"; {copy}')

    def _run(self, env_lines):
        with open(os.path.join(self.dir, ".env"), "w") as fh:
            fh.write(f"TAILSCALE_CERT_HOST={HOST}\n" + "".join(f"{l}\n" for l in env_lines))
        env = {"PATH": f"{self.bin}:/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
               "HOME": self.dir}
        r = subprocess.run(["bash", os.path.join(self.dir, "renew-cert.sh")],
                           env=env, capture_output=True, text=True, cwd=self.dir)
        log = open(os.path.join(self.dir, "cert-renewal.log")).read()
        calls = open(self.calls).read() if os.path.exists(self.calls) else ""
        return r.returncode, log, calls

    def test_an_unchanged_certificate_is_not_called_a_renewal(self):
        self._tailscale(renews=False)
        rc, log, calls = self._run(["CERT_RELOAD_LABELS=com.x.gateway,com.x.reader"])
        self.assertEqual(rc, 0)
        self.assertNotIn("renewal complete", log)
        self.assertIn("WARN", log)
        self.assertIn("same certificate", log)
        self.assertNotIn("kickstart", calls, "restarted services for nothing")

    def test_a_real_renewal_restarts_every_service_that_loads_the_cert(self):
        self._tailscale(renews=True)
        rc, log, calls = self._run(["CERT_RELOAD_LABELS=com.x.gateway, com.x.reader"])
        self.assertEqual(rc, 0, log)
        self.assertIn("renewal complete", log)
        self.assertIn("kickstart -k gui/", calls)
        self.assertIn("/com.x.gateway", calls)
        self.assertIn("/com.x.reader", calls)

    def test_without_the_list_the_gateway_alone_is_restarted(self):
        # Backward compatible: an .env from before CERT_RELOAD_LABELS.
        self._tailscale(renews=True)
        rc, _, calls = self._run(["GATEWAY_LAUNCHD_LABEL=com.x.gateway"])
        self.assertEqual(rc, 0)
        self.assertIn("/com.x.gateway", calls)
        self.assertEqual(calls.count("kickstart"), 1)

    def test_one_failed_restart_does_not_stop_the_others(self):
        self._tailscale(renews=True)
        rc, log, calls = self._run(["CERT_RELOAD_LABELS=com.x.broken,com.x.reader"])
        self.assertNotEqual(rc, 0, "a failed restart must be visible in the exit code")
        self.assertIn("/com.x.reader", calls)
        self.assertIn("ERROR", log)

    def test_a_cert_with_time_left_is_left_alone(self):
        _cert(self.crt, self.key, days=90)
        self._tailscale(renews=True)
        rc, log, calls = self._run(["CERT_RELOAD_LABELS=com.x.gateway"])
        self.assertEqual(rc, 0)
        self.assertIn("skipping renewal", log)
        self.assertEqual(calls, "")


if __name__ == "__main__":
    unittest.main()
