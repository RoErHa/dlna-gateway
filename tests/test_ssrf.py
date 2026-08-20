#!/usr/bin/env python3
"""
tests/test_ssrf.py — the outbound-fetch guard (dlna_ssrf).

Guards the 2026-08-20 audit finding: `/art`, `/stream` and `/radio_stream`
are unauthenticated and took an arbitrary caller-supplied URL, which made
the gateway an SSRF proxy — a working port oracle via /art's error text, and
a full-body read of any internal HTTP service via /stream.

The cases below are the ones that actually matter: loopback and RFC1918 are
refused, the LocalFs file server (a KNOWN device) is still reachable because
the whole feature depends on it, public hosts still work because cover art
and radio are public by nature, and a refusal never leaks its reason.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import dlna_ssrf  # noqa: E402


def _registry(*locations):
    """Patch the known-internal-host lookup with a fixed device set."""
    return patch.object(dlna_ssrf, "_known_internal_hosts",
                        lambda: set(locations))


class TestSchemeAndShape(unittest.TestCase):

    def test_empty_url_refused(self):
        ok, why = dlna_ssrf.check_url("")
        self.assertFalse(ok)
        self.assertIn("empty", why)

    def test_non_http_schemes_refused(self):
        # file:// would read local disk, gopher:// is a classic SSRF
        # escalation, data:/ftp: are simply not audio or art sources.
        for url in ("file:///etc/passwd", "gopher://x/1", "ftp://h/f",
                    "data:text/plain,hi", "javascript:alert(1)"):
            ok, why = dlna_ssrf.check_url(url)
            self.assertFalse(ok, url)
            self.assertIn("scheme", why)

    def test_url_without_host_refused(self):
        ok, why = dlna_ssrf.check_url("http:///just/a/path")
        self.assertFalse(ok)
        self.assertIn("host", why)


class TestPrivateDestinations(unittest.TestCase):
    """The core of the finding: these were all fetchable before."""

    def test_loopback_refused(self):
        with _registry():
            for url in ("http://127.0.0.1:8765/api/servers",
                        "http://127.0.0.1:9/",
                        "http://localhost:8200/"):
                ok, _ = dlna_ssrf.check_url(url)
                self.assertFalse(ok, url)

    def test_rfc1918_refused_when_not_a_known_device(self):
        with _registry():
            for url in ("http://192.168.1.50/", "http://10.0.0.5/",
                        "http://172.16.3.4/"):
                ok, _ = dlna_ssrf.check_url(url)
                self.assertFalse(ok, url)

    def test_cloud_metadata_refused(self):
        # 169.254.169.254 is link-local, which `.is_private` alone misses.
        with _registry():
            ok, _ = dlna_ssrf.check_url("http://169.254.169.254/latest/meta-data/")
            self.assertFalse(ok)

    def test_unspecified_address_refused(self):
        with _registry():
            ok, _ = dlna_ssrf.check_url("http://0.0.0.0:8765/")
            self.assertFalse(ok)


class TestKnownDevicesStillWork(unittest.TestCase):
    """The guard must not break the feature it protects."""

    def test_localfs_file_server_allowed(self):
        # The LocalFs server registers itself as a MediaServer at boot, so it
        # is a known host — /art and /stream depend entirely on reaching it.
        with _registry("192.168.1.125"):
            for url in ("http://192.168.1.125:8200/localfs/art/2f00ce45",
                        "http://192.168.1.125:8200/localfs/stream/0006f30d"):
                ok, why = dlna_ssrf.check_url(url)
                self.assertTrue(ok, f"{url} → {why}")

    def test_known_host_allowed_on_any_port(self):
        # Documented trade-off: granularity is HOST, not host:port, because a
        # UPnP server may serve art from a different port than its descriptor.
        with _registry("192.168.1.227"):
            ok, _ = dlna_ssrf.check_url("http://192.168.1.227:37080/desc.xml")
            self.assertTrue(ok)

    def test_unknown_private_host_still_refused_alongside_known(self):
        with _registry("192.168.1.125"):
            ok, _ = dlna_ssrf.check_url("http://192.168.1.126:8200/")
            self.assertFalse(ok)


class TestPublicDestinations(unittest.TestCase):

    def test_public_ip_allowed(self):
        # Cover art (coverartarchive → archive.org), station logos and radio
        # streams are public by nature; an allowlist there would be endless.
        with _registry():
            ok, why = dlna_ssrf.check_url("http://93.184.216.34/cover.jpg")
            self.assertTrue(ok, why)

    def test_https_public_allowed(self):
        with _registry():
            ok, _ = dlna_ssrf.check_url("https://8.8.8.8/x.png")
            self.assertTrue(ok)


class TestFailClosed(unittest.TestCase):

    def test_unresolvable_host_refused(self):
        ok, why = dlna_ssrf.check_url(
            "http://this-host-does-not-exist.invalid/x")
        self.assertFalse(ok)
        self.assertIn("resolve", why)

    def test_registry_failure_refuses_private(self):
        """A broken registry must fail CLOSED, not open."""
        def _boom():
            raise RuntimeError("registry down")
        with patch.object(dlna_ssrf, "_known_internal_hosts", _boom):
            with self.assertRaises(RuntimeError):
                dlna_ssrf.check_url("http://192.168.1.125:8200/x")

    def test_registry_exception_inside_helper_yields_empty_set(self):
        # The helper swallows its own errors and returns an EMPTY set, which
        # means "no private destination allowed" — the safe direction.
        with patch("dlna_registry.SERVERS") as srv:
            srv.all.side_effect = RuntimeError("boom")
            self.assertEqual(dlna_ssrf._known_internal_hosts(), set())


class TestGuardDoesNotLeak(unittest.TestCase):

    def test_guard_returns_bool_and_logs_reason(self):
        with _registry(), self.assertLogs("dlna.ssrf", level="WARNING") as cm:
            allowed = dlna_ssrf.guard("http://127.0.0.1:8765/api/servers", "art")
        self.assertFalse(allowed)
        # The reason belongs in the LOG only — the caller gets a bare False,
        # so the HTTP response cannot become an oracle again.
        self.assertTrue(any("SSRF guard" in m for m in cm.output))

    def test_guard_allows_public(self):
        with _registry():
            self.assertTrue(dlna_ssrf.guard("http://93.184.216.34/a.jpg", "art"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
