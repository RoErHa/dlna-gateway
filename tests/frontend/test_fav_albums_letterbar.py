"""The ⭐ entry at the front of the browse letter bar shows favourited
albums (the folder-model replacement for the removed right-column view).
"""


def _fav(artist, album, album_key, n=3):
    return {"artist": artist, "album": album, "album_key": album_key,
            "art": "", "track_count": n, "udn": "uuid:localfs-x",
            "added_at": 1}


def test_star_is_first_in_letter_bar(app):
    first = app.locator("#letter-bar .letter-btn").first.text_content()
    assert first == "⭐", f"first letter-bar entry should be ⭐, got {first!r}"


def test_star_shows_favourite_albums(page, stub, gateway):
    gateway.album_favourites = [_fav("Various Artists", "80s Comp", "VA/80s"),
                                _fav("Pink Floyd", "Animals", "PF/Animals")]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelector('#letter-bar .letter-btn') !== null",
        timeout=5000)
    page.locator("#letter-bar .letter-btn", has_text="⭐").click()
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length === 2",
        timeout=4000)
    txt = page.locator("#item-list").text_content()
    assert "80s Comp" in txt and "Animals" in txt


def test_clicking_fav_album_opens_by_album_key(page, stub, gateway):
    gateway.album_favourites = [_fav("Various Artists", "80s Comp", "VA/80s")]
    gateway.album_tracks_by_key["VA/80s"] = [
        {"url": "http://x/1", "title": "S1", "artist": "P1", "album": "O1",
         "type": "audio", "id": "1", "mime": "audio/flac"}]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelector('#letter-bar .letter-btn') !== null",
        timeout=5000)
    page.locator("#letter-bar .letter-btn", has_text="⭐").click()
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length === 1",
        timeout=4000)
    gateway.clear_requests()
    page.locator("#item-list .row", has_text="80s Comp").first.click()
    req = gateway.wait_for_request(
        "/api/album_tracks", timeout=2.0,
        match=lambda r: r["query"].get("album_key") == "VA/80s")
    assert req is not None, "fav album must open via album_key"


def test_empty_favourites_shows_message(page, stub, gateway):
    gateway.album_favourites = []
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelector('#letter-bar .letter-btn') !== null",
        timeout=5000)
    page.locator("#letter-bar .letter-btn", has_text="⭐").click()
    page.wait_for_function(
        "/No favourite albums/.test(document.getElementById('item-list').textContent)",
        timeout=4000)
