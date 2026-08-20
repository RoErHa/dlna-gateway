#!/usr/bin/env python3
"""
dlna_player_registry.py — `QueueRegistry` and the process-wide
`QUEUES` singleton: one `RendererQueue` per renderer UDN.

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

The architectural rule it enforces: ONE active stream per physical output,
server-side. Queuing to a busy renderer returns 409 with what it is playing;
the client re-sends with `force: true` to take over. Different UDNs are
fully independent, which is what lets two people play to two renderers at
once (proven by
tests/test_api_playback.py::test_concurrent_renderers_have_independent_state).

Queues are created lazily and never evicted — a renderer that goes away
leaves a stopped queue behind, which is cheap and means its history survives
the renderer coming back.
"""
import logging
import threading

from dlna_player import RendererQueue

log = logging.getLogger("dlna.player")


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

    def peek(self, udn: str) -> RendererQueue | None:
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
