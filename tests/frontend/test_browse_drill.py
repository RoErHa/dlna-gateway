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


def test_browse_tab_after_drill_restores_letter_bar(page, stub, gateway):
    """Regression (iOS): drill artist→album, then tap Browse again (what you do
    after starting playback) — the letter bar + mode bar must come back so you
    can pick another artist. Before the fix, showTab('browse') left drillAlbum
    set, so loadBrowsePage hid the back button while letter-bar/browse-modes
    stayed hidden too → NO nav chrome at all, stuck on the album."""
    _seed_artist(gateway)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=4000)
    page.locator("#item-list .row").first.click()   # → artist albums
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=4000)
    page.locator("#item-list .row").first.click()   # → album tracks (drilled)
    page.wait_for_selector("#browse-back:visible", timeout=2000)
    assert page.locator("#letter-bar").is_hidden()   # drilled: letter bar hidden

    # Re-enter Browse (as after playback → back to Browse to pick another artist)
    page.locator("#tab-browse").click()
    page.wait_for_timeout(300)

    assert page.locator("#letter-bar").is_visible(), "letter bar must return"
    assert page.locator("#browse-modes").is_visible(), "mode bar must return"
    assert page.locator("#browse-back").is_hidden(), "back button cleared at root"


def test_play_all_button_in_drill(page, stub, gateway):
    _seed_artist(gateway)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=4000)
    page.locator("#item-list .row").first.click()
    page.wait_for_selector("#browse-play-all", timeout=2000)
    assert page.locator("#browse-play-all").is_visible()
