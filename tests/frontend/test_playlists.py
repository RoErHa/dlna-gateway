"""Playlists panel — list, open, play, delete, remove track."""


def test_new_playlist_button_present(app):
    # Default state: pl-actions has the +New button
    assert app.locator("#pl-actions button", has_text="New playlist").count() == 1


def test_new_playlist_prompt_creates(app, gateway):
    app.on("dialog", lambda d: d.accept("My new mix"))
    app.locator("#pl-actions button", has_text="New playlist").click()
    req = gateway.wait_for_request("/api/playlist/create", timeout=2.0)
    assert req is not None
    assert req["query"].get("name") == "My new mix"


def test_clicking_playlist_opens_it(page, stub, gateway):
    gateway.add_playlist("pl-1", "My Mix", [
        {"url": "http://stub/a.flac", "title": "A", "artist": "X", "album": "Y"},
    ])
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#pl-list .pl-item').length >= 1", timeout=4000)
    page.locator("#pl-list .pl-item", has_text="My Mix").click()
    page.wait_for_function(
        "document.getElementById('pl-back-btn').style.display !== 'none'",
        timeout=2000)
    assert "My Mix" in page.locator("#pl-panel-title").text_content()


def test_favourites_has_no_delete_button(page, stub, gateway):
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#pl-list .pl-item').length >= 1", timeout=4000)
    # Click Favourites
    page.locator("#pl-list .pl-item", has_text="Favourites").click()
    page.wait_for_function(
        "document.getElementById('pl-back-btn').style.display !== 'none'",
        timeout=2000)
    assert page.locator("#pl-actions button", has_text="Delete").count() == 0
    # But Play button IS present
    assert page.locator("#pl-actions button", has_text="Play").count() == 1


def test_custom_playlist_has_delete_button(page, stub, gateway):
    gateway.add_playlist("pl-1", "Mix", [])
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#pl-list .pl-item').length >= 2", timeout=4000)
    page.locator("#pl-list .pl-item", has_text="Mix").click()
    page.wait_for_function(
        "document.getElementById('pl-back-btn').style.display !== 'none'",
        timeout=2000)
    assert page.locator("#pl-actions button", has_text="Delete").count() == 1


def test_delete_confirms(page, stub, gateway):
    gateway.add_playlist("pl-1", "Mix", [])
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#pl-list .pl-item').length >= 2", timeout=4000)
    page.locator("#pl-list .pl-item", has_text="Mix").click()
    page.wait_for_function(
        "document.getElementById('pl-back-btn').style.display !== 'none'",
        timeout=2000)
    # Cancel — should NOT delete
    page.on("dialog", lambda d: d.dismiss())
    page.locator("#pl-actions button", has_text="Delete").click()
    page.wait_for_timeout(300)
    assert not gateway.captured(path_contains="/api/playlist/delete")


def test_remove_track_button_removes(page, stub, gateway):
    gateway.add_playlist("pl-1", "Mix", [
        {"url": "http://stub/a.flac", "title": "A", "artist": "X", "album": "Y"},
        {"url": "http://stub/b.flac", "title": "B", "artist": "X", "album": "Y"},
    ])
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#pl-list .pl-item').length >= 2", timeout=4000)
    page.locator("#pl-list .pl-item", has_text="Mix").click()
    page.wait_for_function(
        "document.querySelectorAll('#pl-tracks .pl-track').length >= 2",
        timeout=4000)
    gateway.clear_requests()
    page.locator("#pl-tracks .pl-track .pl-remove").first.click()
    req = gateway.wait_for_request("/api/playlist/remove", timeout=2.0)
    assert req is not None


def test_empty_playlist_message(page, stub, gateway):
    gateway.add_playlist("pl-empty", "Empty", [])
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#pl-list .pl-item').length >= 2", timeout=4000)
    page.locator("#pl-list .pl-item", has_text="Empty").click()
    page.wait_for_function(
        "document.getElementById('pl-tracks').classList.contains('visible')",
        timeout=2000)
    assert "Empty playlist" in page.locator("#pl-tracks").text_content()
