"""
Playwright tests for the Internet-radio ("📡 Stations") frontend.

Specifies the *contract* the app.js implementation must satisfy:
  • The right-column Playlists panel renders a synthetic
    "📡 Radio Stations" entry (id=radio-pl-item) directly BELOW the
    "⭐ Favourite Albums" entry.
  • Clicking it opens a radio view in #pl-tracks: a search box
    (#radio-search), genre chips (.radio-chip), and a list (#radio-list).
  • Typing debounces into GET /api/radio/search?q=… ; genre chips
    search by GET /api/radio/search?tag=… ; clearing returns to the
    favourites list.
  • Station rows (.radio-row) show the genre tags in their sub-line.
  • A search result's ☆ toggles via POST /api/radio/favourites/add
    (full station body), optimistically; a 409 reverts it and toasts
    "full". Favourite rows have a ✕ → POST /api/radio/favourites/remove.
  • Clicking a station plays it — browser output → #browser-audio src
    is /radio_stream?url=… ; UPnP output → POST /api/render_queue with
    an is_stream track.
  • While a station plays, #player switches to its radio variant:
    #seek-section hidden, #np-live shown, station name in #np-title,
    ICY now-playing polled into #np-artist; ⏮/⏭ cycle the favourites.

Tests use the shared StubGateway from conftest.
"""
from __future__ import annotations

import json


# ── Helpers ───────────────────────────────────────────────────────

def _station(uuid, name, *, stream_url=None, favicon="", codec="MP3",
             bitrate=128, country="GB", tags="rock", homepage=""):
    """A complete station dict in the gateway's normalized shape."""
    return {
        "station_uuid": uuid,
        "name":         name,
        "stream_url":   stream_url or f"http://ice.example/{uuid}",
        "homepage":     homepage,
        "favicon":      favicon,
        "codec":        codec,
        "bitrate":      bitrate,
        "country":      country,
        "tags":         tags,
    }


def _open_radio_view(page):
    """Open the right-column "📡 Radio Stations" view."""
    page.evaluate("showPlaylists()")
    page.wait_for_function("document.getElementById('radio-pl-item')",
                           timeout=2000)
    page.locator("#radio-pl-item").click()
    page.wait_for_selector("#radio-list", timeout=2000)


# ── Right-column entry ────────────────────────────────────────────

def test_radio_stations_row_present(app, gateway):
    """The synthetic "📡 Radio Stations" row must sit directly below
    "⭐ Favourite Albums" in #pl-list."""
    app.evaluate("showPlaylists()")
    app.wait_for_function("document.getElementById('radio-pl-item') !== null",
                          timeout=2000)
    ids = app.evaluate(
        "Array.from(document.querySelectorAll('#pl-list .pl-item'))"
        ".map(e => e.id)")
    assert "radio-pl-item" in ids, f"radio-pl-item missing from {ids}"
    assert "album-fav-pl-item" in ids
    assert ids.index("radio-pl-item") == ids.index("album-fav-pl-item") + 1, \
        f"Radio row must be right after Favourite Albums; got {ids}"


def test_clicking_radio_row_opens_view(app, gateway):
    _open_radio_view(app)
    assert app.locator("#radio-search").count() == 1
    assert app.locator("#radio-list").count() == 1
    assert "Radio Stations" in app.locator("#pl-panel-title").text_content()


# ── Search ────────────────────────────────────────────────────────

def test_typing_queries_search(app, gateway):
    gateway.radio_search_results = [_station("u1", "Jazz FM")]
    _open_radio_view(app)
    gateway.clear_requests()
    app.locator("#radio-search").fill("jazz")
    req = gateway.wait_for_request(
        "/api/radio/search",
        match=lambda r: r["query"].get("q") == "jazz")
    assert req is not None, "Typing must fire GET /api/radio/search?q=…"


def test_search_results_render(app, gateway):
    gateway.radio_search_results = [_station(f"u{i}", f"Station {i}")
                                    for i in range(3)]
    _open_radio_view(app)
    app.locator("#radio-search").fill("rock")
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 3", timeout=3000)


def test_clearing_search_restores_favourites(app, gateway):
    gateway.radio_favourites = [_station("f1", "Fav One"),
                                _station("f2", "Fav Two")]
    gateway.radio_search_results = [_station(f"s{i}", f"Result {i}")
                                    for i in range(3)]
    _open_radio_view(app)
    # Favourites shown first — 2 rows.
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 2", timeout=3000)
    app.locator("#radio-search").fill("x")
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 3", timeout=3000)
    # Clearing the box returns to the 2 favourites.
    app.locator("#radio-search").fill("")
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 2", timeout=3000)


def test_genre_chip_searches_by_tag(app, gateway):
    gateway.radio_search_results = [_station("u1", "Prog FM", tags="prog")]
    _open_radio_view(app)
    gateway.clear_requests()
    app.locator('.radio-chip[data-tag="prog"]').click()
    req = gateway.wait_for_request(
        "/api/radio/search",
        match=lambda r: r["query"].get("tag") == "prog")
    assert req is not None, "Genre chip must fire GET /api/radio/search?tag=…"
    app.wait_for_function(
        "document.querySelector('.radio-chip[data-tag=\"prog\"]')"
        ".dataset.active === '1'", timeout=2000)


# ── Favourite add / remove ────────────────────────────────────────

def test_add_station_from_search(app, gateway):
    gateway.radio_search_results = [_station("u1", "KEXP")]
    _open_radio_view(app)
    app.locator("#radio-search").fill("kexp")
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 1", timeout=3000)
    gateway.clear_requests()
    app.locator(".radio-star").first.click()
    req = gateway.wait_for_request("/api/radio/favourites/add", method="POST")
    assert req is not None, "☆ click must POST /api/radio/favourites/add"
    body = json.loads(req["body"])
    assert body["station_uuid"] == "u1", "Full station object must be POSTed"
    # Optimistic flip to ★.
    app.wait_for_function(
        "document.querySelector('.radio-star').dataset.fav === '1'",
        timeout=2000)


def test_add_when_full_toasts(app, gateway):
    gateway.radio_fav_full = True
    gateway.radio_search_results = [_station("u1", "Overflow FM")]
    _open_radio_view(app)
    app.locator("#radio-search").fill("o")
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 1", timeout=3000)
    app.locator(".radio-star").first.click()
    app.wait_for_function(
        "document.getElementById('toast').textContent.toLowerCase()"
        ".includes('full')", timeout=2000)
    # The optimistic ★ must revert to ☆ on the 409.
    assert app.locator(".radio-star").first.get_attribute("data-fav") == "0"


def test_favourites_list_renders(app, gateway):
    gateway.radio_favourites = [_station("f1", "BBC 6"),
                                _station("f2", "FIP")]
    _open_radio_view(app)
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 2", timeout=3000)
    # Favourite rows carry a ✕ remove control.
    assert app.locator(".radio-remove").count() == 2


def test_remove_station(app, gateway):
    gateway.radio_favourites = [_station("f1", "BBC 6")]
    _open_radio_view(app)
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 1", timeout=3000)
    gateway.clear_requests()
    app.locator(".radio-remove").first.click()
    req = gateway.wait_for_request("/api/radio/favourites/remove",
                                   method="POST")
    assert req is not None, "✕ must POST /api/radio/favourites/remove"
    assert json.loads(req["body"])["station_uuid"] == "f1"
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 0", timeout=2000)


def test_station_row_shows_genre(app, gateway):
    """Per user request: the content-type/genre must be visible on the
    station rows (the "playlist side")."""
    gateway.radio_favourites = [_station("f1", "BBC 6",
                                         tags="prog-rock,indie")]
    _open_radio_view(app)
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 1", timeout=3000)
    sub = app.locator(".radio-row .pl-track-sub").first.text_content()
    assert "prog-rock" in sub, f"Genre missing from row sub-line: {sub!r}"


# ── Playback ──────────────────────────────────────────────────────

def test_click_station_plays_browser(app, gateway):
    gateway.radio_favourites = [_station("f1", "BBC 6",
                                         stream_url="http://ice/bbc6")]
    _open_radio_view(app)
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 1", timeout=3000)
    app.locator(".radio-row .pl-track-body").first.click()
    app.wait_for_function(
        "document.getElementById('browser-audio').src.includes('/radio_stream')",
        timeout=2000)
    src = app.evaluate("document.getElementById('browser-audio').src")
    assert "url=" in src and "ice" in src


def test_click_station_plays_upnp(app, gateway):
    gateway.radio_favourites = [_station("f1", "BBC 6",
                                         stream_url="http://ice/bbc6")]
    _open_radio_view(app)
    app.wait_for_function(
        "document.querySelectorAll('.radio-row').length === 1", timeout=3000)
    # Inject a UPnP output option and select it.
    app.evaluate("""
      const s = document.getElementById('output-sel');
      const o = document.createElement('option');
      o.value = 'upnp:uuid:rend-1'; o.textContent = 'Naim';
      s.appendChild(o); s.value = 'upnp:uuid:rend-1';
    """)
    gateway.clear_requests()
    app.locator(".radio-row .pl-track-body").first.click()
    req = gateway.wait_for_request("/api/render_queue", method="POST")
    assert req is not None, "UPnP station play must POST /api/render_queue"
    track = json.loads(req["body"])["tracks"][0]
    assert track["is_stream"] is True, "Station track must carry is_stream"
    assert track["url"] == "http://ice/bbc6"


# ── Now-playing radio variant ─────────────────────────────────────

def test_now_playing_radio_layout(app, gateway):
    st = _station("f1", "BBC Radio 6", stream_url="http://ice/bbc6")
    app.evaluate(f"playStation({json.dumps(st)})")
    app.wait_for_function(
        "getComputedStyle(document.getElementById('seek-section'))"
        ".display === 'none'", timeout=2000)
    assert app.locator("#np-live").is_visible(), \
        "📻 LIVE badge must show in radio mode"
    assert "BBC Radio 6" in app.locator("#np-title").text_content()


def test_now_playing_polls_icy(app, gateway):
    gateway.icy_title = "Pink Floyd - Time"
    st = _station("f1", "BBC Radio 6", stream_url="http://ice/bbc6")
    app.evaluate(f"playStation({json.dumps(st)})")
    req = gateway.wait_for_request("/api/radio/nowplaying", method="GET")
    assert req is not None, "Radio now-playing must poll /api/radio/nowplaying"
    app.wait_for_function(
        "document.getElementById('np-artist').textContent"
        ".includes('Pink Floyd')", timeout=3000)


def test_prev_next_cycles_presets(app, gateway):
    s1 = _station("f1", "Station One", stream_url="http://ice/1")
    s2 = _station("f2", "Station Two", stream_url="http://ice/2")
    gateway.radio_favourites = [s1, s2]
    app.evaluate(f"playStation({json.dumps(s1)})")
    app.wait_for_function(
        "document.getElementById('np-title').textContent"
        ".includes('Station One')", timeout=2000)
    # ⏭ in radio mode steps to the next favourite, not a track skip.
    app.locator("#btn-next").click()
    app.wait_for_function(
        "document.getElementById('np-title').textContent"
        ".includes('Station Two')", timeout=3000)
