"""The browser request guard — CSRF and DNS rebinding (2026-11-04).

dlna_asgi_guard.py holds the reasoning. What is tested hardest here is the
half that would hurt to get wrong: every DEVICE (the Naim, the LG TV,
Amperfy, curl) sends no Sec-Fetch headers and often reaches us by IP, and
must never be refused; and following a link to the PWA from elsewhere is a
cross-site navigation that must keep working.
"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dlna_asgi  # noqa: E402
import dlna_asgi_guard as g  # noqa: E402
import dlna_routes  # noqa: E402

NAMES = frozenset({"localhost", "ronsmacmini", "ronsmacmini.tail5be6ad.ts.net",
                   "rons-mac-mini.local"})
TS = "ronsmacmini.tail5be6ad.ts.net:8443"


def why(method="GET", **headers):
    h = {k.replace("_", "-").lower(): v for k, v in headers.items()}
    return g.refusal(method, h, NAMES)


class TestHostRule(unittest.TestCase):
    def test_rebinding_hostname_is_refused(self):
        """The attack: the browser sends the ATTACKER's name as Host."""
        self.assertEqual(why(host="evil.example:8765"), "host")
        self.assertEqual(why(host="192-168-1-125.rebind.it"), "host")

    def test_any_ip_literal_is_allowed(self):
        for host in ("192.168.1.125:8765", "100.78.51.93", "127.0.0.1:8443",
                     "[::1]:8765", "[fe80::1]"):
            self.assertEqual(why(host=host), "", host)

    def test_known_names_with_or_without_port_case_or_trailing_dot(self):
        for host in (TS, "RonsMacMini.tail5be6ad.ts.net.", "ronsmacmini",
                     "localhost:8765", "Rons-Mac-mini.local:8765"):
            self.assertEqual(why(host=host), "", host)

    def test_no_host_is_a_device_not_a_browser(self):
        self.assertEqual(why(), "")

    def test_http2_authority_is_checked_when_host_is_absent(self):
        self.assertEqual(g.refusal("GET", {":authority": "evil.example"}, NAMES),
                         "host")
        self.assertEqual(g.refusal("GET", {":authority": TS}, NAMES), "")

    def test_garbled_host_is_refused(self):
        self.assertEqual(why(host="[::1"), "host")


class TestAllowedHostnames(unittest.TestCase):
    def test_tailscale_name_short_form_and_extras(self):
        env = {"TAILSCALE_CERT_HOST": "RonsMacMini.tail5be6ad.ts.net",
               "GATEWAY_ALLOWED_HOSTS": " music.home:8765 , Jukebox ,"}
        with mock.patch.object(g.socket, "gethostname",
                               return_value="Rons-Mac-mini.local"):
            names = g.allowed_hostnames(env)
        for n in ("localhost", "ronsmacmini.tail5be6ad.ts.net", "ronsmacmini",
                  "music.home", "jukebox", "rons-mac-mini.local",
                  "rons-mac-mini"):
            self.assertIn(n, names)
        self.assertNotIn("", names)

    def test_nothing_configured_still_allows_localhost(self):
        with mock.patch.object(g.socket, "gethostname", side_effect=OSError):
            self.assertEqual(g.allowed_hostnames({}), frozenset({"localhost"}))


class TestCrossSiteRule(unittest.TestCase):
    def test_cross_site_writes_are_refused(self):
        """A `text/plain` POST from another site needs no CORS preflight."""
        for m in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual(why(m, host=TS, sec_fetch_site="cross-site",
                                 sec_fetch_mode="no-cors"), "cross-site", m)

    def test_cross_site_subresource_gets_are_refused(self):
        """`<img src=".../api/...">` on another site."""
        self.assertEqual(why(host=TS, sec_fetch_site="cross-site",
                             sec_fetch_mode="no-cors"), "cross-site")

    def test_following_a_link_to_the_pwa_still_works(self):
        self.assertEqual(why(host=TS, sec_fetch_site="cross-site",
                             sec_fetch_mode="navigate"), "")

    def test_a_cross_site_navigation_cannot_be_a_post(self):
        """A form POST is a navigation too — and the CSRF vehicle."""
        self.assertEqual(why("POST", host=TS, sec_fetch_site="cross-site",
                             sec_fetch_mode="navigate"), "cross-site")

    def test_the_pwa_itself_and_same_site_are_allowed(self):
        for site in ("same-origin", "same-site", "none"):
            self.assertEqual(why("POST", host=TS, sec_fetch_site=site), "", site)

    def test_devices_send_no_fetch_metadata_and_pass(self):
        """The Naim's SOAP, Amperfy, curl: no Sec-Fetch-*, no Origin."""
        self.assertEqual(why("POST", host="192.168.1.125:8765"), "")


class TestOriginRule(unittest.TestCase):
    """What stops a cross-site write on PLAIN HTTP: browsers send
    Sec-Fetch-* only to HTTPS origins, so on :8765 a hostile POST arrives
    with Origin and nothing else (measured in Chromium and WebKit)."""

    def test_foreign_origin_write_is_refused(self):
        self.assertEqual(why("POST", host="192.168.1.125:8765",
                             origin="http://evil.example"), "origin")
        self.assertEqual(why("POST", host="192.168.1.125:8765",
                             origin="http://192.168.1.50"), "origin")

    def test_opaque_origin_write_is_refused(self):
        self.assertEqual(why("POST", host=TS, origin="null"), "origin")

    def test_same_host_origin_is_allowed_even_on_another_port(self):
        self.assertEqual(why("POST", host=TS, origin=f"https://{TS}"), "")
        self.assertEqual(why("POST", host="192.168.1.125:8765",
                             origin="https://192.168.1.125:8443"), "")

    def test_csp_reports_pass_with_a_null_origin(self):
        """WebKit posts violation reports with Origin: null (measured live)."""
        h = {"host": "192.168.1.125:8765", "origin": "null"}
        self.assertEqual(g.refusal("POST", h, NAMES, "/api/csp_report"), "")
        self.assertEqual(g.refusal("POST", h, NAMES, "/api/csp_reportx"), "origin")
        self.assertEqual(g.refusal("POST", h, NAMES, "/api/control"), "origin")
        self.assertEqual(g.refusal("POST", {"host": "evil.example", "origin": "null"},
                                   NAMES, "/api/csp_report"), "host")

    def test_reads_are_not_judged_by_origin(self):
        self.assertEqual(why("GET", host=TS, origin="http://evil.example"), "")


def _asgi(guard, method="GET", path="/api/version", headers=()):
    """Drive the middleware with a raw ASGI scope; return (status, sent)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": method, "path": path,
             "headers": [(k.encode(), v.encode()) for k, v in headers],
             "client": ("192.168.1.50", 5000)}
    asyncio.run(guard(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, sent


class TestMiddleware(unittest.TestCase):
    def _guard(self):
        async def app(scope, receive, send):
            self.reached.append(scope["path"])
            await send({"type": "http.response.start", "status": 200,
                        "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
        self.reached = []
        return g.RequestGuard(app, names=NAMES)

    def test_refused_request_never_reaches_the_app(self):
        guard = self._guard()
        with self.assertLogs("dlna.guard", "WARNING") as logs:
            status, sent = _asgi(guard, "POST", "/api/playlist/delete",
                                    [("host", "evil.example")])
        self.assertEqual(status, 403)
        self.assertEqual(self.reached, [])
        body = next(m["body"] for m in sent if m["type"] == "http.response.body")
        self.assertEqual(json.loads(body), {"error": "forbidden"})
        self.assertIn("GATEWAY_ALLOWED_HOSTS", logs.output[0])

    def test_allowed_request_passes_through(self):
        guard = self._guard()
        status, _ = _asgi(guard, "POST", "/api/control",
                             [("host", TS), ("sec-fetch-site", "same-origin")])
        self.assertEqual((status, self.reached), (200, ["/api/control"]))

    def test_warning_is_rate_limited_and_its_memory_bounded(self):
        guard = self._guard()
        with self.assertLogs("dlna.guard", "WARNING") as logs:
            for _ in range(5):
                _asgi(guard, headers=[("host", "evil.example")])
        self.assertEqual(len(logs.output), 1)
        with self.assertLogs("dlna.guard", "WARNING"):
            for i in range(g._WARN_KEYS_MAX + 50):
                _asgi(guard, headers=[("host", f"x{i}.example")])
        self.assertLessEqual(len(guard._warned), g._WARN_KEYS_MAX)

    def test_non_http_scopes_pass_untouched(self):
        seen = []

        async def app(scope, receive, send):
            seen.append(scope["type"])
        asyncio.run(g.RequestGuard(app, names=NAMES)(
            {"type": "lifespan"}, None, None))
        self.assertEqual(seen, ["lifespan"])

    def test_it_is_mounted_on_the_gateway(self):
        self.assertIn(g.RequestGuard,
                      [m.cls for m in dlna_asgi.app.user_middleware])


class TestWritesAreNotGets(unittest.TestCase):
    """No route that changes state may answer a GET. Load-bearing on plain
    HTTP, where a cross-site GET carries no header the guard can judge."""

    def _methods(self):
        from tests.test_asgi import _flatten
        out = {}
        for r in _flatten(dlna_asgi.app.routes):
            for m in getattr(r, "methods", None) or ():
                out.setdefault(getattr(r, "path", ""), set()).add(m)
        return out

    def test_query_writes_are_post_only(self):
        methods = self._methods()
        for path in dlna_routes.QUERY_POST_ROUTES:
            self.assertEqual(methods.get(path), {"POST"}, path)

    def test_no_write_hides_in_the_get_table(self):
        for path in dlna_routes.GET_ROUTES:
            for verb in ("/create", "/delete", "/add", "/remove", "/rebuild",
                         "/set", "/clear", "/edit", "/forget"):
                self.assertFalse(path.endswith(verb), path)

    def test_the_frontend_stub_agrees(self):
        """tests/frontend/stub_gateway.py refuses a GET to the same set, so a
        GET left in app.js fails the browser suite."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "frontend"))
        from stub_gateway import QUERY_POST_PATHS
        self.assertEqual(QUERY_POST_PATHS, frozenset(dlna_routes.QUERY_POST_ROUTES))


if __name__ == "__main__":
    unittest.main()
