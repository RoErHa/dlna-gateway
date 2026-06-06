#!/usr/bin/env python3
"""
dlna_fdmon.py — open-file-descriptor watchdog (diagnostic).

An FD LEAK is invisible until the process hits its soft RLIMIT_NOFILE, at
which point it surfaces as a cascade of `sqlite3.OperationalError: unable to
open database file`, `OSError: [Errno 24] Too many open files`, and
`asyncio: socket.accept() out of system resource` — and the UI dies. By then
the log only shows the *symptoms*, not which resource leaked.

This watchdog runs in a daemon thread and periodically records the open-FD
count vs the soft limit, so a leak shows up in gateway.log as a RISING
trajectory long before the crash. When usage crosses a danger threshold it
shells out to `lsof` once and logs a category breakdown (socket peers / file
dirs / fd types), so the next time it climbs we can see *what* is
accumulating (e.g. many sockets to one host, or many open files in one dir).

Wired in from dlna_gateway.start_background_services so it covers both the
stdlib entrypoint and the 2.0 ASGI lifespan. Read-only; never affects serving.
"""
import logging
import os
import resource
import shutil
import subprocess
import threading
import time
from collections import Counter

log = logging.getLogger("dlna.fdmon")


def fd_count() -> int:
    """Open file descriptors held by THIS process, or -1 if unsupported."""
    for d in ("/dev/fd", f"/proc/{os.getpid()}/fd"):
        try:
            return len(os.listdir(d))
        except OSError:
            continue
    return -1


def soft_limit() -> int:
    try:
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:                                # noqa: BLE001
        return -1


def lsof_breakdown(pid: int) -> str:
    """One-line `lsof` summary: fd types, top socket peers, top file dirs.
    Best-effort — returns a marker string if lsof is missing or fails."""
    lsof = shutil.which("lsof")
    if not lsof:
        return "(lsof unavailable)"
    try:
        out = subprocess.run([lsof, "-nP", "-p", str(pid)],
                             capture_output=True, text=True,
                             timeout=20).stdout
    except Exception as e:                           # noqa: BLE001
        return f"(lsof failed: {type(e).__name__}: {e})"
    types: Counter = Counter()
    peers: Counter = Counter()
    dirs: Counter = Counter()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5:
            continue
        typ = parts[4]
        types[typ] += 1
        if typ in ("IPv4", "IPv6"):
            name = parts[8] if len(parts) > 8 else ""
            if "->" in name:                         # established: keep the peer host
                peers[name.split("->", 1)[1].rsplit(":", 1)[0]] += 1
            else:                                    # listen / half-open
                peers["LISTEN/" + name] += 1
        elif typ == "REG":
            dirs[os.path.dirname(parts[-1])] += 1
    return (f"types={dict(types.most_common(8))} "
            f"socket_peers={dict(peers.most_common(8))} "
            f"file_dirs={dict(dirs.most_common(5))}")


def start_fd_monitor(interval: float = 15.0,
                     warn_frac: float = 0.50,
                     dump_frac: float = 0.70,
                     heartbeat_every: int = 16) -> None:
    """Start the FD watchdog daemon thread.

    interval        — seconds between samples (default 15)
    warn_frac       — log a WARN once usage is this fraction of the limit
    dump_frac       — log a WARN + lsof breakdown above this fraction
    heartbeat_every — also log an INFO baseline every Nth sample (default 16
                      = every 4 min) so the steady-state count is visible too
    """
    pid = os.getpid()
    if fd_count() < 0:
        log.info("FD monitor: platform exposes no /dev/fd or /proc — disabled")
        return

    def _loop():
        peak = 0
        last_dump_at = 0
        tick = 0
        while True:
            n = fd_count()
            lim = soft_limit()
            frac = (n / lim) if lim > 0 else 0.0
            if n > peak + 100:                       # visible jump → record it
                peak = n
                log.info(f"FD high-water: {n}/{lim} ({frac:.0%})")
            if frac >= dump_frac and n > last_dump_at + max(50, lim * 0.05):
                last_dump_at = n
                log.warning(f"FD usage HIGH {n}/{lim} ({frac:.0%}) — "
                            f"breakdown: {lsof_breakdown(pid)}")
            elif frac >= warn_frac:
                log.warning(f"FD usage rising {n}/{lim} ({frac:.0%})")
            elif tick % heartbeat_every == 0:
                log.info(f"FD usage {n}/{lim} ({frac:.0%})")
            tick += 1
            time.sleep(interval)

    threading.Thread(target=_loop, daemon=True, name="fd-monitor").start()
    log.info(f"FD monitor started: {fd_count()}/{soft_limit()} fds, "
             f"interval={int(interval)}s warn@{int(warn_frac*100)}% "
             f"dump@{int(dump_frac*100)}%")
