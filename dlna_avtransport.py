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
import time
import urllib.parse
import xml.etree.ElementTree as ET

from dlna_xml import read_capped, safe_fromstring

from dlna_config import close_quietly
# RenderingControl (volume) moved to its own module 2026-08-20; re-exported
# here so existing `from dlna_avtransport import set_volume` imports work.
from dlna_rendering_control import (  # noqa: F401
    _rc_soap,
    get_volume,
    set_volume,
)

log = logging.getLogger("dlna.content")

# Per-renderer rate limiter for "renderer unreachable" WARN lines. The
# monitor polls GetTransportInfo every 2 s and the snapshot poller hits
# it too — without this, a powered-off renderer would flood gateway.log
# with one WARN per poll. Key: av_url → monotonic ts of last WARN.
_state_fail_log: dict = {}
_STATE_FAIL_WARN_SEC = 30.0


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
        ).encode()
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
            body = read_capped(resp, what=f"AVTransport {action}")
            if resp.status not in (200, 204):
                log.error(f"AVTransport {action} → HTTP {resp.status}: {body[:800]}")
                return False
            return True
        finally:
            close_quietly(conn)

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


def avtransport_set_next_uri(av_url: str, media_url: str, title: str = "",
                              mime: str = "audio/x-flac") -> bool:
    """Send `SetNextAVTransportURI` to a UPnP renderer for gapless
    transition. Called right after `avtransport_send` to pre-queue
    the next track so the renderer can flow into it with no audible
    gap when the current one ends.

    Pass `media_url=""` to clear the next URI (e.g. on the last
    track of the queue).

    Returns True on a 200/204 SOAP response. A failure here is
    non-fatal — the existing STOPPED→advance path handles the
    transition (with a small click) the way it always has.
    """
    safe_url   = _xml_esc(media_url or "")
    safe_title = _xml_esc(title)
    safe_mime  = _xml_esc(mime or "audio/x-flac")

    if media_url:
        metadata = (
            '&lt;DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/"'
            ' xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"&gt;'
            '&lt;item id="2" parentID="0" restricted="1"&gt;'
            f'&lt;dc:title&gt;{safe_title}&lt;/dc:title&gt;'
            '&lt;upnp:class&gt;object.item.audioItem.musicTrack&lt;/upnp:class&gt;'
            f'&lt;res protocolInfo="http-get:*:{safe_mime}:*"&gt;{safe_url}&lt;/res&gt;'
            '&lt;/item&gt;&lt;/DIDL-Lite&gt;'
        )
    else:
        metadata = ""

    body = (
        f'<u:SetNextAVTransportURI '
        f'xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        f'<InstanceID>0</InstanceID>'
        f'<NextURI>{safe_url}</NextURI>'
        f'<NextURIMetaData>{metadata}</NextURIMetaData>'
        f'</u:SetNextAVTransportURI>'
    )
    text, err = _av_soap(av_url, "SetNextAVTransportURI", body)
    if err:
        log.debug(f"SetNextAVTransportURI failed: {err}")
        return False
    if media_url:
        log.debug(f"AVTransport queued-next: {title!r}")
    else:
        log.debug("AVTransport cleared next URI")
    return True


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
    ).encode()
    parsed = urllib.parse.urlparse(av_url)
    conn   = http.client.HTTPConnection(parsed.netloc, timeout=8)
    try:
        conn.request("POST", parsed.path, envelope, {
            "Content-Type":   "text/xml; charset=utf-8",
            "SOAPAction":     f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
            "Content-Length": str(len(envelope)),
        })
        resp = conn.getresponse(); read_capped(resp, what="AVTransport")
        log.info(f"AVTransport {action} → {av_url}")
        return resp.status in (200, 204)
    except Exception as e:
        log.error(f"avtransport_pause: {e}")
        return False
    finally:
        close_quietly(conn)


def _av_soap(av_url: str, action: str,
             body_inner: str) -> tuple[str | None, str | None]:
    """Generic AVTransport SOAP helper.

    Returns ``(text, err)``:
      * success → ``(response_body, None)``
      * failure → ``(None, "<short reason>")`` — the reason is the
        underlying transport error (e.g. ``[Errno 61] Connection
        refused``, ``timed out``) or ``HTTP <status>`` for a non-2xx.

    Callers use the reason to distinguish "renderer unreachable" from
    "renderer genuinely reported UNKNOWN" instead of collapsing both
    into a bare None."""
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body>{body_inner}</s:Body></s:Envelope>'
    ).encode()
    parsed = urllib.parse.urlparse(av_url)
    conn   = http.client.HTTPConnection(parsed.netloc, timeout=6)
    try:
        conn.request("POST", parsed.path, envelope, {
            "Content-Type":   "text/xml; charset=utf-8",
            "SOAPAction":     f'"urn:schemas-upnp-org:service:AVTransport:1#{action}"',
            "Content-Length": str(len(envelope)),
        })
        resp = conn.getresponse()
        text = read_capped(resp, what=f"AVTransport {action}").decode(
            "utf-8", errors="replace")
        if resp.status in (200, 204):
            return text, None
        return None, f"HTTP {resp.status}"
    except Exception as e:
        log.debug(f"_av_soap {action}: {e}")
        return None, (str(e) or type(e).__name__)
    finally:
        close_quietly(conn)


def avtransport_probe_state(av_url: str) -> tuple[str, str]:
    """Query CurrentTransportState, distinguishing a lost renderer from
    a renderer that genuinely reports UNKNOWN.

    Returns ``(state, detail)``:
      * ``state`` is the real UPnP state (PLAYING / PAUSED_PLAYBACK /
        STOPPED / NO_MEDIA_PRESENT / TRANSITIONING / UNKNOWN), OR the
        gateway-synthesised ``UNREACHABLE`` when the GetTransportInfo
        SOAP call could not be completed (renderer powered off, network
        drop, HTTP error).
      * ``detail`` is the transport failure reason for an UNREACHABLE
        result (e.g. ``[Errno 61] Connection refused``), or ``""``.

    A WARN line naming the failure reason is emitted on the first
    failure for a given renderer and then at most once per
    ``_STATE_FAIL_WARN_SEC`` — the 2 s monitor poll would otherwise
    flood the log while a renderer stays down. A recovery is logged
    once when the renderer answers again."""
    raw, err = _av_soap(av_url, "GetTransportInfo",
        '<u:GetTransportInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID></u:GetTransportInfo>')
    if err is not None:
        now  = time.monotonic()
        last = _state_fail_log.get(av_url)
        if last is None or now - last > _STATE_FAIL_WARN_SEC:
            log.warning(f"avtransport_get_state: renderer unreachable "
                        f"({av_url}) — {err}")
            _state_fail_log[av_url] = now
        return "UNREACHABLE", err
    if _state_fail_log.pop(av_url, None) is not None:
        log.info(f"avtransport_get_state: renderer reachable again ({av_url})")
    if not raw:
        return "UNKNOWN", ""
    try:
        root = safe_fromstring(raw, what="avtransport")
        for el in root.iter():
            if el.tag.endswith("CurrentTransportState"):
                return (el.text or "UNKNOWN").strip(), ""
    except ET.ParseError:
        pass
    return "UNKNOWN", ""


def avtransport_get_state(av_url: str) -> str:
    """Thin wrapper over avtransport_probe_state() returning just the
    state string. ``UNREACHABLE`` means the SOAP call failed (renderer
    lost); ``UNKNOWN`` means the renderer answered but reported no
    usable state. Callers that need the failure reason should use
    avtransport_probe_state() directly."""
    return avtransport_probe_state(av_url)[0]


def avtransport_get_position(av_url: str) -> dict:
    """
    Returns position info dict:
      {"position": seconds_float, "duration": seconds_float,
       "title": str, "state": str, "track_uri": str}
    All fields may be None on failure. `track_uri` is the renderer's
    currently-playing URI (GetPositionInfo's <TrackURI>) — used to detect
    a gapless auto-advance to a queued SetNextAVTransportURI.
    """
    raw, _err = _av_soap(av_url, "GetPositionInfo",
        '<u:GetPositionInfo xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID></u:GetPositionInfo>')

    def _parse_time(s: str) -> float | None:
        """'H:MM:SS' or 'MM:SS' → float seconds, None if NOT_IMPLEMENTED."""
        if not s or s in ("NOT_IMPLEMENTED", "0:00:00", "00:00:00"):
            return None
        try:
            parts = [float(x) for x in s.split(":")]
            if len(parts) == 3:
                return parts[0]*3600 + parts[1]*60 + parts[2]
            if len(parts) == 2:
                return parts[0]*60 + parts[1]
        except (ValueError, TypeError, AttributeError):
            pass        # documented contract: unparseable → 0
        return None

    result: dict = {"position": None, "duration": None,
                    "title": None, "state": None, "track_uri": None}
    if not raw:
        return result
    try:
        root = safe_fromstring(raw, what="avtransport")
        for el in root.iter():
            tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if tag == "RelTime":
                result["position"] = _parse_time(el.text or "")
            elif tag == "TrackDuration":
                result["duration"] = _parse_time(el.text or "")
            elif tag == "TrackURI":
                result["track_uri"] = (el.text or "").strip() or None
            elif tag == "TrackMetaData" and el.text:
                # Extract title from embedded DIDL-Lite if present
                try:
                    didl = safe_fromstring(el.text, what="didl")
                    for t in didl.iter():
                        if t.tag.endswith("}title") or t.tag == "title":
                            result["title"] = t.text
                            break
                except ET.ParseError as e:
                    # A renderer that returns malformed DIDL just loses its
                    # title; position/duration above are still usable.
                    log.debug(f"CurrentTrackMetaData not parseable: {e}")
    except ET.ParseError:
        pass
    return result


def _sec_to_hms(sec: float) -> str:
    """Seconds → 'H:MM:SS' as AVTransport Seek/REL_TIME wants it."""
    s = max(0, int(sec))
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def avtransport_seek(av_url: str, seconds: float) -> bool:
    """Seek within the current track (AVTransport#Seek, Unit=REL_TIME).
    Audiobook resume (P3): jump to the saved offset after Play. Returns
    False on SOAP fault / transport failure — callers treat a failed
    seek as non-fatal (playback continues from 0:00)."""
    target = _sec_to_hms(seconds)
    raw, err = _av_soap(av_url, "Seek",
        '<u:Seek xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID>'
        '<Unit>REL_TIME</Unit>'
        f'<Target>{target}</Target>'
        '</u:Seek>')
    if err is not None:
        log.warning(f"avtransport_seek to {target} failed: {err}")
        return False
    return raw is not None


def avtransport_stop(av_url: str) -> bool:
    """Send Stop to a renderer."""
    envelope = (
        b'<?xml version="1.0" encoding="utf-8"?>'
        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        b' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        b'<s:Body>'
        b'<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        b'<InstanceID>0</InstanceID></u:Stop>'
        b'</s:Body></s:Envelope>'
    )
    parsed = urllib.parse.urlparse(av_url)
    conn   = http.client.HTTPConnection(parsed.netloc, timeout=8)
    try:
        conn.request("POST", parsed.path, envelope, {
            "Content-Type":   "text/xml; charset=utf-8",
            "SOAPAction":     '"urn:schemas-upnp-org:service:AVTransport:1#Stop"',
            "Content-Length": str(len(envelope)),
        })
        resp = conn.getresponse()
        read_capped(resp, what="AVTransport Stop")
        log.info(f"AVTransport ■ Stop → {av_url}")
        return resp.status in (200, 204)
    except Exception as e:
        log.error(f"AVTransport Stop error: {e}")
        return False
    finally:
        close_quietly(conn)
