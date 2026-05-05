#!/usr/bin/env python3
"""
dlna_avtransport.py — UPnP AVTransport SOAP client for MediaRenderers.

  avtransport_send()         — SetAVTransportURI + Play (start a track)
  avtransport_stop()         — Stop
  avtransport_pause()        — Pause / Play toggle
  avtransport_get_state()    — CurrentTransportState (PLAYING/STOPPED/etc.)
  avtransport_get_position() — Position, duration, title

Separated from dlna_content so the ContentDirectory side (browse/search)
and the AVTransport side (renderer control) live in their own modules
— each is a distinct UPnP service with a separate contract.
"""
import http.client
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional

log = logging.getLogger("dlna.content")


def _xml_esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;").replace('"', "&quot;")


def avtransport_send(av_url: str, media_url: str, title: str,
                     mime: str = "audio/x-flac") -> bool:
    """
    Send SetAVTransportURI + Play to a UPnP renderer.
    Returns True on success, False on failure.
    """
    def soap_call(action: str, body_inner: str) -> bool:
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body>{body_inner}</s:Body></s:Envelope>'
        ).encode("utf-8")
        parsed = urllib.parse.urlparse(av_url)
        conn   = http.client.HTTPConnection(parsed.netloc, timeout=10)
        try:
            conn.request("POST", parsed.path, envelope, {
                "Content-Type":   "text/xml; charset=utf-8",
                "SOAPAction":     f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
                "Content-Length": str(len(envelope)),
                "User-Agent":     "DLNAGateway/1.0",
            })
            resp = conn.getresponse()
            body = resp.read()
            if resp.status not in (200, 204):
                log.error(f"AVTransport {action} → HTTP {resp.status}: {body[:800]}")
                return False
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass

    safe_url   = _xml_esc(media_url)
    safe_title = _xml_esc(title)
    safe_mime  = _xml_esc(mime or "audio/x-flac")

    metadata = (
        '&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
        ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
        '&lt;item id="1" parentID="0" restricted="1"&gt;'
        f'&lt;dc:title&gt;{safe_title}&lt;/dc:title&gt;'
        '&lt;upnp:class&gt;object.item.audioItem.musicTrack&lt;/upnp:class&gt;'
        f'&lt;res protocolInfo="http-get:*:{safe_mime}:*"&gt;{safe_url}&lt;/res&gt;'
        '&lt;/item&gt;&lt;/DIDL-Lite&gt;'
    )

    ok = soap_call("SetAVTransportURI",
        f'<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        f'<InstanceID>0</InstanceID>'
        f'<CurrentURI>{safe_url}</CurrentURI>'
        f'<CurrentURIMetaData>{metadata}</CurrentURIMetaData>'
        f'</u:SetAVTransportURI>')

    if not ok:
        return False

    ok = soap_call("Play",
        '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>')

    if ok:
        log.info(f"AVTransport ▶ {title!r} → {av_url}")
    return ok


def avtransport_pause(av_url: str) -> bool:
    """Toggle pause on a renderer (sends Pause if playing, Play if paused)."""
    # Get current state first so we can toggle correctly
    state = avtransport_get_state(av_url)
    if state == "PAUSED_PLAYBACK":
        action = "Play"
        body   = ('<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                  '<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>')
    else:
        action = "Pause"
        body   = ('<u:Pause xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
                  '<InstanceID>0</InstanceID></u:Pause>')
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body>{body}</s:Body></s:Envelope>'
    ).encode("utf-8")
    parsed = urllib.parse.urlparse(av_url)
    conn   = http.client.HTTPConnection(parsed.netloc, timeout=8)
    try:
        conn.request("POST", parsed.path, envelope, {
            "Content-Type":   "text/xml; charset=utf-8",
            "SOAPAction":     f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
            "Content-Length": str(len(envelope)),
        })
        resp = conn.getresponse(); resp.read()
        log.info(f"AVTransport {action} → {av_url}")
        return resp.status in (200, 204)
    except Exception as e:
        log.error(f"avtransport_pause: {e}")
        return False
    finally:
        try: conn.close()
        except Exception: pass


def _av_soap(av_url: str, action: str, body_inner: str) -> Optional[str]:
    """Generic AVTransport SOAP helper; returns response body text or None."""
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body>{body_inner}</s:Body></s:Envelope>'
    ).encode("utf-8")
    parsed = urllib.parse.urlparse(av_url)
    conn   = http.client.HTTPConnection(parsed.netloc, timeout=6)
    try:
        conn.request("POST", parsed.path, envelope, {
            "Content-Type":   "text/xml; charset=utf-8",
            "SOAPAction":     f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
            "Content-Length": str(len(envelope)),
        })
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", errors="replace")
        return text if resp.status in (200, 204) else None
    except Exception as e:
        log.debug(f"_av_soap {action}: {e}")
        return None
    finally:
        try: conn.close()
        except Exception: pass


def avtransport_get_state(av_url: str) -> str:
    """
    Returns the CurrentTransportState string, e.g.:
      PLAYING | PAUSED_PLAYBACK | STOPPED | NO_MEDIA_PRESENT | TRANSITIONING | UNKNOWN
    """
    raw = _av_soap(av_url, "GetTransportInfo",
        '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID></u:GetTransportInfo>')
    if not raw:
        return "UNKNOWN"
    try:
        root = ET.fromstring(raw)
        for el in root.iter():
            if el.tag.endswith("CurrentTransportState"):
                return (el.text or "UNKNOWN").strip()
    except ET.ParseError:
        pass
    return "UNKNOWN"


def avtransport_get_position(av_url: str) -> dict:
    """
    Returns position info dict:
      {"position": seconds_float, "duration": seconds_float,
       "title": str, "state": str}
    All fields may be None on failure.
    """
    raw = _av_soap(av_url, "GetPositionInfo",
        '<u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID></u:GetPositionInfo>')

    def _parse_time(s: str) -> Optional[float]:
        """'H:MM:SS' or 'MM:SS' → float seconds, None if NOT_IMPLEMENTED."""
        if not s or s in ("NOT_IMPLEMENTED", "0:00:00", "00:00:00"):
            return None
        try:
            parts = [float(x) for x in s.split(":")]
            if len(parts) == 3:
                return parts[0]*3600 + parts[1]*60 + parts[2]
            if len(parts) == 2:
                return parts[0]*60 + parts[1]
        except Exception:
            pass
        return None

    result: dict = {"position": None, "duration": None,
                    "title": None, "state": None}
    if not raw:
        return result
    try:
        root = ET.fromstring(raw)
        for el in root.iter():
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag == "RelTime":
                result["position"] = _parse_time(el.text or "")
            elif tag == "TrackDuration":
                result["duration"] = _parse_time(el.text or "")
            elif tag == "TrackMetaData" and el.text:
                # Extract title from embedded DIDL-Lite if present
                try:
                    didl = ET.fromstring(el.text)
                    for t in didl.iter():
                        if t.tag.endswith("}title") or t.tag == "title":
                            result["title"] = t.text
                            break
                except Exception:
                    pass
    except ET.ParseError:
        pass
    return result


# ─────────────────────────────────────────────────────────────────
# RenderingControl — volume helpers (used by loudness normalization).
# This is a *separate* UPnP service from AVTransport; the SOAP endpoint
# URL on the renderer is different (sourced from the device description
# during discovery and stashed as `_RendererInfo.rc_url`).
# ─────────────────────────────────────────────────────────────────

def _rc_soap(rc_url: str, action: str, body_inner: str,
             timeout: float = 6.0) -> Optional[str]:
    """Generic RenderingControl SOAP helper. Returns response body
    text on 2xx, None otherwise. Catches connection errors so callers
    don't have to."""
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body>{body_inner}</s:Body></s:Envelope>'
    ).encode("utf-8")
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
        try: conn.close()
        except Exception: pass


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


def get_volume(rc_url: str) -> Optional[int]:
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


def avtransport_stop(av_url: str) -> bool:
    """Send Stop to a renderer."""
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID></u:Stop>'
        '</s:Body></s:Envelope>'
    ).encode("utf-8")
    parsed = urllib.parse.urlparse(av_url)
    conn   = http.client.HTTPConnection(parsed.netloc, timeout=8)
    try:
        conn.request("POST", parsed.path, envelope, {
            "Content-Type":   "text/xml; charset=utf-8",
            "SOAPAction":     '"urn:schemas-upnp-org:service:AVTransport:1#Stop"',
            "Content-Length": str(len(envelope)),
        })
        resp = conn.getresponse()
        resp.read()
        log.info(f"AVTransport ■ Stop → {av_url}")
        return resp.status in (200, 204)
    except Exception as e:
        log.error(f"AVTransport Stop error: {e}")
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass
