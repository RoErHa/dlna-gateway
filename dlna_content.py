#!/usr/bin/env python3
"""
dlna_content.py — UPnP ContentDirectory SOAP client + AVTransport sender.

  cd_browse()        — Browse a container
  cd_search()        — FTS search (three parallel SOAP calls)
  avtransport_send() — SetAVTransportURI + Play on a renderer

Standalone test:
    python dlna_content.py http://<server-ip>:<port>/<control-url-path>
"""
import http.client
import logging
import socket
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional

log = logging.getLogger("dlna.content")

# UPnP XML namespaces
_CD_NS  = "urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"
_DC_NS  = "http://purl.org/dc/elements/1.1/"
_UPP_NS = "urn:schemas-upnp-org:metadata-1-0/upnp/"

# Limit concurrent SOAP connections to AssetUPnP — it has a small pool
_SOAP_SEM = threading.Semaphore(3)


# ── SOAP transport ────────────────────────────────────────────────

def _get_lan_source_ip(target_host: str) -> tuple:
    """
    Return the local LAN IP used to reach target_host.
    Forces SOAP connections through the real network interface, not loopback,
    so AssetUPnP (which rejects same-machine requests) sees a LAN source IP.
    """
    try:
        target_ip = target_host.split(":")[0]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect((target_ip, 80))
        local_ip = s.getsockname()[0]
        s.close()
        return (local_ip, 0)   # (host, port) tuple for source_address
    except Exception:
        return None


def _soap_post(host: str, path: str, body: bytes, action: str) -> tuple:
    """
    Send a SOAP POST; return (http_status, response_text).
    Gated through _SOAP_SEM so at most 3 requests are in-flight.
    Binds to the LAN IP so AssetUPnP does not see a loopback source.
    """
    with _SOAP_SEM:
        src = _get_lan_source_ip(host)
        conn = http.client.HTTPConnection(host, timeout=15,
                                          source_address=src)
        try:
            conn.request("POST", path, body=body, headers={
                "Host":            host,
                "Content-Type":    "text/xml; charset=utf-8",
                "Content-Length":  str(len(body)),
                "SOAPAction":      action,
                "User-Agent":      "DLNAGateway/1.0",
                "Connection":      "close",
            })
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", errors="replace")
        finally:
            try:
                conn.close()
            except Exception:
                pass


# ── ContentDirectory Browse ───────────────────────────────────────

_BROWSE_FILTER = (
    "dc:title,dc:creator,dc:date,upnp:class,upnp:artist,upnp:album,"
    "upnp:genre,upnp:albumArtURI,res,res@duration,res@size,res@protocolInfo"
)


def cd_browse(control_url: str, object_id: str = "0",
              start: int = 0, count: int = 500) -> dict:
    """
    Browse a ContentDirectory container.
    Returns {"containers": [...], "items": [...]} or adds "error" key on failure.
    Retries once with filter="*" if the server returns 500 (some servers reject
    specific filter strings on startup or for certain containers).
    """
    parsed = urllib.parse.urlparse(control_url)
    host   = parsed.netloc
    path   = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    action = '"urn:schemas-upnp-org:service:ContentDirectory:1#Browse"'

    def _make_soap(flt: str) -> bytes:
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:Browse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
            f'<ObjectID>{object_id}</ObjectID>'
            '<BrowseFlag>BrowseDirectChildren</BrowseFlag>'
            f'<Filter>{flt}</Filter>'
            f'<StartingIndex>{start}</StartingIndex>'
            f'<RequestedCount>{count}</RequestedCount>'
            '<SortCriteria></SortCriteria>'
            '</u:Browse>'
            '</s:Body>'
            '</s:Envelope>'
        ).encode("utf-8")

    try:
        # First attempt with specific filter
        status, raw = _soap_post(host, path, _make_soap(_BROWSE_FILTER), action)
        if status == 200:
            return _parse_didl(raw)

        # 500 from some servers (e.g. AssetUPnP on startup, or fussy filter)
        # — retry once with wildcard filter before giving up
        if status == 500:
            log.debug(f"Browse SOAP 500 — retrying with filter=* @ {control_url}")
            status2, raw2 = _soap_post(host, path, _make_soap("*"), action)
            if status2 == 200:
                return _parse_didl(raw2)
            log.warning(f"Browse SOAP {status2} (retry) @ {control_url}\n  {raw2[:200]}")
            return {"containers": [], "items": [],
                    "error": f"HTTP {status2} — {raw2[:200]}"}

        log.warning(f"Browse SOAP {status} @ {control_url}\n  {raw[:300]}")
        return {"containers": [], "items": [],
                "error": f"HTTP {status} — {raw[:200]}"}
    except Exception as e:
        log.warning(f"Browse failed ({control_url} id={object_id}): {e}")
        return {"containers": [], "items": [], "error": str(e)}


def browse_all(control_url: str, container_id: str,
               max_items: int = 5000) -> tuple:
    """Paginate through a container, 500 items at a time."""
    all_containers, all_items = [], []
    start, page = 0, 500
    while start < max_items:
        result = cd_browse(control_url, container_id, start=start, count=page)
        all_containers.extend(result.get("containers", []))
        all_items.extend(result.get("items", []))
        fetched = (len(result.get("containers", [])) +
                   len(result.get("items", [])))
        if fetched < page:
            break
        start += page
    return all_containers, all_items


# ── DIDL-Lite parser ──────────────────────────────────────────────

def _parse_didl(soap_xml: str) -> dict:
    """Extract containers and items from a Browse SOAP response."""
    containers, items = [], []
    try:
        root      = ET.fromstring(soap_xml)
        result_el = next(
            (el for el in root.iter()
             if el.tag.endswith("}Result") or el.tag == "Result"),
            None)
        if result_el is None or not (result_el.text or "").strip():
            return {"containers": [], "items": []}

        didl = ET.fromstring(result_el.text)

        for child in didl:
            tag    = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            obj_id = child.get("id", "")
            par_id = child.get("parentID", "")
            title  = (child.findtext(f"{{{_DC_NS}}}title")
                      or child.findtext("title") or "Untitled")

            if tag == "container":
                art = child.findtext(f"{{{_UPP_NS}}}albumArtURI") or ""
                artist = child.findtext(f"{{{_UPP_NS}}}artist") or ""
                containers.append({
                    "id": obj_id, "parent": par_id, "title": title,
                    "artist": artist,
                    "childCount": child.get("childCount", "?"),
                    "type": "container", "art": art,
                })

            elif tag == "item":
                upnp_class = child.findtext(f"{{{_UPP_NS}}}class") or ""
                artist = (child.findtext(f"{{{_UPP_NS}}}artist")
                          or child.findtext(f"{{{_DC_NS}}}creator") or "")
                album  = child.findtext(f"{{{_UPP_NS}}}album") or ""
                res_url = res_mime = res_dur = ""
                file_path = ""

                for res in child.findall(f"{{{_CD_NS}}}res"):
                    url = (res.text or "").strip()
                    if url.startswith("http") and not res_url:
                        res_url  = url
                        proto    = res.get("protocolInfo", "")
                        res_mime = proto.split(":")[2] if proto.count(":") >= 2 else ""
                        res_dur  = res.get("duration", "")
                    elif url.startswith("file://") and not file_path:
                        # Local file path — allows tag editing without re-index
                        import urllib.request
                        file_path = urllib.request.url2pathname(url[7:])

                if not res_url:
                    continue

                mtype = ("video"
                         if "video" in upnp_class or "video" in res_mime
                         else "audio")
                art = child.findtext(f"{{{_UPP_NS}}}albumArtURI") or ""

                genre = child.findtext(f"{{{_UPP_NS}}}genre") or ""
                items.append({
                    "id": obj_id, "parent": par_id, "title": title,
                    "artist": artist, "album": album,
                    "duration": res_dur, "url": res_url,
                    "mime": res_mime, "type": mtype, "art": art,
                    "genre": genre, "file_path": file_path,
                })

    except ET.ParseError as e:
        log.debug(f"DIDL parse error: {e}")

    return {"containers": containers, "items": items}


# ── ContentDirectory Search ───────────────────────────────────────

def cd_search(control_url: str, query: str, count: int = 200) -> dict:
    """
    Run three parallel UPnP searches (title / artist / album) and merge.
    Falls back gracefully — AssetUPnP sometimes rejects Search entirely.
    """
    criteria = [
        f'dc:title contains "{query}"',
        f'upnp:artist contains "{query}"',
        f'upnp:album contains "{query}"',
    ]
    parsed = urllib.parse.urlparse(control_url)
    host   = parsed.netloc
    path   = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    def run_one(criterion: str) -> dict:
        soap = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            '<u:Search xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
            '<ContainerID>0</ContainerID>'
            f'<SearchCriteria>{criterion}</SearchCriteria>'
            '<Filter>dc:title,dc:creator,upnp:class,upnp:artist,upnp:album,'
            'upnp:albumArtURI,res,res@duration,res@protocolInfo</Filter>'
            '<StartingIndex>0</StartingIndex>'
            f'<RequestedCount>{count}</RequestedCount>'
            '<SortCriteria>+upnp:artist,+upnp:album,+dc:title</SortCriteria>'
            '</u:Search>'
            '</s:Body>'
            '</s:Envelope>'
        )
        try:
            status, raw = _soap_post(
                host, path, soap.encode("utf-8"),
                '"urn:schemas-upnp-org:service:ContentDirectory:1#Search"')
            if status == 200:
                return _parse_didl(raw)
            log.warning(f"Search {status} for '{criterion}'")
        except Exception as e:
            log.warning(f"Search error ({criterion}): {e}")
        return {"containers": [], "items": []}

    results = [None] * len(criteria)
    threads = []
    for i, crit in enumerate(criteria):
        def worker(idx=i, c=crit):
            results[idx] = run_one(c)
        t = threading.Thread(target=worker, daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=20)

    seen_urls: set = set()
    seen_ids:  set = set()
    containers, items = [], []

    for r in results:
        if not r:
            continue
        for c in r.get("containers", []):
            if c["id"] not in seen_ids:
                seen_ids.add(c["id"])
                containers.append(c)
        for item in r.get("items", []):
            url = item.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                items.append(item)

    items.sort(key=lambda x: (x.get("artist", "").lower(),
                               x.get("album", "").lower(),
                               x.get("title", "").lower()))
    return {"containers": containers, "items": items}


# ── AVTransport (send to renderer) ────────────────────────────────

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
                log.error(f"AVTransport {action} → HTTP {resp.status}: {body[:200]}")
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


# ── Standalone test ───────────────────────────────────────────────

def _test():
    import sys
    from dlna_config import setup_logging
    setup_logging(debug=True)

    if len(sys.argv) < 2:
        print("Usage: python dlna_content.py <control_url>")
        print("  e.g. python dlna_content.py "
              "http://192.168.1.125:26125/ContentDirectory/.../control.xml")
        return

    url = sys.argv[1]
    log.info(f"=== dlna_content self-test against {url} ===")

    log.info("Browsing root (id=0)…")
    result = cd_browse(url, "0", count=20)
    if result.get("error"):
        log.error(f"FAIL: {result['error']}")
        return

    containers = result.get("containers", [])
    items      = result.get("items", [])
    log.info(f"Root: {len(containers)} containers, {len(items)} items")

    for c in containers[:5]:
        log.info(f"  📁 {c['title']}  id={c['id']}")
    for i in items[:5]:
        log.info(f"  🎵 {i['title']}  url={i['url'][:60]}")

    if containers:
        first = containers[0]
        log.info(f"Browsing first container: {first['title']!r} ({first['id']})…")
        sub = cd_browse(url, first["id"], count=10)
        log.info(f"  → {len(sub.get('containers',[]))} sub-containers, "
                 f"{len(sub.get('items',[]))} items")

    log.info("PASS — dlna_content OK")


if __name__ == "__main__":
    _test()
