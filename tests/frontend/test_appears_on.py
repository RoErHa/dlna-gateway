#!/usr/bin/env python3
"""
tests/frontend/test_appears_on.py — an artist's page opens on THEIR
records.

Browsing to a performer used to list every folder holding one of their
tracks, so an artist you own two albums by had them outnumbered by six
compilations they appear on once. The compilations are now folded behind
a disclosure, ALWAYS — never remembered, so every artist opens the same
way and the page never depends on a state set days ago.

Run standalone:
    .venv/bin/pytest tests/frontend/test_appears_on.py -v
"""


def _seed(gateway):
    """Two of their own records, three compilations they guest on."""
    gateway.artists = [{"artist": "The Band", "album_count": 5}]
    gateway.artist_albums["The Band"] = [
        {"artist": "The Band", "album": "Their First", "album_key": "B/First",
         "track_count": 9, "folder_tracks": 9, "folder_artists": 1,
         "own": True, "art": ""},
        {"artist": "The Band", "album": "Their Second", "album_key": "B/Second",
         "track_count": 11, "folder_tracks": 11, "folder_artists": 1,
         "own": True, "art": ""},
        {"artist": "The Band", "album": "Big Comp", "album_key": "VA/Big",
         "track_count": 1, "folder_tracks": 67, "folder_artists": 54,
         "own": False, "art": ""},
        {"artist": "The Band", "album": "Another Comp", "album_key": "VA/Other",
         "track_count": 1, "folder_tracks": 98, "folder_artists": 93,
         "own": False, "art": ""},
        {"artist": "The Band", "album": "Third Comp", "album_key": "VA/Third",
         "track_count": 2, "folder_tracks": 40, "folder_artists": 30,
         "own": False, "art": ""},
    ]


def _open_artist(app, gateway):
    _seed(gateway)
    app.reload()
    app.wait_for_selector("#item-list", state="attached")
    app.evaluate("""() => showArtistAlbums({artist:"The Band", album_count:5})""")
    app.wait_for_selector(".appears-on", state="attached")


def test_own_albums_are_visible_immediately(app, gateway):
    _open_artist(app, gateway)
    titles = app.eval_on_selector_all(
        "#item-list > .row .row-title", "els => els.map(e => e.textContent)")
    assert titles == ["Their First", "Their Second"]


def test_compilations_are_folded_away(app, gateway):
    """The whole point: they must not be on screen until asked for."""
    _open_artist(app, gateway)
    assert app.locator(".appears-on").get_attribute("open") is None
    assert not app.locator(".ao-list .row-title", has_text="Big Comp").first.is_visible()


def test_the_summary_counts_them(app, gateway):
    _open_artist(app, gateway)
    # The label is uppercased by CSS, so compare case-insensitively —
    # asserting the rendered casing would just pin a style choice.
    txt = app.locator(".appears-on summary").inner_text().lower()
    assert "3 compilations" in txt and "appears on" in txt


def test_opening_it_reveals_them(app, gateway):
    _open_artist(app, gateway)
    app.locator(".appears-on summary").click()
    app.wait_for_timeout(200)
    titles = app.eval_on_selector_all(
        ".ao-list .row-title", "els => els.map(e => e.textContent)")
    assert titles == ["Big Comp", "Another Comp", "Third Comp"]


def test_an_appearance_shows_its_share_of_the_folder(app, gateway):
    """"1 track of 67" says compilation before the title is read."""
    _open_artist(app, gateway)
    app.locator(".appears-on summary").click()
    app.wait_for_timeout(200)
    sub = app.locator(".ao-list .row-sub").first.inner_text()
    assert "1 track" in sub and "of 67" in sub


def test_it_reopens_folded_for_the_next_artist(app, gateway):
    """Never remembered — an artist page must not open in a state set
    days ago and forgotten."""
    _open_artist(app, gateway)
    app.locator(".appears-on summary").click()
    app.wait_for_timeout(150)
    assert app.locator(".appears-on").get_attribute("open") is not None
    app.evaluate("""() => showArtistAlbums({artist:"The Band", album_count:5})""")
    app.wait_for_selector(".appears-on", state="attached")
    app.wait_for_timeout(150)
    assert app.locator(".appears-on").get_attribute("open") is None


def test_an_artist_with_no_compilations_gets_no_disclosure(app, gateway):
    gateway.artists = [{"artist": "Solo Act", "album_count": 1}]
    gateway.artist_albums["Solo Act"] = [
        {"artist": "Solo Act", "album": "Only LP", "album_key": "S/Only",
         "track_count": 8, "folder_tracks": 8, "folder_artists": 1,
         "own": True, "art": ""}]
    app.reload()
    app.wait_for_selector("#item-list", state="attached")
    app.evaluate("""() => showArtistAlbums({artist:"Solo Act", album_count:1})""")
    app.wait_for_selector("#item-list .row", state="attached")
    assert app.locator(".appears-on").count() == 0


def test_a_source_that_omits_own_hides_nothing(app, gateway):
    """A non-localfs source sends no `own` field. Everything must show as
    theirs rather than silently vanishing into a fold."""
    gateway.artists = [{"artist": "Legacy", "album_count": 2}]
    gateway.artist_albums["Legacy"] = [
        {"artist": "Legacy", "album": "One", "album_key": "", "track_count": 3,
         "art": ""},
        {"artist": "Legacy", "album": "Two", "album_key": "", "track_count": 4,
         "art": ""}]
    app.reload()
    app.wait_for_selector("#item-list", state="attached")
    app.evaluate("""() => showArtistAlbums({artist:"Legacy", album_count:2})""")
    app.wait_for_selector("#item-list .row", state="attached")
    assert app.locator(".appears-on").count() == 0
    assert app.locator("#item-list > .row").count() == 2


def test_an_artist_with_only_appearances_gets_them_directly(app, gateway):
    """Folding their only music behind a disclosure would give them a
    page holding one collapsed row and nothing else. The fold stops
    compilations burying real albums; here there are none to bury."""
    gateway.artists = [{"artist": "Guest Only", "album_count": 2}]
    gateway.artist_albums["Guest Only"] = [
        {"artist": "Guest Only", "album": "Comp A", "album_key": "VA/A",
         "track_count": 1, "folder_tracks": 50, "folder_artists": 40,
         "own": False, "art": ""},
        {"artist": "Guest Only", "album": "Comp B", "album_key": "VA/B",
         "track_count": 1, "folder_tracks": 30, "folder_artists": 25,
         "own": False, "art": ""}]
    app.reload()
    app.wait_for_selector("#item-list", state="attached")
    app.evaluate("""() => showArtistAlbums({artist:"Guest Only", album_count:2})""")
    app.wait_for_selector("#item-list .row", state="attached")
    assert app.locator(".appears-on").count() == 0
    titles = app.eval_on_selector_all(
        "#item-list > .row .row-title", "els => els.map(e => e.textContent)")
    assert titles == ["Comp A", "Comp B"]
