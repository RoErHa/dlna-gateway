#!/usr/bin/env python3
"""
dlna_auth_throttle.py — slows down password guessing on the Subsonic API.

WHY (2026-11-04, hardening timeline #6). `/rest/*` is the one surface behind
a password, and nothing limited how fast it could be guessed: every attempt
was answered at full speed, forever. It matters most if the gateway is ever
reachable beyond the tailnet, and it is cheap.

THE RULE, per client IP (hypercorn terminates every connection, so the peer
address is real — the same reasoning as `POST /api/renderers/forget`):

  * MAX_FAILURES failed logins within WINDOW_SEC → that IP is BLOCKED for
    BLOCK_SEC. While blocked, every request answers the ordinary Subsonic
    "wrong username or password" error WITHOUT checking the credentials, so
    a guess made while blocked can never succeed, and the answer reveals
    nothing (posture item 2: no oracle).
  * Each further trip doubles the block, up to MAX_BLOCK_SEC.
  * A successful login forgets the IP entirely. A strike record with no new
    failure for FORGET_SEC is dropped as well.

Amperfy's normal traffic cannot trip it: a client with the right password
never fails, and only failures are counted. What CAN trip it is a client
whose stored password went stale (the password was changed in `.env`) —
which is a client that needs its password updated anyway; it is let back in
a minute after it stops failing.

BOUNDED (posture rule 10): at most MAX_TRACKED addresses. When full, the
expired records go first, then the least recently seen non-blocked ones,
and only when every record is a block, the block closest to expiring.

An IPv6 address is keyed by its /64: one host is handed a whole /64 and can
pick a fresh address from it for every guess.

Pure apart from the clock, which is injected, so tests/test_subsonic.py
drives it directly.
"""
from __future__ import annotations

import ipaddress
import threading
import time

MAX_FAILURES = 10
WINDOW_SEC = 60.0
BLOCK_SEC = 60.0
MAX_BLOCK_SEC = 3600.0
FORGET_SEC = 24 * 3600.0
MAX_TRACKED = 1024


def client_key(ip: str) -> str:
    """The throttle key for a peer address: an IPv4 address as is, an IPv6
    address as its /64 (an IPv4-mapped one as the IPv4 address)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if a.version == 6:
        if a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(ipaddress.ip_network(f"{a}/64", strict=False))
    return str(a)


class AuthThrottle:
    def __init__(self, clock=time.monotonic, max_tracked: int = MAX_TRACKED):
        self._clock = clock
        self._max = max_tracked
        self._lock = threading.Lock()
        # ip -> {"fails", "window_start", "trips", "blocked_until", "seen"}
        self._ips: dict[str, dict] = {}

    def blocked(self, ip: str) -> float:
        """Seconds this IP is still blocked for; 0.0 when it may try."""
        ip = client_key(ip)
        with self._lock:
            rec = self._ips.get(ip)
            if not rec:
                return 0.0
            return max(0.0, rec["blocked_until"] - self._clock())

    def failure(self, ip: str) -> float:
        """Count one failed login. Returns the block length in seconds when
        this failure TRIPPED a block (the caller logs/alerts once), else 0."""
        now, ip = self._clock(), client_key(ip)
        with self._lock:
            rec = self._ips.get(ip)
            if rec is not None and now - rec["seen"] > FORGET_SEC \
                    and rec["blocked_until"] <= now:
                del self._ips[ip]                  # a day quiet: clean slate
                rec = None
            if rec is None:
                self._make_room(now)
                rec = self._ips[ip] = {"fails": 0, "window_start": now,
                                       "trips": 0, "blocked_until": 0.0,
                                       "seen": now}
            rec["seen"] = now
            if now - rec["window_start"] > WINDOW_SEC:
                rec["fails"], rec["window_start"] = 0, now
            rec["fails"] += 1
            if rec["fails"] < MAX_FAILURES:
                return 0.0
            length = min(MAX_BLOCK_SEC, BLOCK_SEC * (2 ** min(rec["trips"], 12)))
            rec["trips"] += 1
            rec["blocked_until"] = now + length
            rec["fails"], rec["window_start"] = 0, now
            return length

    def success(self, ip: str) -> None:
        with self._lock:
            self._ips.pop(client_key(ip), None)

    def tracked(self) -> int:
        with self._lock:
            return len(self._ips)

    def _make_room(self, now: float) -> None:
        """Called with the lock held, before adding one record."""
        if len(self._ips) < self._max:
            return
        for ip in [ip for ip, r in self._ips.items()
                   if r["blocked_until"] <= now and now - r["seen"] > FORGET_SEC]:
            del self._ips[ip]
        if len(self._ips) < self._max:
            return
        # Least recently seen non-blocked records first; only if every
        # record is a block, the one closest to expiring — a hard bound.
        order = sorted(self._ips.items(),
                       key=lambda kv: (kv[1]["blocked_until"] > now,
                                       kv[1]["blocked_until"], kv[1]["seen"]))
        for ip, _ in order[: len(self._ips) - self._max + 1]:
            del self._ips[ip]


THROTTLE = AuthThrottle()
