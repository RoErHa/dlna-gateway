#!/usr/bin/env python3
"""
tools/screenshots.py — regenerate the README screenshots, reproducibly.

    .venv/bin/python tools/screenshots.py            # write docs/img/*.png
    .venv/bin/python tools/screenshots.py --headed   # watch it happen
    .venv/bin/python tools/screenshots.py --only album-grid

WHY IT DRIVES THE TEST STUB. The obvious way to screenshot a music app is to
point it at the real library — which would publish the maintainer's listening
history, one album title at a time, and produce images that can never be
regenerated identically. `tests/frontend/stub_gateway.py` already serves fixed
synthetic data to the Playwright suite, so pointing this at the same stub
gives images that are reproducible by anyone who clones the repo and contain
nothing personal. The library below is invented.

COVER ART is generated here rather than in the stub. The stub answers `/art`
with a 1x1 transparent PNG, which is right for tests (they assert on layout
and requests, never on pixels) and useless for a showcase. Rather than change
a fixture 230 tests depend on, this script intercepts `/art` in the browser
and fulfils it with a generated cover. The suite is untouched.

DARK ONLY, on purpose. The plan for this called for light and dark captures;
the PWA has exactly one palette — the deep-navy set tuned for daylight
legibility in 2026-08-07 — and no `prefers-color-scheme` handling at all.
There is no light theme to photograph, so this captures the surfaces and the
viewports instead: desktop, phone, and the three modes that are hard to
explain in prose (now playing, radio, audiobook resume).

Needs the dev extras (`playwright`, `Pillow`) — like the Playwright suite
itself, this is not part of `run_all.py` and a clone without them is fine.
"""
from __future__ import annotations

import argparse
import re
import colorsys
import hashlib
import io
import os
import sys
import time
import urllib.parse

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

OUT_DIR = os.path.join(PROJECT, "docs", "img")

DESKTOP = {"width": 1280, "height": 800}
PHONE = {"width": 390, "height": 844}          # iPhone 14/15 logical size

# An invented library. Real enough to look like a music collection, fictional
# enough to publish. Keep the album names varied in length — a screenshot is
# also a layout test, and equal-length titles hide truncation bugs.
ALBUMS = [
    ("Alder & Ash", "Aurora Falls", 9),
    ("Beacon Hollow", "Antiphon", 11),
    ("Cassette Nine", "Analogue Heart", 8),
    ("Delta Winter", "After the Thaw", 10),
    ("Ember Lane", "Amber Weather", 7),
    ("Foxglove Sound", "A Paper Boat", 12),
    ("Grainfield", "Autumn Sessions", 6),
    ("Halcyon Drift", "Anchorage", 9),
    ("Iron Meridian", "Ascent", 10),
    ("Juniper Sky", "Almanac", 8),
    ("Kestrel Road", "Open Country", 11),
    ("Lantern Ridge", "Winterlight", 9),
]

# One REAL artist, for the shots where an invented name would teach
# nothing — an artist panel is about dates, places and a career, and
# "Alder & Ash" has none of those a reader can check.
#
# What is safe here and what is not:
#   * a performer's NAME and an album TITLE are not copyrightable — fine
#   * the biography is Wikipedia's, CC BY-SA, and the panel renders its
#     credit + link IN FRAME, so the screenshot carries its own attribution
#   * the PHOTO is the one thing that would not. Commons portraits are
#     CC BY-SA and require naming the photographer, which a screenshot
#     cannot do — so this generates a placeholder instead of fetching one.
#     No attribution gap, and the tool still runs offline.
DEMO_ARTIST = "David Bowie"
DEMO_ALBUM = "Hunky Dory"
DEMO_YEAR = 1971
DEMO_TRACKS = [
    ("Changes", "0:03:37"), ("Oh! You Pretty Things", "0:03:12"),
    ("Eight Line Poem", "0:02:55"), ("Life on Mars?", "0:03:55"),
    ("Kooks", "0:02:53"), ("Quicksand", "0:05:06"),
    ("Fill Your Heart", "0:03:10"), ("Andy Warhol", "0:03:54"),
    ("Song for Bob Dylan", "0:04:13"), ("Queen Bitch", "0:03:20"),
    ("The Bewlay Brothers", "0:05:29"),
]
DEMO_ARTIST_INFO = {
    "artist": DEMO_ARTIST, "mbid": "5441c29d", "mb_type": "Person",
    "gender": "Male", "born": "1947-01-08", "died": "2016-01-10",
    "birth_place": "Brixton", "country": "GB",
    "disambiguation": "English singer\u2010songwriter",
    "genres": ["art rock", "glam rock", "alternative rock", "pop",
               "rock", "art pop"],
    "bio": ("David Robert Jones, known as David Bowie, was an English "
            "singer, songwriter and actor. Regarded as among the most "
            "influential musicians of the 20th century, he was known for "
            "his frequent reinvention and visual presentation, and is "
            "often referred to as the \u201cchameleon of rock\u201d."),
    "bio_url": "https://en.wikipedia.org/wiki/David_Bowie",
    "image_url": "stub://portrait/David Bowie",
    "top_tracks": ["Starman", "Heroes", "Space Oddity", "Ziggy Stardust",
                   "Changes"],
    "also_in": ["Tin Machine", "The Konrads", "The Riot Squad",
                "Davy Jones & The Lower Third"],
    "lineup": [], "lineup_year": None, "lineup_tier": "",
    "track_count": 246,
}

BOOKS = [
    ("Ada Fairweather", "The Cartographer's Apprentice", "Northreach #1"),
    ("Milo Hartsong", "Salt and Silver", "The Tidewater Cycle #2"),
    ("Wren Ashcombe", "A Quiet Kind of Thunder", ""),
]

STATIONS = [
    ("Radio Meridian", "Ambient · Downtempo", "NL", 192),
    ("Nightshift FM", "Jazz · Soul", "GB", 128),
    ("The Long Player", "Progressive · Rock", "DE", 320),
    ("Coastal Classical", "Classical", "FR", 256),
]


def _cover_png(seed: str, size: int = 512) -> bytes:
    """A deterministic, pleasant cover for `seed`.

    Deterministic matters: re-running this script must not produce a diff in
    every committed PNG. The hue comes from a hash of the album name, and the
    palette is kept dark and desaturated so the covers sit inside the app's
    navy rather than fighting it.
    """
    from PIL import Image, ImageDraw

    h = hashlib.sha256(seed.encode("utf-8")).digest()
    hue = h[0] / 255.0
    top = tuple(int(c * 255) for c in colorsys.hls_to_rgb(hue, 0.34, 0.42))
    bot = tuple(int(c * 255) for c in colorsys.hls_to_rgb(
        (hue + 0.08) % 1.0, 0.16, 0.38))

    img = Image.new("RGB", (size, size), top)
    d = ImageDraw.Draw(img)
    for y in range(size):                       # vertical gradient
        t = y / (size - 1)
        d.line([(0, y), (size, y)],
               fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))

    # A couple of concentric arcs — enough shape to read as artwork at 130px
    # without looking like a placeholder.
    band = tuple(min(255, c + 26) for c in top)
    for k in (0.34, 0.52, 0.70):
        r = int(size * k / 2)
        cx = cy = size // 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=band,
                  width=max(2, size // 128))

    initials = "".join(w[0] for w in seed.split()[:2]).upper()
    try:
        from PIL import ImageFont
        font = ImageFont.truetype(
            "/System/Library/Fonts/Supplemental/Futura.ttc", size // 5)
    except Exception:                                        # noqa: BLE001
        font = None
    if font is not None:
        box = d.textbbox((0, 0), initials, font=font)
        d.text(((size - box[2] + box[0]) / 2, (size - box[3] + box[1]) / 2),
               initials, fill=(255, 255, 255, 200), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _seed(gateway) -> None:
    """Populate the stub with the invented library."""
    gateway.servers = [
        {"udn": "uuid:localfs-music", "name": "RoHaLocalFS", "online": True,
         "tracks": 26051, "kind": "music"},
        {"udn": "uuid:localfs-books", "name": "RoHaAudioBooks", "online": True,
         "tracks": 11629, "kind": "audiobooks"},
    ]
    gateway.renderers = [
        {"udn": "uuid:naim-1", "name": "Uniti · living room", "online": True},
    ]
    for artist, album, n in ALBUMS:
        gateway.add_album(artist, album, n, art=f"stub://cover/{album}")
        gateway.add_artist(artist, 1, n, art=f"stub://cover/{album}")

    # The demo album, with a date and real songwriting credits — the two
    # things the 2026-09-20/21 work added and that the older captures
    # cannot show.
    gateway.add_album(DEMO_ARTIST, DEMO_ALBUM, len(DEMO_TRACKS),
                      art=f"stub://cover/{DEMO_ALBUM}", year=DEMO_YEAR)
    gateway.add_artist(DEMO_ARTIST, 1, len(DEMO_TRACKS),
                       art=f"stub://cover/{DEMO_ALBUM}")
    for title, dur in DEMO_TRACKS:
        gateway.add_track(DEMO_ARTIST, DEMO_ALBUM, title, duration=dur,
                          art=f"stub://cover/{DEMO_ALBUM}", year=DEMO_YEAR,
                          composer=DEMO_ARTIST, lyricist=DEMO_ARTIST)
    # showAlbumTracks passes an album_key, and the stub resolves that
    # through its own by-key map — populated here so the demo album
    # opens the same way a LocalFs folder-album does.
    gateway.album_tracks_by_key["bowie/hunky"] = \
        gateway.album_tracks[(DEMO_ARTIST, DEMO_ALBUM)]
    gateway.artist_albums[DEMO_ARTIST] = [{
        "artist": DEMO_ARTIST, "album": DEMO_ALBUM, "year": DEMO_YEAR,
        "track_count": len(DEMO_TRACKS), "folder_tracks": len(DEMO_TRACKS),
        "folder_artists": 1, "own": True, "album_key": "bowie/hunky",
        "art": f"stub://cover/{DEMO_ALBUM}"}]
    gateway.artist_info = DEMO_ARTIST_INFO

    first_artist, first_album, _ = ALBUMS[0]
    for i, title in enumerate(
            ["Clearwater", "Riverbend", "Slack Tide", "The Undertow",
             "Marram", "Nine Fathoms", "Low Sun", "Estuary", "Homing"], 1):
        gateway.add_track(first_artist, first_album, title,
                          duration=f"0:0{3 + i % 3}:{20 + i * 3}",
                          art=f"stub://cover/{first_album}")

    gateway.add_playlist("__favourites__", "Favourites",
                         gateway.album_tracks[(first_artist, first_album)][:4])
    gateway.add_playlist("pl-roadtrip", "Road trip",
                         gateway.tracks_default[:6])
    gateway.add_playlist("pl-sunday", "Sunday morning",
                         gateway.tracks_default[2:7])

    gateway.album_favourites = [
        {"artist": a, "album": b, "art": f"stub://cover/{b}",
         "track_count": n, "udn": "uuid:localfs-music", "added_at": 0}
        for a, b, n in ALBUMS[:6]
    ]
    gateway.radio_favourites = [
        {"station_uuid": f"st-{i}", "name": name, "tags": tags,
         "country": cc, "bitrate": br, "codec": "MP3",
         "stream_url": f"http://stub/{i}", "favicon": "",
         "homepage": "", "added_at": 0}
        for i, (name, tags, cc, br) in enumerate(STATIONS)
    ]


def _route_covers(page) -> None:
    """Serve a generated cover for every /art request."""
    cache: dict[str, bytes] = {}

    def handler(route, request):
        # The url= parameter is percent-encoded, slashes included, so it has
        # to be UNQUOTED before the last path segment can be taken — decoding
        # only %20 left the whole "stub%3A%2F%2Fcover%2FAnalogue Heart" as the
        # seed and stamped the wrong initials on every cover.
        raw = request.url.split("url=", 1)[-1].split("&", 1)[0]
        key = urllib.parse.unquote(raw).rstrip("/").split("/")[-1] or "cover"
        if key not in cache:
            cache[key] = _cover_png(key)
        route.fulfill(status=200, content_type="image/png", body=cache[key])

    # A REGEX, not the glob `**/art*` — that also matches
    # `/api/artist_info`, which got fulfilled with a PNG and made the
    # artist panel render "Bad response." The art route is always
    # `/art?url=…`, so anchor on the query.
    page.route(re.compile(r"/art\?"), handler)
    # The artist portrait arrives through the SAME /art proxy, so the
    # handler above already serves it a generated image. That is
    # deliberate: the real Wikimedia portraits are CC BY-SA and require
    # naming the photographer, which a screenshot cannot carry.


def _boot(page, base_url: str) -> None:
    page.goto(base_url + "/")
    page.wait_for_function(
        "document.getElementById('source-sel') && "
        "!document.getElementById('source-sel').textContent.includes('Scanning')",
        timeout=10000)
    page.wait_for_timeout(400)


def _shot(page, name: str) -> None:
    path = os.path.join(OUT_DIR, f"{name}.png")
    # animations="disabled" resets CSS animations to their first frame. The
    # album cover spins like a record while audio plays (an 8 s infinite
    # rotation), so without this the now-playing capture caught it mid-turn,
    # tilted across the title. It also makes every run byte-comparable.
    page.screenshot(path=path, animations="disabled")
    print(f"  ✓ docs/img/{name}.png")


# ── The captures ──────────────────────────────────────────────────────

def cap_album_grid(page, stub):
    page.set_viewport_size(DESKTOP)
    _boot(page, stub.base_url)
    page.evaluate("setBrowseMode('albums')")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=8000)
    page.wait_for_timeout(600)
    _shot(page, "album-grid")


def cap_album_open(page, stub):
    """An open album: its own DATE beside the artist in the header, and
    a track list. Added 2026-09-21 — albums gained a date everywhere."""
    page.set_viewport_size(DESKTOP)
    _boot(page, stub.base_url)
    page.evaluate("""(a) => showAlbumTracks(a[0], a[1],
                     {artist:a[0]}, 'bowie/hunky')""",
                  [DEMO_ARTIST, DEMO_ALBUM])
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 3", timeout=8000)
    page.wait_for_timeout(700)
    _shot(page, "album-open")


def cap_artist_panel(page, stub):
    """The info panel: life-span, birthplace, genres, an attributed
    biography, best-known-for, and the bands a person played in."""
    page.set_viewport_size(DESKTOP)
    _boot(page, stub.base_url)
    page.evaluate("""(a) => showAlbumTracks(a[0], a[1],
                     {artist:a[0]}, 'bowie/hunky')""",
                  [DEMO_ARTIST, DEMO_ALBUM])
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 3", timeout=8000)
    page.locator("#item-list .row").first.click()
    page.wait_for_timeout(900)
    page.locator("#np-btn-artist").click()
    page.wait_for_selector("#artist-modal.open", timeout=6000)
    page.wait_for_function(
        "!document.getElementById('artist-body').textContent.includes('Loading')",
        timeout=8000)
    page.wait_for_timeout(700)
    _shot(page, "artist-panel")


def cap_now_playing(page, stub):
    page.set_viewport_size(DESKTOP)
    artist, album, _ = ALBUMS[0]
    stub.gateway.renderer_state.update({
        "state": "playing", "alive": True, "paused": False,
        "title": "Slack Tide", "media_title": "Slack Tide",
        "artist": artist, "album": album,
        "duration": 254, "position": 96, "queue_pos": 3, "queue_len": 9,
        "art": f"stub://cover/{album}",
    })
    _boot(page, stub.base_url)
    # Drive it the way a listener would, rather than poking the DOM: pick the
    # UPnP output, open the album, press Play all. That matters because the
    # cover in the player is set by the PLAYBACK path (playTracklist), not by
    # /api/renderer_state — seeding state alone leaves the placeholder note.
    page.select_option("#output-sel", "upnp:uuid:naim-1")
    page.evaluate("setBrowseMode('albums')")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=8000)
    page.locator("#item-list .row").first.click()
    page.wait_for_selector("#browse-play-all", timeout=8000)
    page.locator("#browse-play-all").click()
    # Ask for a state poll now rather than waiting out the idle cadence. Since
    # the 2026-08-07 battery work the state loop only runs at 1 s while audio
    # is genuinely playing; idle it drops to 20 s and relies on SSE to push
    # changes. kickPoll() is the app's own "poll then re-arm" entry point.
    page.evaluate("kickPoll('state')")
    page.wait_for_function(
        "document.getElementById('np-title').textContent.includes('Slack')",
        timeout=8000)
    page.wait_for_timeout(700)
    _shot(page, "now-playing")


def cap_mobile(page, stub):
    page.set_viewport_size(PHONE)
    _boot(page, stub.base_url)
    page.evaluate("setBrowseMode('albums')")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=8000)
    page.wait_for_timeout(600)
    _shot(page, "mobile-browse")


def cap_radio(page, stub):
    page.set_viewport_size(DESKTOP)
    _boot(page, stub.base_url)
    page.wait_for_function("document.getElementById('radio-pl-item')",
                           timeout=8000)
    page.locator("#radio-pl-item").click()
    page.wait_for_timeout(900)
    _shot(page, "radio")


def cap_audiobooks(page, stub):
    """The 📖 continue-listening shelf — a bookmark that lives on the SERVER,
    so a book stopped on the Naim resumes in the car. Hard to convey in prose,
    easy in one picture."""
    page.set_viewport_size(DESKTOP)
    books = [
        ("book-1", BOOKS[0], "Chapter 11 — The Reach", 4120, 9400),
        ("book-2", BOOKS[1], "Chapter 3 — Low Water", 780, 8600),
        ("book-3", BOOKS[2], "Chapter 24 — Homecoming", 7300, 8100),
    ]
    stub.gateway.positions = {
        key: {"album_key": key, "position_sec": pos, "duration_sec": dur,
              "finished": 0, "url": f"http://stub/{key}/ch.m4b",
              "book": title, "author": author,
              "chapter_title": chapter, "art": f"stub://cover/{title}"}
        for key, (author, title, _series), chapter, pos, dur in books
    }
    _boot(page, stub.base_url)
    page.select_option("#source-sel", "uuid:localfs-books")
    # The 📖 shelf is a letter-bar entry, in front of ⭐, and only exists for
    # an audiobooks source.
    page.wait_for_selector(".letter-btn:has-text('📖')", timeout=8000)
    page.locator(".letter-btn:has-text('📖')").click()
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=8000)
    page.wait_for_timeout(600)
    _shot(page, "audiobooks")


CAPTURES = {
    "album-grid": cap_album_grid,
    "album-open": cap_album_open,
    "artist-panel": cap_artist_panel,
    "now-playing": cap_now_playing,
    "mobile-browse": cap_mobile,
    "radio": cap_radio,
    "audiobooks": cap_audiobooks,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--headed", action="store_true",
                    help="show the browser while capturing")
    ap.add_argument("--only", action="append", choices=sorted(CAPTURES),
                    help="capture just this one (repeatable)")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("✗ playwright is not installed. It is a DEV extra:\n"
              "    .venv/bin/pip install -r requirements-dev.txt\n"
              "    .venv/bin/playwright install chromium", file=sys.stderr)
        return 2
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("✗ Pillow is needed to generate the cover art.\n"
              "    .venv/bin/pip install Pillow", file=sys.stderr)
        return 2

    from tests.frontend.stub_gateway import StubServer

    os.makedirs(OUT_DIR, exist_ok=True)
    wanted = args.only or sorted(CAPTURES)

    print(f"Writing {len(wanted)} screenshot(s) to docs/img/ "
          f"(stub gateway, invented library)")
    t0 = time.monotonic()
    for name in wanted:
        stub = StubServer()
        stub.start()
        try:
            _seed(stub.gateway)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=not args.headed)
                # service_workers="block" is REQUIRED, not tidiness. The PWA
                # cache-firsts /art in its Service Worker, and a request made
                # BY a service worker does not pass through page.route() — so
                # the generated covers were silently replaced by the stub's
                # 1x1 transparent PNG and every card rendered blank. Blocking
                # the worker also keeps captures deterministic: no cache
                # carries between runs.
                ctx = browser.new_context(device_scale_factor=2,
                                          service_workers="block")
                page = ctx.new_page()
                page.on("pageerror",
                        lambda e: print(f"    ! page error: {e}",
                                        file=sys.stderr))
                _route_covers(page)
                CAPTURES[name](page, stub)
                browser.close()
        finally:
            stub.stop()
    print(f"Done in {time.monotonic() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
