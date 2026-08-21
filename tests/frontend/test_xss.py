"""
Frontend tests for escaping of UNTRUSTED device/library text.

From the 2026-08-20 audit. `esc()` escaped only `& < >`, which is correct for
a text node but NOT for a quoted attribute — and the PWA does interpolate it
into one: `<option value="${esc(s.udn)}">`. A UDN comes straight from a
discovered device's description XML (`dlna_discovery._fetch_device`), so
anything on the LAN able to answer an M-SEARCH could inject attributes into
the page — e.g. an `onfocus=` handler that beacons out.

`esc()` now escapes both quote characters too, so it is safe in BOTH
positions. These tests assert the injected markup stays inert: the value is
carried as DATA, and no attribute or element the payload tried to create
exists in the DOM.

Verified against the vulnerable code: with the quote-escaping removed, the
UDN test below FAILS (the attribute value truncates at the injected quote —
`uuid:evil`). The other two pass either way, because those sinks are text
nodes where the original `& < >` escaping was already sufficient; they are
kept as guards against a future edit moving that text into an attribute.
"""

_XSS_UDN = 'uuid:evil" onfocus="window.__pwned=1" autofocus x="'
_XSS_NAME = 'Evil<img src=x onerror="window.__pwned=1">Server'


def _boot(page, stub):
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('source-sel') && "
        "!document.getElementById('source-sel').textContent.includes('Scanning')",
        timeout=5000,
    )


def test_quote_in_udn_cannot_break_out_of_the_value_attribute(page, stub, gateway):
    gateway.servers = [{"udn": _XSS_UDN, "name": "Evil", "online": True,
                        "tracks": 1}]
    _boot(page, stub)
    page.wait_for_function(
        "document.querySelectorAll('#source-sel option').length >= 1",
        timeout=5000)

    # The whole payload must survive as ONE attribute value, not as markup.
    value = page.locator("#source-sel option").first.get_attribute("value")
    assert value == _XSS_UDN, f"UDN was mangled or split: {value!r}"

    # Nothing the payload tried to create may exist.
    assert page.evaluate("window.__pwned === undefined"), "XSS handler ran"
    assert page.locator("#source-sel option[autofocus]").count() == 0
    assert page.evaluate(
        "!document.querySelector('#source-sel option').hasAttribute('onfocus')")


def test_markup_in_friendly_name_is_rendered_as_text(page, stub, gateway):
    gateway.servers = [{"udn": "uuid:ok-1", "name": _XSS_NAME, "online": True,
                        "tracks": 1}]
    _boot(page, stub)
    page.wait_for_function(
        "document.querySelectorAll('#source-sel option').length >= 1",
        timeout=5000)

    # No <img> may have been created inside the picker, and the angle
    # brackets must still be visible as literal text.
    assert page.locator("#source-sel img").count() == 0
    assert page.evaluate("window.__pwned === undefined"), "XSS handler ran"
    text = page.locator("#source-sel option").first.text_content()
    assert "<img" in text, f"payload should render as literal text, got {text!r}"


def test_quote_in_track_title_stays_inert(page, stub, gateway):
    """Track text is tag content — equally untrusted, and equally escaped.

    A crafted TITLE is the "media file phones home" case: the payload rides
    in on an indexed file's tags rather than from a device on the LAN. This
    sink is a TEXT NODE, so it was already safe before the quote-escaping
    fix; the test guards against a future edit moving a title into an
    attribute (a `title="…"` tooltip is the obvious way this would happen).
    """
    artist, album = "ABBA", "Arrival"
    title = 'Song" onmouseover="window.__pwned=1" x="'
    gateway.add_artist(artist, album_count=1, track_count=1)
    gateway.add_album(artist, album, track_count=1)
    gateway.artist_albums[artist] = [
        {"artist": artist, "album": album, "track_count": 1, "art": ""}]
    gateway.add_track(artist, album, title)

    page.goto(stub.base_url + "/")
    # Drill letter → artist → album so the TRACK row (which carries the
    # payload) is actually rendered.
    for _ in range(2):
        page.wait_for_function(
            "document.querySelectorAll('#item-list .row').length > 0",
            timeout=5000)
        page.locator("#item-list .row").first.click()
    # Assert it really rendered — otherwise the negative assertions below
    # would pass vacuously, which is how this test failed its first draft.
    page.wait_for_function(
        "document.body.innerText.includes('onmouseover')", timeout=5000)

    assert page.evaluate("window.__pwned === undefined"), "XSS handler ran"
    assert page.locator("[onmouseover]").count() == 0, "payload became markup"
    # The quotes must survive as literal text inside the title element.
    assert title in page.evaluate("document.body.innerText")


def test_album_header_shows_an_ampersand_literally(page, stub, gateway):
    """textContent does not parse HTML, so escaping BEFORE assigning it
    renders the entity itself: "Alder & Ash" appeared as "Alder &amp; Ash"
    in the album header while the track rows below were correct.

    The safety direction was never wrong — over-escaping, not under — but
    `&` is common enough in band names (Simon & Garfunkel, Earth, Wind &
    Fire) that it was visible on a real library. Found while generating the
    README screenshots, 2026-08-21.
    """
    gateway.add_album("Alder & Ash", "Aurora Falls", 2)
    gateway.add_track("Alder & Ash", "Aurora Falls", "Clearwater")
    _boot(page, stub)
    page.evaluate("setBrowseMode('albums')")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=5000)
    page.locator("#item-list .row").first.click()
    page.wait_for_selector("#browse-play-all", timeout=5000)
    assert page.text_content("#browse-section-title") == "Alder & Ash"
