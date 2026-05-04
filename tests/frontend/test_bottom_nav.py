"""Bottom navigation — mobile only, 6 tabs."""


def test_hidden_on_desktop(app):
    nav = app.locator("#bottom-nav")
    assert nav.count() == 1
    assert nav.is_hidden()


def test_six_buttons_present(app):
    expected = ["browse", "search", "playlists", "favourites",
                "radio", "nowplaying"]
    for t in expected:
        assert app.locator(f"#bnav-{t}").count() == 1, f"missing #bnav-{t}"


def test_browse_active_by_default(app):
    assert "active" in (app.locator("#bnav-browse").get_attribute("class") or "")


def test_click_search_swaps_active(mobile_app):
    mobile_app.locator("#bnav-search").click()
    mobile_app.wait_for_timeout(200)
    assert "active" in (mobile_app.locator("#bnav-search").get_attribute("class") or "")


def test_click_playlists_swaps_active(mobile_app):
    mobile_app.locator("#bnav-playlists").click()
    mobile_app.wait_for_timeout(200)
    assert "active" in (mobile_app.locator("#bnav-playlists").get_attribute("class") or "")
