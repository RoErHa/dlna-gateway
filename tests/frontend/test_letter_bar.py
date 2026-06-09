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


def test_desktop_letter_bar_overflows_the_sidebar(app):
    # Regression: on desktop the bar lives in the fixed 360px #browser sidebar
    # and the 29 letters can't all fit — so it MUST be horizontally scrollable.
    sw, cw = app.evaluate(
        "() => {const b=document.getElementById('letter-bar');"
        " return [b.scrollWidth, b.clientWidth];}")
    assert sw > cw, f"expected overflow (scrollWidth {sw} > clientWidth {cw})"


def test_desktop_letter_bar_shows_scrollbar(app):
    # Desktop (mouse, no touch-swipe): the overflowing letters past ~H are only
    # reachable if a scrollbar is shown. Was hidden (scrollbar-width:none) →
    # stuck at H. Must be 'thin'/'auto' on the desktop layout (>768px).
    sbw = app.evaluate(
        "() => getComputedStyle(document.getElementById('letter-bar')).scrollbarWidth")
    assert sbw in ("thin", "auto"), f"desktop scrollbar hidden (scrollbarWidth={sbw!r})"


def test_mobile_letter_bar_keeps_scrollbar_hidden(mobile_app):
    # Mobile keeps the clean hidden-scrollbar single-row swipe (iOS unaffected).
    sbw = mobile_app.evaluate(
        "() => getComputedStyle(document.getElementById('letter-bar')).scrollbarWidth")
    assert sbw == "none", f"mobile scrollbar should stay hidden (got {sbw!r})"
