#!/usr/bin/env python3
"""
dlna_player.py — `RendererQueue`: sequential playback on one UPnP renderer.

A queue owns ONE renderer's playback session: the track list, the position
in it, the renderer's transport state, and the daemon thread that watches it.

── Module family ────────────────────────────────────────────────────
`RendererQueue` was a 604-line class (in an 848-line module) until
2026-08-20. It is assembled from three mixins, one per concern:

    dlna_player_policy.py     pure decision functions + tuning constants
    dlna_player_volume.py     VolumeMixin    — RenderingControl SetVolume
    dlna_player_transport.py  TransportMixin — AVTransport SetURI/Play/Seek
    dlna_player_monitor.py    MonitorMixin   — the 2s state-poll loop
    dlna_player_registry.py   QueueRegistry + the QUEUES singleton

MIXINS, not collaborators, deliberately: a queue is one session and its
fields and lock are genuinely shared, so separate objects would mean
threading the same state through constructor arguments for no gain. The
class's public surface is unchanged, and dlna_player re-exports every name
(including `proxy_stream`), so callers and tests are unaffected.

What stays here is the queue's own lifecycle — start/stop/pause/next/prev
and the coalesced `snapshot()`.

Standalone test:
    python dlna_player.py
"""
import logging
import threading
import time

from dlna_events import EVENTS

# ── Re-exports: the family's public surface ──────────────────────────
from dlna_player_monitor import MonitorMixin
from dlna_player_policy import (  # noqa: F401
    GAIN_TO_VOLUME_RATIO,
    MAX_USER_TRIM_DB,
    STARTUP_VOLUME,
    UNKNOWN_ABORT_SEC,
    WATCHDOG_GRACE_SEC,
    _av_state_to_ui,
    _dur_to_sec,
    _gapless_advanced,
    _monitor_decision,
)
from dlna_player_transport import TransportMixin
from dlna_player_volume import VolumeMixin

log = logging.getLogger("dlna.player")



# ── RendererQueue — sequential playback for UPnP renderers ────────

class RendererQueue(VolumeMixin, TransportMixin, MonitorMixin):
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
        self._thread: threading.Thread | None = None
        self._started_at: float = 0.0
        self._consecutive_fails: int = 0
        # Renderer baseline — the renderer's natural volume when the
        # queue first started. Read once via GetVolume on first play,
        # so we adopt whatever the user has on the Naim's own remote
        # rather than overriding it. None means "not yet read".
        self._renderer_baseline: int | None = None
        # User trim — relative offset from baseline applied to every
        # track in this queue. The PWA volume slider sets this; default
        # 0 means "play at renderer's natural volume" (no surprise jolt
        # on slider tap). Clamped ±MAX_USER_TRIM_DB.
        self._user_trim_db: float = 0.0
        # Audiobook queue (P3): the monitor persists playback positions
        # every ~15 s so a Naim session resumes anywhere else.
        self._is_book: bool = False
        self._last_pos_save: float = 0.0
        # Pre-populate with a "stopped" default so concurrent callers
        # during the first fetch have something to return rather than
        # block waiting for the SOAP.
        self._snap_cache: dict = {"state": "stopped", "alive": False,
                                   "renderer": "", "queue_len": 0,
                                   "queue_pos": 0}
        self._snap_cache_at: float = 0.0
        # >0 while a renderer is known unreachable; see snapshot().
        self._unreachable_until: float = 0.0
        # Try-acquire lock: the first caller who finds cache stale
        # becomes the fetcher; everyone else gets the stale cache
        # immediately (no blocking). Without this, N concurrent callers
        # all wait behind the fetch and each sees multi-second latency.
        self._snap_fetch_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────

    def start(self, av_url: str, tracks: list, renderer_name: str = "",
              rc_url: str = "", start_at_sec: float = 0.0,
              is_book: bool = False):
        """
        Start playing a list of tracks on the renderer.
        Cancels any currently active queue, and explicitly stops the renderer
        before sending the new URI+Play so the renderer isn't mid-transition
        when the new Play lands (which returns HTTP 500 on Naim/Rygel).

        rc_url is the RenderingControl SOAP endpoint (used for loudness
        normalization SetVolume calls). Optional — when empty, no
        per-track volume adjustment happens.

        Audiobooks (P3): `start_at_sec` seeks within the FIRST track once
        it plays (resume mid-chapter — REL_TIME Seek, fired async with a
        retry because a Seek during TRANSITIONING faults on the Naim).
        `is_book` makes the monitor persist the playback position every
        ~15 s so a session stopped on the Naim resumes anywhere else.
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
            self._is_book            = bool(is_book)
            self._stop_event.clear()

        self._invalidate_snap()
        if not tracks:
            return

        log.info(f"RendererQueue: new queue with {len(tracks)} track(s) "
                 f"→ {renderer_name}")
        if self._send_current() and start_at_sec > 1:
            self._seek_async(start_at_sec)

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
        EVENTS.publish({"type": "state"})       # SSE: playback stopped (R2)

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

    # How long to stop SOAPing a renderer that just proved unreachable.
    # Reaching a switched-off device costs a full TCP connect timeout (~6 s
    # measured against the LG TV), and the 500 ms cache means the NEXT poll
    # pays it again — so with one dead renderer the PWA's state poll cost 6 s
    # over and over. Queues are never evicted, so this is the normal state of
    # affairs for any device that has been used once and then switched off.
    # 30 s bounds the staleness on recovery, and the monitor thread probes
    # independently while a queue is actually playing, so a renderer coming
    # back mid-session is still noticed at once.
    _UNREACHABLE_BACKOFF_SEC = 30.0

    def snapshot(self) -> dict:
        """Return current queue state for the UI state poll. Cached for
        _SNAP_TTL_SEC; concurrent callers that miss the TTL return the
        stale cache rather than block — only the first caller to find
        stale cache fires the SOAP round-trip.

        A renderer that answered UNREACHABLE is not re-dialled for
        _UNREACHABLE_BACKOFF_SEC; its cached (stopped) snapshot is served
        instead."""
        from dlna_avtransport import avtransport_get_state, avtransport_get_position

        now = time.monotonic()
        with self._lock:
            cache     = dict(self._snap_cache)
            cached_at = self._snap_cache_at
            quiet_til = self._unreachable_until

        if now - cached_at < self._SNAP_TTL_SEC:
            return cache

        # Known unreachable and still inside the backoff: answer from cache
        # rather than spend another connect timeout finding out again.
        if cache and now < quiet_til:
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
                # UNREACHABLE means the SOAP call itself failed — the device
                # is off or gone, not merely quiet. Arm the backoff so the
                # next poll is answered from cache instead of spending
                # another connect timeout learning the same thing.
                with self._lock:
                    self._unreachable_until = (
                        time.monotonic() + self._UNREACHABLE_BACKOFF_SEC
                        if state == "UNREACHABLE" else 0.0)
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


# ── Registry ──────────────────────────────────────────────────────
# Imported AFTER RendererQueue is defined: dlna_player_registry constructs
# queues, so it imports this module — a module-level import at the top would
# be circular. Re-exported so `from dlna_player import QUEUES` still works.
from dlna_player_registry import QUEUES, QueueRegistry  # noqa: E402,F401

# The browser-audio stream proxy lives in dlna_stream_proxy — imported
# here so existing callers can keep `from dlna_player import proxy_stream`.
from dlna_stream_proxy import PROXY_IDLE_SEC, proxy_stream  # noqa: E402,F401


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
