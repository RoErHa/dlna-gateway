"""Letter picker (#, 0, A–Z)."""


def test_letter_count(app):
    # LETTERS in app.js = ["⭐","#","0","A".."Z"] = 1 + 1 + 1 + 26 = 29
    # (the leading ⭐ shows favourited albums; see test_fav_albums_letterbar.py)
    assert app.locator(".letter-btn").count() == 29


def test_letter_a_active_by_default(app):
    btn_a = app.locator(".letter-btn", has_text="A").first
    assert "active" in (btn_a.get_attribute("class") or "")


def test_click_letter_fires_api_with_letter(app, gateway):
    gateway.clear_requests()
    app.locator(".letter-btn", has_text="B").first.click()
    req = gateway.wait_for_request("/api/browse_letter", timeout=2.0,
                                   match=lambda r: r["query"].get("letter") == "B")
    assert req is not None


def test_letter_bar_hidden_in_search_tab(app):
    app.locator("#tab-search").click()
    assert app.locator("#letter-bar").is_hidden()
