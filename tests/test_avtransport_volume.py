#!/usr/bin/env python3
"""
tests/test_avtransport_volume.py — UPnP RenderingControl SetVolume / GetVolume.

These are *new* SOAP helpers added in dlna_avtransport. The renderer's
RenderingControl service is distinct from AVTransport (different SOAP
endpoint URL, different action names, different urn).

Tests mock the underlying http.client so we can assert exact wire-shape
without a live renderer.

Run standalone:
    python3 -m unittest tests.test_avtransport_volume -v
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from dlna_avtransport import set_volume, get_volume


def _mock_http(status: int = 200, body: bytes = b""):
    """Build a (HTTPConnection_class, captured_request_dict) pair.
    Tests instantiate the class once, exercise the helper, then read the
    captured request."""
    captured = {}
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body

    class FakeConn:
        def __init__(self, host, timeout=None):
            captured["host"] = host
            captured["timeout"] = timeout
        def request(self, method, path, body=b"", headers=None):
            captured["method"]  = method
            captured["path"]    = path
            captured["body"]    = body
            captured["headers"] = headers or {}
        def getresponse(self):
            return resp
        def close(self):
            pass

    return FakeConn, captured


# ── set_volume ────────────────────────────────────────────────────

class TestSetVolume(unittest.TestCase):

    def test_body_shape(self):
        FakeConn, cap = _mock_http(status=200)
        with patch("dlna_avtransport.http.client.HTTPConnection", FakeConn):
            ok = set_volume("http://192.168.1.42:50001/Render", 50)
        self.assertTrue(ok)
        body = cap["body"].decode()
        # Action namespace is RenderingControl, NOT AVTransport
        self.assertIn("RenderingControl", body)
        self.assertIn("<u:SetVolume", body)
        self.assertIn("<InstanceID>0</InstanceID>", body)
        self.assertIn("<Channel>Master</Channel>", body)
        self.assertIn("<DesiredVolume>50</DesiredVolume>", body)
        # SOAPAction header must point at the right service
        soap_action = cap["headers"].get("SOAPAction", "")
        self.assertIn("RenderingControl:1#SetVolume", soap_action)

    def test_clamps_below_zero(self):
        FakeConn, cap = _mock_http(status=200)
        with patch("dlna_avtransport.http.client.HTTPConnection", FakeConn):
            set_volume("http://r/Render", -5)
        self.assertIn("<DesiredVolume>0</DesiredVolume>", cap["body"].decode())

    def test_clamps_above_hundred(self):
        FakeConn, cap = _mock_http(status=200)
        with patch("dlna_avtransport.http.client.HTTPConnection", FakeConn):
            set_volume("http://r/Render", 250)
        self.assertIn("<DesiredVolume>100</DesiredVolume>", cap["body"].decode())

    def test_returns_false_on_soap_fault(self):
        FakeConn, _ = _mock_http(status=500, body=b"<fault/>")
        with patch("dlna_avtransport.http.client.HTTPConnection", FakeConn):
            ok = set_volume("http://r/Render", 50)
        self.assertFalse(ok)

    def test_returns_false_on_connection_error(self):
        def raises(*a, **k):
            raise ConnectionRefusedError("renderer offline")
        with patch("dlna_avtransport.http.client.HTTPConnection",
                   side_effect=raises):
            ok = set_volume("http://r/Render", 50)
        self.assertFalse(ok)


# ── get_volume ────────────────────────────────────────────────────

_GET_VOLUME_RESPONSE = (
    b'<?xml version="1.0"?>'
    b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    b'<s:Body>'
    b'<u:GetVolumeResponse xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
    b'<CurrentVolume>42</CurrentVolume>'
    b'</u:GetVolumeResponse>'
    b'</s:Body></s:Envelope>'
)


class TestGetVolume(unittest.TestCase):

    def test_parses_current_volume(self):
        FakeConn, cap = _mock_http(status=200, body=_GET_VOLUME_RESPONSE)
        with patch("dlna_avtransport.http.client.HTTPConnection", FakeConn):
            v = get_volume("http://r/Render")
        self.assertEqual(v, 42)
        # And we sent the right SOAPAction
        self.assertIn("RenderingControl:1#GetVolume",
                      cap["headers"].get("SOAPAction", ""))

    def test_returns_none_on_fault(self):
        FakeConn, _ = _mock_http(status=500, body=b"")
        with patch("dlna_avtransport.http.client.HTTPConnection", FakeConn):
            self.assertIsNone(get_volume("http://r/Render"))

    def test_returns_none_on_garbled_response(self):
        FakeConn, _ = _mock_http(status=200, body=b"not xml")
        with patch("dlna_avtransport.http.client.HTTPConnection", FakeConn):
            self.assertIsNone(get_volume("http://r/Render"))

    def test_returns_none_on_connection_error(self):
        def raises(*a, **k):
            raise TimeoutError("renderer slow")
        with patch("dlna_avtransport.http.client.HTTPConnection",
                   side_effect=raises):
            self.assertIsNone(get_volume("http://r/Render"))


if __name__ == "__main__":
    unittest.main()
