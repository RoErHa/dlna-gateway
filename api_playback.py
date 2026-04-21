#!/usr/bin/env python3
"""
api_playback.py — Playback, state, and stream proxy API handlers.

Handles: /api/renderer_state, /api/index/status, /api/index/rebuild,
         /api/render_queue, /api/render, /api/control,
         /api/edit_track, /stream
"""
import json
import logging
import threading

from dlna_content import avtransport_send
from dlna_discovery import RENDERERS, SERVERS
from dlna_library import DB, INDEXER
from dlna_player import RENDERER_QUEUE, proxy_stream

log = logging.getLogger("dlna.api.playback")


# ── GET handlers ──────────────────────────────────────────────────

def renderer_state(h, params):
    h._json(200, RENDERER_QUEUE.snapshot())


def index_status(h, params):
    udn   = params.get("udn", "")
    count = DB.track_count(udn) if udn else 0
    h._json(200, {**INDEXER.state.get(), "db_tracks": count})


def index_rebuild(h, params):
    udn = params.get("udn", "")
    srv = SERVERS.get(udn)
    if not srv:
        h._json(404, {"error": "Server not found"})
        return
    INDEXER.start(srv, force=True)
    h._json(200, {"ok": True, "message": "Reindex started"})


def stream(h, params):
    url = params.get("url", "")
    if not url:
        h.send_error(400, "Missing url")
        return
    proxy_stream(url, h)


# ── POST handlers ─────────────────────────────────────────────────

def render_queue(h, body):
    try:
        data   = json.loads(body)
        udn    = data.get("udn", "")
        tracks = data.get("tracks", [])
        rnd    = RENDERERS.get(udn)
        if not rnd:
            h._json(404, {"error": f"Renderer {udn!r} not found"})
            return
        if not tracks:
            h._json(400, {"error": "No tracks"})
            return
        log.info(f"POST /api/render_queue  {len(tracks)} tracks → {rnd.name}")
        threading.Thread(
            target=RENDERER_QUEUE.start,
            args=(rnd.av_url, tracks, rnd.name),
            daemon=True).start()
        h._json(200, {"ok": True, "tracks": len(tracks)})
    except Exception as e:
        log.exception(f"render_queue error: {e}")
        h._json(500, {"error": str(e)})


def render(h, body):
    try:
        data  = json.loads(body)
        udn   = data.get("udn", "")
        url   = data.get("url", "")
        title = data.get("title", "")
        mime  = data.get("mime", "")
        rnd   = RENDERERS.get(udn)
        if not rnd:
            h._json(404, {"error": f"Renderer {udn!r} not found"})
            return
        ok = avtransport_send(rnd.av_url, url, title, mime)
        log.info(f"POST /api/render  {title!r} → {rnd.name}  ok={ok}")
        h._json(200 if ok else 502, {"ok": ok})
    except Exception as e:
        log.exception(f"render error: {e}")
        h._json(500, {"error": str(e)})


def control(h, body):
    try:
        cmd    = json.loads(body)
        action = cmd.get("action", "")
        device = cmd.get("device", "")

        if device.startswith("upnp:"):
            udn = device.replace("upnp:", "")
            rnd = RENDERERS.get(udn)
            if not rnd:
                h._json(404, {"error": "Renderer not found"})
                return
            if action == "pause":
                RENDERER_QUEUE.pause()
            elif action == "stop":
                RENDERER_QUEUE.stop()
            elif action == "next":
                RENDERER_QUEUE.next_track()
            elif action == "prev":
                RENDERER_QUEUE.prev_track()
            else:
                log.debug(f"Renderer control: {action!r} not implemented")
            h._json(200, {"ok": True})
        else:
            h._json(400, {"error": f"Unknown device: {device!r}"})

    except Exception as e:
        log.warning(f"control error: {e}")
        h._json(400, {"error": str(e)})


def edit_track(h, body):
    try:
        data   = json.loads(body)
        url    = data.get("url", "")
        artist = data.get("artist")
        album  = data.get("album")
        title  = data.get("title")
        genre  = data.get("genre")
        if not url:
            h._json(400, {"error": "Missing url"})
            return
        ok = DB.update_track_meta(url, artist=artist, album=album,
                                  title=title, genre=genre)
        log.info(f"edit_track: {url[:60]}  "
                 f"fields={[k for k, v in [('artist', artist), ('album', album), ('title', title), ('genre', genre)] if v is not None]}")
        h._json(200, {"ok": ok})
    except Exception as e:
        log.exception(f"edit_track error: {e}")
        h._json(500, {"error": str(e)})
