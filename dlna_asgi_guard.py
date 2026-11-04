#!/usr/bin/env python3
"""
dlna_asgi_guard.py — which BROWSER requests the gateway will answer.

WHY THIS EXISTS (2026-11-04). The security posture says "the LAN and the
tailnet are the access control". That leaves one way in that is neither: the
owner's own browser. Any web page opened on a LAN device runs inside the
network and can send requests to this gateway. Two attacks follow:

  * CSRF — a page cannot READ our replies (CORS), but it does not need to in
    order to act. `<img src=".../api/playlist/delete?id=1">` deleted a
    playlist (ids are sequential, so it could delete all of them), and a
    `text/plain` POST — which a browser sends without a CORS preflight —
    reached `/api/render_queue`, `/api/control` and `/api/edit_track`,
    because the handlers parse the body as JSON whatever its Content-Type.
  * DNS rebinding — a page re-points ITS OWN hostname at our IP. The browser
    then treats us as same-origin with the attacker and lets it read every
    reply: the library, playlists, the whole API.

THE TWO RULES, and why each is shaped the way it is:

1. **The Host header must name this gateway.** Rebinding is the one attack
   that cannot fake this: the browser sends the ATTACKER's hostname, because
   that is what it resolved. So any IP-literal Host is allowed (a rebinding
   browser never sends one) — no list of this machine's addresses to keep up
   to date, and the Naim/LG/Amperfy reaching us by IP need nothing. A NAME
   must be `localhost`, this Mac's own name, the Tailscale certificate host
   (and its MagicDNS short form), or one listed in `GATEWAY_ALLOWED_HOSTS`.
   A request with no Host at all (HTTP/1.0 devices) is allowed: every
   browser sends one.

2. **A cross-site browser request may only be a top-level navigation.**
   Browsers mark where a request came from in `Sec-Fetch-Site` (all current
   engines, Safari since 16.4). `cross-site` is refused unless it is a GET
   navigation — following a link to the PWA must keep working. `same-site`
   is allowed on purpose: it means another port on THIS host, i.e. software
   already running here. Devices (the Naim, the TV, Amperfy, curl) send no
   Sec-Fetch headers at all and are never affected.

3. **Without Sec-Fetch-Site, a write's `Origin` must name the request's own
   host.** This is NOT just a fallback for old browsers: measured against
   the live gateway in real Chromium and WebKit (2026-11-04), browsers send
   Sec-Fetch-* only to HTTPS origins, so on the plain `:8765` listener the
   hostile cross-site POST arrived with `Origin` and no `Sec-Fetch-Site`,
   and THIS rule is what refused it. Browsers send `Origin` on every
   cross-origin POST, `mode: "no-cors"` included.

   The corollary: on plain HTTP a cross-site GET carries nothing to judge
   it by, and passes. That is why every route that CHANGES state must be a
   POST (dlna_routes.QUERY_POST_ROUTES) — the method change is load-bearing
   on `:8765`, not tidiness. A cross-site GET can still not READ a reply
   (CORS), only cause one.

Refusals are a plain 403 and a rate-limited WARN naming the host and the
rule, so a legitimate name that was missed is a one-line `.env` fix, not a
debugging session. Pure ASGI (not a BaseHTTPMiddleware) so it adds no task
per request and never buffers the streaming relays.

Guarded by tests/test_request_guard.py.
"""
import ipaddress
import json
import logging
import os
import socket
import time
from urllib.parse import urlsplit

import dlna_alerts

log = logging.getLogger("dlna.guard")

_SAFE_METHODS = frozenset({"GET", "HEAD"})
# CSP violation reports (dlna_asgi_headers). WebKit sends them with
# `Origin: null`, which rule 3 rightly refuses for a real write — measured
# live 2026-11-04, and it would have hidden every iPhone/Safari report, the
# ones the report-only rollout exists for. A report can only add a
# rate-limited, globally capped log line, so it skips rules 2 and 3; the Host
# rule still applies.
_REPORT_PATHS = frozenset({"/api/csp_report"})
_WARN_EVERY_SEC = 60.0
_WARN_KEYS_MAX = 256        # the rate-limit memory must stay bounded too


def _hostname(value: str) -> str:
    """The hostname part of a Host / :authority value — port, brackets and a
    trailing dot removed, lowercased. '' for an empty or garbled value."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    if v.startswith("["):                       # [v6]:port
        end = v.find("]")
        return v[1:end] if end > 0 else ""
    if v.count(":") == 1:                       # name:port / v4:port
        v = v.split(":", 1)[0]
    return v.rstrip(".")


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def allowed_hostnames(env=None) -> frozenset:
    """The NAMES this gateway answers to. IP literals are always allowed and
    are deliberately not listed (see the module docstring, rule 1)."""
    env = os.environ if env is None else env
    names = {"localhost"}
    try:
        h = socket.gethostname().lower().rstrip(".")
        if h:
            names.add(h)
            short = h.split(".", 1)[0]
            names.update({short, short + ".local"})
    except OSError as e:
        log.debug(f"gethostname failed ({e}) — this Mac's name not allowed")
    ts = env.get("TAILSCALE_CERT_HOST", "").strip().lower().rstrip(".")
    if ts:
        names.update({ts, ts.split(".", 1)[0]})   # + the MagicDNS short name
    for extra in env.get("GATEWAY_ALLOWED_HOSTS", "").split(","):
        extra = _hostname(extra)
        if extra:
            names.add(extra)
    return frozenset(names)


def host_allowed(host_value: str, names: frozenset) -> bool:
    if not (host_value or "").strip():
        return True                             # no Host: not a browser
    name = _hostname(host_value)
    return bool(name) and (_is_ip(name) or name in names)


def refusal(method: str, headers: dict, names: frozenset, path: str = "") -> str:
    """Why this request must be refused — '' when it may proceed. `headers`
    is lowercased name → value. Pure, so the rules are tested directly."""
    host = headers.get("host") or headers.get(":authority", "")
    if not host_allowed(host, names):
        return "host"
    if method == "POST" and path in _REPORT_PATHS:
        return ""
    site = headers.get("sec-fetch-site", "").lower()
    if site == "cross-site":
        if method in _SAFE_METHODS and \
                headers.get("sec-fetch-mode", "").lower() == "navigate":
            return ""
        return "cross-site"
    if not site and method not in _SAFE_METHODS:
        origin = headers.get("origin", "").strip()
        if origin:
            o = _hostname(urlsplit(origin).netloc) if origin != "null" else ""
            if not o or o != _hostname(host):
                return "origin"
    return ""


class RequestGuard:
    """ASGI middleware applying `refusal()` to every HTTP request."""

    def __init__(self, app, names: frozenset | None = None):
        self.app = app
        self.names = allowed_hostnames() if names is None else names
        self._warned: dict = {}

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers") or ()}
        why = refusal(scope.get("method", "GET").upper(), headers, self.names,
                      str(scope.get("path", "")))
        if not why:
            await self.app(scope, receive, send)
            return
        self._warn(why, scope, headers)
        body = json.dumps({"error": "forbidden"}).encode()
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    def _warn(self, why: str, scope, headers: dict) -> None:
        host = headers.get("host") or headers.get(":authority", "")
        key = (why, host, headers.get("origin", ""))
        now = time.monotonic()
        if now - self._warned.get(key, -_WARN_EVERY_SEC) < _WARN_EVERY_SEC:
            return
        if len(self._warned) >= _WARN_KEYS_MAX:   # keys are attacker-chosen
            self._warned.clear()
        self._warned[key] = now
        client = (scope.get("client") or ("?",))[0]
        hint = (" — if this is a name you use, add it to GATEWAY_ALLOWED_HOSTS"
                " in .env" if why == "host" else "")
        log.warning(f"guard ✗ refused {scope.get('method')} {scope.get('path')}"
                    f" from {client}: {why} (host={host!r}"
                    f" origin={headers.get('origin', '')!r}"
                    f" site={headers.get('sec-fetch-site', '')!r}){hint}")
        # One push an hour at most (dlna_alerts rate-limits per kind): either
        # someone is probing, or a client you use was refused and needs a
        # GATEWAY_ALLOWED_HOSTS entry. Header text is clipped — it is the
        # caller's, not ours.
        dlna_alerts.alert(
            "guard_refused", "browser request refused",
            f"{scope.get('method')} {str(scope.get('path'))[:80]} from "
            f"{client}: {why} (host={host[:80]!r}). If this is one of your "
            f"own clients, add its name to GATEWAY_ALLOWED_HOSTS in .env.",
            priority="low", tags=("shield",))
