#!/usr/bin/env python3
"""
dlna_player.py — UPnP renderer queue and browser stream proxy.

Standalone test:
    python dlna_player.py
"""
import logging
import threading
import time
from typing import Optional

log = logging.getLogger("dlna.player")


def _dur_to_sec(dur) -> int:
    """Coerce a track duration to an int second count. Accepts int, float,
    empty/None, or UPnP-style 'H:MM:SS(.fff)' / 'MM:SS' strings (how the
    library DB actually stores them). Returns 0 when unparseable so callers
    never raise on a malformed duration."""
    if not dur:
        return 0
    if isinstance(dur, (int, float)):
        return int(dur)
    s = str(dur).strip()
    if not s:
        return 0
    if ":" in s:
        try:
            total = 0.0
            for part in s.split(":"):
                total = total * 60 + float(part)
            return int(total)
        except (ValueError, TypeError):
            return 0
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


# Per-track loudness gain → renderer-volume-unit conversion.
# A value of 2 means "one Naim volume unit ≈ 0.5 dB". Approximation —
# the renderer's volume curve is logarithmic and renderer-specific.
# Tune by ear after the first listen on the actual hardware.
GAIN_TO_VOLUME_RATIO: int = 2

# User-trim slider clamp. The slider is a relative offset around the
# renderer's natural volume; ±5 dB caps "ear damage from accidental
# slider flick" while still giving meaningful range. Edit the constant
# to widen/narrow the slider.
MAX_USER_TRIM_DB: float = 5.0

# Monitor stall guards. The monitor advances a track on the normal
# PLAYING → STOPPED transition. But when the renderer goes unreachable
# mid-track, GetTransportInfo SOAP starts failing and the state reads
# UNKNOWN — the PLAYING → STOPPED transition is never observed and the
# queue would otherwise stall on one track forever (the real-world
# 2026-05-20 incident: 'Starman' stuck for 36 minutes).
#
# WATCHDOG_GRACE_SEC — once wall-clock playback runs this far past the
#   track's own declared duration while the renderer is NOT actively
#   PLAYING/TRANSITIONING/PAUSED, advance regardless of observed state.
# UNKNOWN_ABORT_SEC — if the renderer reads UNKNOWN continuously for
#   this long (and the track has no duration for the watchdog to use),
#   abort the queue rather than poll a dead renderer indefinitely.
WATCHDOG_GRACE_SEC: float = 90.0
UNKNOWN_ABORT_SEC:  float = 300.0


def _monitor_decision(prev_state: str, cur_state: str,
                      elapsed: float, dur: float,
                      is_stream: bool = False):
    """Pure decision helper for ``RendererQueue._monitor``.

    Given the previous and current transport state, how long the
    current track has been playing (wall-clock seconds since its Play
    was sent), and its declared duration, decide whether to advance.

    Returns ``(advance: bool, reason: str)`` — reason is ``'finished'``
    for the normal end-of-track transition, ``'watchdog'`` when the
    duration-based stall guard fires, and ``''`` when not advancing.

    ``is_stream`` (internet radio) suppresses both: a live stream has
    no end and no duration, and a momentary ``STOPPED`` is a rebuffer,
    not a track ending. Radio is ended only by an explicit user stop.
    """
    if is_stream:
        return False, ""
    # Normal end-of-track: the renderer reported PLAYING and is now
    # STOPPED (or has no media) → the track played out.
    if (prev_state in ("PLAYING", "TRANSITIONING") and
            cur_state in ("STOPPED", "NO_MEDIA_PRESENT")):
        return True, "finished"
    # Watchdog: the renderer is not observably progressing (typically
    # UNKNOWN from a failed SOAP poll, or wedged in STOPPED) yet
    # wall-clock playback has run past the track's own duration plus a
    # grace margin. PAUSED_PLAYBACK is excluded so a deliberately
    # paused queue is never skipped.
    if (cur_state not in ("PLAYING", "TRANSITIONING", "PAUSED_PLAYBACK")
            and elapsed > 0 and dur > 0
            and elapsed > dur + WATCHDOG_GRACE_SEC):
        return True, "watchdog"
    return False, ""


# ── RendererQueue — sequential playback for UPnP renderers ────────

class RendererQueue:
    """
    Manages sequential track playback on a UPnP AVTransport renderer.

    When a track finishes (state goes STOPPED after PLAYING) the next
    track in the queue is automatically sent via SetAVTransportURI+Play.

    Only one queue is active per renderer; starting a new queue on the same
    renderer cancels the old one. Multiple RendererQueue instances coexist
    (one per renderer UDN) — see QueueRegistry below.
    """

    _MAX_CONSECUTIVE_FAILS = 5

    def __init__(self):
        self._lock    = threading.Lock()
        self._tracks: list  = []
        self._index:  int   = 0
        self._av_url: str   = ""
        self._rc_url: str   = ""   # RenderingControl SOAP endpoint (volume)
        self._rnd_name: str = ""
        self._stop_event    = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started_at: float = 0.0
        self._consecutive_fails: int = 0
        # Renderer baseline — the renderer's natural volume when the
        # queue first started. Read once via GetVolume on first play,
        # so we adopt whatever the user has on the Naim's own remote
        # rather than overriding it. None means "not yet read".
        self._renderer_baseline: Optional[int] = None
        # User trim — relative offset from baseline applied to every
        # track in this queue. The PWA volume slider sets this; default
        # 0 means "play at renderer's natural volume" (no surprise jolt
        # on slider tap). Clamped ±MAX_USER_TRIM_DB.
        self._user_trim_db: float = 0.0
        # Pre-populate with a "stopped" default so concurrent callers
        # during the first fetch have something to return rather than
        # block waiting for the SOAP.
        self._snap_cache: dict = {"state": "stopped", "alive": False,
                                   "renderer": "", "queue_len": 0,
                                   "queue_pos": 0}
        self._snap_cache_at: float = 0.0
        # Try-acquire lock: the first caller who finds cache stale
        # becomes the fetcher; everyone else gets the stale cache
        # immediately (no blocking). Without this, N concurrent callers
        # all wait behind the fetch and each sees multi-second latency.
        self._snap_fetch_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────

    def start(self, av_url: str, tracks: list, renderer_name: str = "",
              rc_url: str = ""):
        """
        Start playing a list of tracks on the renderer.
        Cancels any currently active queue, and explicitly stops the renderer
        before sending the new URI+Play so the renderer isn't mid-transition
        when the new Play lands (which returns HTTP 500 on Naim/Rygel).

        rc_url is the RenderingControl SOAP endpoint (used for loudness
        normalization SetVolume calls). Optional — when empty, no
        per-track volume adjustment happens.
        """
        from dlna_avtransport import avtransport_stop
        self._log_track_end("queue_replaced")
        self._cancel()

        with self._lock:
            prev_url = self._av_url

        if prev_url:
            try:
                avtransport_stop(prev_url)
                time.sleep(0.5)
            except Exception as e:
                log.warning(f"RendererQueue: prior Stop failed: {e}")

        with self._lock:
            self._av_url             = av_url
            self._rc_url             = rc_url
            self._tracks             = list(tracks)
            self._index              = 0
            self._rnd_name           = renderer_name
            self._consecutive_fails  = 0
            self._started_at         = 0.0
            # Each new queue re-reads the renderer's natural volume and
            # resets the user trim — yesterday's slider position doesn't
            # carry into today's session.
            self._renderer_baseline  = None
            self._user_trim_db       = 0.0
            self._stop_event.clear()

        self._invalidate_snap()
        if not tracks:
            return

        log.info(f"RendererQueue: new queue with {len(tracks)} track(s) "
                 f"→ {renderer_name}")
        self._send_current()

        self._thread = threading.Thread(
            target=self._monitor, daemon=True, name="renderer-queue")
        self._thread.start()

    def stop(self):
        """Stop playback and cancel the queue."""
        from dlna_avtransport import avtransport_stop
        log.info("RendererQueue: user STOP")
        self._log_track_end("user_stop")
        self._cancel()
        with self._lock:
            url = self._av_url
        if url:
            avtransport_stop(url)
        self._invalidate_snap()

    def _set_volume_async(self, rc_url: str, level: int):
        """Fire SetVolume in a daemon thread so the caller never blocks
        on the renderer's SOAP. Naim's HTTP server is single-threaded
        and can take >1s to respond when GetTransportInfo polls are in
        flight; if we waited synchronously, the per-track call would
        delay SetURI/Play and the user would perceive it as a freeze."""
        from dlna_avtransport import set_volume
        threading.Thread(
            target=lambda: set_volume(rc_url, level),
            daemon=True, name="set-volume").start()

    def set_user_trim_db(self, trim_db: float):
        """User moved the gateway volume slider (relative trim, -5..+5 dB).
        Update the cached trim AND fire SetVolume immediately so the
        change is audible mid-track, not deferred until the next song.

        SetVolume runs in a daemon thread so the HTTP handler returns
        instantly even when Naim's SOAP is slow."""
        trim_db = max(-MAX_USER_TRIM_DB, min(MAX_USER_TRIM_DB, float(trim_db)))
        with self._lock:
            self._user_trim_db = trim_db
            rc_url   = self._rc_url
            baseline = self._renderer_baseline
            tracks   = list(self._tracks)
            idx      = self._index
        if not rc_url:
            return
        # If we know the baseline, apply NOW so the user hears the change.
        # Otherwise the trim is stored and applied on first play.
        if baseline is None:
            return
        # Add the currently-playing track's loudness gain so the trim
        # composes correctly mid-song.
        cur_gain_db = 0.0
        if 0 <= idx < len(tracks):
            try:
                from dlna_library import DB
                cur_gain_db = DB.gain_db_for_url(tracks[idx].get("url", ""))
            except Exception:
                cur_gain_db = 0.0
        offset = round((cur_gain_db + trim_db) * GAIN_TO_VOLUME_RATIO)
        level  = max(0, min(100, baseline + offset))
        self._set_volume_async(rc_url, level)

    def pause(self):
        """Toggle pause on the renderer."""
        from dlna_avtransport import avtransport_pause
        with self._lock:
            url = self._av_url
        log.info("RendererQueue: user PAUSE toggle")
        if url:
            avtransport_pause(url)
        self._invalidate_snap()

    def next_track(self):
        """Skip to the next track immediately."""
        with self._lock:
            if self._index >= len(self._tracks) - 1:
                log.info("RendererQueue: user NEXT (at end — no-op)")
                return
        log.info("RendererQueue: user NEXT")
        self._log_track_end("user_next")
        with self._lock:
            self._index += 1
        self._invalidate_snap()
        self._send_current()

    def prev_track(self):
        """Go back to the previous track."""
        with self._lock:
            if self._index <= 0:
                log.info("RendererQueue: user PREV (at start — no-op)")
                return
        log.info("RendererQueue: user PREV")
        self._log_track_end("user_prev")
        with self._lock:
            self._index -= 1
        self._invalidate_snap()
        self._send_current()

    # Short TTL on snapshot(): every poll fires two SOAP calls to the
    # renderer, and the dlna_content semaphore caps concurrent SOAP at 3.
    # Under heavy polling (multiple browser tabs, chaos-style hammering)
    # that queue builds up seconds of latency. 500ms coalescing loses
    # nothing UI-relevant and caps real-world load.
    _SNAP_TTL_SEC = 0.5

    def snapshot(self) -> dict:
        """Return current queue state for the UI state poll. Cached for
        _SNAP_TTL_SEC; concurrent callers that miss the TTL return the
        stale cache rather than block — only the first caller to find
        stale cache fires the SOAP round-trip."""
        from dlna_avtransport import avtransport_get_state, avtransport_get_position

        now = time.monotonic()
        with self._lock:
            cache     = dict(self._snap_cache)
            cached_at = self._snap_cache_at

        if now - cached_at < self._SNAP_TTL_SEC:
            return cache

        # Stale. Try to become the fetcher. If we can't, we're in the
        # middle of another caller's fetch — just return the stale data
        # rather than queue up a duplicate SOAP or block the caller.
        if not self._snap_fetch_lock.acquire(blocking=False):
            return cache

        try:
            with self._lock:
                av_url = self._av_url
                tracks = list(self._tracks)
                idx    = self._index
                rname  = self._rnd_name

            if not av_url or not tracks:
                snap = {"state": "stopped", "alive": False,
                        "renderer": rname, "queue_len": 0, "queue_pos": 0}
            else:
                # Fire the two SOAP calls in parallel — halves snapshot
                # latency. Each bounded by _av_soap's 6s timeout, so an
                # unresponsive renderer caps us at ~6s not 12s.
                result = {"state": None, "pos": None}
                def _fetch_state():
                    result["state"] = avtransport_get_state(av_url)
                def _fetch_pos():
                    result["pos"] = avtransport_get_position(av_url)
                ts = threading.Thread(target=_fetch_state, daemon=True)
                tp = threading.Thread(target=_fetch_pos,   daemon=True)
                ts.start(); tp.start()
                ts.join(timeout=7); tp.join(timeout=7)
                state = result["state"] or "UNKNOWN"
                pos   = result["pos"]   or {"position": None, "duration": None,
                                             "title": None}
                cur = tracks[idx] if 0 <= idx < len(tracks) else {}
                snap = {
                    "state":       _av_state_to_ui(state),
                    "alive":       state in ("PLAYING", "PAUSED_PLAYBACK", "TRANSITIONING"),
                    "paused":      state == "PAUSED_PLAYBACK",
                    "renderer":    rname,
                    "title":       pos.get("title") or cur.get("title", ""),
                    "artist":      cur.get("artist", ""),
                    "album":       cur.get("album", ""),
                    "art":         cur.get("art", ""),
                    "position":    pos.get("position"),
                    "duration":    pos.get("duration"),
                    "queue_len":   len(tracks),
                    "queue_pos":   idx + 1,
                    "uri":         cur.get("url", ""),
                    "media_title": pos.get("title") or cur.get("title", ""),
                }

            with self._lock:
                self._snap_cache    = snap
                self._snap_cache_at = time.monotonic()
            return dict(snap)
        finally:
            self._snap_fetch_lock.release()

    # ── Internal ──────────────────────────────────────────────────

    def _invalidate_snap(self):
        """Force the next poll to re-fetch from the renderer. Called after
        any mutation (start/stop/pause/next/prev) so UI doesn't see up to
        500ms of stale state. We zero the timestamp but LEAVE the cache
        dict in place — concurrent callers that lose the fetch-lock race
        need a non-None fallback value to return."""
        with self._lock:
            self._snap_cache_at = 0.0

    def _cancel(self):
        self._stop_event.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3)

    def _log_track_end(self, reason: str):
        """Emit a single INFO line when the current track stops playing,
        regardless of the reason (natural end, user-pressed button,
        SOAP fault, queue replacement). Elapsed seconds come from the
        monotonic clock stamped when the track's Play was sent."""
        with self._lock:
            start  = self._started_at
            idx    = self._index
            tracks = list(self._tracks)
        if not tracks or start <= 0 or not (0 <= idx < len(tracks)):
            return
        elapsed = time.monotonic() - start
        t       = tracks[idx]
        dur     = _dur_to_sec(t.get("duration"))
        dur_s   = f"/{dur}s" if dur else ""
        log.info(f"RendererQueue ■ END   [{idx+1}/{len(tracks)}] "
                 f"{t.get('title','?')!r} played {elapsed:.1f}s{dur_s} "
                 f"reason={reason}")
        with self._lock:
            self._started_at = 0.0

    def _apply_loudness_gain(self, t: dict):
        """Compute and send per-track SetVolume just before SetURI/Play.
        No-op if rc_url is empty (renderer has no RenderingControl URL).

        On the very first track, GetVolume is called once to adopt the
        renderer's current volume (whatever the user has on the Naim
        remote) as the BASELINE. Subsequent tracks reuse the cached
        baseline; the per-track effective volume is:

            level = clamp(0, 100, baseline +
                          round((loudness_gain_db + user_trim_db) * RATIO))

        The user trim defaults to 0 dB so a fresh queue plays at the
        renderer's natural volume — never at the slider's rail."""
        from dlna_avtransport import set_volume, get_volume
        with self._lock:
            rc_url   = self._rc_url
            baseline = self._renderer_baseline
            trim_db  = self._user_trim_db
        if not rc_url:
            return
        # First-play baseline adoption
        if baseline is None:
            cur = get_volume(rc_url)
            if cur is None:
                log.debug(f"RendererQueue: GetVolume failed on {rc_url} — "
                          f"skipping loudness gain")
                return
            baseline = cur
            with self._lock:
                self._renderer_baseline = cur
        # Per-track gain
        from dlna_library import DB
        try:
            gain_db = DB.gain_db_for_url(t.get("url", ""))
        except Exception:
            gain_db = 0.0
        offset = round((gain_db + trim_db) * GAIN_TO_VOLUME_RATIO)
        level  = max(0, min(100, baseline + offset))
        # Fire-and-forget so a slow Naim SOAP (busy serving the snapshot
        # poller) doesn't delay SetURI/Play. Worst case: a few hundred
        # ms of audio at the wrong level — inaudible compared to the
        # alternative of the track stalling.
        self._set_volume_async(rc_url, level)
        if abs(offset) >= 1:
            log.debug(f"RendererQueue: loudness {gain_db:+.1f} dB + "
                      f"trim {trim_db:+.1f} dB → SetVolume({level}) "
                      f"(baseline={baseline}, offset={offset:+d})")

    def _send_current(self) -> bool:
        """Send SetURI + Play for tracks[_index]. Returns True on success.
        On failure, logs the skip, auto-advances, and aborts the queue
        after _MAX_CONSECUTIVE_FAILS failures so we don't silently chew
        through every track when a renderer is wedged."""
        from dlna_avtransport import avtransport_send
        with self._lock:
            if not self._tracks or not self._av_url:
                return False
            idx    = self._index
            tracks = list(self._tracks)
            av_url = self._av_url
            rname  = self._rnd_name
        if not (0 <= idx < len(tracks)):
            return False

        t = tracks[idx]
        dur = _dur_to_sec(t.get("duration"))
        dur_s = f" ({dur}s)" if dur else ""
        log.info(f"RendererQueue ▶ START [{idx+1}/{len(tracks)}] "
                 f"{t.get('title','?')!r} — "
                 f"{t.get('artist','?')} / {t.get('album','?')}"
                 f"{dur_s} → {rname}")

        with self._lock:
            self._started_at = time.monotonic()

        # Apply per-track loudness normalization BEFORE the Play —
        # changing volume after the audio is already streaming would be
        # audible as a step.
        self._apply_loudness_gain(t)

        ok = avtransport_send(av_url, t.get("url",""),
                              t.get("title",""), t.get("mime",""))
        if ok:
            with self._lock:
                self._consecutive_fails = 0
            return True

        log.warning(f"RendererQueue ✗ SEND FAILED [{idx+1}/{len(tracks)}] "
                    f"{t.get('title','?')!r} — SetURI/Play returned False "
                    f"(url={t.get('url','')})")
        self._log_track_end("send_failed")

        with self._lock:
            self._consecutive_fails += 1
            fails = self._consecutive_fails
            more  = self._index < len(self._tracks) - 1

        if fails >= self._MAX_CONSECUTIVE_FAILS:
            log.warning(f"RendererQueue ⚠ ABORT {fails} consecutive send "
                        f"failures — stopping queue (renderer likely wedged; "
                        f"kickstart the gateway if this persists)")
            self._stop_event.set()
            return False

        if more:
            with self._lock:
                self._index += 1
            return self._send_current()

        log.info("RendererQueue ■ QUEUE END — all remaining tracks failed")
        self._stop_event.set()
        return False

    def _monitor(self):
        """
        Poll GetTransportInfo every 2 s and advance the queue.

        A track ends one of three ways:
          * normal   — PLAYING/TRANSITIONING → STOPPED/NO_MEDIA_PRESENT.
          * watchdog — wall-clock playback runs past the track's own
            duration + WATCHDOG_GRACE_SEC while the renderer is not
            actively PLAYING (typically UNKNOWN because GetTransportInfo
            SOAP is failing). Without this a renderer that goes
            unreachable mid-track strands the queue forever.
          * abort    — the renderer reads UNKNOWN continuously for
            UNKNOWN_ABORT_SEC with no duration for the watchdog to use;
            the queue stops rather than poll a dead renderer.
        """
        from dlna_avtransport import avtransport_probe_state
        POLL_SEC      = 2.0
        prev_state    = "UNKNOWN"
        unknown_since = 0.0
        self._stop_event.wait(4.0)

        while not self._stop_event.is_set():
            with self._lock:
                av_url = self._av_url
                idx    = self._index
                total  = len(self._tracks)
                start  = self._started_at
                cur_t  = (self._tracks[idx]
                          if 0 <= idx < len(self._tracks) else None)

            if not av_url:
                break

            cur_state, detail = avtransport_probe_state(av_url)
            if cur_state != prev_state:
                # UNREACHABLE means the SOAP call itself failed — name
                # the transport error so the cause (renderer powered
                # off, network drop, HTTP fault) is in the log, not
                # guessed. avtransport_probe_state rate-limits the
                # underlying WARN; this transition line fires once.
                if cur_state == "UNREACHABLE":
                    log.warning(f"RendererQueue: state {prev_state} → "
                                f"UNREACHABLE [{idx+1}/{total}] — "
                                f"{detail or 'transport failed'}")
                else:
                    log.info(f"RendererQueue: state {prev_state} → "
                             f"{cur_state} [{idx+1}/{total}]")
            else:
                log.debug(f"RendererQueue monitor: state={cur_state} "
                          f"[{idx+1}/{total}]")

            # Track how long the renderer has been out of contact —
            # either genuinely UNKNOWN or UNREACHABLE (SOAP failing).
            if cur_state in ("UNKNOWN", "UNREACHABLE"):
                if unknown_since == 0.0:
                    unknown_since = time.monotonic()
            else:
                unknown_since = 0.0

            elapsed   = (time.monotonic() - start) if start > 0 else 0.0
            dur       = _dur_to_sec(cur_t.get("duration")) if cur_t else 0
            is_stream = bool(cur_t.get("is_stream")) if cur_t else False
            advance, reason = _monitor_decision(prev_state, cur_state,
                                                elapsed, dur, is_stream)
            prev_state = cur_state

            if advance:
                if reason == "watchdog":
                    log.warning(f"RendererQueue ⚠ WATCHDOG [{idx+1}/{total}] "
                                f"{cur_t.get('title','?')!r} — renderer "
                                f"state {cur_state!r}, {elapsed:.0f}s since "
                                f"start vs {dur}s duration; advancing")
                self._log_track_end(reason)
                with self._lock:
                    more = self._index < len(self._tracks) - 1
                    if more:
                        self._index += 1

                if more:
                    log.info(f"RendererQueue: advancing to next track "
                             f"[{idx+2}/{total}]")
                    self._send_current()
                    self._stop_event.wait(4.0)
                else:
                    log.info(f"RendererQueue ■ QUEUE END — "
                             f"playlist finished ({total} track(s))")
                    self._stop_event.set()
                    break
                continue

            # Renderer unreachable too long with nothing the watchdog
            # could act on — stop instead of polling a dead device.
            if (unknown_since and
                    time.monotonic() - unknown_since > UNKNOWN_ABORT_SEC):
                log.warning(f"RendererQueue ⚠ ABORT renderer out of contact "
                            f"(state {cur_state}) for >{UNKNOWN_ABORT_SEC:.0f}s "
                            f"[{idx+1}/{total}] — stopping queue")
                self._log_track_end("renderer_lost")
                self._stop_event.set()
                break

            self._stop_event.wait(POLL_SEC)


def _av_state_to_ui(state: str) -> str:
    return {
        "PLAYING":          "playing",
        "PAUSED_PLAYBACK":  "paused",
        "STOPPED":          "stopped",
        "NO_MEDIA_PRESENT": "stopped",
        "TRANSITIONING":    "playing",
    }.get(state, "stopped")


# ── QueueRegistry — per-renderer queue owner ──────────────────────

class QueueRegistry:
    """Owns one RendererQueue per renderer UDN.

    Concurrent multi-renderer playback: each physical output gets its own
    queue. Queues are lazily created on first access and persist for the
    lifetime of the process — there's no churn (at most a handful of
    renderers ever exist on a LAN).
    """

    def __init__(self):
        self._queues: dict = {}
        self._lock = threading.Lock()

    def get(self, udn: str) -> RendererQueue:
        """Return the queue for this UDN, creating it on first use."""
        with self._lock:
            q = self._queues.get(udn)
            if q is None:
                q = RendererQueue()
                self._queues[udn] = q
            return q

    def peek(self, udn: str) -> Optional[RendererQueue]:
        """Return the queue for this UDN if one exists, else None (does
        NOT create). Use this when probing state to avoid allocating a
        queue for an unknown UDN."""
        with self._lock:
            return self._queues.get(udn)

    def is_busy(self, udn: str) -> bool:
        """True iff this UDN has an active queue (renderer not stopped).
        Step C will use this to return 409 Conflict on second-session
        queue posts."""
        q = self.peek(udn)
        if q is None:
            return False
        return bool(q.snapshot().get("alive"))

    def snapshot_all(self) -> dict:
        """Return {udn: snapshot} for every queue that has ever been
        created. Useful for a global 'what's playing anywhere' view."""
        with self._lock:
            items = list(self._queues.items())
        return {udn: q.snapshot() for udn, q in items}


QUEUES = QueueRegistry()


# The browser-audio stream proxy lives in dlna_stream_proxy — imported
# here so existing callers can keep `from dlna_player import proxy_stream`.
from dlna_stream_proxy import proxy_stream, PROXY_IDLE_SEC  # noqa: F401


# ── Standalone test ───────────────────────────────────────────────

def _test():
    from dlna_config import setup_logging
    setup_logging(debug=True)
    log.info("=== dlna_player self-test ===")

    # Duration parser covers every format the library DB actually stores
    assert _dur_to_sec("0:04:51.000") == 291
    assert _dur_to_sec("3:45")        == 225
    assert _dur_to_sec("")            == 0
    assert _dur_to_sec(None)          == 0
    assert _dur_to_sec(42)            == 42
    assert _dur_to_sec("abc")         == 0
    log.info("PASS — _dur_to_sec handles all formats")

    # QueueRegistry lazily creates per-UDN queues
    reg = QueueRegistry()
    assert reg.peek("uuid:test") is None
    assert reg.is_busy("uuid:test") is False
    q1 = reg.get("uuid:a")
    q2 = reg.get("uuid:b")
    assert q1 is not q2, "each UDN must get its own queue"
    assert reg.get("uuid:a") is q1, "get() must be idempotent"
    assert reg.snapshot_all().keys() == {"uuid:a", "uuid:b"}
    log.info("PASS — QueueRegistry lazy per-UDN allocation")

    # Module-level registry exists
    snap = QUEUES.snapshot_all()
    log.info(f"QUEUES.snapshot_all(): {snap}")
    log.info("PASS — dlna_player OK")


if __name__ == "__main__":
    _test()
