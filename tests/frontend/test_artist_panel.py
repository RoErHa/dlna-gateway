"""The ℹ️ Artist panel (step 3).

Opens from the now-playing row beside 📜 Lyrics, on one
/api/artist_info request. Everything it renders was decided
server-side; these tests pin the parts the UI could still get wrong —
chiefly that an INFERRED line-up never reads like a credit.
"""

FLOYD = {
    "artist": "Pink Floyd", "mb_type": "Group", "born": "1965",
    "died": "2014", "birth_place": "London", "country": "GB",
    "disambiguation": "", "genres": ["progressive rock", "psychedelic rock"],
    "bio": "Pink Floyd are an English rock band formed in London in 1965.",
    "bio_url": "https://en.wikipedia.org/wiki/Pink_Floyd",
    "image_url": "", "top_tracks": ["Money", "Time"], "also_in": [],
    "lineup": [{"name": "David Gilmour", "instruments": "guitar, lead vocals"},
               {"name": "Richard Wright", "instruments": "keyboard"}],
    "lineup_year": 1975, "lineup_tier": "inferred", "track_count": 241,
}


URL = "http://stub/x.flac"


def _play(app, gateway, artist="Pink Floyd", year=1975):
    # The year reaches npTrack through /api/track_meta, which the
    # now-playing panel already fetches — so seed it here the same way
    # production supplies it.
    gateway.track_meta[URL] = {"title": "Wish You Were Here",
                               "year": year, "composer": "", "lyricist": ""}
    app.evaluate("""(a) => startPlay({url:a[0],
        title:'Wish You Were Here', artist:a[1], album:'WYWH', art:'',
        type:'audio'}, null);""", [URL, artist])
    app.wait_for_function(
        "document.getElementById('np-actions').style.display === 'flex'",
        timeout=2000)
    if year:
        app.wait_for_function("typeof npTrack !== 'undefined' && npTrack && npTrack.year", timeout=2500)


def _open(app, gateway, data=FLOYD):
    gateway.artist_info = data
    _play(app, gateway)
    app.locator("#np-btn-artist").click()
    app.wait_for_selector("#artist-modal.open", timeout=2500)
    # The modal opens BEFORE the fetch resolves; wait for the content.
    app.wait_for_function(
        "!document.getElementById('artist-body').textContent.includes('Loading')",
        timeout=3000)


def test_button_exists_next_to_lyrics(app):
    assert app.locator("#np-btn-artist").count() == 1


def test_panel_shows_the_core_facts(app, gateway):
    _open(app, gateway)
    body = app.locator("#artist-body").text_content()
    assert "1965" in body
    assert "London" in body
    assert "progressive rock" in body


def test_biography_always_carries_its_wikipedia_link(app, gateway):
    """The text is CC BY-SA — the link is the attribution, not a
    nicety, so it must render whenever the bio does."""
    _open(app, gateway)
    assert "English rock band" in app.locator("#artist-body").text_content()
    href = app.locator("#artist-body a[href*='wikipedia.org']").first
    assert href.count() >= 1


def test_lineup_is_shown_with_instruments_and_the_year(app, gateway):
    _open(app, gateway)
    body = app.locator("#artist-body").text_content()
    assert "David Gilmour" in body
    assert "guitar" in body
    assert "1975" in body


def test_an_inferred_lineup_is_labelled_as_inferred(app, gateway):
    """THE one the UI must not get wrong. A line-up derived from
    membership dates is a guess; presenting it in the same voice as a
    credit is the failure dlna_artist_infer exists to avoid."""
    _open(app, gateway)
    label = app.locator("#artist-body .tier").first.text_content().lower()
    assert "inferred" in label


def test_a_credited_lineup_reads_differently(app, gateway):
    d = dict(FLOYD, lineup_tier="credits", lineup_year=None)
    _open(app, gateway, d)
    body = app.locator("#artist-body").text_content().lower()
    assert "credited on this recording" in body
    assert "inferred" not in body


def test_empty_blocks_are_absent_not_blank(app, gateway):
    """A sparse artist should shrink the panel, not show a column of
    empty headings."""
    d = dict(FLOYD, lineup=[], top_tracks=[], genres=[], bio="", bio_url="")
    _open(app, gateway, d)
    body = app.locator("#artist-body").text_content().lower()
    assert "best known" not in body
    assert "line-up" not in body


def test_a_band_says_formed_and_a_person_says_born(app, gateway):
    """A band has no date of birth."""
    _open(app, gateway)
    assert "formed" in app.locator("#artist-body").text_content().lower()
    # Dismiss first — a second click lands on the open overlay.
    app.locator("#artist-close").click()
    app.wait_for_function(
        "!document.getElementById('artist-modal').classList.contains('open')",
        timeout=2000)
    d = dict(FLOYD, mb_type="Person", born="1958-07-30", died="",
             lineup=[], artist="Kate Bush")
    _open(app, gateway, d)
    assert "born" in app.locator("#artist-body").text_content().lower()


def test_unknown_artist_explains_itself(app, gateway):
    gateway.artist_info = None          # stub answers 404
    _play(app, gateway)
    app.locator("#np-btn-artist").click()
    app.wait_for_function(
        "!document.getElementById('artist-body').textContent.includes('Loading')",
        timeout=3000)
    body = app.locator("#artist-body").text_content().lower()
    assert "no information" in body or "not in" in body


def test_close_button_dismisses(app, gateway):
    _open(app, gateway)
    app.locator("#artist-close").click()
    app.wait_for_function(
        "!document.getElementById('artist-modal').classList.contains('open')",
        timeout=2000)


def test_the_year_of_the_playing_track_is_sent(app, gateway):
    """The line-up depends on it; sending nothing yields no line-up."""
    gateway.artist_info = FLOYD
    _play(app, gateway)
    gateway.clear_requests()
    app.locator("#np-btn-artist").click()
    app.wait_for_selector("#artist-modal.open", timeout=2500)
    req = gateway.wait_for_request("/api/artist_info", timeout=2.0)
    assert req is not None
    assert req["query"].get("year") == "1975"
    assert req["query"].get("artist") == "Pink Floyd"


def test_a_missing_control_cannot_kill_app_js(app):
    """REGRESSION 2026-09-21. `$("id").addEventListener(...)` at the top
    level of app.js throws when the element is absent, and since this is
    one long script that kills every line below — the app renders its
    full chrome and NO content.

    It is not hypothetical: a half-updated Service Worker cache pairs an
    OLD index.html with a NEW app.js, and adding the ℹ️ button did
    exactly this to a live client. A missing button is cosmetic; a dead
    app.js is the whole application."""
    assert app.evaluate("typeof bindClick") == "function"
    # Binding a control this document does not have must be survivable.
    assert app.evaluate("bindClick('no-such-control-here', () => {})") is False
    # …and the app is still alive afterwards.
    assert app.evaluate("typeof startPolling") == "function"
