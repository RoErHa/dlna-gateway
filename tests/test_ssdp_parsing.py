#!/usr/bin/env python3
"""
tests/test_ssdp_parsing.py — the unauthenticated UDP input path.

WHY. SSDP is the one surface where a stranger on the LAN hands the gateway
bytes with no authentication, no connection, and no return address it has to
prove. The 2026-08-20 audit hardened the XML the gateway parses
(`dlna_xml`), but the SSDP HEADERS were never examined — that gap is Track B3
of the public-release plan.

What was wrong, all of it reachable with a one-line UDP sender:

  * **Every packet carrying a LOCATION spawned a thread**, which slept 1.5 s
    and then made an HTTP request. Unique LOCATIONs meant unbounded threads:
    a few thousand packets a second is a thread bomb, and the gateway dies of
    file-descriptor exhaustion — with SQLite's "unable to open database file"
    as the visible symptom, so it does not even look like an attack.
  * **The URL was fetched with no validation and read with no limit.**
    `LOCATION: file:///etc/passwd` was fetched; a URL that streams for ever
    was buffered into memory until the process died. `safe_fromstring`'s 4 MB
    cap sits AFTER the read, so it never got a chance.
  * **The URL did not have to name the sender.** A device announces its own
    description URL; nothing checked that, so the gateway could be pointed at
    any third party and made to request it — an unauthenticated reflector.

The parse is now one pure function so it can be attacked directly here.
"""
import os
import sys
import time
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dlna_discovery_ssdp as ssdp        # noqa: E402


def _notify(location: str, extra: str = "") -> bytes:
    return ("NOTIFY * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            "NT: urn:schemas-upnp-org:device:MediaServer:1\r\n"
            "NTS: ssdp:alive\r\n"
            f"LOCATION: {location}\r\n"
            f"{extra}"
            "\r\n").encode()


class TestLegitimatePacketsStillWork(unittest.TestCase):
    """First, and most important: a fix that is too strict shows up as 'the
    Naim disappeared', not as a red test. These are the real shapes."""

    def test_a_normal_device_announcement(self):
        pkt = _notify("http://192.168.1.227:35231/desc.xml")
        self.assertEqual(
            ssdp.parse_location(pkt, "192.168.1.227"),
            "http://192.168.1.227:35231/desc.xml")

    def test_header_name_is_case_insensitive(self):
        pkt = b"NOTIFY * HTTP/1.1\r\nLocation: http://10.0.0.4:80/d.xml\r\n\r\n"
        self.assertEqual(ssdp.parse_location(pkt, "10.0.0.4"),
                         "http://10.0.0.4:80/d.xml")

    def test_msearch_response_from_the_device(self):
        pkt = (b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\n"
               b"LOCATION: http://192.168.1.50:2869/upnphost/dev.xml\r\n"
               b"ST: upnp:rootdevice\r\n\r\n")
        self.assertEqual(ssdp.parse_location(pkt, "192.168.1.50"),
                         "http://192.168.1.50:2869/upnphost/dev.xml")

    def test_https_is_accepted(self):
        pkt = _notify("https://192.168.1.9/desc.xml")
        self.assertIsNotNone(ssdp.parse_location(pkt, "192.168.1.9"))

    def test_no_source_given_skips_the_source_check(self):
        """Callers that genuinely have no peer address (the subnet-scan
        path, tests) must still be able to parse."""
        pkt = _notify("http://192.168.1.227:35231/desc.xml")
        self.assertIsNotNone(ssdp.parse_location(pkt, ""))


class TestHostilePacketsAreRefused(unittest.TestCase):
    def test_non_http_schemes(self):
        for loc in ("file:///etc/passwd",
                    "gopher://192.168.1.5:70/x",
                    "ftp://192.168.1.5/desc.xml",
                    "data:text/xml,<root/>",
                    "//192.168.1.5/desc.xml"):
            with self.subTest(loc=loc):
                self.assertIsNone(ssdp.parse_location(_notify(loc), ""))

    def test_a_url_that_does_not_name_the_sender(self):
        """The reflector vector: any peer could name any third party."""
        pkt = _notify("http://internal-admin.example.com/desc.xml")
        self.assertIsNone(ssdp.parse_location(pkt, "192.168.1.99"))

    def test_a_url_naming_another_lan_host(self):
        pkt = _notify("http://192.168.1.1:80/setup.cgi")
        self.assertIsNone(ssdp.parse_location(pkt, "192.168.1.99"))

    def test_an_absurdly_long_url(self):
        pkt = _notify("http://192.168.1.5/" + "A" * 4000)
        self.assertIsNone(ssdp.parse_location(pkt, "192.168.1.5"))

    def test_garbage_and_empty_packets(self):
        for data in (b"", b"\x00" * 512, b"NOTIFY * HTTP/1.1\r\n\r\n",
                     b"LOCATION:\r\n\r\n", b"LOCATION: \r\n\r\n",
                     os.urandom(1400)):
            with self.subTest(data=data[:16]):
                self.assertIsNone(ssdp.parse_location(data, "192.168.1.5"))

    def test_a_url_with_no_host(self):
        for loc in ("http:///desc.xml", "http://", "http://:8080/x"):
            with self.subTest(loc=loc):
                self.assertIsNone(ssdp.parse_location(_notify(loc), ""))

    def test_credentials_in_the_url_are_refused(self):
        """`http://192.168.1.5@evil.example.com/` reads as the sender to a
        careless eye and resolves to somewhere else entirely."""
        pkt = _notify("http://192.168.1.5@evil.example.com/desc.xml")
        self.assertIsNone(ssdp.parse_location(pkt, "192.168.1.5"))

    def test_only_the_start_of_a_huge_packet_is_scanned(self):
        """A jumbo datagram must not turn into an unbounded regex scan."""
        pad = b"X-Pad: " + b"p" * 60000 + b"\r\n"
        pkt = (b"NOTIFY * HTTP/1.1\r\n" + pad +
               b"LOCATION: http://192.168.1.5/d.xml\r\n\r\n")
        self.assertIsNone(ssdp.parse_location(pkt, "192.168.1.5"))


class TestRegistrationIsBounded(unittest.TestCase):
    """Parsing safely is not enough — the work each packet triggers has to
    be capped, or the flood just moves one step later."""

    def test_there_is_a_concurrency_cap(self):
        self.assertIsInstance(ssdp._INFLIGHT, type(ssdp.threading.Semaphore()))

    def test_the_cap_is_small_enough_to_matter(self):
        self.assertLessEqual(ssdp._MAX_INFLIGHT, 32)

    def test_spawn_refuses_beyond_the_cap(self):
        """Take every slot, then prove the next packet is dropped rather
        than queued — devices re-announce, so dropping is the safe choice."""
        taken = []
        try:
            while ssdp._INFLIGHT.acquire(blocking=False):
                taken.append(True)
                if len(taken) > ssdp._MAX_INFLIGHT:
                    self.fail("semaphore is unbounded")
            self.assertFalse(ssdp._spawn_registration("http://192.168.1.5/d.xml", ""))
        finally:
            for _ in taken:
                ssdp._INFLIGHT.release()

    def test_spawn_accepts_when_slots_are_free(self):
        calls = []
        real = ssdp._register
        ssdp._register = lambda loc, udn="": calls.append(loc)
        try:
            self.assertTrue(ssdp._spawn_registration("http://192.168.1.5/d.xml", ""))
            for _ in range(50):
                if calls:
                    break
                time.sleep(0.02)
        finally:
            ssdp._register = real
        self.assertEqual(calls, ["http://192.168.1.5/d.xml"])


class TestDeviceDescriptionReadIsCapped(unittest.TestCase):
    """The fetch that a LOCATION triggers must not read without a limit."""

    def test_fetch_device_stops_reading_at_the_cap(self):
        import dlna_discovery

        self.assertTrue(hasattr(dlna_discovery, "_MAX_DESC_BYTES"))
        self.assertLessEqual(dlna_discovery._MAX_DESC_BYTES, 2 * 1024 * 1024)

        asked = {}

        class _Endless:
            def read(self, n=-1):
                asked["n"] = n
                # Honour the limit the caller asked for; if it asked for
                # everything, this would never return in real life.
                return b"<root>" + b"x" * (n - 6 if n and n > 6 else 0)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        import urllib.request
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: _Endless()
        try:
            dlna_discovery._fetch_device(
                "http://192.168.1.5/d.xml",
                dlna_discovery.SERVERS, dlna_discovery.RENDERERS)
        finally:
            urllib.request.urlopen = real

        self.assertIn("n", asked, "_fetch_device read with no limit")
        self.assertGreater(asked["n"], 0)
        self.assertLessEqual(asked["n"], dlna_discovery._MAX_DESC_BYTES + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
