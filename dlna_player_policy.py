#!/usr/bin/env python3
"""
dlna_player_policy.py — the pure decision functions behind renderer
playback, plus the tuning constants.

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

Everything here is a pure function of its arguments: no locks, no SOAP, no
clock. That is deliberate — these encode the rules that were expensive to
learn, and keeping them side-effect-free is what makes them exhaustively
unit-testable (tests/test_player.py::TestMonitorDecision).

`_dur_to_sec` returns 0 rather than raising on ANY malformed input. Before
2026-04-23 it was a bare `int(dur)`, and `playlist_tracks.duration` stores
UPnP 'H:MM:SS' TEXT — the ValueError killed the renderer-queue daemon thread
silently, so playback died before SetURI was ever sent. chaos.py found it.

`_monitor_decision` carries the two stall guards. The renderer going
unreachable mid-track means the PLAYING → STOPPED transition is never
observed, so without these the queue sits on one track forever (2026-05-20:
'Starman' stuck for 36 minutes). PAUSED_PLAYBACK is excluded from the
watchdog so a deliberately paused queue is never skipped, and `is_stream`
suppresses it entirely — a radio stream has no duration and a momentary
STOPPED is a rebuffer, not an end-of-track.
"""
import logging

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


# Absolute renderer volume set ONCE at queue start. The Naim's scale is
# 0–100; 22 is a comfortable living-room level. We deliberately do NOT
# read the renderer's current volume first — a STOPPED Naim reports 0
# via GetVolume, and adopting that as the baseline was the cause of the
# "every track plays silent" bug (2026-05-30). After this one-shot set
# we never re-assert per-track, so a manual change on the Naim's own
# remote sticks; only the PWA slider re-sets it.
STARTUP_VOLUME: int = 22


# User-trim slider → renderer-volume-unit conversion.
# A value of 2 means "one slider dB ≈ 2 Naim volume units" (≈ 0.5 dB
# per unit). Approximation — the renderer's curve is logarithmic and
# renderer-specific. Tune by ear.
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


def _gapless_advanced(cur_state: str, track_uri, cur_url: str,
                      next_url: str) -> bool:
    """True when the renderer has auto-transitioned to the queued NEXT
    track (gapless) — it's actively playing and its current TrackURI is
    the next track's URL rather than the current one.

    The renderer auto-plays a SetNextAVTransportURI without ever passing
    through STOPPED, so the PLAYING→STOPPED advance never fires; the
    monitor uses this to sync its index WITHOUT re-sending (a re-send
    would double-play the track the renderer is already playing).

    Safe-degrades to False when the renderer doesn't report a usable
    TrackURI (None / NOT_IMPLEMENTED) — the queue then falls back to the
    STOPPED→advance path, i.e. pre-C6 behaviour."""
    if cur_state not in ("PLAYING", "TRANSITIONING"):
        return False
    if not track_uri or not next_url:
        return False
    return track_uri == next_url and track_uri != cur_url


def _av_state_to_ui(state: str) -> str:
    return {
        "PLAYING":          "playing",
        "PAUSED_PLAYBACK":  "paused",
        "STOPPED":          "stopped",
        "NO_MEDIA_PRESENT": "stopped",
        "TRANSITIONING":    "playing",
    }.get(state, "stopped")
