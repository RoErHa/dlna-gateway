#!/usr/bin/env python3
"""
tests/test_avtransport_setnext.py — Phase 4 SetNextAVTransportURI
coverage. Mocks the SOAP layer; no network. Pinned alongside the
existing test_avtransport_volume.py so the AVTransport surface stays
consistent.

Run standalone:
    python3 -m unittest tests.test_avtransport_setnext -v
"""
import os
import sys
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dlna_avtransport as av


class TestSetNextUri(unittest.TestCase):

    def test_sends_setnext_with_url_in_body(self):
        captured = {}
        def fake_soap(av_url, action, body):
            captured["av_url"] = av_url
            captured["action"] = action
            captured["body"]   = body
            return ("ok", "")
        with patch.object(av, "_av_soap", side_effect=fake_soap):
            ok = av.avtransport_set_next_uri(
                "http://renderer:1234/av",
                "http://gateway:8200/localfs/stream/abc",
                "Comfortably Numb", "audio/flac")
        self.assertTrue(ok)
        self.assertEqual(captured["action"], "SetNextAVTransportURI")
        self.assertIn("<u:SetNextAVTransportURI", captured["body"])
        self.assertIn("http://gateway:8200/localfs/stream/abc",
                      captured["body"])
        self.assertIn("<NextURI>", captured["body"])
        self.assertIn("Comfortably Numb", captured["body"])

    def test_empty_url_clears_next(self):
        captured = {}
        def fake_soap(av_url, action, body):
            captured["body"] = body
            return ("ok", "")
        with patch.object(av, "_av_soap", side_effect=fake_soap):
            ok = av.avtransport_set_next_uri(
                "http://renderer:1234/av", "")
        self.assertTrue(ok)
        # NextURI element is empty
        self.assertIn("<NextURI></NextURI>", captured["body"])
        # And the metadata block is empty too (no DIDL-Lite spam)
        self.assertIn("<NextURIMetaData></NextURIMetaData>",
                      captured["body"])

    def test_xml_special_chars_escaped(self):
        captured = {}
        def fake_soap(av_url, action, body):
            captured["body"] = body
            return ("ok", "")
        with patch.object(av, "_av_soap", side_effect=fake_soap):
            av.avtransport_set_next_uri(
                "http://renderer:1234/av",
                "http://host/file?a=1&b=2",
                "Title <with> & special \"chars\"",
                "audio/flac")
        body = captured["body"]
        self.assertNotIn("?a=1&b=2", body,
                         "Raw ampersand must be XML-escaped")
        self.assertIn("&amp;", body)
        self.assertIn("&lt;with&gt;", body)
        self.assertIn("&quot;chars&quot;", body)

    def test_soap_error_returns_false(self):
        with patch.object(av, "_av_soap",
                          return_value=("", "HTTP 500")):
            ok = av.avtransport_set_next_uri(
                "http://renderer:1234/av",
                "http://host/file.flac", "x", "audio/flac")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
