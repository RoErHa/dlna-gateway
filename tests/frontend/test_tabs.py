"""Tab bar (Browse / Search)."""


def test_both_tabs_present(app):
    assert app.locator("#tab-browse").count() == 1
    assert app.locator("#tab-search").count() == 1


def test_browse_active_on_load(app):
    assert "active" in (app.locator("#tab-browse").get_attribute("class") or "")
    assert "active" not in (app.locator("#tab-search").get_attribute("class") or "")


def test_click_search_swaps_active(app):
    app.locator("#tab-search").click()
    assert "active" in (app.locator("#tab-search").get_attribute("class") or "")
    assert "active" not in (app.locator("#tab-browse").get_attribute("class") or "")
    # Browse modes hidden in search
    assert app.locator("#browse-modes").is_hidden()


def test_click_browse_returns(app):
    app.locator("#tab-search").click()
    app.locator("#tab-browse").click()
    assert "active" in (app.locator("#tab-browse").get_attribute("class") or "")
    # Browse modes + letter bar visible (only when curServer exists, which it does)
    assert app.locator("#browse-modes").is_visible()
    assert app.locator("#letter-bar").is_visible()
