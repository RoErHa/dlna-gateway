#!/usr/bin/env python3
"""
dlna_events.py — in-process event bus for Server-Sent Events (R2, 2.0).

The 2.0 ASGI stack pushes live updates to the PWA over an SSE stream
(`GET /api/events`) instead of the PWA polling `/api/state` etc. Backend code
that already knows about a change (a RendererQueue advancing a track, the
Indexer's progress, a device appearing/leaving) calls `EVENTS.publish({...})`
from ITS OWN thread; the bus marshals the event onto the ASGI event loop and
fans it out to every connected SSE subscriber.

The cross-thread bridge is the whole point: publishers run in worker threads
(RendererQueue monitor, Indexer, discovery), but `asyncio.Queue` is only safe
on its loop. `publish()` uses `loop.call_soon_threadsafe` so a thread can hand
an event to the loop without locks on the asyncio side.

    from dlna_events import EVENTS
    EVENTS.publish({"type": "now_playing", "title": "...", "udn": "..."})

The loop is bound once at ASGI startup (dlna_asgi._lifespan →
EVENTS.bind_loop). Before binding, publish() is a no-op (nothing is listening).
"""
import asyncio
import json
import logging
import threading

log = logging.getLogger("dlna.events")


class EventBus:
    """Thread-safe fan-out of event dicts to asyncio.Queue subscribers.

    publish() is callable from any thread; subscribe()/unsubscribe() are called
    from the SSE endpoint on the event loop. A slow subscriber whose queue is
    full has events dropped (liveness over completeness — SSE clients reconcile
    via a full refresh on reconnect)."""

    def __init__(self, max_queue: int = 256):
        self._subs: set = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._max_queue = max_queue

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Bind (or clear) the event loop publish() marshals onto."""
        self._loop = loop

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def subscribe(self) -> "asyncio.Queue":
        """Register a new subscriber queue (call on the loop)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: "asyncio.Queue") -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict) -> None:
        """Fan `event` out to all subscribers. Safe from any thread; a no-op
        until a loop is bound."""
        loop = self._loop
        if loop is None:
            return
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                loop.call_soon_threadsafe(self._offer, q, event)
            except RuntimeError:
                pass            # loop is closed/closing — drop

    @staticmethod
    def _offer(q: "asyncio.Queue", event: dict) -> None:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass                # slow client — drop this event


# Module singleton — the gateway's one event bus.
EVENTS = EventBus()


def sse_format(event: dict) -> str:
    """Serialise an event dict as one SSE frame. `type` becomes the SSE event
    name (so clients can `addEventListener(name, …)`); the whole dict is the
    JSON `data:` payload. Always ends with the blank-line frame terminator."""
    name = str(event.get("type", "message"))
    data = json.dumps(event, ensure_ascii=False)
    return f"event: {name}\ndata: {data}\n\n"
