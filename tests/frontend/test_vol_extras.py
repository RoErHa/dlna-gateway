"""Volume slider, shuffle, radio."""


def test_volume_initial_value(app):
    assert app.locator("#vol").input_value() == "80"
    assert app.locator("#vol-label").text_content().strip() == "80"


def test_volume_input_updates_label_and_posts_for_upnp(page, stub, gateway):
    gateway.renderers = [{"udn": "uuid:naim", "name": "Naim"}]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#output-sel option').length >= 2", timeout=4000)
    page.select_option("#output-sel", "upnp:uuid:naim")
    page.evaluate("""
      const v=document.getElementById('vol');
      v.value=42;
      v.dispatchEvent(new Event('input'));
    """)
    assert page.locator("#vol-label").text_content().strip() == "42"
    req = gateway.wait_for_request("/api/control", method="POST", timeout=2.0)
    assert req is not None and "volume" in req["body"] and "42" in req["body"]


def test_shuffle_toggle_persists(page, stub, gateway):
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('disc-label').textContent !== 'Scanning…'", timeout=5000)
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
