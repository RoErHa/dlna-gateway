#!/usr/bin/env python3
"""
tests/test_xml_safety.py — untrusted XML cannot amplify or blow up memory.

Audit follow-up (2026-08-20). Every XML the gateway parses arrives over the
network, and the SOAP control endpoints under `/gw/*` are reachable
unauthenticated by anything on the LAN. `xml.etree.ElementTree` expands
internal entities, which was measured on this codebase before the fix: a
three-level billion-laughs body expanded 1000x, and ten levels reaches
gigabytes from a single small request.

External entities were already refused by ElementTree, so this was denial of
service rather than file disclosure — but a machine whose job is to keep
playing music is exactly the wrong thing to be able to OOM from the LAN.

`dlna_xml.safe_fromstring` refuses DTDs outright (SOAP/DIDL/UPnP descriptors
have no legitimate use for one) and caps input size. These tests hold that,
and equally hold that legitimate traffic still parses — a hardening that
breaks the Naim would be worse than the bug.
"""
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api_upnp  # noqa: E402
from dlna_xml import MAX_XML_BYTES, safe_fromstring  # noqa: E402

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY a "AAAAAAAAAA">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
]><r>&c;</r>"""

XXE = (b'<?xml version="1.0"?>\n'
       b'<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>')

GOOD_SOAP = (
    b'<?xml version="1.0"?>'
    b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    b'<s:Body><u:GetProtocolInfo '
    b'xmlns:u="urn:schemas-upnp-org:service:ConnectionManager:1"/>'
    b'</s:Body></s:Envelope>'
)


class TestRefusesDangerousDocuments(unittest.TestCase):

    def test_entity_expansion_refused(self):
        with self.assertRaises(ET.ParseError):
            safe_fromstring(BILLION_LAUGHS)

    def test_external_entity_refused(self):
        # ElementTree refuses these on its own; the DTD ban stops it earlier.
        with self.assertRaises(ET.ParseError):
            safe_fromstring(XXE)

    def test_any_doctype_refused_even_if_harmless(self):
        """The rule is 'no DTD', not 'no dangerous DTD' — a scanner that tries
        to judge intent is the thing that eventually gets fooled."""
        with self.assertRaises(ET.ParseError):
            safe_fromstring(b'<!DOCTYPE html><r>hi</r>')

    def test_doctype_detected_case_insensitively(self):
        with self.assertRaises(ET.ParseError):
            safe_fromstring(b'<!doctype foo [<!ENTITY a "x">]><r>&a;</r>')

    def test_oversized_input_refused_before_parsing(self):
        huge = b"<r>" + (b"A" * (MAX_XML_BYTES + 1)) + b"</r>"
        with self.assertRaises(ET.ParseError):
            safe_fromstring(huge)

    def test_refusal_is_parseerror_so_callers_need_no_new_branch(self):
        """Every call site already handles ParseError as 'malformed XML from a
        device'. A hostile body must take that same path."""
        for bad in (BILLION_LAUGHS, XXE, b"<unclosed>"):
            with self.assertRaises(ET.ParseError):
                safe_fromstring(bad)


class TestLegitimateTrafficStillParses(unittest.TestCase):

    def test_plain_document(self):
        el = safe_fromstring(b'<r><a>1</a></r>')
        self.assertEqual(el.find("a").text, "1")

    def test_str_input_accepted(self):
        el = safe_fromstring('<r><a>ü</a></r>')
        self.assertEqual(el.find("a").text, "ü")

    def test_xml_declaration_and_comments_ok(self):
        el = safe_fromstring(
            b'<?xml version="1.0" encoding="utf-8"?><!-- note --><r>ok</r>')
        self.assertEqual(el.text, "ok")

    def test_namespaced_soap_envelope(self):
        el = safe_fromstring(GOOD_SOAP)
        self.assertIn("Envelope", el.tag)


class TestNetworkFacingEndpoints(unittest.TestCase):
    """The two handlers an unauthenticated LAN peer can POST to."""

    def test_soap_endpoints_survive_the_bomb(self):
        for fn in (api_upnp.cd_control_soap, api_upnp.cm_control_soap):
            status, _ctype, _payload = fn(BILLION_LAUGHS)
            self.assertNotEqual(status, 200, fn.__name__)

    def test_connection_manager_still_answers_a_real_request(self):
        status, _ctype, payload = api_upnp.cm_control_soap(GOOD_SOAP)
        self.assertEqual(status, 200)
        self.assertIn(b"GetProtocolInfo", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
