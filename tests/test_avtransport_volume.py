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

import dlna_avtransport
from dlna_avtransport import (set_volume, get_volume,
                              avtransport_get_state, avtransport_probe_state)


def _state_conn(status=200, body=b"", raise_on_request=None):
    """FakeConn factory for avtransport_probe_state tests. Pass
    raise_on_request=<exception instance> to simulate a transport
    failure (the renderer being unreachable)."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body

    class FakeConn:
        def __init__(self, host, timeout=None):
            pass
        def request(self, method, path, body=b"", headers=None):
            if raise_on_request is not None:
                raise raise_on_request
        def getresponse(self):
            return resp
        def close(self):
            pass

    return FakeConn


_TINFO = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
    '<s:Body><u:GetTransportInfoResponse '
    'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
    '<CurrentTransportState>{state}</CurrentTransportState>'
    '<CurrentTransportStatus>OK</CurrentTransportStatus>'
    '</u:GetTransportInfoResponse></s:Body></s:Envelope>'
)


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


# ── avtransport_probe_state — UNREACHABLE vs UNKNOWN distinction ───

class TestProbeState(unittest.TestCase):
    """The 2026-05-20 instrumentation change: a failed GetTransportInfo
    SOAP call must report UNREACHABLE (renderer lost) with the transport
    error reason — NOT be collapsed into a bare UNKNOWN that hides the
    cause."""

    URL = "http://192.168.1.227:59218/Control/RygelAVTransport"

    def setUp(self):
        # Isolate the per-renderer WARN rate limiter between tests.
        dlna_avtransport._state_fail_log.clear()

    def test_success_returns_real_state_no_detail(self):
        conn = _state_conn(200, _TINFO.format(state="PLAYING").encode())
        with patch("dlna_avtransport.http.client.HTTPConnection", conn):
            state, detail = avtransport_probe_state(self.URL)
        self.assertEqual(state, "PLAYING")
        self.assertEqual(detail, "")

    def test_connection_refused_is_unreachable_with_reason(self):
        conn = _state_conn(raise_on_request=ConnectionRefusedError(
            61, "Connection refused"))
        with patch("dlna_avtransport.http.client.HTTPConnection", conn):
            state, detail = avtransport_probe_state(self.URL)
        self.assertEqual(state, "UNREACHABLE")
        self.assertIn("Connection refused", detail)

    def test_timeout_is_unreachable(self):
        conn = _state_conn(raise_on_request=TimeoutError("timed out"))
        with patch("dlna_avtransport.http.client.HTTPConnection", conn):
            state, detail = avtransport_probe_state(self.URL)
        self.assertEqual(state, "UNREACHABLE")
        self.assertTrue(detail)

    def test_http_error_is_unreachable(self):
        conn = _state_conn(status=500, body=b"oops")
        with patch("dlna_avtransport.http.client.HTTPConnection", conn):
            state, detail = avtransport_probe_state(self.URL)
        self.assertEqual(state, "UNREACHABLE")
        self.assertEqual(detail, "HTTP 500")

    def test_renderer_reported_unknown_is_not_unreachable(self):
        # A 200 response that genuinely says UNKNOWN — renderer answered,
        # so it is reachable; detail stays empty.
        conn = _state_conn(200, _TINFO.format(state="UNKNOWN").encode())
        with patch("dlna_avtransport.http.client.HTTPConnection", conn):
            state, detail = avtransport_probe_state(self.URL)
        self.assertEqual(state, "UNKNOWN")
        self.assertEqual(detail, "")

    def test_garbled_body_is_unknown_not_unreachable(self):
        conn = _state_conn(200, b"<not xml")
        with patch("dlna_avtransport.http.client.HTTPConnection", conn):
            state, detail = avtransport_probe_state(self.URL)
        self.assertEqual(state, "UNKNOWN")
        self.assertEqual(detail, "")

    def test_get_state_wrapper_returns_unreachable_string(self):
        conn = _state_conn(raise_on_request=ConnectionRefusedError(
            61, "Connection refused"))
        with patch("dlna_avtransport.http.client.HTTPConnection", conn):
            self.assertEqual(avtransport_get_state(self.URL), "UNREACHABLE")

    def test_failure_logs_warning_with_reason(self):
        conn = _state_conn(raise_on_request=ConnectionRefusedError(
            61, "Connection refused"))
        with patch("dlna_avtransport.http.client.HTTPConnection", conn):
            with self.assertLogs("dlna.content", level="WARNING") as cm:
                avtransport_probe_state(self.URL)
        self.assertTrue(any("Connection refused" in m for m in cm.output))
        self.assertTrue(any("unreachable" in m.lower() for m in cm.output))

    def test_repeat_failures_are_rate_limited(self):
        conn = _state_conn(raise_on_request=ConnectionRefusedError(
            61, "Connection refused"))
        with patch("dlna_avtransport.http.client.HTTPConnection", conn):
            with self.assertLogs("dlna.content", level="WARNING") as cm:
                for _ in range(8):
                    avtransport_probe_state(self.URL)
        # 8 consecutive failures → exactly ONE WARN line, not eight.
        warns = [m for m in cm.output if m.startswith("WARNING")]
        self.assertEqual(len(warns), 1, f"expected 1 WARN, got {warns}")

    def test_recovery_logs_once_and_resets_limiter(self):
        fail = _state_conn(raise_on_request=ConnectionRefusedError(
            61, "Connection refused"))
        ok   = _state_conn(200, _TINFO.format(state="PLAYING").encode())
        with patch("dlna_avtransport.http.client.HTTPConnection", fail):
            avtransport_probe_state(self.URL)
        # Renderer answers again → INFO "reachable again" once.
        with patch("dlna_avtransport.http.client.HTTPConnection", ok):
            with self.assertLogs("dlna.content", level="INFO") as cm:
                avtransport_probe_state(self.URL)
        self.assertTrue(any("reachable again" in m for m in cm.output))
        # Limiter cleared, so the next failure WARNs again immediately.
        with patch("dlna_avtransport.http.client.HTTPConnection", fail):
            with self.assertLogs("dlna.content", level="WARNING") as cm:
                avtransport_probe_state(self.URL)
        self.assertTrue(any("unreachable" in m.lower() for m in cm.output))


if __name__ == "__main__":
    unittest.main()
