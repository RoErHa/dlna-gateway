"""Drill-down navigation: letter → artist → album → tracks."""


def _seed_artist(gateway, artist="ABBA", album="Arrival"):
    gateway.add_artist(artist, album_count=1, track_count=10)
    gateway.add_album(artist, album, track_count=10)
    gateway.artist_albums[artist] = [
        {"artist": artist, "album": album, "track_count": 10, "art": ""}
    ]
    for i in range(3):
        gateway.add_track(artist, album, f"Track {i+1}")


def test_click_artist_drills_in(page, stub, gateway):
    _seed_artist(gateway)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=4000)
    page.locator("#item-list .row").first.click()
    page.wait_for_selector("#browse-back:visible", timeout=2000)
    # Mode bar hidden in drill-down
    assert page.locator("#browse-modes").is_hidden()


def test_back_from_album_returns_to_artist(page, stub, gateway):
    _seed_artist(gateway)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=4000)
    page.locator("#item-list .row").first.click()  # → artist's albums
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=4000)
    page.locator("#item-list .row").first.click()  # → album tracks
    page.wait_for_selector("#browse-back:visible", timeout=2000)
    page.locator("#browse-back span").first.click()  # ← back
    # Back to artist's albums — back button still visible
    page.wait_for_timeout(300)
    assert page.locator("#browse-back").is_visible()


def test_back_from_artist_returns_to_letter_view(page, stub, gateway):
    _seed_artist(gateway)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=4000)
    page.locator("#item-list .row").first.click()
    page.wait_for_selector("#browse-back:visible", timeout=2000)
    page.locator("#browse-back span").first.click()
    page.wait_for_timeout(300)
    # Back hidden, mode bar visible again
    assert page.locator("#browse-back").is_hidden()
    assert page.locator("#browse-modes").is_visible()


def test_play_all_button_in_drill(page, stub, gateway):
    _seed_artist(gateway)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=4000)
    page.locator("#item-list .row").first.click()
    page.wait_for_selector("#browse-play-all", timeout=2000)
    assert page.locator("#browse-play-all").is_visible()
