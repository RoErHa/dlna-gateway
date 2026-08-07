"""R2 SSE — the PWA subscribes to /api/events (EventSource) for live pushes.

The backend publishers (state / index / devices) are unit-tested in
test_player.py + test_asgi.py. Here we assert the frontend side: on boot the
PWA opens an EventSource to /api/events (so server pushes accelerate the polls).
The event→refresh listeners call the already-tested pollState/pollIndex/
refreshServers functions, so there's nothing extra to drive here.
"""


def test_pwa_opens_event_source(app, gateway):
    r = gateway.wait_for_request("/api/events", method="GET", timeout=5)
    assert r is not None, "PWA must open an EventSource to /api/events on boot"
    accept = (r["headers"].get("Accept") or r["headers"].get("accept") or "")
    assert "text/event-stream" in accept, \
        f"EventSource should Accept text/event-stream (got {accept!r})"


def _set_visibility(app, state: str) -> None:
    app.evaluate(
        """(state) => {
          const hidden = state === 'hidden';
          Object.defineProperty(document, 'hidden',
              {value: hidden, configurable: true});
          Object.defineProperty(document, 'visibilityState',
              {value: state, configurable: true});
          document.dispatchEvent(new Event('visibilitychange'));
        }""",
        state,
    )


def test_sse_closed_on_hidden_reopened_on_visible(app, gateway):
    """Screen lock must CLOSE the SSE stream (its server keepalives
    would keep the iPhone radio out of low-power state for a whole
    locked-screen listening session — the 2026-07 battery-drain bug);
    returning to visible must open a fresh EventSource.

    The stub closes each /api/events response after the initial frames
    with retry=86400000, so the browser never reconnects an existing
    EventSource on its own — a second /api/events request can ONLY come
    from a new EventSource, which initEventSource() only creates after
    closeEventSource() has nulled the old one. No new request = the
    hidden branch failed to close the stream."""
    assert gateway.wait_for_request("/api/events", method="GET", timeout=5)
    gateway.clear_requests()
    _set_visibility(app, "hidden")
    _set_visibility(app, "visible")
    r = gateway.wait_for_request("/api/events", method="GET", timeout=5)
    assert r is not None, \
        "PWA must close SSE on hidden and open a NEW EventSource on visible"
