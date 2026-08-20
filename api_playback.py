#!/usr/bin/env python3
"""
api_playback.py — playback control, index management, status, and the
public face of the api_playback module family.

Handles the write side of playback: posting a queue to a renderer,
transport control, volume trim, rebuilding the index, and the browser-side
error reports that land in gateway.log.

── Module family ────────────────────────────────────────────────────
This file was 749 lines until 2026-08-20, mixing cover art, playback
control and the metadata/position layer:

    api_playback_state.py  shared handles (DB/INDEXER/QUEUES/SERVERS/…)
    api_playback_art.py    the /art subsystem: fetch, cache, downscale, serve
    api_playback_meta.py   track metadata, lyrics, positions, book meta
    api_playback.py        playback control, index, status + re-exports

Every public name is re-exported below, so callers (dlna_asgi_*,
dlna_routes, api_subsonic_media) and the test sites that patch through this
module are unaffected.

⚠ Shared handles are bound ONCE, in api_playback_state. WHOLESALE rebinding
in a test must target the owner (`patch.object(api_playback_state, "QUEUES",
…)`); patching `api_playback.QUEUES` only rebinds this module's re-export.

── Concurrency contract ─────────────────────────────────────────────
ONE active stream per physical output, enforced server-side: posting to a
busy renderer returns 409 with what it is playing, and the client re-sends
with `force: true` to take over. Different UDNs are fully independent.

Workers started by `/api/render_queue` are wrapped so any crash inside
`RendererQueue.start()` lands in gateway.log rather than dying silently in
/tmp/dlna-gateway.err — that silence is how a whole class of playback bugs
stayed invisible.
"""
import logging
import threading

from dlna_avtransport import avtransport_send
from dlna_config import VERSION, close_quietly  # noqa: F401

# ── Re-exports: the family's public surface ──────────────────────────
from api_playback_art import (  # noqa: F401
    _ART_JPEG_QUALITY,
    _PIL_LANCZOS,
    _PILImage,
    _ART_MAX_BYTES,
    _ART_MAX_REDIRECTS,
    _ART_MIN_BYTES,
    _ART_SIZE_BUCKETS,
    _ART_TIMEOUT,
    _scale_image,
    _size_bucket,
    art,
    art_fetch,
    art_fetch_cached,
    art_fetch_scaled,
)
from api_playback_meta import (  # noqa: F401
    _SENTINEL,
    _chapters_cache,
    book_meta_all_payload,
    chapters_payload,
    edit_track,
    lyrics,
    lyrics_payload,
    position_get_payload,
    position_save_payload,
    positions_list_payload,
    track_meta,
)
import api_playback_state as _st
from api_playback_state import (  # noqa: F401
    DB,
    _parse_json_or_400,
    INDEXER,
    QUEUES,
    RENDERERS,
    SERVERS,
    get_provider,
    proxy_stream,
)

log = logging.getLogger("dlna.api.playback")


def version(h, params):
    """Report the running gateway version (release-line marker). Lets a
    side-by-side 1.x / 2.0 instance be told apart from the PWA and curl."""
    h._json(200, {"version": VERSION})


# ── GET handlers ──────────────────────────────────────────────────

def renderer_state(h, params):
    udn = params.get("udn", "")
    if udn:
        h._json(200, _st.QUEUES.get(udn).snapshot())
        return
    # No UDN → "what's playing anywhere". Return every queue's snapshot,
    # plus a flat legacy view of the first alive one so old UI tabs that
    # haven't been updated still render something sensible.
    all_snaps = _st.QUEUES.snapshot_all()
    legacy = {"state": "stopped", "alive": False, "renderer": "",
              "queue_len": 0, "queue_pos": 0}
    for snap in all_snaps.values():
        if snap.get("alive"):
            legacy = snap
            break
    h._json(200, {**legacy, "queues": all_snaps})


def index_status(h, params):
    udn   = params.get("udn", "")
    count = _st.DB.track_count(udn) if udn else 0
    h._json(200, {**_st.INDEXER.state.get(), "db_tracks": count})


# AcoustID enrichment is fully removed in 2.0 (Option A: beets is the sole
# metadata authority). The endpoints, the dlna_acoustid worker module, and its
# wiring are all gone; historical metadata_overrides rows (incl. source=
# 'acoustid') stay as data and are cleaned by tools/post_beets_reindex.py.


def index_rebuild(h, params):
    udn = params.get("udn", "")
    srv = _st.SERVERS.get(udn)
    if not srv:
        h._json(404, {"error": "Server not found"})
        return

    # LocalFs-style providers don't speak UPnP ContentDirectory — the
    # generic Indexer crawls via provider.cd_browse(), which a
    # LocalFsProvider doesn't have (it crashed the rebuild before this
    # fix). Dispatch them to their own mutagen rescan instead. Detect by
    # capability (has rescan, no cd_browse) so any future filesystem-style
    # provider works without a hard import.
    provider = _st.get_provider(udn)
    if provider is not None and hasattr(provider, "rescan") \
            and not hasattr(provider, "cd_browse"):
        def _localfs_rebuild():
            try:
                _st.INDEXER.state.update(status="running", progress=0, total=0,
                                     tracks=0, server=srv.name, error="")
                stats = provider.rescan(force=True)
                _st.INDEXER.state.update(status="idle",
                                     tracks=stats.get("scanned", 0), error="")
                log.info(f"LocalFs rebuild complete for {srv.name}: {stats}")
            except Exception as e:                   # noqa: BLE001
                log.exception(f"LocalFs rebuild failed: {e}")
                _st.INDEXER.state.update(status="error", error=str(e))
        threading.Thread(target=_localfs_rebuild, daemon=True,
                         name="localfs-rebuild").start()
        h._json(200, {"ok": True, "message": "LocalFs rescan started"})
        return

    _st.INDEXER.start(srv, force=True)
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
        # Audiobooks (P3): resume offset within the first track + the
        # book flag that turns on the monitor's position persistence.
        is_book = bool(data.get("book", False))
        try:
            start_at = max(0.0, float(data.get("start_at_sec") or 0))
        except (TypeError, ValueError):
            start_at = 0.0
        rnd    = _st.RENDERERS.get(udn)
        if not rnd:
            h._json(404, {"error": f"Renderer {udn!r} not found"})
            return
        if not tracks:
            h._json(400, {"error": "No tracks"})
            return

        # Busy check: one physical output can only play one thing. If the
        # renderer is already active for another session, refuse with 409
        # unless the client explicitly opted to take over via force=true.
        if not force and _st.QUEUES.is_busy(udn):
            busy = _st.QUEUES.get(udn).snapshot()
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
        queue = _st.QUEUES.get(udn)

        def _start_safe(q=queue, av_url=rnd.av_url, rc_url=rnd.rc_url,
                        tracks=tracks, name=rnd.name):
            try:
                q.start(av_url, tracks, name, rc_url=rc_url,
                        start_at_sec=start_at, is_book=is_book)
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
        rnd   = _st.RENDERERS.get(udn)
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
            rnd = _st.RENDERERS.get(udn)
            if not rnd:
                h._json(404, {"error": "Renderer not found"})
                return
            queue = _st.QUEUES.get(udn)
            if action == "pause":
                queue.pause()
            elif action == "stop":
                queue.stop()
            elif action == "next":
                queue.next_track()
            elif action == "prev":
                queue.prev_track()
            elif action == "trim_db":
                # User moved the gateway volume slider while OUT was UPnP.
                # The slider is a relative trim around the renderer's
                # natural volume (NOT an absolute SetVolume) — default 0
                # so a tap can't accidentally blast the room. Clamped
                # ±5 dB inside RendererQueue.set_user_trim_db.
                try:
                    trim = float(cmd.get("value", 0))
                except (TypeError, ValueError):
                    h._json(400, {"error": "value must be a float (dB)"})
                    return
                queue.set_user_trim_db(trim)
            else:
                log.debug(f"Renderer control: {action!r} not implemented")
            h._json(200, {"ok": True})
        else:
            h._json(400, {"error": f"Unknown device: {device!r}"})

    except Exception as e:
        log.warning(f"control error: {e}")
        h._json(400, {"error": str(e)})


# Dedicated logger for browser-side events so they're easy to grep
# ("grep dlna.client gateway.log") and don't drown in the API chatter.
_client_log = logging.getLogger("dlna.client")


def client_log(h, body):
    """Accept a structured error report from the PWA and write it to
    gateway.log. This is what gives us OBSERVABILITY of browser-side
    events that happen on a phone three rooms away — autoplay blocked,
    MediaError codes, retry outcomes.

    Body shape is free-form {kind: str, ...}. The handler clamps sizes
    defensively so a broken or malicious client can't flood the log."""
    try:
        data = _parse_json_or_400(h, body)
        if data is None:
            return
        kind = str(data.get("kind", "unknown"))[:40]
        # Pull out and clamp common fields that we care about; stringify
        # the rest into a single compact tail.
        fields = []
        for key in ("code", "codeName", "err", "reason", "retries",
                    "title", "artist", "ready_state", "network_state"):
            if key in data:
                v = str(data[key])[:120]
                fields.append(f"{key}={v}")
        ua = str(data.get("ua") or data.get("user_agent", ""))[:80]
        msg = str(data.get("message", ""))[:200]
        tail = "  ".join(fields)
        _client_log.info(
            f"client_log[{kind}]  {tail}"
            f"{'  msg=' + msg if msg else ''}"
            f"{'  ua=' + ua if ua else ''}")
        h._json(200, {"ok": True})
    except Exception as e:
        log.exception(f"client_log error: {e}")
        h._json(500, {"error": str(e)})
