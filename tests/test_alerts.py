"""Push alerts (dlna_alerts.py, hardening timeline #3, 2026-11-04).

What matters most: alerts are OFF unless configured, one problem gives one
push rather than one a minute, a failing notifier never reaches its caller,
and the topic URL (the secret on ntfy.sh) never lands in a log. Then each
place that raises one: boot (self-restart, restart loop), the runtime watch,
a root waiting an hour, the browser guard, the certificate, and the three
weekly shell jobs.
"""
import http.server
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlna_alerts as A  # noqa: E402
import dlna_alerts_sms as S  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://ntfy.example/secret-topic-name"


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def alerter(env=None, post=None, clock=None):
    sent = []
    a = A.Alerter(env={"ALERT_PUSH_URL": URL} if env is None else env,
                  post=post or sent.append, clock=clock or FakeClock())
    return a, sent


class TestOffByDefault(unittest.TestCase):
    def test_no_url_sends_nothing(self):
        a, sent = alerter(env={})
        self.assertFalse(a.enabled())
        self.assertFalse(a.alert("x", "t", "m"))
        self.assertFalse(a.send_now("x", "t", "m"))
        self.assertEqual(sent, [])

    def test_the_test_suite_itself_has_alerts_off(self):
        """tests/__init__.py blanks both channels, so no test can reach the
        phone."""
        self.assertEqual(os.environ.get("ALERT_PUSH_URL"), "")
        self.assertEqual(os.environ.get("ALERT_SMS_TO"), "")
        self.assertFalse(A.ALERTS.enabled())

    def test_the_test_suite_never_writes_the_live_state_dir(self):
        """A runtime-watch test leaving a marker in the repo made the next
        live boot push a false 'restarted itself'."""
        self.assertNotEqual(A._state_dir(None), A.STATE_DIR)
        self.assertIn("dlna-test-alerts-", A._state_dir(None))


class TestRequestShape(unittest.TestCase):
    def test_ntfy_post(self):
        req = A.build_request(URL, "tok", "backup failed", "details",
                              priority="high", tags=("warning",))
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, URL)
        self.assertEqual(req.data, b"details")
        h = {k.lower(): v for k, v in req.header_items()}
        self.assertIn("backup failed", h["title"])
        self.assertTrue(h["title"].startswith("DLNA Gateway ("))
        self.assertEqual(h["priority"], "4")
        self.assertEqual(h["tags"], "warning")
        self.assertEqual(h["authorization"], "Bearer tok")

    def test_no_token_no_authorization_header(self):
        req = A.build_request(URL, "", "t", "m")
        self.assertNotIn("authorization", {k.lower() for k, _ in req.header_items()})

    def test_non_ascii_title_is_rfc2047_encoded(self):
        req = A.build_request(URL, "", "café — kapot", "m")
        title = dict(req.header_items())["Title"]
        title.encode("latin-1")                      # would raise otherwise
        self.assertTrue(title.startswith("=?UTF-8?B?"))

    def test_message_is_bounded(self):
        self.assertLessEqual(len(A.build_request(URL, "", "t", "x" * 10000).data), 4000)


class TestRateLimit(unittest.TestCase):
    def test_one_push_per_kind_per_interval(self):
        clock = FakeClock()
        a, sent = alerter(clock=clock)
        self.assertTrue(a.alert("root", "t", "m"))
        self.assertFalse(a.alert("root", "t", "m"))
        self.assertTrue(a.alert("other", "t", "m"))      # kinds are independent
        clock.t += 3599
        self.assertFalse(a.alert("root", "t", "m"))
        clock.t += 2
        self.assertTrue(a.alert("root", "t", "m"))
        a.drain()
        self.assertEqual(len(sent), 3)

    def test_interval_comes_from_env(self):
        clock = FakeClock()
        a, _ = alerter(env={"ALERT_PUSH_URL": URL, "ALERT_MIN_INTERVAL_SEC": "10"},
                       clock=clock)
        a.alert("k", "t", "m")
        clock.t += 11
        self.assertTrue(a.alert("k", "t", "m"))


class TestNeverHurtsTheCaller(unittest.TestCase):
    def test_failed_send_is_logged_without_the_url(self):
        def boom(req):
            raise OSError("network down")
        a, _ = alerter(post=boom)
        with self.assertLogs("dlna.alerts", "WARNING") as logs:
            self.assertFalse(a.send_now("k", "t", "m"))
        self.assertIn("network down", logs.output[0])
        self.assertNotIn("secret-topic-name", "\n".join(logs.output))

    def test_full_queue_drops_instead_of_blocking(self):
        gate = threading.Event()
        a, _ = alerter(post=lambda req: gate.wait(5))
        try:
            with self.assertLogs("dlna.alerts", "WARNING") as logs:
                results = [a.alert(f"k{i}", "t", "m") for i in range(A._QUEUE_MAX + 5)]
            self.assertIn(False, results)
            self.assertIn("queue is full", "\n".join(logs.output))
        finally:
            gate.set()

    def test_a_crash_inside_alert_never_raises(self):
        a, _ = alerter()
        with mock.patch.object(a, "_admit", side_effect=RuntimeError("bug")), \
                self.assertLogs("dlna.alerts", "WARNING"):
            self.assertFalse(a.alert("k", "t", "m"))


class _Receiver(http.server.BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Receiver.got.append((dict(self.headers), body))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class TestRealSend(unittest.TestCase):
    """The real urllib path, against a local server, in-process and as the
    CLI the shell jobs call."""

    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), _Receiver)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}/topic"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        _Receiver.got.clear()

    def test_queued_alert_arrives(self):
        a = A.Alerter(env={"ALERT_PUSH_URL": self.url, "ALERT_PUSH_TOKEN": "s3"})
        self.assertTrue(a.alert("k", "volume gone", "mount it"))
        a.drain()
        headers, body = _Receiver.got[0]
        self.assertEqual(body, b"mount it")
        self.assertEqual(headers["Authorization"], "Bearer s3")

    def _cli(self, *args, url=""):
        env = dict(os.environ, ALERT_PUSH_URL=url)
        return subprocess.run([sys.executable, os.path.join(PROJECT, "dlna_alerts.py"),
                               *args], env=env, capture_output=True, text=True,
                              timeout=30)

    def test_cli_sends_and_exits_0(self):
        r = self._cli("backup_failed", "backup FAILED", "why", "--priority", "high",
                      url=self.url)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(_Receiver.got[0][0]["Priority"], "4")

    def test_cli_off_exits_3(self):
        self.assertEqual(self._cli("--test").returncode, 3)

    def test_cli_unreachable_exits_1(self):
        self.assertEqual(self._cli("k", "t", "m", url="http://127.0.0.1:9/x").returncode, 1)


class TestBootReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.a, self.sent = alerter()

    def kinds(self):
        self.a.drain()
        return list(self.a._last)

    def test_runtime_exit_marker_is_reported_once_by_the_next_process(self):
        A.note_runtime_exit("python3.15 is gone", state_dir=self.dir)
        A.on_boot(self.dir, self.a, now=5000)
        self.assertEqual(self.kinds(), ["runtime_restart"])
        self.assertIn(b"python3.15 is gone", self.sent[0].data)
        self.assertFalse(os.path.exists(os.path.join(self.dir, A.RUNTIME_EXIT_MARKER)))

    def test_ordinary_boots_are_quiet(self):
        for t in (0, 3600, 7200):
            A.on_boot(self.dir, self.a, now=t)
        self.assertEqual(self.kinds(), [])

    def test_restart_loop_is_reported(self):
        for i in range(A.RESTART_LOOP_BOOTS):
            A.on_boot(self.dir, self.a, now=10000 + i * 15)
        self.assertEqual(self.kinds(), ["restart_loop"])

    def test_boot_history_stays_bounded(self):
        for i in range(200):
            A.on_boot(self.dir, alerter(env={})[0], now=float(i))
        with open(os.path.join(self.dir, A.BOOT_HISTORY)) as f:
            self.assertLessEqual(len(f.readlines()), 50)

    def test_restart_loop_rule(self):
        self.assertFalse(A.restart_loop([0, 1000, 2000, 3000, 4000], 4000))
        self.assertTrue(A.restart_loop([0, 10, 20, 30, 40], 40))


class TestRuntimeWatchRaisesThem(unittest.TestCase):
    def _watch(self, ready):
        import dlna_runtime_watch as rw
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        f = os.path.join(d, "python")
        open(f, "w").close()
        w = rw.RuntimeWatch([f], confirm=1, exit_fn=lambda c: None,
                            ready_fn=lambda: ready, holder_fn=lambda: None)
        os.remove(f)
        return w

    def test_exit_leaves_the_marker_for_the_next_boot(self):
        w = self._watch(ready=True)
        with mock.patch.object(A, "note_runtime_exit") as note, \
                self.assertLogs("dlna.runtime", "CRITICAL"):
            self.assertEqual(w.tick(), "exit")
        note.assert_called_once()

    def test_no_replacement_python_alerts_now(self):
        w = self._watch(ready=False)
        with mock.patch.object(A, "alert") as alert, self.assertLogs(level="WARNING"):
            self.assertEqual(w.tick(), "waiting")
            w.tick()
        self.assertEqual([c.args[0] for c in alert.call_args_list], ["runtime_stale"])


class TestRootWaitingAnHour(unittest.TestCase):
    def test_alerts_once_per_root_after_the_limit(self):
        import dlna_localfs_watch as lw
        w = lw.RootWatch()
        with self.assertLogs("dlna.localfs.watch", "WARNING"):
            w.register("Video", "/nonexistent/video-root", lambda p: None)
        since = w._roots["Video"]["since"]
        with mock.patch.object(A, "alert") as alert:
            self.assertEqual(w.alert_long_waits(now=since + 3599), [])
            self.assertEqual(w.alert_long_waits(now=since + 3601), ["Video"])
            self.assertEqual(w.alert_long_waits(now=since + 9999), [])
        self.assertEqual(alert.call_args.args[0], "root_waiting:Video")
        self.assertIn("/nonexistent/video-root", alert.call_args.args[2])


class TestGuardRefusalAlerts(unittest.TestCase):
    def test_a_refusal_raises_a_low_priority_alert(self):
        import dlna_asgi_guard as g
        from tests.test_request_guard import NAMES, _asgi

        async def app(scope, receive, send):
            raise AssertionError("must not be reached")
        with mock.patch.object(A, "alert") as alert, \
                self.assertLogs("dlna.guard", "WARNING"):
            _asgi(g.RequestGuard(app, names=NAMES), "POST", "/api/control",
                  [("host", "evil.example")])
        self.assertEqual(alert.call_args.args[0], "guard_refused")
        self.assertEqual(alert.call_args.kwargs["priority"], "low")
        self.assertIn("GATEWAY_ALLOWED_HOSTS", alert.call_args.args[2])


class TestCertificate(unittest.TestCase):
    def test_days_left_from_a_real_certificate(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl not installed")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        crt = os.path.join(d, "c.crt")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", os.path.join(d, "k"), "-out", crt, "-days", "5",
                        "-subj", "/CN=t"], check=True, capture_output=True)
        self.assertAlmostEqual(A.cert_days_left(crt), 5, delta=0.1)
        a, _ = alerter()
        with self.assertLogs("dlna.alerts", "WARNING"):
            A.check_cert(crt, a)
        self.assertIn("cert_expiring", a._last)

    def test_healthy_or_missing_certificate_is_quiet(self):
        a, _ = alerter()
        with mock.patch.object(A, "cert_days_left", return_value=40):
            A.check_cert("x.crt", a)
        A.check_cert(None, a)
        self.assertEqual(a._last, {})

    def test_default_certfile_follows_the_tailscale_host(self):
        self.assertIsNone(A.default_certfile({"TAILSCALE_CERT_HOST": "nope.invalid"}))
        self.assertIsNone(A.default_certfile({}))


class TestSms(unittest.TestCase):
    """The Messages channel (dlna_alerts_sms.py)."""

    def test_number_and_text_are_arguments_never_script_source(self):
        """Alert text can carry a stranger's Host header; pasted into the
        AppleScript it would run as code."""
        evil = 'x" & (do shell script "touch /tmp/pwned") & "'
        cmd = S.command("+31600000000", evil)
        self.assertEqual(cmd[:2], ["/usr/bin/osascript", "-e"])
        self.assertEqual(cmd[2], S.APPLESCRIPT)            # fixed source
        self.assertEqual(cmd[3:], ["+31600000000", evil, "SMS"])

    def test_service_order(self):
        self.assertEqual(S.command("n", "t", "iMessage")[-1], "iMessage")
        self.assertEqual(S.command("n", "t", "imessage")[-1], "iMessage")
        self.assertEqual(S.command("n", "t", "anything")[-1], "SMS")

    def test_text_is_bounded(self):
        t = S.sms_text("title", "x" * 5000)
        self.assertLessEqual(len(t), 600)
        self.assertTrue(t.startswith("DLNA Gateway: title\n"))

    def test_a_refusal_raises_without_the_number(self):
        def run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, "", "Not authorized (-1743)")
        with self.assertRaises(RuntimeError) as cm:
            S.send("t", "m", env={"ALERT_SMS_TO": "+31612345678"}, run=run)
        self.assertIn("-1743", str(cm.exception))
        self.assertNotIn("612345678", str(cm.exception))

    def test_sms_alone_enables_alerts_and_is_used(self):
        texts = []
        a = A.Alerter(env={"ALERT_SMS_TO": "+31612345678"},
                      sms=lambda t, m, env: texts.append((t, m)))
        self.assertTrue(a.enabled())
        self.assertTrue(a.send_now("k", "volume gone", "mount it"))
        self.assertEqual(texts, [("volume gone", "mount it")])

    def test_both_channels_and_one_failing_still_counts(self):
        pushed = []

        def bad_sms(t, m, env):
            raise RuntimeError("Messages not signed in")
        a = A.Alerter(env={"ALERT_SMS_TO": "+31612345678", "ALERT_PUSH_URL": URL},
                      post=pushed.append, sms=bad_sms)
        with self.assertLogs("dlna.alerts", "WARNING") as logs:
            self.assertTrue(a.send_now("k", "t", "m"))
        self.assertEqual(len(pushed), 1)
        self.assertNotIn("612345678", "\n".join(logs.output))

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("osacompile"),
                         "macOS only")
    def test_the_applescript_compiles(self):
        """Syntax only: compiling sends nothing."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        r = subprocess.run(["osacompile", "-o", os.path.join(d, "a.scpt"),
                            "-e", S.APPLESCRIPT], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestShellJobs(unittest.TestCase):
    """The weekly jobs, run for real under /bin/bash 3.2 in a sandbox whose
    dlna_alerts.py is a fake that records how it was called."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.calls = os.path.join(self.dir, "calls")
        with open(os.path.join(self.dir, "dlna_alerts.py"), "w") as f:
            f.write("import sys\n"
                    f"open({self.calls!r}, 'a').write(repr(sys.argv[1:]) + '\\n')\n")

    def run_script(self, name, env):
        shutil.copy(os.path.join(PROJECT, name), self.dir)
        full = dict(os.environ, PATH="/usr/bin:/bin:/usr/sbin:/sbin", **env)
        return subprocess.run(["/bin/bash", os.path.join(self.dir, name)],
                              env=full, cwd=self.dir, capture_output=True,
                              text=True, timeout=60)

    def recorded(self):
        if not os.path.exists(self.calls):
            return []
        with open(self.calls) as f:
            return [eval(line) for line in f]                 # noqa: S307

    def test_failed_backup_pushes_why(self):
        r = self.run_script("backup-library.sh",
                            {"LIBRARY_DB": "missing.db",
                             "BACKUP_DIR": os.path.join(self.dir, "b")})
        self.assertEqual(r.returncode, 1)
        (call,) = self.recorded()
        self.assertEqual(call[0], "backup_failed")
        self.assertIn("missing.db not found", call[2])
        self.assertIn("high", call)

    def test_good_backup_pushes_nothing(self):
        if not shutil.which("sqlite3"):
            self.skipTest("sqlite3 not installed")
        db = os.path.join(self.dir, "lib.db")
        subprocess.run(["sqlite3", db, "CREATE TABLE tracks(x); INSERT INTO tracks VALUES(1);"],
                       check=True)
        r = self.run_script("backup-library.sh",
                            {"LIBRARY_DB": db, "BACKUP_DIR": os.path.join(self.dir, "b")})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.recorded(), [])

    def test_failed_cert_renewal_pushes(self):
        r = self.run_script("renew-cert.sh", {"TAILSCALE_CERT_HOST": ""})
        self.assertEqual(r.returncode, 1)
        (call,) = self.recorded()
        self.assertEqual(call[0], "cert_renew_failed")


if __name__ == "__main__":
    logging.basicConfig()
    unittest.main()
