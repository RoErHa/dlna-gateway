"""Security headers + the CSP violation endpoint (hardening #4, 2026-11-04).

dlna_asgi_headers.py holds the reasoning. The browser suite proves the policy
does not break the PWA (it runs ENFORCED there, and any violation fails the
test); this file proves the server sends what it should, leaves the renderers'
/gw/* surface alone, and that the one exception the browser suite makes
('unsafe-eval', for Playwright's own predicates) cannot hide a real problem:
the app never evaluates strings.
"""
import asyncio
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlna_asgi  # noqa: E402
import dlna_asgi_headers as H  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(app, method="GET", path="/", body=b"", headers=()):
    """Drive an ASGI app; return (status, headers dict, body)."""
    sent, chunks = [], [body]

    async def receive():
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": False}
        # A real client stays connected until the response is done; Starlette
        # listens for the disconnect meanwhile and cancels this when finished.
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "", "scheme": "http",
             "http_version": "1.1", "server": ("192.168.1.125", 8765),
             "headers": [(b"host", b"192.168.1.125:8765"), *headers],
             "client": ("192.168.1.50", 5000)}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    hdrs = {k.decode().lower(): v.decode() for k, v in start.get("headers") or ()}
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], hdrs, data


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"ok"})


class TestPolicy(unittest.TestCase):
    def test_script_is_self_only(self):
        """The directive the whole item exists for."""
        self.assertIn("script-src 'self';", H.POLICY + ";")
        for loose in ("'unsafe-inline' https", "'unsafe-eval'", "script-src *"):
            self.assertNotIn(loose, H.POLICY.split("style-src")[0])

    def test_framing_objects_and_base_are_shut(self):
        for d in ("frame-ancestors 'none'", "object-src 'none'", "base-uri 'none'"):
            self.assertIn(d, H.POLICY)

    def test_modes(self):
        names = lambda m: {k for k, _ in H.header_list(m)}  # noqa: E731
        common = {b"x-content-type-options", b"referrer-policy", b"x-frame-options"}
        self.assertEqual(names("enforce"), common | {b"content-security-policy"})
        self.assertEqual(names("report-only"),
                         common | {b"content-security-policy-report-only"})
        self.assertEqual(names("off"), common)
        ro = dict(H.header_list("report-only"))[b"content-security-policy-report-only"]
        self.assertTrue(ro.endswith(b"report-uri /api/csp_report"))

    def test_enforced_still_reports(self):
        """Enforced, a blocked script is otherwise a dead button with
        nothing in the log."""
        en = dict(H.header_list("enforce"))[b"content-security-policy"]
        self.assertTrue(en.endswith(b"report-uri /api/csp_report"))

    def test_mode_comes_from_env_and_defaults_to_enforce(self):
        self.assertEqual(H.csp_mode({}), "enforce")
        self.assertEqual(H.csp_mode({"CSP_MODE": "Report-Only"}), "report-only")
        self.assertEqual(H.csp_mode({"CSP_MODE": "nonsense"}), "enforce")


class TestMiddleware(unittest.TestCase):
    def test_headers_are_added(self):
        _, h, _ = call(H.SecurityHeaders(_ok_app, mode="enforce"), path="/api/state")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertEqual(h["referrer-policy"], "same-origin")
        self.assertEqual(h["x-frame-options"], "DENY")
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])

    def test_renderer_surface_is_untouched(self):
        _, h, _ = call(H.SecurityHeaders(_ok_app, mode="enforce"), path="/gw/device.xml")
        self.assertNotIn("content-security-policy", h)
        self.assertNotIn("x-content-type-options", h)

    def test_a_route_header_is_never_overridden(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"referrer-policy", b"no-referrer")]})
            await send({"type": "http.response.body", "body": b""})
        _, h, _ = call(H.SecurityHeaders(app, mode="enforce"))
        self.assertEqual(h["referrer-policy"], "no-referrer")

    def test_non_http_scopes_pass(self):
        seen = []

        async def app(scope, receive, send):
            seen.append(scope["type"])
        asyncio.run(H.SecurityHeaders(app, mode="enforce")({"type": "lifespan"}, None, None))
        self.assertEqual(seen, ["lifespan"])

    def test_mounted_inside_the_guard(self):
        classes = [m.cls for m in dlna_asgi.app.user_middleware]
        self.assertIn(H.SecurityHeaders, classes)
        # user_middleware is outermost-first: the guard must wrap the headers.
        from dlna_asgi_guard import RequestGuard
        self.assertLess(classes.index(RequestGuard), classes.index(H.SecurityHeaders))

    def test_the_real_app_sends_them_on_the_pwa(self):
        status, h, _ = call(dlna_asgi.app, path="/static/app.css")
        self.assertEqual(status, 200)
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertTrue("content-security-policy-report-only" in h
                        or "content-security-policy" in h)


class TestReports(unittest.TestCase):
    def setUp(self):
        H._warned.clear()
        H._window.update(start=0.0, lines=0)

    def post(self, body):
        return call(dlna_asgi.app, "POST", H.REPORT_PATH, body,
                    headers=[(b"content-type", b"application/csp-report")])

    def test_report_uri_format_is_logged(self):
        body = json.dumps({"csp-report": {
            "effective-directive": "script-src-attr", "blocked-uri": "inline",
            "document-uri": "https://gw/", "source-file": "https://gw/static/app.js",
            "line-number": 12}}).encode()
        with self.assertLogs("dlna.csp", "WARNING") as logs:
            status, _, _ = self.post(body)
        self.assertEqual(status, 204)
        self.assertIn("script-src-attr", logs.output[0])
        self.assertIn("app.js:12", logs.output[0])

    def test_reporting_api_format_is_understood(self):
        v = H._violations(json.dumps([{"type": "csp-violation", "body": {
            "effectiveDirective": "img-src", "blockedURL": "https://x/y.png"}}]).encode())
        self.assertEqual(v[0]["blockedURL"], "https://x/y.png")

    def test_garbage_is_ignored(self):
        for b in (b"", b"not json", b"[1,2]", b'{"x":1}'):
            self.assertEqual(H._violations(b), [])
        self.assertEqual(self.post(b"\xff\xfe")[0], 204)

    def test_rate_limited_and_bounded(self):
        v = {"effective-directive": "img-src", "blocked-uri": "https://x/a"}
        self.assertTrue(H.log_violation(v, now=1000))
        self.assertFalse(H.log_violation(v, now=1001))
        self.assertTrue(H.log_violation(v, now=1000 + H._WARN_EVERY_SEC + 1))
        with self.assertLogs("dlna.csp", "WARNING"):
            for i in range(H._WARN_KEYS_MAX + 20):
                H.log_violation({"blocked-uri": f"https://x/{i}"}, now=5000)
        self.assertLessEqual(len(H._warned), H._WARN_KEYS_MAX)

    def test_fake_reports_cannot_flood_the_log(self):
        H._window.update(start=0.0, lines=0)
        with self.assertLogs("dlna.csp", "WARNING") as logs:
            for i in range(200):
                H.log_violation({"blocked-uri": f"https://x/{i}"}, now=9000)
        self.assertEqual(len(logs.output), H._LINES_PER_WINDOW)
        self.assertTrue(H.log_violation({"blocked-uri": "https://x/new"},
                                        now=9000 + H._WARN_EVERY_SEC))

    def test_a_null_origin_report_reaches_the_route_through_the_guard(self):
        status, _, _ = call(dlna_asgi.app, "POST", H.REPORT_PATH, b"{}",
                            headers=[(b"origin", b"null")])
        self.assertEqual(status, 204)

    def test_oversized_report_is_refused(self):
        self.assertEqual(self.post(b"x" * (H._REPORT_MAX_BYTES + 1))[0], 413)


class TestTheAppNeedsNoLooseScript(unittest.TestCase):
    """What lets the browser suite add 'unsafe-eval' for Playwright without
    hiding anything, and what keeps `script-src 'self'` true."""

    def read(self, name):
        with open(os.path.join(PROJECT, "static", name), encoding="utf-8") as f:
            return f.read()

    def test_no_string_evaluation(self):
        for name in ("app.js", "sw.js"):
            code = self.read(name)
            for pat in (r"\beval\s*\(", r"new\s+Function\s*\(",
                        r"set(?:Timeout|Interval)\s*\(\s*[\"'`]"):
                self.assertIsNone(re.search(pat, code), f"{name}: {pat}")

    def test_no_inline_handlers_or_scripts(self):
        html = self.read("index.html")
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", html))
        self.assertIsNone(re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html))
        self.assertIsNone(re.search(r"javascript:", html, re.I))
        app = self.read("app.js")
        # Markup app.js builds: an on…=" attribute inside a template string.
        self.assertIsNone(re.search(r"<[^>`]*\son(?:click|error|change|load|input|"
                                    r"focus|mouseover|keydown|submit)=\"", app))


if __name__ == "__main__":
    unittest.main()


class TestNoThirdParty(unittest.TestCase):
    """The PWA loads nothing from another origin (2026-11-08). The fonts were
    the last thing: Google Fonts saw the time and IP of every page view."""

    def test_the_policy_names_no_other_origin(self):
        self.assertNotIn("http", H.POLICY)

    def test_index_html_links_nothing_off_site(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "static", "index.html")
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        self.assertEqual(re.findall(r'(?:href|src)="(https?://[^"]+)"', html), [])

    def test_the_fonts_are_ours(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "static", "app.css"), encoding="utf-8") as fh:
            css = fh.read()
        urls = re.findall(r"url\(([^)]+)\)", css)
        fonts = [u for u in urls if u.endswith(".woff2")]
        self.assertGreaterEqual(len(fonts), 7)
        for u in fonts:
            self.assertFalse(u.startswith(("http", "//")), u)
            with open(os.path.join(root, "static", u), "rb") as fh:
                self.assertEqual(fh.read(4), b"wOF2", u)
