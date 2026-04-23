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
from dlna_player import QUEUES, proxy_stream

log = logging.getLogger("dlna.api.playback")


def _parse_json_or_400(h, body):
    """Parse a JSON request body into a dict. On failure (malformed JSON
    OR top-level non-object like '[]' / '"string"' / '42'), send 400
    and return None so the caller can bail.

    Malformed input is a client error, not a server error — returning
    500 would be wrong and trip the chaos suite's 5xx gate. The dict
    check is important: json.loads('[]') succeeds but then data.get()
    raises AttributeError, which is what surfaced in the chaos run."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as e:
        h._json(400, {"error": f"invalid JSON: {e}"})
        return None
    if not isinstance(data, dict):
        h._json(400, {"error": f"expected JSON object, got {type(data).__name__}"})
        return None
    return data


# ── GET handlers ──────────────────────────────────────────────────

def renderer_state(h, params):
    udn = params.get("udn", "")
    if udn:
        h._json(200, QUEUES.get(udn).snapshot())
        return
    # No UDN → "what's playing anywhere". Return every queue's snapshot,
    # plus a flat legacy view of the first alive one so old UI tabs that
    # haven't been updated still render something sensible.
    all_snaps = QUEUES.snapshot_all()
    legacy = {"state": "stopped", "alive": False, "renderer": "",
              "queue_len": 0, "queue_pos": 0}
    for snap in all_snaps.values():
        if snap.get("alive"):
            legacy = snap
            break
    h._json(200, {**legacy, "queues": all_snaps})


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
        data = _parse_json_or_400(h, body)
        if data is None:
            return
        udn    = data.get("udn", "")
        tracks = data.get("tracks", [])
        force  = bool(data.get("force", False))
        rnd    = RENDERERS.get(udn)
        if not rnd:
            h._json(404, {"error": f"Renderer {udn!r} not found"})
            return
        if not tracks:
            h._json(400, {"error": "No tracks"})
            return

        # Busy check: one physical output can only play one thing. If the
        # renderer is already active for another session, refuse with 409
        # unless the client explicitly opted to take over via force=true.
        if not force and QUEUES.is_busy(udn):
            busy = QUEUES.get(udn).snapshot()
            log.info(f"render_queue 409 busy {rnd.name}: already playing "
                     f"{busy.get('title','?')!r}")
            h._json(409, {
                "error":     "renderer_busy",
                "message":   f"{rnd.name} is already playing. "
                             f"Pass force=true to take over.",
                "busy_with": {
                    "title":    busy.get("title", ""),
                    "artist":   busy.get("artist", ""),
                    "renderer": busy.get("renderer", rnd.name),
                },
            })
            return

        log.info(f"POST /api/render_queue  {len(tracks)} tracks → {rnd.name}"
                 f"{'  (force=True, taking over)' if force else ''}")
        queue = QUEUES.get(udn)

        def _start_safe(q=queue, av_url=rnd.av_url, tracks=tracks, name=rnd.name):
            try:
                q.start(av_url, tracks, name)
            except Exception:
                log.exception(
                    f"RendererQueue.start crashed for {name} — "
                    f"queue of {len(tracks)} track(s) aborted")

        threading.Thread(target=_start_safe, daemon=True).start()
        h._json(200, {"ok": True, "tracks": len(tracks)})
    except Exception as e:
        log.exception(f"render_queue error: {e}")
        h._json(500, {"error": str(e)})


def render(h, body):
    try:
        data = _parse_json_or_400(h, body)
        if data is None:
            return
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
        cmd = _parse_json_or_400(h, body)
        if cmd is None:
            return
        action = cmd.get("action", "")
        device = cmd.get("device", "")

        if device.startswith("upnp:"):
            udn = device.replace("upnp:", "")
            rnd = RENDERERS.get(udn)
            if not rnd:
                h._json(404, {"error": "Renderer not found"})
                return
            queue = QUEUES.get(udn)
            if action == "pause":
                queue.pause()
            elif action == "stop":
                queue.stop()
            elif action == "next":
                queue.next_track()
            elif action == "prev":
                queue.prev_track()
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
        data = _parse_json_or_400(h, body)
        if data is None:
            return
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
