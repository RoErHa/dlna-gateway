"""Volume trim slider, ± buttons, shuffle, radio.

The volume slider is a *relative* trim around the renderer's natural
volume — NOT an absolute level. Default 0 dB → tapping or sliding the
control can never blast a UPnP renderer (regression bug 2026-05-05).
Range ±5 dB; each slider step = 0.5 dB.
"""
import json


def test_volume_slider_default_is_zero_db(app):
    """Safety-critical: slider must default to 0 (no change) so a tap
    on the freshly-loaded PWA can't send Naim to volume 80."""
    assert app.locator("#vol").input_value() == "0"
    assert "0.0" in app.locator("#vol-label").text_content()


def test_volume_slider_range_is_minus10_to_plus10(app):
    """Slider units are 0.5-dB steps; 10 units of slider = 5 dB of trim."""
    assert app.locator("#vol").get_attribute("min") == "-10"
    assert app.locator("#vol").get_attribute("max") == "10"


def test_vol_up_down_buttons_present(app):
    assert app.locator("#btn-vol-up").count() == 1
    assert app.locator("#btn-vol-down").count() == 1


def test_vol_up_button_steps_half_db(app, gateway):
    """Each + click = +1 slider unit = +0.5 dB trim."""
    gateway.clear_requests()
    app.locator("#btn-vol-up").click()
    app.wait_for_timeout(150)
    assert app.locator("#vol").input_value() == "1"
    assert "+0.5" in app.locator("#vol-label").text_content()


def test_vol_down_button_steps_half_db(app, gateway):
    gateway.clear_requests()
    app.locator("#btn-vol-down").click()
    app.wait_for_timeout(150)
    assert app.locator("#vol").input_value() == "-1"
    assert "-0.5" in app.locator("#vol-label").text_content()


def test_vol_up_clamps_at_plus_5_db(app):
    """Mash + 20 times → still capped at +5 dB."""
    for _ in range(20):
        app.locator("#btn-vol-up").click()
    app.wait_for_timeout(150)
    assert app.locator("#vol").input_value() == "10"
    assert "+5.0" in app.locator("#vol-label").text_content()


def test_vol_down_clamps_at_minus_5_db(app):
    for _ in range(20):
        app.locator("#btn-vol-down").click()
    app.wait_for_timeout(150)
    assert app.locator("#vol").input_value() == "-10"
    assert "-5.0" in app.locator("#vol-label").text_content()


def test_slider_drag_coalesces_to_one_post(page, stub, gateway):
    """Regression guard for the slider-flood bug (2026-05-05): dragging
    the slider used to fire 30+ POST /api/control per second, choking
    Naim's single-threaded SOAP server and producing audible
    stair-stepping. Now coalesced via debounce: 10 input events in
    rapid succession → AT MOST 2 POSTs (one debounced + one optional
    'change' flush)."""
    gateway.renderers = [{"udn": "uuid:naim", "name": "Naim"}]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#output-sel option').length >= 2", timeout=4000)
    page.select_option("#output-sel", "upnp:uuid:naim")
    gateway.clear_requests()
    # Simulate a 10-step rapid drag (no inter-event sleep — same as
    # what the browser dispatches at high pointer-event rates).
    page.evaluate("""
      const v = document.getElementById('vol');
      for (let i = 0; i < 10; i++) {
        v.value = i;
        v.dispatchEvent(new Event('input'));
      }
    """)
    # Wait long enough for the debounce window to elapse + flush
    page.wait_for_timeout(300)
    posts = gateway.captured(method="POST", path_contains="/api/control")
    assert len(posts) <= 2, (
        f"slider drag must coalesce to at most 2 POSTs; got {len(posts)} "
        f"({[p['body'] for p in posts]})")
    assert len(posts) >= 1, "must still send at least the final value"


def test_buttons_post_immediately(page, stub, gateway):
    """The ± buttons are NOT debounced — each click is a deliberate
    user action and must reach the renderer at once."""
    import time as _t
    gateway.renderers = [{"udn": "uuid:naim", "name": "Naim"}]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#output-sel option').length >= 2", timeout=4000)
    page.select_option("#output-sel", "upnp:uuid:naim")
    gateway.clear_requests()
    t0 = _t.time()
    page.locator("#btn-vol-up").click()
    req = gateway.wait_for_request("/api/control", method="POST", timeout=0.5)
    elapsed = (_t.time() - t0) * 1000
    assert req is not None, "+ button must POST immediately, no debounce"
    assert elapsed < 200, f"button POST took {elapsed:.0f} ms — should be near-instant"


def test_volume_input_posts_trim_db_for_upnp(page, stub, gateway):
    """The POST body must use action='trim_db' (NOT 'volume' — that
    semantic was the absolute-volume regression that blasted Naim) and
    address the specific renderer."""
    gateway.renderers = [{"udn": "uuid:naim", "name": "Naim"}]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#output-sel option').length >= 2", timeout=4000)
    page.select_option("#output-sel", "upnp:uuid:naim")
    gateway.clear_requests()
    # Move slider to +4 (= +2.0 dB trim)
    page.evaluate("""
      const v = document.getElementById('vol');
      v.value = 4;
      v.dispatchEvent(new Event('input'));
    """)
    assert "+2.0" in page.locator("#vol-label").text_content()
    req = gateway.wait_for_request("/api/control", method="POST", timeout=2.0)
    assert req is not None
    body = json.loads(req["body"])
    assert body.get("action") == "trim_db", (
        "must be 'trim_db' (relative offset), not 'volume' (absolute level "
        "— that was the slider-blast regression)")
    assert body.get("value") == 2.0
    assert body.get("device") == "upnp:uuid:naim"


def test_loudness_status_endpoint(app, gateway):
    """The /api/loudness/status endpoint is the contract the PWA reads to
    show scanner progress. Stub gateway returns a sensible default; the
    test just asserts the endpoint exists and shape is right."""
    import json
    txt = app.evaluate("fetch('/api/loudness/status').then(r => r.text())")
    data = json.loads(txt)
    for key in ("scanned", "total", "in_progress", "target_peak_dbtp"):
        assert key in data, f"loudness status missing {key}: {data}"
    assert isinstance(data["in_progress"], bool)
    assert isinstance(data["scanned"], int)
    assert isinstance(data["total"], int)
    assert isinstance(data["target_peak_dbtp"], (int, float))


def test_shuffle_toggle_persists(page, stub, gateway):
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('source-sel') && !document.getElementById('source-sel').textContent.includes('Scanning')", timeout=5000)
    initial = page.evaluate("shuffleEnabled")
    page.locator("#btn-shuffle").click()
    page.wait_for_timeout(200)
    after = page.evaluate("shuffleEnabled")
    assert after != initial
    stored = page.evaluate("localStorage.getItem('dlna_shuffle')")
    assert stored in ("0", "1")


def test_radio_button_loads_and_plays(app, gateway):
    for i in range(5):
        gateway.radio_tracks.append({
            "url": f"http://stub/r{i}.flac",
            "title": f"Random {i}",
            "artist": "Various", "album": "Radio",
            "duration": "0:03:00", "art": "", "type": "audio",
        })
    app.locator("#btn-radio").click()
    req = app.evaluate("null")  # no-op; using gateway capture
    r = gateway.wait_for_request("/api/radio", timeout=3.0)
    assert r is not None
    # And the audio element should get a src
    app.wait_for_function(
        "document.getElementById('browser-audio').src.includes('/stream')",
        timeout=2000)
