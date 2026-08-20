#!/usr/bin/env python3
"""
dlna_player_volume.py — `VolumeMixin`: renderer volume, via
RenderingControl SOAP.

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

THIS IS BIT-PERFECT. The gateway is not in the audio path for a UPnP
renderer; it calls `SetVolume`, so the Naim attenuates in its own
DAC/analog domain. No PCM is ever touched here.

Two hard-won rules:
  * The startup volume is set ONCE per queue and never re-asserted
    per-track, so a change on the Naim's own remote sticks for the session.
  * We deliberately do NOT read the renderer's current volume first. A
    STOPPED Naim reports 0 from GetVolume, and adopting that as the
    baseline silenced every track (the 2026-05-30 bug).

SetVolume runs on a daemon thread so the HTTP handler returns instantly
even when the renderer's SOAP is slow.
"""
import logging
import threading

from dlna_player_policy import (
    GAIN_TO_VOLUME_RATIO,
    MAX_USER_TRIM_DB,
    STARTUP_VOLUME,
)

log = logging.getLogger("dlna.player")


class VolumeMixin:
    """See module docstring. Mixed into `RendererQueue`; never
    instantiated alone — it relies on the host's fields and lock."""

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
        if not rc_url:
            return
        # If we know the baseline, apply NOW so the user hears the change.
        # Otherwise the trim is stored and applied on first play.
        if baseline is None:
            return
        # Loudness gain is no longer applied (always 0), so the slider is
        # a straight trim around the startup baseline.
        offset = round(trim_db * GAIN_TO_VOLUME_RATIO)
        level  = max(0, min(100, baseline + offset))
        self._set_volume_async(rc_url, level)

    def _apply_startup_volume(self):
        """Set the renderer to STARTUP_VOLUME once per queue, on the FIRST
        track only. No-op if rc_url is empty (renderer has no
        RenderingControl URL) or the baseline is already set (i.e. this
        isn't the first track — volume is set once per queue, never
        re-asserted per-track, so a manual change on the Naim's own remote
        sticks for the rest of the session; only the PWA slider re-sets it).

        We deliberately do NOT read the renderer's current volume first: a
        STOPPED Naim reports 0 via GetVolume, and adopting that as the
        baseline silenced every track (the 2026-05-30 bug this replaces).
        Loudness gain is no longer applied — the only offset is the user
        trim, which defaults to 0 on a fresh queue, so the first track plays
        at exactly STARTUP_VOLUME."""
        with self._lock:
            rc_url   = self._rc_url
            baseline = self._renderer_baseline
            trim_db  = self._user_trim_db
        if not rc_url:
            return
        if baseline is not None:
            return          # already set this queue — don't re-assert
        baseline = STARTUP_VOLUME
        with self._lock:
            self._renderer_baseline = baseline
        offset = round(trim_db * GAIN_TO_VOLUME_RATIO)
        level  = max(0, min(100, baseline + offset))
        # Fire-and-forget so a slow Naim SOAP (busy serving the snapshot
        # poller) doesn't delay SetURI/Play.
        self._set_volume_async(rc_url, level)
        log.debug(f"RendererQueue: startup SetVolume({level}) "
                  f"(baseline={baseline}, trim={trim_db:+.1f} dB)")
