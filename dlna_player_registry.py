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
import time

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

    # Long enough for a healthy renderer's two parallel SOAP calls (which
    # RendererQueue.snapshot already caps at ~6 s between them), short enough
    # that one dead renderer cannot define the whole response time.
    _ALL_TIMEOUT_SEC = 7.0

    def snapshot_all(self) -> dict:
        """Return {udn: snapshot} for every queue that has ever been
        created. Useful for a global 'what's playing anywhere' view.

        Fanned out in PARALLEL, and that is the point. Queues are never
        evicted, so this walks every renderer ever seen — including ones that
        are now switched off. `RendererQueue.snapshot` already fires its two
        SOAP calls concurrently so an unresponsive renderer costs ~6 s rather
        than 12 s; doing the queues sequentially threw that away one level up
        and made the cost the SUM over dead renderers.

        Measured on the live gateway (2026-08-21, audit Track B4) with the LG
        TV switched off: `/api/renderer_state` with no udn took **6.011 s**,
        while the same call for the reachable Naim took 2 ms. The PWA polls
        that endpoint every second while audio plays, and each call held a
        threadpool token for the whole six seconds. With the fan-out the cost
        is the slowest renderer, not the sum of them.

        A queue whose thread does not answer in time is reported from its own
        cached snapshot instead, so a wedged renderer degrades to slightly
        stale data rather than holding up every other renderer's state.
        """
        with self._lock:
            items = list(self._queues.items())
        if not items:
            return {}
        if len(items) == 1:
            udn, q = items[0]
            return {udn: q.snapshot()}

        out: dict = {}
        out_lock = threading.Lock()

        def _one(udn: str, q) -> None:
            snap = q.snapshot()
            with out_lock:
                out[udn] = snap

        threads = [threading.Thread(target=_one, args=(udn, q), daemon=True,
                                    name=f"snap-{udn[:12]}")
                   for udn, q in items]
        for t in threads:
            t.start()
        deadline = time.monotonic() + self._ALL_TIMEOUT_SEC
        for t in threads:
            t.join(timeout=max(0.0, deadline - time.monotonic()))

        # Anything that missed the deadline still owes us an entry: use its
        # last cached snapshot so the shape of the response never changes.
        with out_lock:
            missing = [(udn, q) for udn, q in items if udn not in out]
            for udn, q in missing:
                out[udn] = dict(getattr(q, "_snap_cache", {}) or
                                {"state": "stopped", "alive": False,
                                 "renderer": "", "queue_len": 0,
                                 "queue_pos": 0})
        if missing:
            log.debug(f"snapshot_all: {len(missing)} renderer(s) did not "
                      f"answer within {self._ALL_TIMEOUT_SEC}s — served cached")
        return out


QUEUES = QueueRegistry()
