"""Header bar — server status, OUT picker, search."""
import pytest


def test_disc_label_online(app, gateway):
    label = app.locator("#disc-label").text_content()
    assert "online" in label.lower()


def test_disc_label_offline(page, stub, gateway):
    gateway.servers = [{"udn": "uuid:asset-1", "name": "AssetUPnP",
                        "online": False, "tracks": 0}]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('disc-label').textContent.toLowerCase().includes('offline')",
        timeout=5000,
    )


def test_disc_label_scanning(page, stub, gateway):
    gateway.servers = []
    page.goto(stub.base_url + "/")
    label = page.locator("#disc-label").text_content()
    assert "scanning" in label.lower()


def test_out_dropdown_default_browser(app):
    assert app.locator("#output-sel option").count() >= 1
    first_text = app.locator("#output-sel option").first.text_content()
    assert "Browser" in first_text


def test_out_dropdown_lists_all_renderers(page, stub, gateway):
    gateway.renderers = [
        {"udn": "uuid:naim-1", "name": "Naim Uniti"},
        {"udn": "uuid:lounge", "name": "Lounge Speaker"},
    ]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#output-sel option').length >= 3",
        timeout=5000,
    )
    options = page.locator("#output-sel option").all_text_contents()
    joined = " | ".join(options)
    assert "Naim" in joined and "Lounge" in joined


def test_search_input_present(app):
    s = app.locator("#search-input")
    assert s.count() == 1
    assert s.get_attribute("placeholder") and "Search" in s.get_attribute("placeholder")


def test_search_input_typing_triggers_api(app, gateway):
    app.locator("#search-input").fill("abba")
    # debounce is 400ms — give it room
    req = gateway.wait_for_request("/api/search", timeout=2.0)
    assert req is not None
    assert req["query"].get("q") == "abba"


def test_search_input_enter_fires_immediately(app, gateway):
    s = app.locator("#search-input")
    s.fill("zappa")
    s.press("Enter")
    req = gateway.wait_for_request("/api/search", timeout=1.0)
    assert req is not None
    assert req["query"].get("q") == "zappa"
