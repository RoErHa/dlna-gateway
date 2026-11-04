#!/usr/bin/env python3
"""
dlna_alerts.py — tell the owner, on their phone, when something goes wrong.

WHY THIS EXISTS (2026-11-04, hardening timeline #3). The gateway already
DETECTS its own failures (a deleted Python, an unmounted volume, a refused
browser request, a failed weekly job), but it only wrote them to
`gateway.log`, which nobody reads. On 2026-10-31 it ran half-alive for 2.5
days for exactly that reason.

THE CHANNEL is a push notification over the ntfy protocol: a POST whose body
is the message, with `Title` / `Priority` / `Tags` headers. ntfy.sh or a
self-hosted ntfy server both speak it, and the ntfy phone app shows it.
Everything about the channel lives in `.env`:

  ALERT_PUSH_URL          the topic URL. Unset = alerts are OFF.
  ALERT_PUSH_TOKEN        optional bearer token (protected topic / server).
  ALERT_MIN_INTERVAL_SEC  per-kind quiet period, default 3600.

A public ntfy.sh topic is readable by anyone who knows its name, so the topic
must be long and random. Messages say WHAT happened and WHEN, never library
content or secrets.

THE RULES this module keeps:

* **Off unless configured.** No URL → nothing is sent and nothing is logged
  above DEBUG. A fresh clone never phones anywhere.
* **One problem, one notification.** Each `kind` (e.g. `root_waiting`) is
  rate-limited to one push per `ALERT_MIN_INTERVAL_SEC`, so a problem that
  repeats every 30 s does not become a push every 30 s.
* **A notifier can never hurt playback.** `alert()` only queues; one daemon
  thread sends, the queue is bounded (posture rule 10: nothing unbounded) and
  a full queue drops the alert with a log line. A failed send is logged and
  forgotten. Nothing here raises into its caller.
* **The gateway's own crash is reported by the NEXT process.** When the
  runtime watch exits because brew deleted our Python, this process cannot be
  trusted to make a TLS call. It leaves a marker; the fresh process reads it at
  boot (`on_boot`) and sends the alert. A run of boots in quick succession —
  launchd respawning a gateway that keeps dying — is reported the same way.

The weekly shell jobs (backup, cert renewal, upgrade check) use the CLI:

  python3 dlna_alerts.py <kind> <title> <message> [--priority high]
  python3 dlna_alerts.py --test

Exit status: 0 sent, 3 alerts are off, 1 the send failed.

Guarded by tests/test_alerts.py.
"""
import base64
import logging
import os
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.request

log = logging.getLogger("dlna.alerts")

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(_BASE_DIR, ".alerts")     # gitignored


def _state_dir(state_dir: str | None) -> str:
    """Resolved at CALL time, so the test suite can point it elsewhere
    (ALERT_STATE_DIR) — a test that runs the runtime watch's exit path must
    not leave a marker the live gateway then reports."""
    return state_dir or os.environ.get("ALERT_STATE_DIR") or STATE_DIR
RUNTIME_EXIT_MARKER = "runtime-exit"
BOOT_HISTORY = "boot-history"

_TIMEOUT = 10.0
_QUEUE_MAX = 32
_DEFAULT_INTERVAL = 3600.0
RESTART_LOOP_BOOTS = 5          # this many boots …
RESTART_LOOP_WINDOW = 600.0     # … within this many seconds is a loop
CERT_WARN_DAYS = 14
_PRIORITIES = {"low": "2", "default": "3", "high": "4", "urgent": "5"}


def _env(key: str, env=None) -> str:
    return ((os.environ if env is None else env).get(key) or "").strip()


def _interval(env=None) -> float:
    try:
        return max(0.0, float(_env("ALERT_MIN_INTERVAL_SEC", env)
                              or _DEFAULT_INTERVAL))
    except ValueError:
        return _DEFAULT_INTERVAL


def _header_text(text: str) -> str:
    """HTTP headers are latin-1; ntfy decodes RFC 2047 for anything else."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        return f"=?UTF-8?B?{b64}?="


def build_request(url: str, token: str, title: str, message: str,
                  priority: str = "default", tags=()) -> urllib.request.Request:
    """The ntfy POST. Pure, so its shape is tested without a network."""
    host = socket.gethostname().split(".", 1)[0] or "gateway"
    headers = {"Title": _header_text(f"DLNA Gateway ({host}): {title}"[:200]),
               "Priority": _PRIORITIES.get(priority, "3"),
               "Content-Type": "text/plain; charset=utf-8"}
    if tags:
        headers["Tags"] = _header_text(",".join(tags))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, data=message[:4000].encode("utf-8"),
                                  headers=headers, method="POST")


def _post(req: urllib.request.Request) -> None:
    ctx = ssl.create_default_context()           # verification stays ON
    with urllib.request.urlopen(req, timeout=_TIMEOUT, context=ctx) as r:
        r.read(1024)


class Alerter:
    """Rate-limited, queued, never-raising push alerts."""

    def __init__(self, env=None, post=_post, clock=time.monotonic):
        self._env = env                 # None → os.environ at call time
        self._post = post
        self._clock = clock
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAX)
        self._thread: threading.Thread | None = None

    def enabled(self) -> bool:
        return bool(_env("ALERT_PUSH_URL", self._env))

    def _admit(self, kind: str) -> bool:
        """True when `kind` is outside its quiet period (and claims it)."""
        now = self._clock()
        with self._lock:
            last = self._last.get(kind)
            if last is not None and now - last < _interval(self._env):
                return False
            self._last[kind] = now
            return True

    def alert(self, kind: str, title: str, message: str, *,
              priority: str = "default", tags=()) -> bool:
        """Queue an alert. True when queued; never raises, never blocks."""
        try:
            if not self.enabled():
                log.debug(f"alert {kind!r} not sent — ALERT_PUSH_URL unset")
                return False
            if not self._admit(kind):
                log.debug(f"alert {kind!r} suppressed — sent one recently")
                return False
            try:
                self._queue.put_nowait((kind, title, message, priority, tags))
            except queue.Full:
                log.warning(f"alert {kind!r} dropped — the alert queue is full")
                return False
            self._ensure_worker()
            return True
        except Exception as e:                                # noqa: BLE001
            log.warning(f"alert {kind!r} could not be queued: {e!r}")
            return False

    def send_now(self, kind: str, title: str, message: str, *,
                 priority: str = "default", tags=()) -> bool:
        """Send synchronously (the CLI). Not rate-limited: a weekly job is
        its own rate limit. True when the server accepted it."""
        url = _env("ALERT_PUSH_URL", self._env)
        if not url:
            return False
        try:
            self._post(build_request(url, _env("ALERT_PUSH_TOKEN", self._env),
                                     title, message, priority, tags))
            log.info(f"alert ✉ sent {kind}: {title}")
            return True
        except Exception as e:                                # noqa: BLE001
            # Never log the URL: the topic name IS the secret on ntfy.sh.
            log.warning(f"alert ✗ could not send {kind!r}: {e!r}")
            return False

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._work, daemon=True,
                                            name="alerts")
            self._thread.start()

    def _work(self) -> None:
        while True:
            kind, title, message, priority, tags = self._queue.get()
            self.send_now(kind, title, message, priority=priority, tags=tags)

    def drain(self, timeout: float = 5.0) -> None:
        """Tests: wait until the queue has been sent."""
        end = time.monotonic() + timeout
        while not self._queue.empty() and time.monotonic() < end:
            time.sleep(0.01)
        time.sleep(0.05)


# The gateway's one alerter.
ALERTS = Alerter()


def alert(kind: str, title: str, message: str, **kw) -> bool:
    return ALERTS.alert(kind, title, message, **kw)


# ── what the NEXT process reports ──────────────────────────────────────
def note_runtime_exit(reason: str, state_dir: str | None = None) -> None:
    """Called by the runtime watch just before it exits. Plain file I/O on
    purpose: the Python we are running from may already be gone."""
    state_dir = _state_dir(state_dir)
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, RUNTIME_EXIT_MARKER), "w") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {reason}\n")
    except OSError as e:
        log.warning(f"alerts: could not leave the runtime-exit marker: {e}")


def restart_loop(boots: list[float], now: float) -> bool:
    """Pure: do these boot times show launchd respawning a dying gateway?"""
    recent = [b for b in boots if now - b <= RESTART_LOOP_WINDOW]
    return len(recent) >= RESTART_LOOP_BOOTS


def on_boot(state_dir: str | None = None, alerter: Alerter | None = None,
            now: float | None = None) -> None:
    """Report what the previous process could not: a self-restart after the
    runtime watch fired, and a restart loop."""
    a = alerter or ALERTS
    now = time.time() if now is None else now
    state_dir = _state_dir(state_dir)
    try:
        os.makedirs(state_dir, exist_ok=True)
        marker = os.path.join(state_dir, RUNTIME_EXIT_MARKER)
        if os.path.exists(marker):
            with open(marker) as f:
                reason = f.read().strip()
            os.remove(marker)
            a.alert("runtime_restart", "restarted itself",
                    f"The runtime watch restarted the gateway onto a new "
                    f"Python: {reason}. It is back up; nothing to do unless "
                    f"this repeats.", tags=("arrows_counterclockwise",))
        hist = os.path.join(state_dir, BOOT_HISTORY)
        boots = []
        if os.path.exists(hist):
            with open(hist) as f:
                for line in f:
                    try:
                        boots.append(float(line))
                    except ValueError:
                        continue
        boots = [b for b in boots if now - b <= RESTART_LOOP_WINDOW] + [now]
        with open(hist, "w") as f:          # bounded: only the window is kept
            f.write("".join(f"{b}\n" for b in boots[-50:]))
        if restart_loop(boots, now):
            a.alert("restart_loop", "restart loop",
                    f"The gateway has started {len(boots)} times in "
                    f"{int(RESTART_LOOP_WINDOW / 60)} minutes — launchd is "
                    f"respawning a process that keeps dying. See "
                    f"/tmp/dlna-gateway.err and gateway.log.",
                    priority="high", tags=("rotating_light",))
    except Exception as e:                                    # noqa: BLE001
        log.warning(f"alerts: boot report failed: {e!r}")


# ── the certificate, checked from inside the gateway ───────────────────
def cert_days_left(path: str, now: float | None = None) -> float | None:
    try:
        not_after = ssl._ssl._test_decode_cert(path)["notAfter"]
    except Exception as e:                                    # noqa: BLE001
        log.debug(f"alerts: cannot read {path}: {e!r}")
        return None
    return (ssl.cert_time_to_seconds(not_after)
            - (time.time() if now is None else now)) / 86400


def check_cert(path: str | None, alerter: Alerter | None = None) -> None:
    """WARN + alert when the TLS certificate is close to expiring: the
    weekly renewal should never let that happen, so if it does, that job
    is dead."""
    if not path:
        return
    days = cert_days_left(path)
    if days is not None and days < CERT_WARN_DAYS:
        msg = (f"The TLS certificate expires in {days:.0f} days — the weekly "
               f"renewal (com.roha.dlna-cert-renew) is not working. See "
               f"cert-renewal.log, or run ./renew-cert.sh --force.")
        log.warning(msg)
        (alerter or ALERTS).alert("cert_expiring", "certificate expiring",
                                  msg, priority="high", tags=("lock",))


def default_certfile(env=None) -> str | None:
    """The certificate hypercorn serves (same lookup as hypercorn_conf)."""
    path = _env("GATEWAY_CERTFILE", env)
    if not path:
        host = _env("TAILSCALE_CERT_HOST", env)
        path = os.path.join(_BASE_DIR, f"{host}.crt") if host else ""
    return path if path and os.path.isfile(path) else None


def start_cert_watch(path: str | None, every_sec: float = 6 * 3600) -> None:
    if not path:
        return

    def _loop():
        while True:
            check_cert(path)
            time.sleep(every_sec)
    threading.Thread(target=_loop, daemon=True, name="cert-watch").start()


# ── CLI for the weekly shell jobs ──────────────────────────────────────
def main(argv: list[str]) -> int:
    import dlna_config  # noqa: F401  — loads .env
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if argv[:1] == ["--test"]:
        argv = ["test", "test alert",
                "If you can read this on your phone, gateway alerts work."]
    priority = "default"
    if "--priority" in argv:
        i = argv.index("--priority")
        priority = argv[i + 1] if i + 1 < len(argv) else "default"
        del argv[i:i + 2]
    if len(argv) != 3:
        print(__doc__.split("The weekly shell jobs", 1)[1].split("Guarded")[0])
        return 2
    if not ALERTS.enabled():
        print("alerts are off (ALERT_PUSH_URL is not set in .env)")
        return 3
    return 0 if ALERTS.send_now(*argv, priority=priority) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
