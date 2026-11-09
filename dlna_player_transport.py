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

`_seek_async` waits until the renderer reports PLAYING, seeks, then reads the
position back and retries once if it did not land (2026-11-09: a Seek sent
1.5 s after Play was silently dropped by the Naim, which takes ~4 s to
start). Failure is non-fatal — the chapter plays from 0:00 — but it is
logged, and the monitor saves no position until the Seek has settled.
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

    # Resume timing. The Naim takes ~4 s from Play to PLAYING (every
    # START→PLAYING pair in gateway.log, 2026-11-09), and a Seek sent
    # before that is ACCEPTED and then dropped — no fault, the chapter
    # just plays from 0:00. So: wait for PLAYING, seek, then ASK where it
    # is, and retry once if it did not land.
    _RESUME_PLAYING_TIMEOUT = 20.0
    _RESUME_POLL_SEC = 0.5
    _RESUME_SETTLE_SEC = 1.5
    _RESUME_CONFIRM_SEC = 6.0
    _RESUME_TOLERANCE_SEC = 10.0

    def _seek_async(self, seconds: float):
        """Fire the resume Seek off-thread. While it runs, `_resume_token`
        is set, and the monitor saves NO position: its first save lands
        ~3 s into the chapter, and before this it overwrote the bookmark
        with that whenever the Seek had not (yet) taken — so one failed
        resume destroyed the bookmark for every later attempt."""
        token = object()
        self._resume_token = token
        threading.Thread(target=self._resume_seek, args=(seconds, token),
                         daemon=True, name="renderer-seek").start()

    def _resume_current(self, token) -> bool:
        # start() clears _stop_event right after cancelling, so an old
        # seek thread can miss the set; the token is what tells it a
        # newer queue owns the renderer now.
        return (getattr(self, "_resume_token", None) is token
                and not self._stop_event.is_set())

    def _position_after_seek(self, av_url: str, token, get_position):
        """The renderer's position once it reports one, or None when it
        still reports none after `_RESUME_CONFIRM_SEC`."""
        waited = 0.0
        while True:
            at = get_position(av_url).get("position")
            if at is not None or waited >= self._RESUME_CONFIRM_SEC \
                    or not self._resume_current(token):
                return at
            self._stop_event.wait(self._RESUME_POLL_SEC)
            waited += self._RESUME_POLL_SEC

    def _resume_seek(self, seconds: float, token) -> None:
        from dlna_avtransport import (_sec_to_hms, avtransport_get_position,
                                      avtransport_probe_state, avtransport_seek)
        target = _sec_to_hms(seconds)
        try:
            with self._lock:
                av_url = self._av_url
            if not av_url:
                return
            waited, state = 0.0, ""
            while waited < self._RESUME_PLAYING_TIMEOUT:
                if not self._resume_current(token):
                    return
                state, _ = avtransport_probe_state(av_url)
                if state == "PLAYING":
                    break
                self._stop_event.wait(self._RESUME_POLL_SEC)
                waited += self._RESUME_POLL_SEC
            if state != "PLAYING":
                log.warning(f"RendererQueue ✗ RESUME → {target}: renderer "
                            f"never reached PLAYING in {waited:.0f}s "
                            f"(state {state or '?'}); playing from 0:00")
                return
            at = None
            for attempt in (1, 2):
                if not self._resume_current(token):
                    return
                ok = avtransport_seek(av_url, seconds)
                self._stop_event.wait(self._RESUME_SETTLE_SEC)
                at = self._position_after_seek(av_url, token,
                                               avtransport_get_position)
                if ok and at is None:
                    # After a Seek into a long file the Naim reports
                    # 0:00:00 (= no position) while it fetches the new
                    # spot — three "did not land" warnings on 2026-11-09
                    # were resumes the owner HEARD work. Unknown is not
                    # wrong: don't seek again, don't cry failure.
                    log.info(f"RendererQueue ⏩ RESUME → {target} sent "
                             f"(renderer reports no position yet — "
                             f"unconfirmed, attempt {attempt})")
                    return
                if ok and at is not None and \
                        abs(at - seconds) <= self._RESUME_TOLERANCE_SEC:
                    log.info(f"RendererQueue ⏩ RESUME → {target} ✓ "
                             f"(renderer at {_sec_to_hms(at)}, "
                             f"attempt {attempt}, after {waited:.1f}s)")
                    return
            log.warning(f"RendererQueue ✗ RESUME → {target}: Seek did not "
                        f"land (renderer at "
                        f"{_sec_to_hms(at) if at is not None else '?'})")
        except Exception as e:                                # noqa: BLE001
            log.warning(f"RendererQueue ✗ RESUME → {target}: {e}")
        finally:
            if getattr(self, "_resume_token", None) is token:
                self._resume_token = None

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
