"""
Playwright tests for the Album Favourites feature.

Specifies the *contract* the frontend implementation must satisfy:
  • A star button (#browse-fav-album) appears in the album header ONLY
    when the album has more than one track. Single-track "albums" are
    almost always orphan tracks indexed without album metadata; we
    don't want to favourite those.
  • The star toggles between ☆ (not favourited) and ★ (favourited),
    driven by /api/album_favourites/check at album-load time and by
    /api/album_favourites/{add,remove} on click (album_key-aware).

NOTE: the right-column "⭐ Favourite Albums" browse view was removed
(2026-06-01) — its (artist, album) entries didn't survive the LocalFs
folder-album migration. Only the album-header star remains. Tests use
the shared StubGateway from conftest.
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



# ── album_key (folder-identity favourites, A2) ────────────────────

def test_star_check_uses_album_key(app, gateway):
    # A LocalFs compilation opened by album_key must check/toggle the
    # favourite by album_key, not (artist, album).
    gateway.album_tracks_by_key["VA/Comp"] = [
        {"url": "http://x/1", "title": "A", "artist": "P1", "album": "O1",
         "type": "audio", "id": "1", "mime": "audio/flac"},
        {"url": "http://x/2", "title": "B", "artist": "P2", "album": "O2",
         "type": "audio", "id": "2", "mime": "audio/flac"}]
    gateway.clear_requests()
    app.evaluate(
        "showAlbumTracks('Various Artists','Comp',null,'VA/Comp')")
    app.wait_for_function(
        "document.getElementById('browse-section-hdr').style.display !== 'none'",
        timeout=3000)
    req = gateway.wait_for_request(
        "/api/album_favourites/check", timeout=2.0,
        match=lambda r: r["query"].get("album_key") == "VA/Comp")
    assert req is not None, "fav check must carry album_key"


def test_star_add_uses_album_key(app, gateway):
    gateway.album_tracks_by_key["VA/Comp2"] = [
        {"url": "http://x/3", "title": "C", "artist": "P3", "album": "O3",
         "type": "audio", "id": "3", "mime": "audio/flac"},
        {"url": "http://x/4", "title": "D", "artist": "P4", "album": "O4",
         "type": "audio", "id": "4", "mime": "audio/flac"}]
    app.evaluate(
        "showAlbumTracks('Various Artists','Comp 2',null,'VA/Comp2')")
    app.wait_for_selector("#browse-fav-album", timeout=3000)
    gateway.clear_requests()
    app.locator("#browse-fav-album").click()
    req = gateway.wait_for_request(
        "/api/album_favourites/add", timeout=2.0,
        match=lambda r: r["query"].get("album_key") == "VA/Comp2")
    assert req is not None, "fav add must carry album_key"
