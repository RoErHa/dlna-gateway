"""
Frontend tests for audiobooks P1+P2: the resume button in the book
header, the position-save plumbing, and the playback-rate control.

The audiobooks source is identified by kind="audiobooks" on its
/api/servers entry; resume behaviour must be OFF for music sources.
Uses the page/stub/gateway fixtures (like test_source_picker) so
servers can be seeded before navigation.
"""
import json

MUSIC = {"udn": "uuid:localfs-music", "name": "RoHaLocalFS",
         "online": True, "tracks": 26051, "kind": "music"}
BOOKS = {"udn": "uuid:localfs-books", "name": "RoHaAudioBooks",
         "online": True, "tracks": 120, "kind": "audiobooks"}

BOOK_KEY = "Author - The Book"


def _seed_book(gateway):
    tracks = []
    for i in (1, 2, 3):
        tracks.append({
            "url": f"http://stub/book/ch{i}.m4b", "title": f"Chapter {i}",
            "artist": "Author", "album": "The Book", "album_key": BOOK_KEY,
            "duration": "0:30:00", "art": "", "type": "audio",
            "id": f"ch{i}", "mime": "audio/mp4"})
    gateway.album_tracks[("Author", "The Book")] = tracks
    gateway.album_tracks_by_key[BOOK_KEY] = tracks
    return tracks


def _boot(page, stub):
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('source-sel') && "
        "!document.getElementById('source-sel').textContent.includes('Scanning')",
        timeout=5000)


def _open_book(page):
    page.select_option("#source-sel", "uuid:localfs-books")
    page.wait_for_function(
        "document.getElementById('source-sel').value === 'uuid:localfs-books'",
        timeout=3000)
    page.evaluate(
        f"showAlbumTracks('Author', 'The Book', null, {BOOK_KEY!r})")
    page.wait_for_function(
        "document.getElementById('item-list').textContent.includes('Chapter 1')",
        timeout=3000)


def test_resume_button_shown_with_saved_position(page, stub, gateway):
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    gateway.positions[BOOK_KEY] = {
        "album_key": BOOK_KEY, "url": tracks[1]["url"],
        "position_sec": 754, "duration_sec": 1800,
        "finished": 0, "updated_at": 0}
    _boot(page, stub)
    _open_book(page)
    page.wait_for_selector("#browse-resume", state="visible", timeout=3000)
    label = page.text_content("#browse-resume")
    assert "Resume" in label
    assert "Ch 2" in label          # chapter index from the track list
    assert "12:34" in label         # fmtSec(754)


def test_resume_button_hidden_without_position(page, stub, gateway):
    gateway.servers = [MUSIC, BOOKS]
    _seed_book(gateway)
    _boot(page, stub)
    _open_book(page)
    assert page.is_hidden("#browse-resume")


def test_resume_button_never_shown_on_music_source(page, stub, gateway):
    """Even with a saved position for the same album_key, a music
    source must not grow a resume button."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    gateway.positions[BOOK_KEY] = {
        "album_key": BOOK_KEY, "url": tracks[0]["url"],
        "position_sec": 10, "duration_sec": 1800,
        "finished": 0, "updated_at": 0}
    _boot(page, stub)   # stays on the first (music) source
    page.evaluate(
        f"showAlbumTracks('Author', 'The Book', null, {BOOK_KEY!r})")
    page.wait_for_function(
        "document.getElementById('item-list').textContent.includes('Chapter 1')",
        timeout=3000)
    assert page.is_hidden("#browse-resume")


def test_resume_click_queues_from_saved_chapter_unshuffled(page, stub, gateway):
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    gateway.positions[BOOK_KEY] = {
        "album_key": BOOK_KEY, "url": tracks[1]["url"],
        "position_sec": 754, "duration_sec": 1800,
        "finished": 0, "updated_at": 0}
    _boot(page, stub)
    _open_book(page)
    page.wait_for_selector("#browse-resume", state="visible", timeout=3000)
    # Shuffle ON must not scramble a book queue.
    page.evaluate("shuffleEnabled = true")
    page.click("#browse-resume")
    page.wait_for_function("browserQueue.length === 2", timeout=3000)
    q0 = page.evaluate("browserQueue[0].url")
    q1 = page.evaluate("browserQueue[1].url")
    assert q0 == tracks[1]["url"]
    assert q1 == tracks[2]["url"]
    assert page.evaluate("browserQueueIsBook") is True
    # Rate control appears in book mode.
    assert page.is_visible("#ab-rate")


def test_finished_book_offers_start_over(page, stub, gateway):
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    gateway.positions[BOOK_KEY] = {
        "album_key": BOOK_KEY, "url": tracks[2]["url"],
        "position_sec": 1795, "duration_sec": 1800,
        "finished": 1, "updated_at": 0}
    _boot(page, stub)
    _open_book(page)
    page.wait_for_selector("#browse-resume", state="visible", timeout=3000)
    assert "Start over" in page.text_content("#browse-resume")
    page.click("#browse-resume")
    page.wait_for_function("browserQueue.length === 3", timeout=3000)
    assert page.evaluate("browserQueue[0].url") == tracks[0]["url"]


def test_pause_posts_position(page, stub, gateway):
    """The pause listener must POST {album_key, url, position_sec} to
    /api/position. Media state is driven directly (house pattern from
    test_audio_errors) — the stub serves a real WAV so metadata loads."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    _boot(page, stub)
    gateway.clear_requests()
    page.evaluate(f"""
      browserQueue = {json.dumps(tracks)};
      browserIdx = 0;
      browserQueueIsBook = true;
      browserAudio.src = "/stream?url=" + encodeURIComponent(browserQueue[0].url);
      new Promise(res => {{
        browserAudio.addEventListener("loadedmetadata", () => {{
          browserAudio.currentTime = 0.5;
          browserAudio.dispatchEvent(new Event("pause"));
          res(true);
        }}, {{once: true}});
      }})
    """)
    r = gateway.wait_for_request(
        "/api/position", method="POST",
        match=lambda r: BOOK_KEY in r["body"])
    assert r is not None
    body = json.loads(r["body"])
    assert body["url"] == tracks[0]["url"]
    assert body["position_sec"] >= 0.4
    assert body["finished"] is False    # chapter 1 of 3, nowhere near done


def test_music_queue_never_posts_position(page, stub, gateway):
    """A music queue (browserQueueIsBook=false) must stay silent on
    /api/position even when tracks carry an album_key."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    _boot(page, stub)
    gateway.clear_requests()
    page.evaluate(f"""
      browserQueue = {json.dumps(tracks)};
      browserIdx = 0;
      browserQueueIsBook = false;
      browserAudio.src = "/stream?url=" + encodeURIComponent(browserQueue[0].url);
      new Promise(res => {{
        browserAudio.addEventListener("loadedmetadata", () => {{
          browserAudio.currentTime = 0.5;
          browserAudio.dispatchEvent(new Event("pause"));
          res(true);
        }}, {{once: true}});
      }})
    """)
    r = gateway.wait_for_request("/api/position", method="POST", timeout=1.0)
    assert r is None


def test_rootlevel_single_file_book_keys_by_url(page, stub, gateway):
    """A single-file book at the library root has album_key='' — its
    position must key by the file URL, not the shared '' (which would
    make ALL root-level books share one resume row)."""
    gateway.servers = [MUSIC, BOOKS]
    track = {"url": "http://stub/root/The Dark Forest.m4b",
             "title": "The Dark Forest", "artist": "Cixin Liu",
             "album": "The Dark Forest", "album_key": "",
             "duration": "12:00:00", "art": "", "type": "audio",
             "id": "tdf", "mime": "audio/mp4"}
    _boot(page, stub)
    gateway.clear_requests()
    page.evaluate(f"""
      browserQueue = [{json.dumps(track)}];
      browserIdx = 0;
      browserQueueIsBook = true;
      browserAudio.src = "/stream?url=" + encodeURIComponent(browserQueue[0].url);
      new Promise(res => {{
        browserAudio.addEventListener("loadedmetadata", () => {{
          browserAudio.currentTime = 0.5;
          browserAudio.dispatchEvent(new Event("pause"));
          res(true);
        }}, {{once: true}});
      }})
    """)
    r = gateway.wait_for_request(
        "/api/position", method="POST",
        match=lambda r: "Dark Forest" in r["body"])
    assert r is not None
    body = json.loads(r["body"])
    assert body["album_key"] == track["url"]   # URL fallback, never ''


def test_book_header_shows_series_line(page, stub, gateway):
    """OpenLibrary overlay: the book header carries the series + number
    + canonical author when book_meta has a row."""
    gateway.servers = [MUSIC, BOOKS]
    _seed_book(gateway)
    gateway.book_meta[BOOK_KEY] = {
        "album_key": BOOK_KEY, "author": "Peter F. Hamilton",
        "title": "The Reality Dysfunction",
        "series": "Night's Dawn", "series_seq": 1,
        "source": "openlibrary"}
    _boot(page, stub)
    _open_book(page)
    page.wait_for_selector("#browse-series", state="visible", timeout=3000)
    line = page.text_content("#browse-series")
    assert "Night's Dawn #1" in line
    assert "Peter F. Hamilton" in line


def test_book_header_no_meta_no_line(page, stub, gateway):
    gateway.servers = [MUSIC, BOOKS]
    _seed_book(gateway)
    _boot(page, stub)
    _open_book(page)
    assert page.is_hidden("#browse-series")


def test_album_rows_carry_series_chip(page, stub, gateway):
    """The albums letter-bar rows on the audiobooks source get a 📚
    series chip when the overlay knows the book."""
    gateway.servers = [MUSIC, BOOKS]
    gateway.albums_default = [
        {"artist": "Peter F Hamilton", "album": "The Reality Dysfunction",
         "track_count": 40, "art": "", "album_key": BOOK_KEY}]
    gateway.book_meta[BOOK_KEY] = {
        "album_key": BOOK_KEY, "author": "Peter F. Hamilton",
        "title": "The Reality Dysfunction",
        "series": "Night's Dawn", "series_seq": 1,
        "source": "openlibrary"}
    _boot(page, stub)
    page.select_option("#source-sel", "uuid:localfs-books")
    page.wait_for_function(
        "document.getElementById('source-sel').value === 'uuid:localfs-books'",
        timeout=3000)
    # Wait for the meta map, then render the albums letter view.
    page.wait_for_function("abMeta !== null", timeout=3000)
    page.evaluate("browseMode='albums'; renderBrowseItems("
                  + json.dumps([{"artist": "Peter F Hamilton",
                                 "album": "The Reality Dysfunction",
                                 "track_count": 40, "art": "",
                                 "album_key": BOOK_KEY}]) + ")")
    row = page.text_content("#item-list")
    assert "📚 Night's Dawn #1" in row


def test_seek_slider_active_only_for_books(page, stub, gateway):
    """The time slider is drag/tap-seekable ONLY for audiobook queues —
    the .seekable class gates the pointer handlers and the CSS
    affordance; music stays display-only."""
    gateway.servers = [MUSIC, BOOKS]
    _seed_book(gateway)
    _boot(page, stub)
    # Music (default): not seekable.
    page.evaluate("browserQueueIsBook = false; _abApplyRate();")
    assert "seekable" not in (page.get_attribute("#seek-track", "class") or "")
    # Book mode: seekable.
    page.evaluate("browserQueueIsBook = true; _abApplyRate();")
    assert "seekable" in page.get_attribute("#seek-track", "class")


def test_seek_click_scrubs_book_chapter_and_saves(page, stub, gateway):
    """Clicking the slider in book mode seeks the <audio> to that
    fraction and the 'seeked' event persists the new position."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    _boot(page, stub)
    page.evaluate(f"""
      browserQueue = {json.dumps(tracks)};
      browserIdx = 0;
      browserQueueIsBook = true;
      _abApplyRate();
      activeDevice = "browser";
      browserAudio.src = "/stream?url=" + encodeURIComponent(browserQueue[0].url);
      new Promise(res => browserAudio.addEventListener(
        "loadedmetadata", () => res(true), {{once: true}}));
    """)
    gateway.clear_requests()
    # Click mid-track (the stub WAV is 1s long → expect ≈0.5s).
    box = page.locator("#seek-track").bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_function("browserAudio.currentTime > 0.3", timeout=3000)
    t = page.evaluate("browserAudio.currentTime")
    assert 0.3 <= t <= 0.7
    # The seeked event saved the scrubbed position.
    r = gateway.wait_for_request(
        "/api/position", method="POST",
        match=lambda r: BOOK_KEY in r["body"])
    assert r is not None
    assert json.loads(r["body"])["position_sec"] >= 0.3


def test_seek_click_inert_for_music(page, stub, gateway):
    """Same click on a music queue must not move playback."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    _boot(page, stub)
    page.evaluate(f"""
      browserQueue = {json.dumps(tracks)};
      browserIdx = 0;
      browserQueueIsBook = false;
      _abApplyRate();
      activeDevice = "browser";
      browserAudio.src = "/stream?url=" + encodeURIComponent(browserQueue[0].url);
      new Promise(res => browserAudio.addEventListener(
        "loadedmetadata", () => res(true), {{once: true}}));
    """)
    box = page.locator("#seek-track").bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(300)
    assert page.evaluate("browserAudio.currentTime") == 0


def test_continue_listening_shelf(page, stub, gateway):
    """The 📖 letter-bar entry (audiobooks source only) lists in-progress
    books with chapter + progress; clicking opens the book."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    gateway.positions[BOOK_KEY] = {
        "album_key": BOOK_KEY, "url": tracks[1]["url"],
        "position_sec": 754, "duration_sec": 1800, "finished": 0,
        "updated_at": 0, "book": "The Book", "author": "Author",
        "art": "", "chapter_title": "Chapter 2"}
    gateway.positions["done-book"] = {
        "album_key": "done-book", "url": "http://stub/x", "position_sec": 1,
        "duration_sec": 2, "finished": 1, "updated_at": 0,
        "book": "Finished Book", "author": "", "art": "",
        "chapter_title": ""}
    _boot(page, stub)
    page.select_option("#source-sel", "uuid:localfs-books")
    page.wait_for_function(
        "document.getElementById('source-sel').value === 'uuid:localfs-books'",
        timeout=3000)
    # The 📖 letter appears only on the audiobooks source.
    page.wait_for_function(
        "[...document.querySelectorAll('.letter-btn')].some(b=>b.textContent==='📖')",
        timeout=3000)
    page.evaluate("setLetter('📖')")
    page.wait_for_function(
        "document.getElementById('item-list').textContent.includes('The Book')",
        timeout=3000)
    body = page.text_content("#item-list")
    assert "Chapter 2" in body and "12:34" in body
    assert "Finished Book" not in body      # finished rows stay off the shelf
    # Clicking the row opens the book view (album_tracks fetch fires).
    gateway.clear_requests()
    page.click("#item-list .row")
    r = gateway.wait_for_request("/api/album_tracks", method="GET")
    assert r is not None


def test_chapter_picker_populates_and_seeks(page, stub, gateway):
    """A chapter-atom track shows the 📑 picker; choosing one seeks."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    gateway.chapters[tracks[0]["url"]] = [
        {"start": 0.0, "end": 0.4, "title": "Opening"},
        {"start": 0.5, "end": 1.0, "title": "The Middle"},
    ]
    _boot(page, stub)
    page.evaluate(f"""
      browserQueue = {json.dumps(tracks)};
      browserIdx = 0;
      browserQueueIsBook = true;
      _abApplyRate();
      browserAudio.src = "/stream?url=" + encodeURIComponent(browserQueue[0].url);
      _abLoadChapters(browserQueue[0]);
      new Promise(res => browserAudio.addEventListener(
        "loadedmetadata", () => res(true), {{once: true}}));
    """)
    page.wait_for_selector("#ab-chapters", state="visible", timeout=3000)
    opts = page.locator("#ab-chapters option").all_text_contents()
    assert any("The Middle" in o for o in opts)
    page.select_option("#ab-chapters", "0.5")
    page.wait_for_function("browserAudio.currentTime >= 0.5", timeout=2000)


def test_sleep_timer_pauses_and_saves(page, stub, gateway):
    """The sleep timer pauses playback when it fires; the pause listener
    persists the position. Armed with a tiny fraction of a minute so the
    test doesn't wait."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_book(gateway)
    _boot(page, stub)
    gateway.clear_requests()
    page.evaluate(f"""
      browserQueue = {json.dumps(tracks)};
      browserIdx = 0;
      browserQueueIsBook = true;
      _abApplyRate();
      browserAudio.src = "/stream?url=" + encodeURIComponent(browserQueue[0].url);
      new Promise(res => browserAudio.addEventListener(
        "loadedmetadata", () => {{
          browserAudio.play().catch(()=>{{}});
          browserAudio.currentTime = 0.2;
          _abSleepArm(0.005);   // ~0.3 s
          res(true);
        }}, {{once: true}}));
    """)
    page.wait_for_function("browserAudio.paused === true", timeout=4000)
    r = gateway.wait_for_request(
        "/api/position", method="POST",
        match=lambda r: BOOK_KEY in r["body"])
    assert r is not None


def test_rate_control_applies_and_persists(page, stub, gateway):
    gateway.servers = [MUSIC, BOOKS]
    _seed_book(gateway)
    _boot(page, stub)
    page.evaluate("browserQueueIsBook = true; _abApplyRate();")
    assert page.is_visible("#ab-rate")
    page.select_option("#ab-rate", "1.5")
    assert page.evaluate("browserAudio.playbackRate") == 1.5
    assert page.evaluate("localStorage.getItem('dlna_ab_rate')") == "1.5"
    # Leaving book mode hides the control and restores 1×.
    page.evaluate("browserQueueIsBook = false; _abApplyRate();")
    assert page.is_hidden("#ab-rate")
    assert page.evaluate("browserAudio.playbackRate") == 1
