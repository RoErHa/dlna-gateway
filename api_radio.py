#!/usr/bin/env python3
"""
api_radio.py — Internet-radio API handlers (Phase 1).

Handles:
  GET  /api/radio/search             — proxy radio-browser.info search
  GET  /api/radio/favourites         — the saved stations (≤25)
  POST /api/radio/favourites/add     — add a station (full station JSON)
  POST /api/radio/favourites/remove  — remove by {station_uuid}
  POST /api/radio/favourites/reorder — persist preset order {order:[…]}

The station catalogue is radio-browser.info; only the user's
favourites (capped at LibraryDB.RADIO_FAV_MAX) are persisted. See the
"Internet radio" section in CLAUDE.md. Phase 1 has no ICY metadata and
no now-playing screen — playback is the caller's job via the existing
/api/render_queue (a station is just a single is_stream "track").
"""
import http.client
import json
import logging
import os
import random
import urllib.parse

import dlna_stream_proxy
from dlna_library import DB
from dlna_player import QUEUES

from dlna_config import close_quietly

log = logging.getLogger("dlna.api.radio")

# radio-browser.info — community station directory. Contacted over
# HTTPS; an identifying User-Agent is required, same contract as
# MusicBrainz. The API is DNS round-robin across several mirrors; we
# shuffle a small static list so load spreads and one dead mirror
# doesn't kill search. Contact email comes from GATEWAY_CONTACT_EMAIL
# (set in .env — see dlna_art_fetcher for the placeholder-warning).
_RB_HOSTS      = ["de1.api.radio-browser.info", "nl1.api.radio-browser.info"]
_RB_USER_AGENT = (f"DLNAGateway/1.0 ( "
                  f"{os.environ.get('GATEWAY_CONTACT_EMAIL','you@example.com').strip() or 'you@example.com'} )")
_RB_TIMEOUT    = 10.0


def _radiobrowser_get(path: str):
    """GET a radio-browser JSON path, trying mirrors in random order.
    Returns the decoded JSON (list/dict) or None if every mirror fails."""
    hosts = _RB_HOSTS[:]
    random.shuffle(hosts)
    for host in hosts:
        conn = http.client.HTTPSConnection(host, timeout=_RB_TIMEOUT)
        try:
            conn.request("GET", path, headers={"User-Agent": _RB_USER_AGENT})
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                log.warning(f"radio-browser {host} → HTTP {resp.status}")
                continue
            return json.loads(body)
        except Exception as e:
            log.warning(f"radio-browser {host} unreachable: {e}")
            continue
        finally:
            close_quietly(conn)
    return None


def _normalize_station(s: dict) -> dict:
    """Map a radio-browser station record onto the gateway's station
    shape — the exact keys LibraryDB.radio_fav_add() expects."""
    return {
        "station_uuid": s.get("stationuuid", ""),
        "name":         (s.get("name") or "").strip(),
        "stream_url":   s.get("url_resolved") or s.get("url") or "",
        "homepage":     s.get("homepage", ""),
        "favicon":      s.get("favicon", ""),
        "codec":        s.get("codec", ""),
        "bitrate":      s.get("bitrate", 0),
        "country":      s.get("countrycode", ""),
        "tags":         s.get("tags", ""),
    }


def search(h, params):
    code, body = search_payload(params)
    h._json(code, body)


def search_payload(params) -> tuple:
    """Core of GET /api/radio/search?q=&country=&tag=&limit= → (status, body).

    Proxy a radio-browser station search. HLS streams are filtered out
    — UPnP renderers can't play them and browser <audio> only does on
    Safari (see CLAUDE.md). On 200 the body is a JSON array of normalized
    stations. Shared by the legacy handler and the 2.0 native route.
    """
    q       = (params.get("q") or "").strip()
    country = (params.get("country") or "").strip()
    tag     = (params.get("tag") or "").strip()
    try:
        limit = max(1, min(100, int(params.get("limit", "40"))))
    except (TypeError, ValueError):
        limit = 40
    if not q and not country and not tag:
        return 400, {"error": "need one of q, country, tag"}

    query = {"limit": str(limit * 2),          # over-fetch; HLS filter trims
             "hidebroken": "true",
             "order": "clickcount", "reverse": "true"}
    if q:       query["name"]        = q
    if country: query["countrycode"] = country
    if tag:     query["tagList"]     = tag
    path = "/json/stations/search?" + urllib.parse.urlencode(query)

    data = _radiobrowser_get(path)
    if data is None:
        return 502, {"error": "radio directory unreachable"}
    if not isinstance(data, list):
        return 502, {"error": "radio directory returned bad data"}
    out = [_normalize_station(s) for s in data
           if not s.get("hls") and (s.get("url_resolved") or s.get("url"))]
    return 200, out[:limit]


def favourites(h, params):
    """GET /api/radio/favourites — saved stations + the cap, for the UI
    to know when the user is at the limit."""
    h._json(200, {"stations": DB.radio_fav_list(),
                  "limit":    DB.RADIO_FAV_MAX})


def _body_json(body):
    """Parse a POST body as JSON; None on any failure."""
    try:
        return json.loads(body or b"{}")
    except (ValueError, TypeError):
        return None


def favourite_add(h, body):
    """POST /api/radio/favourites/add

    Body is the full station object as returned by /api/radio/search.
    Returns 409 {error:"favourites_full"} when the 25-cap is hit; the
    UI must then ask the user to remove a favourite first.
    """
    station = _body_json(body)
    if not isinstance(station, dict):
        h._json(400, {"error": "bad JSON body"})
        return
    result = DB.radio_fav_add(station)
    if result == "bad":
        h._json(400, {"error": "missing station_uuid / name / stream_url"})
    elif result == "full":
        h._json(409, {"error": "favourites_full", "limit": DB.RADIO_FAV_MAX})
    else:  # 'ok' | 'exists'
        h._json(200, {"ok": True, "created": result == "ok"})


def favourite_remove(h, body):
    """POST /api/radio/favourites/remove — body {station_uuid}."""
    data = _body_json(body)
    if not isinstance(data, dict) or not data.get("station_uuid"):
        h._json(400, {"error": "missing station_uuid"})
        return
    ok = DB.radio_fav_remove(data["station_uuid"])
    h._json(200, {"ok": ok})


def favourite_reorder(h, body):
    """POST /api/radio/favourites/reorder — body {order:[uuid,…]}."""
    data  = _body_json(body)
    order = data.get("order") if isinstance(data, dict) else None
    if (not isinstance(order, list)
            or not all(isinstance(u, str) for u in order)):
        h._json(400, {"error": "order must be a list of station_uuid"})
        return
    ok = DB.radio_fav_reorder(order)
    h._json(200, {"ok": ok})


def radio_stream(h, params):
    """GET /radio_stream?url=<stream_url>

    Browser-audio proxy for an internet-radio stream — de-interleaves
    ICY metadata so <audio> gets clean audio and the StreamTitle is
    parked for /api/radio/nowplaying. The UPnP/Naim path does NOT use
    this — the renderer streams the station URL directly.
    """
    url = params.get("url", "")
    if not url:
        h.send_error(400, "Missing url")
        return
    dlna_stream_proxy.proxy_radio_stream(url, h)


def nowplaying(h, params):
    """GET /api/radio/nowplaying

    Current "now playing" text for the radio screen, from one of:
      ?stream=<url>  — browser path: the ICY StreamTitle the
                       proxy_radio_stream de-interleaver last saw.
      ?udn=<udn>     — UPnP path: the renderer's CurrentTrackMetaData
                       (the Naim parses ICY itself; it surfaces in the
                       existing queue snapshot as media_title).
    Returns {title, source}. An empty title means nothing is known yet
    (stream just started, or the station sends no metadata).
    """
    code, body = nowplaying_payload(params)
    h._json(code, body)


def nowplaying_payload(params) -> tuple:
    """Core of GET /api/radio/nowplaying → (status, body). Shared legacy +
    native. NB the `?udn=` path calls snapshot() which may issue SOAP to the
    renderer — runs in a threadpool on the native route."""
    stream = params.get("stream", "")
    udn    = params.get("udn", "")
    if stream:
        info = dlna_stream_proxy.icy_now(stream)
        return 200, {"title":  (info or {}).get("title", ""),
                     "source": "icy", "stream": stream}
    if udn:
        q    = QUEUES.peek(udn)
        snap = q.snapshot() if q else {}
        return 200, {"title":  snap.get("media_title", ""),
                     "source": "upnp", "udn": udn}
    return 400, {"error": "need stream or udn"}
