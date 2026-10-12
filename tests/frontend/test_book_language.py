"""
🌐 audiobooks by language — the letter-bar entry on the audiobooks
source, its language list, and the books of one language.

What matters, each a test:
  * the entry exists ONLY on the audiobooks source;
  * a language is named in itself ("Nederlands"), and the unplaced books
    are an honest "Unknown" bucket — requested as `language=` (empty),
    never folded into a guess;
  * the books view hides "Play all" (nobody plays a whole language) and
    the next view gets it back.
"""
from tests.frontend.test_audiobooks import BOOKS, MUSIC, _boot, _seed_book

LANGS = [{"language": "en", "books": 3},
         {"language": "nl", "books": 2},
         {"language": "", "books": 1}]

NL_BOOKS = [
    {"album_key": "libravox NL/max_havelaar", "album": "Max Havelaar",
     "artist": "Multatuli", "track_count": 27, "art": "",
     "language": "nl", "language_source": "detected"},
    {"album_key": "libravox NL/eline_vere", "album": "Eline Vere",
     "artist": "Louis Couperus", "track_count": 40, "art": "",
     "language": "nl", "language_source": "detected"},
]


def _letters(page):
    return page.eval_on_selector_all(
        "#letter-bar .letter-btn", "els => els.map(e => e.textContent)")


def _to_books_source(page):
    page.select_option("#source-sel", "uuid:localfs-books")
    page.wait_for_function(
        "document.getElementById('source-sel').value === 'uuid:localfs-books'",
        timeout=3000)


def _open_languages(page, gateway):
    gateway.clear_requests()
    page.click("#letter-bar .letter-btn:text-is('🌐')")
    page.wait_for_selector(".lang-row", timeout=3000)


def _seed(gateway):
    gateway.servers = [MUSIC, BOOKS]
    gateway.book_languages = LANGS
    gateway.book_language_albums = {"nl": NL_BOOKS, "": [NL_BOOKS[0]]}


def test_globe_only_on_audiobooks_source(page, stub, gateway):
    _seed(gateway)
    _boot(page, stub)
    assert "🌐" not in _letters(page)
    _to_books_source(page)
    letters = _letters(page)
    assert letters[:3] == ["📖", "🌐", "⭐"]


def test_language_list_names_each_language_in_itself(page, stub, gateway):
    _seed(gateway)
    _boot(page, stub)
    _to_books_source(page)
    _open_languages(page, gateway)
    assert gateway.wait_for_request(
        "/api/book_languages",
        match=lambda r: r["query"].get("udn") == "uuid:localfs-books")
    titles = page.eval_on_selector_all(
        ".lang-row .row-title", "els => els.map(e => e.textContent)")
    assert titles == ["English", "Nederlands", "Unknown"]
    subs = page.eval_on_selector_all(
        ".lang-row .row-sub", "els => els.map(e => e.textContent)")
    assert subs == ["3 books", "2 books", "1 book"]


def test_opening_a_language_lists_its_books(page, stub, gateway):
    _seed(gateway)
    _boot(page, stub)
    _to_books_source(page)
    _open_languages(page, gateway)
    page.click(".lang-row[data-lang='nl']")
    assert gateway.wait_for_request(
        "/api/book_language_albums",
        match=lambda r: r["query"].get("language") == "nl")
    page.wait_for_function(
        "document.getElementById('item-list').textContent.includes('Eline Vere')",
        timeout=3000)
    assert "Max Havelaar" in page.text_content("#item-list")
    assert page.text_content("#browse-section-title") == "Nederlands · 2 books"
    assert page.is_hidden("#browse-play-all")


def test_unknown_asks_for_the_empty_language(page, stub, gateway):
    _seed(gateway)
    _boot(page, stub)
    _to_books_source(page)
    _open_languages(page, gateway)
    with page.expect_request(
            lambda r: "/api/book_language_albums" in r.url) as req:
        page.click(".lang-row[data-lang='']")
    # Blank must reach the server PRESENT and empty: a missing parameter
    # is a 400 there, and anything else would be a guess.
    query = req.value.url.split("?", 1)[1].split("&")
    assert "language=" in query
    page.wait_for_function(
        "document.getElementById('item-list').textContent.includes('Max Havelaar')",
        timeout=3000)


def test_back_returns_to_languages_and_play_all_comes_back(page, stub, gateway):
    _seed(gateway)
    _seed_book(gateway)
    _boot(page, stub)
    _to_books_source(page)
    _open_languages(page, gateway)
    page.click(".lang-row[data-lang='nl']")
    page.wait_for_function(
        "document.getElementById('item-list').textContent.includes('Eline Vere')",
        timeout=3000)
    page.click("#browse-back span:text-is('← back')")
    page.wait_for_selector(".lang-row", timeout=3000)
    # Any later drill-down view must get its Play-all button back.
    page.evaluate("showAlbumTracks('Author', 'The Book', null, 'Author - The Book')")
    page.wait_for_function(
        "document.getElementById('item-list').textContent.includes('Chapter 1')",
        timeout=3000)
    assert page.is_visible("#browse-play-all")


# ── The edit form's language field ────────────────────────────────

CH_URL = "http://stub/book/ch1.m4b"


def _open_edit(page, url):
    page.evaluate("url => openEditModal({url, title: 'Chapter 1', "
                  "artist: 'Author', album: 'The Book', genre: ''})", url)
    page.wait_for_selector("#edit-modal.open", timeout=1500)


def _post_bodies(gateway):
    import json
    return [json.loads(r["body"]) for r in
            gateway.captured(path_contains="/api/book_language", method="POST")]


def test_edit_form_hides_language_for_a_non_book(page, stub, gateway):
    _boot(page, stub)
    _open_edit(page, "http://stub/music/song.flac")
    assert page.is_hidden("#edit-lang-field")


def test_edit_form_offers_auto_with_what_was_detected(page, stub, gateway):
    gateway.book_language[CH_URL] = {
        "book": True, "album_key": "Author - The Book", "language": "",
        "source": "detected", "detected": ""}
    _boot(page, stub)
    _open_edit(page, CH_URL)
    page.wait_for_selector("#edit-lang-field:not([hidden])", timeout=1500)
    opts = page.eval_on_selector_all(
        "#edit-lang option", "els => els.map(e => e.textContent)")
    assert opts[0] == "Auto — not detected"
    assert "Nederlands" in opts and "English" in opts
    assert page.input_value("#edit-lang") == ""


def test_choosing_a_language_posts_it_and_nothing_else(page, stub, gateway):
    gateway.book_language[CH_URL] = {
        "book": True, "album_key": "Author - The Book", "language": "",
        "source": "detected", "detected": ""}
    _boot(page, stub)
    _open_edit(page, CH_URL)
    page.wait_for_selector("#edit-lang-field:not([hidden])", timeout=1500)
    gateway.clear_requests()
    page.select_option("#edit-lang", "nl")
    page.click("#edit-save")
    assert gateway.wait_for_request("/api/book_language", method="POST")
    assert _post_bodies(gateway) == [{"url": CH_URL, "language": "nl"}]
    page.wait_for_function(
        "!document.getElementById('edit-modal').classList.contains('open')",
        timeout=1500)
    assert not gateway.captured(path_contains="/api/edit_track", method="POST")


def test_a_manual_language_preselects_and_auto_clears_it(page, stub, gateway):
    gateway.book_language[CH_URL] = {
        "book": True, "album_key": "Author - The Book", "language": "en",
        "source": "manual", "detected": "nl"}
    _boot(page, stub)
    _open_edit(page, CH_URL)
    page.wait_for_selector("#edit-lang-field:not([hidden])", timeout=1500)
    assert page.input_value("#edit-lang") == "en"
    assert page.text_content("#edit-lang option[value='']") == \
        "Auto — detected: Nederlands"
    gateway.clear_requests()
    page.select_option("#edit-lang", "")
    page.click("#edit-save")
    assert gateway.wait_for_request("/api/book_language", method="POST")
    assert _post_bodies(gateway) == [{"url": CH_URL, "language": ""}]


def test_unchanged_language_posts_nothing(page, stub, gateway):
    gateway.book_language[CH_URL] = {
        "book": True, "album_key": "Author - The Book", "language": "en",
        "source": "manual", "detected": ""}
    _boot(page, stub)
    _open_edit(page, CH_URL)
    page.wait_for_selector("#edit-lang-field:not([hidden])", timeout=1500)
    gateway.clear_requests()
    page.click("#edit-save")
    page.wait_for_timeout(400)
    assert not _post_bodies(gateway)
