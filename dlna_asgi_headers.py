#!/usr/bin/env python3
"""
dlna_asgi_headers.py — the security headers every browser-facing response
carries, and the endpoint that hears about violations of the policy.

WHY (2026-11-04, hardening timeline #4). Defence in depth for the PWA, and
what a public project is expected to send:

  Content-Security-Policy   what the page may load and run. The point is
                            `script-src 'self'`: an injected <script>, an
                            inline `onclick=` or a `javascript:` URL does not
                            run, so an escaping bug (posture item 4) stops
                            being code execution.
  frame-ancestors 'none'    (+ X-Frame-Options DENY for old engines) — no
                            other site can frame the PWA to trick a click.
  X-Content-Type-Options    nosniff — a response is only ever what its
                            Content-Type says.
  Referrer-Policy           same-origin — the gateway's URLs (which name your
                            tracks) are never sent to another site.

THE POLICY, directive by directive, and why each source is there:

  default-src 'self'
  script-src  'self'             app.js + the vendored hls.js. No inline
                                 script and no inline handlers: app.js binds
                                 every event itself (data-act / data-onerr,
                                 see "CSP" in app.js).
  style-src   'self' 'unsafe-inline' https://fonts.googleapis.com
                                 'unsafe-inline' because app.js builds HTML
                                 with style="" attributes in ~100 places. CSS
                                 cannot run script; the cost of allowing it is
                                 far smaller than script's.
  font-src    'self' https://fonts.gstatic.com    the Google Fonts files.
  img-src     'self' data: blob:  every cover goes through same-origin /art.
  media-src   'self' blob:        /stream, /radio_stream, video; hls.js feeds
                                 MSE from blob: URLs.
  worker-src  'self' blob:        sw.js, and hls.js's blob: worker.
  connect-src 'self'              fetch + the SSE stream.
  manifest-src 'self'
  object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'

ROLLOUT — `CSP_MODE` in .env: `enforce` (the default since 2026-11-08, after
four days report-only on the owner's iPhone and Mac logged no violation),
`report-only`, or `off`. Both CSP modes POST each violation to
/api/csp_report, which logs it (`csp ✗ violation (<mode>) …`, rate-limited,
bounded memory); enforced, that line is the only trace of something the
policy broke. In report-only the browser blocks nothing — the switch back
if enforcing ever breaks the PWA. The test suites run the
policy ENFORCED on both engines, so report-only is only for what they cannot
see — a real iPhone, a real Safari.

`/gw/*` is left untouched: renderers ignore these headers, and an embedded
HTTP client with a small header buffer is not worth the risk.

Guarded by tests/test_security_headers.py.
"""
from __future__ import annotations

import json
import logging
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import Response

log = logging.getLogger("dlna.csp")

POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    "connect-src 'self'",
    "manifest-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])
REPORT_PATH = "/api/csp_report"
_MODES = ("report-only", "enforce", "off")
DEFAULT_MODE = "enforce"   # since 2026-11-08 (hardening #4)

_REPORT_MAX_BYTES = 16 * 1024
_WARN_EVERY_SEC = 600.0
_WARN_KEYS_MAX = 256
# Reports are exempt from the guard's Origin rule (WebKit sends `null`), so
# any page could post fake ones: an overall ceiling keeps that from flooding
# the log, whatever the keys.
_LINES_PER_WINDOW = 30
_window = {"start": 0.0, "lines": 0}


def csp_mode(env=None) -> str:
    v = ((os.environ if env is None else env).get("CSP_MODE") or "").strip().lower()
    return v if v in _MODES else DEFAULT_MODE


def header_list(mode: str) -> list[tuple[bytes, bytes]]:
    """The headers for one response. Pure, so the exact set is tested."""
    out = [(b"x-content-type-options", b"nosniff"),
           (b"referrer-policy", b"same-origin"),
           (b"x-frame-options", b"DENY")]
    # Enforced, the browser still REPORTS: without report-uri a blocked
    # script is a dead button and nothing in the log (2026-11-08).
    if mode == "enforce":
        out.append((b"content-security-policy",
                    f"{POLICY}; report-uri {REPORT_PATH}".encode()))
    elif mode == "report-only":
        out.append((b"content-security-policy-report-only",
                    f"{POLICY}; report-uri {REPORT_PATH}".encode()))
    return out


class SecurityHeaders:
    """Pure ASGI: adds the headers to every HTTP response except /gw/*,
    without overriding one a route set itself. Streams are untouched — only
    the response-start message is edited."""

    def __init__(self, app, mode: str | None = None):
        self.app = app
        self.headers = header_list(mode or csp_mode())
        self.mode = mode or csp_mode()
        log.info(f"security headers: CSP {self.mode}")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or str(scope.get("path", "")).startswith("/gw/"):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers") or ()}
                extra = [(k, v) for k, v in self.headers if k not in have]
                if extra:
                    message = dict(message)
                    message["headers"] = list(message.get("headers") or ()) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ── violation reports ──────────────────────────────────────────────────
router = APIRouter()
_warned: dict = {}


def _violations(body: bytes) -> list[dict]:
    """Both report formats: `report-uri` sends {"csp-report": {...}}, the
    Reporting API sends a list of {"type": "csp-violation", "body": {...}}."""
    try:
        data = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("csp-report"), dict):
        return [data["csp-report"]]
    if isinstance(data, list):
        return [r["body"] for r in data
                if isinstance(r, dict) and isinstance(r.get("body"), dict)]
    return []


def _field(v: dict, *names: str) -> str:
    for n in names:
        if v.get(n):
            return str(v[n])[:200]
    return ""


def log_violation(v: dict, now: float | None = None) -> bool:
    """Log one violation, at most once per (directive, blocked) per 10 min.
    True when it was logged."""
    directive = _field(v, "effective-directive", "effectiveDirective",
                       "violated-directive")
    blocked = _field(v, "blocked-uri", "blockedURL")
    key = (directive, blocked)
    now = time.monotonic() if now is None else now
    if now - _warned.get(key, -_WARN_EVERY_SEC) < _WARN_EVERY_SEC:
        return False
    if now - _window["start"] >= _WARN_EVERY_SEC:
        _window.update(start=now, lines=0)
    if _window["lines"] >= _LINES_PER_WINDOW:
        return False
    _window["lines"] += 1
    if len(_warned) >= _WARN_KEYS_MAX:          # the report body is the caller's
        _warned.clear()
    _warned[key] = now
    log.warning(f"csp ✗ violation ({csp_mode()}) directive={directive!r} "
                f"blocked={blocked!r} page={_field(v, 'document-uri', 'documentURL')!r} "
                f"source={_field(v, 'source-file', 'sourceFile')}:"
                f"{_field(v, 'line-number', 'lineNumber')} "
                f"sample={_field(v, 'script-sample', 'sample')!r}")
    return True


@router.post(REPORT_PATH, include_in_schema=False)
async def csp_report_route(request: Request):
    body = b""
    async for chunk in request.stream():         # bounded: posture rule 10
        body += chunk
        if len(body) > _REPORT_MAX_BYTES:
            return Response(status_code=413)
    for v in _violations(body)[:20]:
        log_violation(v)
    return Response(status_code=204)
