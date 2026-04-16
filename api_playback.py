#!/usr/bin/env python3
"""
api_playback.py — Playback, state, and stream proxy API handlers.

Handles: /api/play, /api/play_tracks, /api/state, /api/renderer_state,
         /api/capabilities, /api/index/status, /api/index/rebuild,
         /api/cast_devices, /api/cast_state, /api/cast_queue,
         /api/render_queue, /api/render, /api/control,
         /api/edit_track, /stream
"""
import json
import logging
import os
import socket
import threading

from dlna_cast import CAST_DEVICES, CAST_QUEUE
from dlna_config import M3U_TMP
from dlna_content import avtransport_send
from dlna_discovery import RENDERERS, SERVERS
from dlna_library import DB, INDEXER
from dlna_player import PLAYER, RENDERER_QUEUE, proxy_stream, IINA_PATHS

log = logging.getLogger("dlna.api.playback")


def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return socket.gethostbyname(socket.gethostname())


# ── GET handlers ──────────────────────────────────────────────────

def cast_devices(h, params):
    h._json(200, [d.to_dict() for d in CAST_DEVICES.all()])


def cast_state(h, params):
    h._json(200, CAST_QUEUE.snapshot())


def play(h, params):
    url   = params.get("url", "")
    title = params.get("title", "")
    if not url:
        h._json(400, {"error": "Missing url"})
        return
    threading.Thread(target=PLAYER.play, args=(url, title), daemon=True).start()
    log.info(f"GET /api/play  title={title!r}")
    h._json(200, {"ok": True})


def renderer_state(h, params):
    h._json(200, RENDERER_QUEUE.snapshot())


def capabilities(h, params):
    import shutil
    iina_ok = any(
        os.path.exists(p) or bool(shutil.which(p))
        for p in IINA_PATHS)
    h._json(200, {"iina": iina_ok})


def state(h, params):
    h._json(200, PLAYER.snapshot())


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

def play_tracks(h, body):
    try:
        data   = json.loads(body)
        tracks = data.get("tracks", [])
        title  = data.get("title", "Playlist")
        if not tracks:
            h._json(400, {"error": "No tracks provided"})
            return
        m3u = DB.tracks_to_m3u(tracks, M3U_TMP)
        threading.Thread(target=PLAYER.play, args=(m3u, title), daemon=True).start()
        log.info(f"POST /api/play_tracks  {len(tracks)} tracks → {m3u}")
        h._json(200, {"ok": True, "tracks": len(tracks)})
    except Exception as e:
        log.exception(f"play_tracks error: {e}")
        h._json(500, {"error": str(e)})


def cast_queue(h, body):
    try:
        data   = json.loads(body)
        uuid   = data.get("uuid", "")
        tracks = data.get("tracks", [])
        dev    = CAST_DEVICES.get(uuid)
        if not dev:
            h._json(404, {"error": f"Cast device {uuid!r} not found"})
            return
        if not tracks:
            h._json(400, {"error": "No tracks"})
            return
        lan_ip = _get_lan_ip()
        port   = h.server.server_address[1]
        stream_base = f"http://{lan_ip}:{port}"
        log.info(f"POST /api/cast_queue  {len(tracks)} tracks → {dev.name}  (base: {stream_base})")
        threading.Thread(
            target=CAST_QUEUE.start,
            args=(uuid, tracks, stream_base),
            daemon=True).start()
        h._json(200, {"ok": True, "tracks": len(tracks), "device": dev.name})
    except Exception as e:
        log.exception("cast_queue error")
        h._json(500, {"error": str(e)})


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
        device = cmd.get("device", "iina")

        if device.startswith("cast:"):
            uuid = device.replace("cast:", "")
            dev  = CAST_DEVICES.get(uuid)
            if not dev:
                h._json(404, {"error": "Cast device not found"})
                return
            if action == "pause":
                CAST_QUEUE.pause()
            elif action == "stop":
                CAST_QUEUE.stop()
            elif action in ("next", "prev"):
                lan_ip = _get_lan_ip()
                port   = h.server.server_address[1]
                base   = f"http://{lan_ip}:{port}"
                if action == "next":
                    CAST_QUEUE.next_track(base)
                else:
                    CAST_QUEUE.prev_track(base)
            else:
                log.debug(f"Cast control: {action!r} not implemented")
            h._json(200, {"ok": True})

        elif device.startswith("upnp:"):
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
            # IINA / mpv
            if action == "pause":
                PLAYER.ipc({"command": ["cycle", "pause"]})
            elif action == "stop":
                PLAYER.ipc({"command": ["stop"]})
                with PLAYER._lock:
                    PLAYER.state = "stopped"
            elif action == "next":
                PLAYER.ipc({"command": ["playlist-next", "force"]})
            elif action == "prev":
                PLAYER.ipc({"command": ["playlist-prev", "force"]})
            elif action == "shuffle":
                PLAYER.ipc({"command": ["cycle", "shuffle"]})
            elif action == "seek":
                PLAYER.ipc({"command": ["seek",
                            float(cmd.get("value", 0)), "relative"]})
            elif action == "seek_abs":
                PLAYER.ipc({"command": ["seek",
                            float(cmd.get("value", 0)), "absolute"]})
            elif action == "volume":
                PLAYER.ipc({"command": ["set_property", "volume",
                            int(cmd.get("value", 80))]})
            h._json(200, {"ok": True})

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
