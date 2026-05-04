"""Browse mode bar (Artists / Albums / Tracks / Genres)."""


def test_four_modes_present(app):
    for m in ("artists", "albums", "tracks", "genres"):
        assert app.locator(f"#bmode-{m}").count() == 1


def test_artists_active_on_load(app):
    assert "active" in (app.locator("#bmode-artists").get_attribute("class") or "")


def test_click_albums_swaps_active_and_calls_api(app, gateway):
    gateway.clear_requests()
    app.locator("#bmode-albums").click()
    assert "active" in (app.locator("#bmode-albums").get_attribute("class") or "")
    assert "active" not in (app.locator("#bmode-artists").get_attribute("class") or "")
    req = gateway.wait_for_request("/api/browse_letter", timeout=2.0,
                                   match=lambda r: r["query"].get("mode") == "albums")
    assert req is not None, "expected a browse_letter call with mode=albums"


def test_click_tracks_calls_api(app, gateway):
    gateway.clear_requests()
    app.locator("#bmode-tracks").click()
    req = gateway.wait_for_request("/api/browse_letter", timeout=2.0,
                                   match=lambda r: r["query"].get("mode") == "tracks")
    assert req is not None, "expected a browse_letter call with mode=tracks"


def test_click_genres_hides_letter_bar_and_calls_genres_api(app, gateway):
    app.locator("#bmode-genres").click()
    # /api/genres should be hit
    req = gateway.wait_for_request("/api/genres", timeout=2.0)
    assert req is not None
    # Letter bar hidden in genres mode
    assert app.locator("#letter-bar").is_hidden()
