"""Smoke test — proves the stub + Playwright + app.js boot correctly."""


def test_page_loads_and_shows_server_status(app, gateway):
    # The standalone disc-label was removed; server status now lives in the
    # SRC dropdown options (name + tracks; a "(offline)" suffix only when
    # offline). The default stub server is online, so its name shows with no
    # offline marker.
    opts = app.locator("#source-sel").text_content()
    assert "AssetUPnP" in opts, f"got: {opts!r}"
    assert "(offline)" not in opts, f"got: {opts!r}"


def test_default_layout_present(app):
    # All the major panels should be in the DOM
    for sel in ("#browser", "#player", "#pl-panel",
                "#btn-pp", "#btn-stop", "#btn-next", "#btn-prev",
                "#vol", "#btn-shuffle", "#btn-radio",
                "#tab-browse", "#tab-search"):
        assert app.locator(sel).count() == 1, f"missing: {sel}"
