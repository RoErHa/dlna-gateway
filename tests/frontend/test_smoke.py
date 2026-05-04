"""Smoke test — proves the stub + Playwright + app.js boot correctly."""


def test_page_loads_and_shows_server_status(app, gateway):
    # Disc label should reflect the one online server from the default state
    label = app.locator("#disc-label").text_content()
    assert "online" in label.lower(), f"got: {label!r}"


def test_default_layout_present(app):
    # All the major panels should be in the DOM
    for sel in ("#browser", "#player", "#pl-panel",
                "#btn-pp", "#btn-stop", "#btn-next", "#btn-prev",
                "#vol", "#btn-shuffle", "#btn-radio",
                "#tab-browse", "#tab-search"):
        assert app.locator(sel).count() == 1, f"missing: {sel}"
