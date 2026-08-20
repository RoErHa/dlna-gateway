#!/usr/bin/env python3
"""
dlna_rendering_control.py — the UPnP **RenderingControl** SOAP client:
the renderer's hardware volume (`SetVolume` / `GetVolume`).

Split out of dlna_avtransport.py (2026-08-20), which had reached 471
lines. RenderingControl is a genuinely *separate* UPnP service from
AVTransport — different SCPD, different control URL on the renderer
(sourced from the device description at discovery and stashed as
`_RendererInfo.rc_url`) — so the split follows the protocol boundary
rather than cutting one service in half.

BIT-PERFECT NOTE: this is the renderer's own hardware/analog volume.
The gateway never touches PCM on the UPnP path — it is not in the audio
path at all — so attenuation happens inside the Naim's DAC.

`get_volume` is deliberately NOT used on the playback path: a STOPPED
Naim reports 0, and adopting that as the baseline silenced playback
(the 2026-05-30 bug). `RendererQueue` sets a fixed startup volume once
per queue instead.
"""
from __future__ import annotations

import http.client
import logging
import urllib.parse
import xml.etree.ElementTree as ET

from dlna_config import close_quietly

log = logging.getLogger("dlna.avtransport")


def _rc_soap(rc_url: str, action: str, body_inner: str,
             timeout: float = 6.0) -> str | None:
    """Generic RenderingControl SOAP helper. Returns response body
    text on 2xx, None otherwise. Catches connection errors so callers
    don't have to."""
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body>{body_inner}</s:Body></s:Envelope>'
    ).encode()
    parsed = urllib.parse.urlparse(rc_url)
    try:
        conn = http.client.HTTPConnection(parsed.netloc, timeout=timeout)
    except Exception as e:
        log.debug(f"_rc_soap {action}: connect failed: {e}")
        return None
    try:
        conn.request("POST", parsed.path, envelope, {
            "Content-Type":   "text/xml; charset=utf-8",
            "SOAPAction":     f'"urn:schemas-upnp-org:service:RenderingControl:1#{action}"',
            "Content-Length": str(len(envelope)),
            "User-Agent":     "DLNAGateway/1.0",
        })
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", errors="replace")
        if resp.status not in (200, 204):
            log.debug(f"_rc_soap {action} → HTTP {resp.status}: {text[:200]}")
            return None
        return text
    except Exception as e:
        log.debug(f"_rc_soap {action}: {e}")
        return None
    finally:
        close_quietly(conn)



def set_volume(rc_url: str, level: int) -> bool:
    """Set the renderer's volume on Master channel. Clamped 0-100."""
    level = max(0, min(100, int(level)))
    raw = _rc_soap(rc_url, "SetVolume",
        '<u:SetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
        '<InstanceID>0</InstanceID>'
        '<Channel>Master</Channel>'
        f'<DesiredVolume>{level}</DesiredVolume>'
        '</u:SetVolume>')
    return raw is not None


def get_volume(rc_url: str) -> int | None:
    """Read the renderer's current volume on Master channel. Returns None
    on fault or garbled response — callers should treat None as
    "unknown, fall back to a sensible default" rather than fail-fast."""
    raw = _rc_soap(rc_url, "GetVolume",
        '<u:GetVolume xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
        '<InstanceID>0</InstanceID>'
        '<Channel>Master</Channel>'
        '</u:GetVolume>')
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == "CurrentVolume" and el.text:
            try:
                return int(el.text.strip())
            except ValueError:
                return None
    return None

