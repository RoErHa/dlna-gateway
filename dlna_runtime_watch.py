#!/usr/bin/env python3
"""
dlna_runtime_watch.py — is the Python running us still the one on disk?

The incident (2026-10-31 → 11-03): a routine `brew upgrade` installed
python@3.15 3.15.2 at 21:57:43 and DELETED the 3.15.0_1 keg the running
gateway had been started from. The process kept running from files that no
longer existed. 95 seconds later every request to the :8200 file server was
dropped (`RemoteDisconnected` → the PWA's `MediaError code 4`, skipping a
whole album in 14 s), and from 22:28 macOS refused the process access to
/Volumes/SAMDATA (`[Errno 1] Operation not permitted`) although the disk was
mounted. Internet radio kept working — it touches neither — so the gateway
looked half-alive for two and a half days, until a manual restart fixed it
instantly. Nothing in the app was wrong; the process was simply stale.

So this watches the FILES the process runs from — the real interpreter
binary, the stdlib, and the venv's site-packages — fingerprinted as
`(st_dev, st_ino)` at boot. Gone or replaced (a `brew reinstall` keeps the
path but changes the inode; a venv rebuild moves site-packages) for
`confirm` consecutive checks → the worker exits NON-ZERO.

Why non-zero, and why that restarts us: hypercorn's master treats a worker
exiting 0 as "respawn it" — from the master's own, equally stale, interpreter
path, which is the deleted one. A non-zero exit instead makes the master shut
down and exit, and launchd's `KeepAlive` starts a fresh job from the plist,
whose venv python resolves through `/opt/homebrew/opt/` to the NEW keg.

Three refusals:
  * Never restart while there is nothing to restart INTO (`replacement_ready`):
    mid-upgrade, or python uninstalled, an exit would only become a launchd
    respawn loop. Wait, warn, and re-check.
  * Never act on one sample: brew moves files around while it works.
  * Never restart while a MAINTENANCE run holds `.maintenance.pid`.
    `tools/upgrade.py --apply` renames `.venv` aside and spends minutes
    building and GATING a new one before it restarts the gateway itself;
    without this, the watch would see site-packages vanish and restart the
    gateway onto a half-installed, ungated venv — the one thing that tool's
    design exists to prevent. `setup.sh` holds it while it (re)builds the
    venv. The file holds the holder's PID, so a run that crashed and left it
    behind is ignored rather than switching the watch off for good.

Env: RUNTIME_WATCH_SEC (default 60; 0 disables).
"""
import logging
import os
import sys
import threading
import time
from collections.abc import Callable

import dlna_alerts

log = logging.getLogger("dlna.runtime")

# EX_TEMPFAIL — "try again later", which is exactly what we are asking for.
EXIT_CODE = 75

Fingerprint = dict[str, tuple[int, int] | None]

MAINTENANCE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".maintenance.pid")


def maintenance_holder(path: str = MAINTENANCE_FILE) -> int | None:
    """PID of the live process holding the maintenance marker, or None
    (no marker, unreadable, or its holder is dead — a stale marker)."""
    try:
        with open(path) as fh:
            pid = int(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None                     # crashed run left it behind
    except PermissionError:
        return pid                      # alive, just not ours to signal
    return pid


class maintenance:
    """`with maintenance():` — hold the marker for the duration of a venv
    swap. Used by tools/upgrade.py; setup.sh writes the same file in bash."""

    def __init__(self, path: str = MAINTENANCE_FILE):
        self.path = path

    def __enter__(self):
        with open(self.path, "w") as fh:
            fh.write(f"{os.getpid()}\n")
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except FileNotFoundError:
            log.debug("maintenance marker already gone: %s", self.path)
        return False


def runtime_files() -> list:
    """The files whose disappearance means this process is running stale:
    the interpreter binary (resolved), the stdlib, and — when importable —
    the venv's site-packages (via hypercorn, the one package that is
    certainly loaded)."""
    paths = [os.path.realpath(sys.executable), os.path.realpath(os.__file__)]
    try:
        import hypercorn
        paths.append(os.path.realpath(hypercorn.__file__))
    except ImportError:
        log.debug("runtime watch: hypercorn not importable; not fingerprinted")
    return paths


def _stat_id(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None                    # gone (or unreadable) — the signal
    return (st.st_dev, st.st_ino)


def fingerprint(paths) -> Fingerprint:
    return {p: _stat_id(p) for p in paths}


def stale_reason(boot: Fingerprint, now: Fingerprint) -> str | None:
    """Pure: why `now` says the process is running stale, or None.
    A path that could not be fingerprinted at BOOT is never judged —
    we cannot tell a change from something that was never there."""
    for path, then in boot.items():
        if then is None:
            continue
        cur = now.get(path)
        if cur is None:
            return f"{path} is gone"
        if cur != then:
            return f"{path} was replaced"
    return None


def replacement_ready(prefix: str | None = None) -> bool:
    """Is there a Python for launchd to restart us INTO? The venv's
    `bin/python3` resolved through its symlinks to a real executable."""
    exe = os.path.realpath(os.path.join(prefix or sys.prefix, "bin", "python3"))
    return os.path.isfile(exe) and os.access(exe, os.X_OK)


class RuntimeWatch:
    """One check per `tick()`; the thread just calls it on a timer. Kept as
    an object with injected `exit_fn` / `ready_fn` so the decision can be
    tested without killing the test runner."""

    def __init__(self, paths=None, *, confirm: int = 2,
                 exit_fn: Callable[[int], None] = os._exit,
                 ready_fn: Callable[[], bool] = replacement_ready,
                 holder_fn: Callable[[], int | None] = maintenance_holder):
        self.paths = list(paths if paths is not None else runtime_files())
        self.boot = fingerprint(self.paths)
        self.confirm = max(1, confirm)
        self.exit_fn = exit_fn
        self.ready_fn = ready_fn
        self.holder_fn = holder_fn
        self.strikes = 0
        self._warned_not_ready = False
        self._noted_holder = None

    def tick(self) -> str:
        """Returns what it did: 'ok' | 'suspect' | 'deferred' | 'waiting' |
        'exit'."""
        reason = stale_reason(self.boot, fingerprint(self.paths))
        if reason is None:
            if self.strikes:
                log.info("runtime watch: runtime files are back; standing down")
            self.strikes = 0
            self._warned_not_ready = False
            self._noted_holder = None
            return "ok"
        self.strikes += 1
        if self.strikes < self.confirm:
            log.warning(f"runtime watch: {reason} — re-checking before acting")
            return "suspect"
        holder = self.holder_fn()
        if holder is not None:
            if holder != self._noted_holder:
                log.info(f"runtime watch: {reason}, but maintenance (pid "
                         f"{holder}) is in progress — it restarts the gateway "
                         f"itself; standing by")
                self._noted_holder = holder
            return "deferred"
        if not self.ready_fn():
            if not self._warned_not_ready:
                log.warning(f"runtime watch: {reason}, but no replacement "
                            f"Python is installed yet — not restarting")
                dlna_alerts.alert(
                    "runtime_stale", "half-alive — Python is gone",
                    f"{reason}. No replacement Python is installed yet, so "
                    f"the gateway cannot restart itself; music from the "
                    f"library will fail until it does. Install the Python "
                    f"(brew) and it restarts on its own.",
                    priority="high", tags=("warning",))
                self._warned_not_ready = True
            return "waiting"
        log.critical(f"runtime watch: {reason} — this process is running a "
                     f"Python that is no longer on disk (a brew upgrade?). "
                     f"Exiting {EXIT_CODE} so launchd restarts the gateway "
                     f"on the new one.")
        # This process cannot be trusted to make a TLS call any more; the
        # NEXT one reports the restart (dlna_alerts.on_boot).
        dlna_alerts.note_runtime_exit(reason)
        for h in logging.getLogger().handlers + log.handlers:
            h.flush()
        self.exit_fn(EXIT_CODE)
        return "exit"


def start_runtime_watch(interval: float | None = None) -> RuntimeWatch | None:
    """Start the watch thread. Returns the watch (None when disabled)."""
    if interval is None:
        try:
            interval = float(os.environ.get("RUNTIME_WATCH_SEC", "60"))
        except ValueError:
            interval = 60.0
    if interval <= 0:
        log.info("runtime watch: disabled (RUNTIME_WATCH_SEC=0)")
        return None
    watch = RuntimeWatch()

    def _loop():
        while True:
            time.sleep(interval)
            try:
                watch.tick()
            except Exception as e:                            # noqa: BLE001
                # A broken watchdog must never take the gateway with it.
                log.warning(f"runtime watch: check failed: {e!r}")

    threading.Thread(target=_loop, daemon=True, name="runtime-watch").start()
    log.info(f"runtime watch: started, every {int(interval)}s over "
             f"{len(watch.paths)} file(s): {', '.join(watch.paths)}")
    return watch
