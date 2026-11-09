"""
Frontend half of "one resume slot per BOOK" (dlna_book_key.py).

The server hands every audiobook file a `book_key`: its folder, or one
book of a folder that holds several (the Poirot folder is 27 books, one
file each). The PWA must save under it, end a book at the last file of
THAT book, and — in a folder of books — treat tapping a file as opening
that book, resuming its bookmark.
"""
import json

from tests.frontend.test_audiobooks import BOOKS, MUSIC, _boot

FOLDER = "BBC Radio 4 Hercule Poirot"
NILE = FOLDER + " ▸ bbc radio death on nile"
CARDS = FOLDER + " ▸ bbc radio cards on table"


def _seed_poirot(gateway):
    tracks = [
        {"url": "http://stub/poirot/nile.m4b", "title": "Death on the Nile",
         "album": "BBC Radio 4: Death on the Nile", "book_key": NILE},
        {"url": "http://stub/poirot/cards.m4b", "title": "Cards on the Table",
         "album": "BBC Radio 4: Cards on the Table", "book_key": CARDS},
    ]
    for i, t in enumerate(tracks):
        t.update(artist="BBC", album_key=FOLDER, duration="1:30:00", art="",
                 type="audio", id=f"p{i}", mime="audio/mp4")
    gateway.album_tracks_by_key[FOLDER] = tracks
    return tracks


def _books(page):
    page.select_option("#source-sel", "uuid:localfs-books")
    page.wait_for_function(
        "document.getElementById('source-sel').value === 'uuid:localfs-books'",
        timeout=3000)


def test_tapping_a_book_in_a_folder_of_books_resumes_it(page, stub, gateway):
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_poirot(gateway)
    gateway.positions[NILE] = {"album_key": NILE, "url": tracks[0]["url"],
                               "position_sec": 754, "duration_sec": 5400,
                               "finished": 0, "updated_at": 0}
    _boot(page, stub)
    _books(page)
    # Later toasts overwrite this one, so record every toast.
    page.evaluate("window._toasts=[]; const _t=toast;"
                  "window.toast=(m,ms)=>{_toasts.push(String(m)); _t(m,ms);}")
    page.evaluate(f"startPlay({json.dumps(tracks[0])}, null)")
    page.wait_for_function(
        "_toasts.some(m => m.includes('Resuming · 12:34'))", timeout=3000)
    assert page.evaluate("browserQueue[0].url") == tracks[0]["url"]


def test_another_books_bookmark_does_not_hijack_the_tap(page, stub, gateway):
    """The Poirot bug, seen from the PWA: Nile's bookmark must not make a
    tap on Cards jump into the Nile."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_poirot(gateway)
    gateway.positions[NILE] = {"album_key": NILE, "url": tracks[0]["url"],
                               "position_sec": 754, "duration_sec": 5400,
                               "finished": 0, "updated_at": 0}
    _boot(page, stub)
    _books(page)
    page.evaluate("window._toasts=[]; const _t=toast;"
                  "window.toast=(m,ms)=>{_toasts.push(String(m)); _t(m,ms);}")
    page.evaluate(f"startPlay({json.dumps(tracks[1])}, null)")
    page.wait_for_function("browserQueue.length === 1", timeout=3000)
    assert page.evaluate("browserQueue[0].url") == tracks[1]["url"]
    assert not page.evaluate("_toasts.some(m => m.includes('Resuming'))")


def test_position_is_saved_under_the_book_and_ends_with_the_book(page, stub, gateway):
    """Nile is the last file of its BOOK, though not of the queue: near
    its end it is finished, and it saves under its own key."""
    gateway.servers = [MUSIC, BOOKS]
    tracks = _seed_poirot(gateway)
    _boot(page, stub)
    gateway.clear_requests()
    page.evaluate(f"""
      browserQueue = {json.dumps(tracks)};
      browserIdx = 0;
      browserQueueIsBook = true;
      browserAudio.src = "/stream?url=" + encodeURIComponent(browserQueue[0].url);
      new Promise(res => {{
        browserAudio.addEventListener("loadedmetadata", () => {{
          browserAudio.currentTime = Math.max(0, browserAudio.duration - 0.1);
          browserAudio.dispatchEvent(new Event("pause"));
          res(true);
        }}, {{once: true}});
      }})
    """)
    r = gateway.wait_for_request("/api/position", method="POST")
    body = json.loads(r["body"])
    assert body["album_key"] == NILE
    assert body["finished"] is True
