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
