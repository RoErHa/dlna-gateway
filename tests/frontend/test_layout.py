"""Per-device layout + the album cover grid (2026-08-07 redesign).

The app had ONE breakpoint (768px): phone below, desktop three-column above.
Two very common situations fell on the wrong side of it, and both are
guarded here:

  • iPad upright (820–834px) took the desktop layout, where #browser 360px +
    #pl-panel 260px = 620px of fixed chrome left the player about 200px —
    too narrow for the six transport buttons.
  • Every iPhone on its side is 852px wide (932 on a Pro Max), so it also
    took the desktop layout: three columns and a status bar inside 393px of
    height. The rule that catches it is keyed on HEIGHT, not width, so it
    works on any model and never catches an iPad (1024×768 sideways).

Plus the album cover grid, whose failure mode is subtle enough to deserve a
dedicated test: a cover sized `width:100%` + `aspect-ratio:1` contributes no
intrinsic HEIGHT, so inside a bounded scroll container the auto grid rows got
squeezed to share the visible height and every title spilled onto the card
below — but only once there was MORE THAN ONE ROW of cards. A single-row
check would have passed while the real thing was broken.
"""
import pytest

from tests.frontend.conftest import _boot


ALBUMS = [
    ("Miles Davis", "A Kind of Blue", 5),
    ("Brian Eno", "Ambient 1: Music for Airports", 4),
    ("The Beatles", "Abbey Road", 17),
    ("Pink Floyd", "Animals", 5),
    ("Radiohead", "Amnesiac", 11),
    ("Bill Evans", "Alone", 6),
    ("Nick Drake", "A Skin Too Few", 9),
    ("Steely Dan", "Aja", 7),
]


def _sized(page, stub, gateway, w, h, albums=False):
    """Boot the app at an exact viewport. Seeds a letter-A album shelf when
    the test needs the grid — enough albums to make MORE THAN ONE row."""
    if albums:
        for artist, album, n in ALBUMS:
            gateway.add_album(artist, album, n)
    page.set_viewport_size({"width": w, "height": h})
    _boot(page, stub)
    return page


def _box(page, sel):
    return page.evaluate(
        """(sel) => {
             const el = document.querySelector(sel);
             if (!el) return null;
             const cs = getComputedStyle(el);
             if (cs.display === 'none') return null;
             const b = el.getBoundingClientRect();
             return {w: Math.round(b.width), h: Math.round(b.height)};
           }""", sel)


def _albums_mode(page):
    page.evaluate("setBrowseMode('albums')")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=5000)
    page.wait_for_timeout(250)


# ── iPad upright — the worst case before the redesign ─────────────


def test_ipad_portrait_is_two_columns(page, stub, gateway):
    _sized(page, stub, gateway, 820, 1180)
    assert _box(page, "#pl-panel") is None, \
        "the playlist column must not hold width on an upright iPad"
    player = _box(page, "#player")
    assert player and player["w"] >= 380, \
        f"player got {player and player['w']}px — the ~200px squeeze is back"
    assert _box(page, "#browser"), "browse pane must still be visible"


def test_ipad_portrait_exposes_playlist_tabs(page, stub, gateway):
    _sized(page, stub, gateway, 820, 1180)
    assert _box(page, "#tab-playlists"), \
        "with no playlist column, Playlists needs a tab to reach it"
    assert _box(page, "#tab-favourites")


def test_ipad_portrait_playlist_tab_swaps_the_pane(page, stub, gateway):
    _sized(page, stub, gateway, 820, 1180)
    page.click("#tab-playlists")
    page.wait_for_timeout(250)
    assert _box(page, "#pl-panel"), "Playlists tab must reveal the pane"
    assert _box(page, "#browser") is None, "…in place of the browse pane"
    page.click("#tab-browse")
    page.wait_for_timeout(250)
    assert _box(page, "#browser"), "Browse tab must bring the browse pane back"


def test_desktop_keeps_three_columns_and_hides_tablet_tabs(page, stub, gateway):
    _sized(page, stub, gateway, 1440, 900)
    assert _box(page, "#browser") and _box(page, "#player") and _box(page, "#pl-panel"), \
        "all three panes belong on a desktop width"
    assert _box(page, "#tab-playlists") is None, \
        "the pane is always on screen here — the tab would be redundant"


def test_desktop_sidebars_are_fluid(page, stub, gateway):
    """They were frozen at 360/260px at every width, so extra screen went
    into empty space around the cover rather than into content."""
    _sized(page, stub, gateway, 1280, 800)
    narrow = _box(page, "#browser")["w"]
    page.set_viewport_size({"width": 1800, "height": 900})
    page.wait_for_timeout(250)
    wide = _box(page, "#browser")["w"]
    assert wide > narrow, f"browse pane stayed {narrow}px from 1280→1800"


# ── Phone on its side — was the full desktop layout in 393px ──────


def test_landscape_phone_drops_desktop_chrome(page, stub, gateway):
    _sized(page, stub, gateway, 852, 393)
    assert _box(page, "#statusbar") is None, "no height to spend on a status bar"
    assert _box(page, ".tab-bar") is None
    assert _box(page, "header")["h"] <= 48, "header must be compact when short"


def test_landscape_phone_shows_list_beside_player(page, stub, gateway):
    _sized(page, stub, gateway, 852, 393)
    browser, player = _box(page, "#browser"), _box(page, "#player")
    assert browser and player, "both panes should be on screen at once"
    assert browser["w"] > 200 and player["w"] > 200, \
        f"panes too narrow: browse={browser['w']} player={player['w']}"


def test_landscape_phone_nav_is_a_side_rail(page, stub, gateway):
    """A rail, not a bottom bar — vertical space is the scarce resource.
    It also must not steal the panes' width: #bottom-nav is a SIBLING of
    .workspace, so a naive position:static dropped it into the body's
    column flow below the panes and squeezed them to nothing."""
    _sized(page, stub, gateway, 852, 393)
    nav = _box(page, "#bottom-nav")
    assert nav, "navigation must still be reachable"
    assert nav["w"] <= 80, f"rail should be narrow, got {nav['w']}px"
    assert nav["h"] > 200, f"rail should be tall, got {nav['h']}px"
    ws_left = page.evaluate(
        "Math.round(document.querySelector('.workspace').getBoundingClientRect().left)")
    assert ws_left >= nav["w"] - 1, \
        f"workspace starts at {ws_left}px — the rail is overlapping the panes"


def test_landscape_rule_does_not_catch_a_sideways_ipad(page, stub, gateway):
    """An iPad sideways is 1024×768 — tall enough that it must keep the
    full desktop layout, status bar and all."""
    _sized(page, stub, gateway, 1180, 820)
    assert _box(page, "#statusbar"), "an iPad sideways is not a landscape phone"
    assert _box(page, "#pl-panel"), "…and keeps its three columns"


# ── Phone upright — header on one row ─────────────────────────────


def test_phone_header_is_one_row(page, stub, gateway):
    """It used to wrap: pickers on row 1, a full-width search field on row 2.
    Together with the mode bar and letter bar that left the list barely half
    the screen."""
    _sized(page, stub, gateway, 393, 852)
    assert _box(page, "header")["h"] <= 70, \
        "header wrapped to a second row again"
    assert _box(page, "#search-toggle"), "search collapses to a button here"
    assert _box(page, "#search-input") is None, "…and the field starts hidden"


def test_phone_search_toggle_expands_and_collapses(page, stub, gateway):
    _sized(page, stub, gateway, 393, 852)
    page.click("#search-toggle")
    page.wait_for_timeout(200)
    assert _box(page, "#search-input"), "tapping 🔍 must reveal the field"
    page.click("#search-toggle")
    page.wait_for_timeout(200)
    assert _box(page, "#search-input") is None, "tapping again must collapse it"


def test_phone_bottom_nav_search_opens_the_field(page, stub, gateway):
    """Arriving at Search from the bottom nav has to open the field too —
    otherwise you land on a search screen with nothing to type into."""
    _sized(page, stub, gateway, 393, 852)
    page.click("#bnav-search")
    page.wait_for_timeout(250)
    assert _box(page, "#search-input"), "bottom-nav Search must reveal the field"


def test_no_horizontal_scroll_anywhere(page, stub, gateway):
    for w, h in [(375, 667), (393, 852), (852, 393), (820, 1180), (1180, 820), (1440, 900)]:
        _sized(page, stub, gateway, w, h)
        overflows = page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth")
        assert not overflows, f"page scrolls sideways at {w}×{h}"


# ── Album cover grid ──────────────────────────────────────────────


def test_albums_render_as_a_grid(page, stub, gateway):
    _sized(page, stub, gateway, 820, 1180, albums=True)
    _albums_mode(page)
    assert page.evaluate(
        "document.getElementById('item-list').classList.contains('grid')")
    cols = page.evaluate(
        "getComputedStyle(document.getElementById('item-list'))"
        ".gridTemplateColumns.split(' ').length")
    assert cols >= 2, f"only {cols} column(s) of covers on an upright iPad"


def test_artists_stay_a_list(page, stub, gateway):
    """Artists and tracks are text-first — a cover adds nothing."""
    page.set_viewport_size({"width": 1280, "height": 800})
    gateway.add_artist("Aphex Twin", 4, 40)
    _boot(page, stub)
    page.evaluate("setBrowseMode('artists')")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=5000)
    assert not page.evaluate(
        "document.getElementById('item-list').classList.contains('grid')")


def test_switching_off_albums_clears_the_grid(page, stub, gateway):
    _sized(page, stub, gateway, 820, 1180, albums=True)
    gateway.add_artist("Aphex Twin", 4, 40)
    _albums_mode(page)
    page.evaluate("setBrowseMode('artists')")
    page.wait_for_timeout(600)
    assert not page.evaluate(
        "document.getElementById('item-list').classList.contains('grid')"), \
        "the grid layout leaked into the artists list"


@pytest.mark.parametrize("w,h", [(393, 852), (852, 393), (820, 1180), (1440, 900)])
def test_grid_cards_never_overflow(page, stub, gateway, w, h):
    """THE regression. With `width:100%` + `aspect-ratio:1` the covers give
    the grid no intrinsic height, so the auto rows were squeezed to share the
    scroll container's visible height — 114px rows around 129px covers, every
    title spilling onto the card below. It only showed once the cards needed
    more than one row, which is why the seed above has eight albums.
    Fixed with grid-auto-rows:max-content."""
    _sized(page, stub, gateway, w, h, albums=True)
    _albums_mode(page)
    bad = page.evaluate("""() => {
      const rows = [...document.querySelectorAll('#item-list .row')];
      return rows.filter(r => {
        const b = r.getBoundingClientRect();
        return [...r.children].some(c => c.getBoundingClientRect().bottom > b.bottom + 1);
      }).length;
    }""")
    assert bad == 0, f"{bad} cover card(s) overflow their own box at {w}×{h}"
    rows_seen = page.evaluate(
        "new Set([...document.querySelectorAll('#item-list .row')]"
        ".map(r => Math.round(r.getBoundingClientRect().top))).size")
    assert rows_seen >= 2, \
        "seed did not produce a second row — this test would pass vacuously"


def test_grid_covers_are_square(page, stub, gateway):
    _sized(page, stub, gateway, 820, 1180, albums=True)
    _albums_mode(page)
    wh = page.evaluate("""() => {
      const c = document.querySelector('#item-list .row .row-icon, #item-list .row .row-art');
      const b = c.getBoundingClientRect();
      return [Math.round(b.width), Math.round(b.height)];
    }""")
    assert abs(wh[0] - wh[1]) <= 1, f"cover is {wh[0]}×{wh[1]}, not square"


def test_grid_play_button_is_visible_on_touch(page, stub, gateway):
    """No hover on a touch screen — the control has to be visible to be used."""
    _sized(page, stub, gateway, 393, 852, albums=True)
    _albums_mode(page)
    assert _box(page, "#item-list .row .row-actions"), \
        "the ▶ overlay must not depend on hover"


# ── Palette ───────────────────────────────────────────────────────


def test_navy_palette_tokens(page, stub, gateway):
    """--ink-dim carries every artist line, duration, count and label. At the
    old warm #9a907f it sat at 6.2:1 against the ground, which is what washed
    out in sunlight; the navy set puts it at 10.8:1."""
    _sized(page, stub, gateway, 1280, 800)
    tok = page.evaluate("""() => {
      const cs = getComputedStyle(document.documentElement);
      const g = n => cs.getPropertyValue(n).trim().toUpperCase();
      return {bg: g('--bg'), ink: g('--ink'), dim: g('--ink-dim'), amber: g('--amber')};
    }""")
    assert tok["bg"] == "#0A1526", f"background is {tok['bg']}, not the navy"
    assert tok["dim"] == "#B4C9E6", f"--ink-dim is {tok['dim']} — daylight fix reverted?"
    assert tok["ink"] == "#F4F8FF"
    assert tok["amber"] == "#FFC24A"


def test_body_actually_paints_the_navy(page, stub, gateway):
    """A token nobody applies is not a repaint."""
    _sized(page, stub, gateway, 1280, 800)
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") \
        == "rgb(10, 21, 38)"


# ── /art size buckets ─────────────────────────────────────────────
# The PWA used to ask for the FULL-resolution cover everywhere: a 36px list
# thumbnail and a 130px grid card both pulled the multi-MB embedded original.
# Measured on the real library, a 772 KB cover is 12 KB at size=256 and 42 KB
# at size=512 — and the album grid puts a dozen of them on screen at once.
# Each surface now names its bucket; the sizes are FIXED rather than derived
# from devicePixelRatio so every device asks for the same few URLs and shares
# one on-disk scaled copy per bucket.


def _art_sizes(gateway):
    """The `size` query value of every /art request the page has made.
    The stub records path and query as separate fields — `path` carries no
    query string, so read `query`."""
    return [r["query"].get("size")
            for r in gateway.captured(path_contains="/art")]


def test_grid_covers_request_the_cover_bucket(page, stub, gateway):
    gateway.add_album("Miles Davis", "A Kind of Blue", 5, art="http://s/cover.jpg")
    page.set_viewport_size({"width": 820, "height": 1180})
    _boot(page, stub)
    gateway.clear_requests()
    _albums_mode(page)
    page.wait_for_timeout(400)
    sizes = _art_sizes(gateway)
    assert sizes, "the grid fetched no artwork at all"
    assert all(s == "512" for s in sizes), \
        f"grid covers asked for {set(sizes)}, expected the 512 bucket"


def test_list_rows_request_the_thumb_bucket(page, stub, gateway):
    # Letter A: the stub filters the tracks list by TITLE initial, and the
    # letter bar starts on "A".
    gateway.add_track("Miles Davis", "Kind of Blue", "All Blues",
                      art="http://s/cover.jpg")
    page.set_viewport_size({"width": 1280, "height": 800})
    _boot(page, stub)
    gateway.clear_requests()
    page.evaluate("setBrowseMode('tracks')")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=5000)
    page.wait_for_timeout(400)
    sizes = _art_sizes(gateway)
    assert sizes, "the track list fetched no artwork at all"
    assert all(s == "256" for s in sizes), \
        f"36px list thumbs asked for {set(sizes)}, expected the 256 bucket"


def test_now_playing_and_lock_screen_share_one_url(page, stub, gateway):
    """The panel and MediaSession must resolve to the SAME url — one fetch and
    one cache entry per track, not two. 1024 is generous for a lock screen
    (iOS never shows it above ~600px) and far below a multi-MB original."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _boot(page, stub)
    urls = page.evaluate("""() => {
      const t = {art: 'http://s/cover.jpg'};
      return [artUrl(t.art, ART_FULL), artUrl(t.art, ART_FULL)];
    }""")
    assert urls[0] == urls[1]
    assert "size=1024" in urls[0], f"lock-screen art url is {urls[0]}"


def test_art_url_without_a_size_is_unchanged(page, stub, gateway):
    """Callers that pass no size must still get the plain proxy url, so any
    surface not yet audited keeps working exactly as before."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _boot(page, stub)
    u = page.evaluate("artUrl('http://s/cover.jpg')")
    assert u == "/art?url=" + "http%3A%2F%2Fs%2Fcover.jpg"
    assert "size=" not in u


def test_buckets_match_the_gateway_ladder(page, stub, gateway):
    """A size the server doesn't bucket to would be scaled to the next one up
    and cached under a variant nothing else reuses."""
    page.set_viewport_size({"width": 1280, "height": 800})
    _boot(page, stub)
    vals = page.evaluate("[ART_THUMB, ART_COVER, ART_FULL]")
    assert vals == [256, 512, 1024], f"art buckets drifted: {vals}"
