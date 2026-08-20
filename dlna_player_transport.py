#!/usr/bin/env python3
"""
dlna_player_transport.py — `TransportMixin`: pushing URIs at the
renderer over AVTransport (SetURI / Play / Seek / SetNextAVTransportURI).

Split out of dlna_player.py on 2026-08-20, when `RendererQueue` had grown
to a 604-line class with an 148-line `_monitor`. The family:

    dlna_player_policy.py     pure decision functions + the tuning constants
    dlna_player_volume.py     VolumeMixin    — RenderingControl SetVolume
    dlna_player_transport.py  TransportMixin — AVTransport SetURI/Play/Seek
    dlna_player_monitor.py    MonitorMixin   — the 2s state-poll loop
    dlna_player_registry.py   QueueRegistry + the QUEUES singleton
    dlna_player.py            RendererQueue core + re-exports

MIXINS, not collaborators: `RendererQueue` inherits all three, so every
`self.<field>` and cross-mixin call resolves through the MRO and the class's
public surface is unchanged. A queue is ONE renderer's playback session —
the state is genuinely shared, so splitting it into separate objects would
mean threading the same lock and fields through constructor arguments for
no gain.

`_send_current` CHECKS the SOAP return value. It used to ignore it: when
SetURI failed the renderer stayed STOPPED, the monitor saw STOPPED, advanced,
sent the next track, which also failed — silently chewing through an entire
queue. The symptom was "all 35 songs skipped, nothing in the log, only a
restart fixes it". Now every failure logs `✗ SEND FAILED` with the URL, and
`_MAX_CONSECUTIVE_FAILS` aborts the queue rather than burning through it.

`_queue_next_uri` is what makes playback gapless — the renderer needs the
next URI BEFORE the current track ends.

`_seek_async` fires ~1.5s after Play with one retry: a Seek issued while the
Naim is still TRANSITIONING faults. Failure is non-fatal — the chapter just
starts at 0:00 instead of the saved position.
"""
import logging
import threading
import time

from dlna_events import EVENTS
from dlna_player_policy import _dur_to_sec

log = logging.getLogger("dlna.player")


class TransportMixin:
    """See module docstring. Mixed into `RendererQueue`; never
    instantiated alone — it relies on the host's fields and lock."""

    def _seek_async(self, seconds: float):
        """Fire the resume Seek off-thread: the renderer needs a moment
        to reach PLAYING (a Seek during TRANSITIONING faults on the
        Naim), so wait, seek, and retry once. Failure is non-fatal —
        the chapter simply plays from 0:00."""
        def _run():
            from dlna_avtransport import avtransport_seek
            self._stop_event.wait(1.5)
            if self._stop_event.is_set():
                return
            with self._lock:
                av_url = self._av_url
            if not av_url:
                return
            if not avtransport_seek(av_url, seconds):
                self._stop_event.wait(2.0)
                if not self._stop_event.is_set():
                    avtransport_seek(av_url, seconds)
        threading.Thread(target=_run, daemon=True,
                         name="renderer-seek").start()

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

        # Set the startup volume BEFORE the Play (once per queue; a no-op
        # on tracks 2+). Doing it before avoids an audible step.
        self._apply_startup_volume()

        ok = avtransport_send(av_url, t.get("url",""),
                              t.get("title",""), t.get("mime",""))
        if ok:
            with self._lock:
                self._consecutive_fails = 0
            self._queue_next_uri()
            EVENTS.publish({"type": "state"})   # SSE: now-playing changed (R2)
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

    def _queue_next_uri(self):
        """Pre-queue the track after the current _index via
        SetNextAVTransportURI so the renderer transitions to it gaplessly
        (no re-buffer / click). On the last track, send an empty NextURI
        to clear any previously-queued URI. Failure is non-fatal — the
        STOPPED→advance path still works (with a small gap), as it did
        before P4. Called after a Play, and again after a detected
        gapless auto-advance (to queue the NEW next track)."""
        from dlna_avtransport import avtransport_set_next_uri
        with self._lock:
            av_url = self._av_url
            next_t = (dict(self._tracks[self._index + 1])
                      if 0 <= self._index + 1 < len(self._tracks) else {})
        if not av_url:
            return
        avtransport_set_next_uri(
            av_url, next_t.get("url", ""),
            next_t.get("title", ""), next_t.get("mime", ""))
