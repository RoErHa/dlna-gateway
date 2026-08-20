#!/usr/bin/env python3
"""
dlna_player_monitor.py — `MonitorMixin`: the per-queue 2-second
state-poll loop that advances tracks and enforces the stall guards.

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

This loop is the reason playback survives a renderer that misbehaves. It
watches for the normal PLAYING → STOPPED transition, but every interesting
case is a failure case: the renderer going unreachable, a Seek faulting, a
gapless auto-advance the renderer performed on its own (detected via the
TrackURI, not by us telling it to), and the audiobook position persistence
that has to keep working when the user stops playback from the Naim's own
front panel rather than the PWA.

The decision itself is NOT here — it lives in `dlna_player_policy` as pure
functions so it can be exhaustively tested without threads or SOAP. This
module is the I/O shell around those decisions.
"""
import logging
import time

from dlna_player_policy import (
    UNKNOWN_ABORT_SEC,
    _dur_to_sec,
    _gapless_advanced,
    _monitor_decision,
)

log = logging.getLogger("dlna.player")


class MonitorMixin:
    """See module docstring. Mixed into `RendererQueue`; never
    instantiated alone — it relies on the host's fields and lock."""

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
        from dlna_avtransport import (avtransport_probe_state,
                                      avtransport_get_position)
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

            # Audiobook queue: persist the position every ~15 s while
            # actually playing, so a session stopped on the Naim resumes
            # in the PWA / CarPlay at the right spot. Never fatal — a
            # failed save just waits for the next poll.
            if (self._is_book and cur_state == "PLAYING" and cur_t
                    and time.monotonic() - self._last_pos_save >= 15.0):
                self._last_pos_save = time.monotonic()
                try:
                    pos = avtransport_get_position(av_url)
                    if pos.get("position"):
                        from dlna_library import DB
                        key = cur_t.get("album_key") or cur_t.get("url", "")
                        DB.position_set(key, cur_t.get("url", ""),
                                        pos["position"], pos.get("duration"))
                except Exception as e:                        # noqa: BLE001
                    log.debug(f"RendererQueue: book position save failed: {e}")

            # Track how long the renderer has been out of contact —
            # either genuinely UNKNOWN or UNREACHABLE (SOAP failing).
            if cur_state in ("UNKNOWN", "UNREACHABLE"):
                if unknown_since == 0.0:
                    unknown_since = time.monotonic()
            else:
                unknown_since = 0.0

            # Gapless auto-advance (C6): the renderer may move to the
            # queued NextURI without ever reporting STOPPED, so the
            # PLAYING→STOPPED advance never fires — _index would lag and
            # the eventual STOPPED would re-send the already-played track
            # (double-play). Detect via the renderer's current TrackURI
            # and sync _index WITHOUT re-sending, then queue the new next.
            if (cur_state in ("PLAYING", "TRANSITIONING") and cur_t is not None
                    and not bool(cur_t.get("is_stream"))):
                with self._lock:
                    nxt = (self._tracks[self._index + 1]
                           if 0 <= self._index + 1 < len(self._tracks)
                           else None)
                if nxt is not None:
                    track_uri = avtransport_get_position(av_url).get("track_uri")
                    if _gapless_advanced(cur_state, track_uri,
                                         cur_t.get("url", ""),
                                         nxt.get("url", "")):
                        log.info(f"RendererQueue ⏭ GAPLESS advance "
                                 f"[{idx+1}→{idx+2}/{total}] — renderer moved "
                                 f"to queued URI")
                        self._log_track_end("finished")
                        with self._lock:
                            self._index += 1
                            self._started_at = time.monotonic()
                        self._queue_next_uri()
                        prev_state = cur_state
                        self._stop_event.wait(POLL_SEC)
                        continue

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
