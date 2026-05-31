"""Header bar — server status, OUT picker, search."""
import pytest


def test_source_dropdown_shows_server_name(page, stub, gateway):
    # The standalone disc-dot/label was removed; server name + online
    # state now live in the SRC dropdown options.
    gateway.servers = [{"udn": "uuid:asset-1", "name": "AssetUPnP",
                        "online": True, "tracks": 1234}]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#source-sel option').length >= 1 && "
        "document.querySelector('#source-sel option').textContent"
        ".includes('AssetUPnP')",
        timeout=5000,
    )


def test_no_disc_label_element(app):
    # The redundant live-server status label was removed from the header.
    assert app.locator("#disc-label").count() == 0


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
