"""
Playwright tests for the Album Favourites feature.

Specifies the *contract* the frontend implementation must satisfy:
  • A star button (#browse-fav-album) appears in the album header ONLY
    when the album has more than one track. Single-track "albums" are
    almost always orphan tracks indexed without album metadata; we
    don't want to favourite those.
  • The star toggles between ☆ (not favourited) and ★ (favourited),
    driven by /api/album_favourites/check at album-load time and by
    /api/album_favourites/{add,remove} on click.
  • The right-column Playlists panel renders a synthetic
    "⭐ Favourite Albums" entry (id=album-fav-pl-item) at the top —
    above the existing "⭐ Favourites" track-level playlist.
  • Clicking that entry opens a list view of favourited albums with
    rows of class `album-fav-row` (data-artist / data-album).
  • Clicking an album row navigates to that album's tracks
    (same showAlbumTracks() flow as drilling in via Browse).

Tests use the shared StubGateway from conftest.
"""
from __future__ import annotations

import pytest


# ── Helpers ───────────────────────────────────────────────────────

def _seed_multi_track_album(gw, artist="Pink Floyd", album="Animals",
                            n_tracks=5):
    """Seed an album with N tracks so the star button is eligible
    (track_count > 1)."""
    gw.add_artist(artist, album_count=1, track_count=n_tracks)
    gw.add_album(artist, album, track_count=n_tracks, art="")
    for i in range(n_tracks):
        gw.add_track(artist, album, f"Side {i+1}",
                     url=f"http://stub/{artist}/{album}/t{i}.flac")


def _seed_single_track_album(gw, artist="Lone", album="OnlyOne"):
    """Seed an 'album' with exactly one track — the star must NOT show."""
    gw.add_artist(artist, album_count=1, track_count=1)
    gw.add_album(artist, album, track_count=1, art="")
    gw.add_track(artist, album, "Only Track",
                 url=f"http://stub/{artist}/{album}/only.flac")


def _navigate_to_album(page, artist, album):
    """Drive the UI down to the album-tracks view by calling the
    function that the HTML buttons call. Avoids brittle multi-click
    chains through letter-bar / artist drill-down."""
    page.evaluate(f"showAlbumTracks({artist!r}, {album!r})")
    # Wait for the album header to appear — the most reliable signal
    # that showAlbumTracks() has finished loading tracks.
    page.wait_for_function(
        "document.getElementById('browse-section-hdr').style.display !== 'none'",
        timeout=3000)


# ── Star button gating ───────────────────────────────────────────

def test_star_button_visible_for_multi_track_album(app, gateway):
    _seed_multi_track_album(gateway)
    _navigate_to_album(app, "Pink Floyd", "Animals")
    star = app.locator("#browse-fav-album")
    assert star.count() == 1, "Star button must exist in the album header"
    assert star.is_visible(), "Star button must be visible for >1-track album"


def test_star_button_hidden_for_single_track_album(app, gateway):
    """Per user spec: the star is gated on track_count > 1.
    Single-track 'albums' are usually metadata-less orphans and
    shouldn't be favouritable at all."""
    _seed_single_track_album(gateway)
    _navigate_to_album(app, "Lone", "OnlyOne")
    star = app.locator("#browse-fav-album")
    # Either not in the DOM yet, or in the DOM but display:none.
    if star.count():
        assert not star.is_visible(), \
            "Star button must NOT be visible for single-track album"


def test_star_initial_state_unfavourited(app, gateway):
    _seed_multi_track_album(gateway)
    _navigate_to_album(app, "Pink Floyd", "Animals")
    star = app.locator("#browse-fav-album")
    # Empty star icon and data-fav="0"
    assert star.get_attribute("data-fav") == "0"
    assert "☆" in star.text_content()


def test_star_initial_state_favourited(app, gateway):
    """If the album is already favourited, the star should load filled
    (the frontend hits /api/album_favourites/check on album open)."""
    _seed_multi_track_album(gateway)
    gateway.album_favourites.append({
        "artist": "Pink Floyd", "album": "Animals",
        "art": "", "track_count": 5,
        "udn": gateway.servers[0]["udn"], "added_at": 0,
    })
    _navigate_to_album(app, "Pink Floyd", "Animals")
    star = app.locator("#browse-fav-album")
    # Wait for the check round-trip to update the UI.
    app.wait_for_function(
        "document.getElementById('browse-fav-album')"
        " && document.getElementById('browse-fav-album').dataset.fav === '1'",
        timeout=3000)
    assert "★" in star.text_content()


# ── Star click toggles ───────────────────────────────────────────

def test_click_unfavourited_star_calls_add(app, gateway):
    _seed_multi_track_album(gateway)
    _navigate_to_album(app, "Pink Floyd", "Animals")
    gateway.clear_requests()
    app.locator("#browse-fav-album").click()
    req = gateway.wait_for_request("/api/album_favourites/add",
                                   match=lambda r: r["query"].get("artist") == "Pink Floyd"
                                                   and r["query"].get("album") == "Animals")
    assert req is not None, "Click must trigger /api/album_favourites/add"
    # After the add, the UI reflects the new state.
    app.wait_for_function(
        "document.getElementById('browse-fav-album').dataset.fav === '1'",
        timeout=2000)


def test_click_favourited_star_calls_remove(app, gateway):
    _seed_multi_track_album(gateway)
    gateway.album_favourites.append({
        "artist": "Pink Floyd", "album": "Animals",
        "art": "", "track_count": 5,
        "udn": gateway.servers[0]["udn"], "added_at": 0,
    })
    _navigate_to_album(app, "Pink Floyd", "Animals")
    # Wait for the check round-trip to flip to ★ before we click.
    app.wait_for_function(
        "document.getElementById('browse-fav-album').dataset.fav === '1'",
        timeout=3000)
    gateway.clear_requests()
    app.locator("#browse-fav-album").click()
    req = gateway.wait_for_request("/api/album_favourites/remove",
                                   match=lambda r: r["query"].get("album") == "Animals")
    assert req is not None, "Click must trigger /api/album_favourites/remove"
    app.wait_for_function(
        "document.getElementById('browse-fav-album').dataset.fav === '0'",
        timeout=2000)


# ── Right-column Playlists list ──────────────────────────────────

def test_playlists_panel_shows_favourite_albums_first(app, gateway):
    """Per user spec: the synthetic "⭐ Favourite Albums" item must be
    the FIRST entry in the right-column playlists list — above the
    existing "⭐ Favourites" track-level playlist and any user
    playlists."""
    gateway.add_playlist("pl-1", "My Mixtape", [])
    # Trigger the playlists tab refresh (also runs at boot).
    app.evaluate("showPlaylists()")
    app.wait_for_function(
        "document.querySelectorAll('#pl-list .pl-item').length >= 2",
        timeout=2000)
    items = app.locator("#pl-list .pl-item")
    first_text = items.nth(0).text_content()
    assert "Favourite Albums" in first_text, \
        f"First item must be 'Favourite Albums', got: {first_text!r}"
    # Specific id we rely on for click handler routing.
    assert app.locator("#album-fav-pl-item").count() == 1


def test_clicking_favourite_albums_item_opens_album_list(app, gateway):
    gateway.album_favourites.append({
        "artist": "Pink Floyd", "album": "Animals",
        "art": "", "track_count": 5,
        "udn": gateway.servers[0]["udn"], "added_at": 0,
    })
    gateway.album_favourites.append({
        "artist": "Beatles", "album": "Abbey Road",
        "art": "", "track_count": 17,
        "udn": gateway.servers[0]["udn"], "added_at": 0,
    })
    app.evaluate("showPlaylists()")
    app.wait_for_function(
        "document.getElementById('album-fav-pl-item') !== null",
        timeout=2000)
    gateway.clear_requests()
    app.locator("#album-fav-pl-item").click()
    # The frontend should fetch the list…
    req = gateway.wait_for_request("/api/album_favourites", method="GET",
                                   match=lambda r: r["path"] == "/api/album_favourites")
    assert req is not None
    # …and render rows.
    app.wait_for_function(
        "document.querySelectorAll('.album-fav-row').length === 2",
        timeout=2000)
    rows = app.locator(".album-fav-row")
    texts = [rows.nth(i).text_content() for i in range(rows.count())]
    assert any("Animals" in t for t in texts)
    assert any("Abbey Road" in t for t in texts)


def test_click_album_fav_row_navigates_to_album(app, gateway):
    """Clicking one of the favourited albums in the right-column list
    must drill into that album's tracks view — same destination as the
    Browse → Artist → Album path."""
    gateway.album_favourites.append({
        "artist": "Pink Floyd", "album": "Animals",
        "art": "", "track_count": 5,
        "udn": gateway.servers[0]["udn"], "added_at": 0,
    })
    # Seed the album_tracks for the navigation target.
    _seed_multi_track_album(gateway, artist="Pink Floyd", album="Animals")
    app.evaluate("showPlaylists()")
    app.wait_for_function(
        "document.getElementById('album-fav-pl-item') !== null",
        timeout=2000)
    app.locator("#album-fav-pl-item").click()
    app.wait_for_function(
        "document.querySelectorAll('.album-fav-row').length === 1",
        timeout=2000)
    gateway.clear_requests()
    app.locator(".album-fav-row").first.click()
    # Frontend must fetch tracks for the album we clicked.
    req = gateway.wait_for_request(
        "/api/album_tracks",
        match=lambda r: r["query"].get("album") == "Animals")
    assert req is not None, "Clicking a favourite-album row must fetch its tracks"
    # And the album-tracks header should be visible.
    app.wait_for_function(
        "document.getElementById('browse-section-hdr').style.display !== 'none'",
        timeout=3000)


# ── Back-navigation invariants (regression for the Browse-stuck bug) ──

def test_back_from_fav_album_returns_to_favourites_view(app, gateway):
    """Regression for the bug where back from a fav-drilled album
    landed in a bogus "<artist>'s albums" view (pollution of the
    Browse drill stack). Back must return the user to the right-
    column Favourite Albums list."""
    gateway.album_favourites.append({
        "artist": "Pink Floyd", "album": "Animals",
        "art": "", "track_count": 5,
        "udn": gateway.servers[0]["udn"], "added_at": 0,
    })
    _seed_multi_track_album(gateway, artist="Pink Floyd", album="Animals")
    app.evaluate("showPlaylists()")
    app.wait_for_function(
        "document.getElementById('album-fav-pl-item') !== null",
        timeout=2000)
    app.locator("#album-fav-pl-item").click()
    app.wait_for_function(
        "document.querySelectorAll('.album-fav-row').length === 1",
        timeout=2000)
    app.locator(".album-fav-row").first.click()
    # Wait for the album view to render
    app.wait_for_function(
        "document.getElementById('browse-section-hdr').style.display !== 'none'",
        timeout=3000)
    # Click the back affordance in the album header.
    app.evaluate("drillBack()")
    # We must end up back in the favourites list (rows visible again),
    # NOT in some artist-albums view.
    app.wait_for_function(
        "document.querySelectorAll('.album-fav-row').length === 1",
        timeout=2000)


def test_fav_album_click_does_not_leak_letter_bar_items(app, gateway):
    """Regression for the bug where clicking a favourite-album row
    would race against loadBrowsePage() (kicked off by mobileTab→
    showTab→loadBrowsePage). The losing race appended the album
    tracks BELOW a fresh letter-bar listing of artists/albums/tracks.
    Symptom: many rows + the actual album tracks at the bottom.
    Fix: don't call mobileTab inside _showFavAlbumTracks — swap the
    body class directly so no second fetch is initiated.
    """
    # Pre-populate the letter-bar so loadBrowsePage(), if it were
    # called, would render NON-album items into #item-list. Without
    # the fix, the race appends album tracks below these.
    for i in range(8):
        gateway.add_artist(f"Artist {i}", album_count=1, track_count=3)
    # Prime browse by drilling once via the regular flow — this leaves
    # rendered artist rows in #item-list, the same state a user reaches
    # after browsing before clicking a favourite.
    app.evaluate("loadBrowsePage()")
    app.wait_for_function(
        "document.querySelectorAll('#item-list .row').length >= 8",
        timeout=2000)

    # Now wire up a favourite + its tracks and click it.
    gateway.album_favourites.append({
        "artist": "Pink Floyd", "album": "Animals",
        "art": "", "track_count": 5,
        "udn": gateway.servers[0]["udn"], "added_at": 0,
    })
    _seed_multi_track_album(gateway, artist="Pink Floyd", album="Animals",
                            n_tracks=5)
    app.evaluate("showPlaylists()")
    app.wait_for_function(
        "document.getElementById('album-fav-pl-item') !== null",
        timeout=2000)
    app.locator("#album-fav-pl-item").click()
    app.wait_for_function(
        "document.querySelectorAll('.album-fav-row').length === 1",
        timeout=2000)
    app.locator(".album-fav-row").first.click()

    # Wait for the album view to load.
    app.wait_for_function(
        "document.getElementById('browse-section-hdr').style.display !== 'none'",
        timeout=3000)
    # Give any racing fetch a moment to land — its rows MUST NOT appear.
    app.wait_for_timeout(300)

    # Exactly the album's tracks — no leftover artist/album rows from
    # the pre-populated letter-bar listing.
    n = app.locator("#item-list .row").count()
    assert n == 5, (
        f"Expected exactly 5 album tracks in #item-list, got {n}. "
        "Race regression: loadBrowsePage() leaked rows underneath "
        "the album tracks.")


def test_browse_tab_chrome_recovers_after_fav_album_back(app, gateway):
    """Regression for "Browse does not work after selecting an album
    to play from the album favourites": after the user goes through
    the favourites flow and clicks back, the Browse tab must show
    the letter bar + browse-modes again (drillAlbum cleared)."""
    gateway.album_favourites.append({
        "artist": "Pink Floyd", "album": "Animals",
        "art": "", "track_count": 5,
        "udn": gateway.servers[0]["udn"], "added_at": 0,
    })
    _seed_multi_track_album(gateway, artist="Pink Floyd", album="Animals")
    app.evaluate("showPlaylists()")
    app.wait_for_function(
        "document.getElementById('album-fav-pl-item') !== null",
        timeout=2000)
    app.locator("#album-fav-pl-item").click()
    app.wait_for_function(
        "document.querySelectorAll('.album-fav-row').length === 1",
        timeout=2000)
    app.locator(".album-fav-row").first.click()
    app.wait_for_function(
        "document.getElementById('browse-section-hdr').style.display !== 'none'",
        timeout=3000)
    app.evaluate("drillBack()")
    # Now click Browse tab — letter bar + modes must be visible again.
    app.evaluate("showTab('browse')")
    app.wait_for_function(
        "document.getElementById('letter-bar').style.display !== 'none' && "
        "document.getElementById('browse-modes').style.display !== 'none'",
        timeout=2000)
