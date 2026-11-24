#!/usr/bin/env python3
"""
dlna_player_seek.py — `SeekMixin`: a person's seek within the current track,
and the `state` event every other part of the queue sends (2.4.0).

Not the audiobook RESUME seek: that one (`TransportMixin._resume_seek`) runs
on its own thread, waits for PLAYING and checks where it landed. This one is
asked for while the track already plays, answers the caller, and is refused
rather than retried — pressing again is the retry.

What is hard here is the POSITION, not the Seek:

  * The Naim reports 0:00:00 (= None) for seconds after a Seek into a long
    file. A second "+30" that took that for zero would throw the listener
    back to 0:30. So the last seek is remembered (`_seek_mark`, per track)
    and quick presses add up from it.
  * A Seek before the renderer is PLAYING is accepted and dropped, so
    anything but PLAYING / PAUSED_PLAYBACK is refused.
  * Once our queue is over (`_stop_event`), what the renderer plays is
    somebody else's: never seek that.
"""
import logging
import time

from dlna_events import EVENTS
from dlna_player_policy import _dur_to_sec, seek_target

log = logging.getLogger("dlna.player")


class SeekMixin:
    """See module docstring. Mixed into `RendererQueue`; never
    instantiated alone — it relies on the host's fields and lock."""

    # Trust the last seek over the renderer for this long...
    _SEEK_SETTLE_SEC = 4.0
    # ...and this long when the renderer reports no position at all.
    _SEEK_UNKNOWN_SEC = 20.0
    # No position this soon after Play is simply 0:00.
    _SEEK_JUST_STARTED_SEC = 5.0

    def _publish_state(self) -> None:
        """Tell SSE clients to re-read THIS renderer's state. The snapshot
        cache is dropped first, or they would be answered from before."""
        self._invalidate_snap()
        EVENTS.publish({"type": "state", "udn": self._udn})

    def _seek_base(self, reported, idx: int, started: float, playing: bool):
        """Where the track is now, in seconds, or None when nobody knows."""
        now = time.monotonic()
        mark = self._seek_mark
        if mark is not None and mark[0] == idx:
            age = now - mark[1]
            limit = (self._SEEK_SETTLE_SEC if reported is not None
                     else self._SEEK_UNKNOWN_SEC)
            if age < limit:
                return mark[2] + (age if playing else 0.0)
        if reported is not None:
            return reported
        if started > 0 and now - started < self._SEEK_JUST_STARTED_SEC:
            return 0.0
        return None

    def seek(self, value: float, relative: bool) -> tuple[str, float | None]:
        """Seek to `value` seconds, or by `value` seconds when relative.
        → ("ok", target) | ("not_seekable", None) | ("refused", None)."""
        from dlna_avtransport import (_sec_to_hms, avtransport_get_position,
                                      avtransport_probe_state, avtransport_seek)
        with self._seek_lock:            # quick presses, one after the other
            with self._lock:
                av_url, idx, started = self._av_url, self._index, self._started_at
                track = (self._tracks[idx]
                         if 0 <= idx < len(self._tracks) else None)
            if (not av_url or track is None or track.get("is_stream")
                    or self._stop_event.is_set()):
                return "not_seekable", None
            state, _ = avtransport_probe_state(av_url)
            if state not in ("PLAYING", "PAUSED_PLAYBACK"):
                return "not_seekable", None
            pos = avtransport_get_position(av_url)
            base = 0.0
            if relative:
                base = self._seek_base(pos.get("position"), idx, started,
                                       state == "PLAYING")
                if base is None:
                    return "not_seekable", None
            target = seek_target(base, value, relative,
                                 pos.get("duration")
                                 or _dur_to_sec(track.get("duration")))
            if not avtransport_seek(av_url, target):
                return "refused", None
            now = time.monotonic()
            with self._lock:
                self._resume_token = None        # the person's choice wins
                if self._index == idx:
                    self._seek_mark = (idx, now, target)
                    # The watchdog compares this clock with the duration, so
                    # it must follow: a rewound track is not overdue.
                    if self._started_at > 0:
                        self._started_at = max(now - target, 0.001)
            log.info(f"RendererQueue ⏩ SEEK → {_sec_to_hms(target)}")
        self._publish_state()
        return "ok", target
