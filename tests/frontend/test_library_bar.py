"""
tests/frontend/test_library_bar.py — telling a person which drive to mount.

The 2026-09-11 outage ran for seventeen hours behind ONE boot-time
WARNING in gateway.log. The gateway now waits for a missing root and
wires it the moment it appears, but waiting is only half the job: from
the sofa the library is still missing, and nothing in the window said
so. These tests are about the half that is visible.
"""
import pytest



def _waiting(label="Music", root="/Volumes/MEDIA/Music"):
    return {"label": label, "root": root, "state": "waiting",
            "message": f"{label} library unavailable — mount {root}. "
                       "It will be picked up automatically, no restart needed."}


def _active(label="Audiobooks", root="/Volumes/MEDIA2/Audio_Books"):
    return {"label": label, "root": root, "state": "active"}


def test_a_waiting_root_names_the_volume_to_mount(gateway, app):
    gateway.libraries = [_waiting()]
    app.reload()
    bar = app.locator("#library-bar")
    bar.wait_for(state="visible", timeout=5000)
    assert "/Volumes/MEDIA/Music" in bar.inner_text()


def test_the_bar_stays_hidden_when_every_volume_is_there(gateway, app):
    gateway.libraries = [_active(), _active("Music", "/Volumes/MEDIA/Music")]
    app.reload()
    app.wait_for_timeout(600)
    assert not app.locator("#library-bar").is_visible()


def test_no_configured_roots_means_no_bar(gateway, app):
    """An unconfigured library is OFF, not broken. Warning about a video
    library nobody asked for is the noise that buries the real thing."""
    gateway.libraries = []
    app.reload()
    app.wait_for_timeout(600)
    assert not app.locator("#library-bar").is_visible()


def test_only_the_waiting_root_is_named(gateway, app):
    """Books on a healthy disk are not the problem and must not be
    listed as one — the roots are independent."""
    gateway.libraries = [_active(), _waiting()]
    app.reload()
    bar = app.locator("#library-bar")
    bar.wait_for(state="visible", timeout=5000)
    text = bar.inner_text()
    assert "/Volumes/MEDIA/Music" in text
    assert "Audio_Books" not in text


def test_two_missing_volumes_are_both_named(gateway, app):
    gateway.libraries = [_waiting(), _waiting("Video", "/Volumes/MEDIA/GWMovies")]
    app.reload()
    bar = app.locator("#library-bar")
    bar.wait_for(state="visible", timeout=5000)
    text = bar.inner_text()
    assert "/Volumes/MEDIA/Music" in text
    assert "/Volumes/MEDIA/GWMovies" in text


def test_the_bar_clears_itself_once_the_drive_is_mounted(gateway, app):
    """The whole point of waiting: the person plugs the drive in and the
    app catches up on its own, with no reload and no restart."""
    gateway.libraries = [_waiting()]
    app.reload()
    app.locator("#library-bar").wait_for(state="visible", timeout=5000)

    gateway.libraries = [_active("Music", "/Volumes/MEDIA/Music")]
    app.evaluate("refreshLibraries()")
    app.wait_for_selector("#library-bar", state="hidden", timeout=5000)


@pytest.mark.csp_violations_expected
def test_the_message_is_escaped_not_interpreted(gateway, app):
    """`message` and `root` are server-built strings, but a root is a
    path from config and paths can hold anything. It lands in innerHTML,
    so it must be escaped — same rule as every other untrusted text."""
    gateway.libraries = [_waiting("Music", "/Volumes/<img src=x onerror=x>/M")]
    app.reload()
    bar = app.locator("#library-bar")
    bar.wait_for(state="visible", timeout=5000)
    assert bar.locator("img").count() == 0
    assert "<img" in bar.inner_text()


def test_a_failed_libraries_fetch_never_blanks_the_bar(gateway, app):
    """A dropped tailnet must not read as 'the drive came back'."""
    gateway.libraries = [_waiting()]
    app.reload()
    app.locator("#library-bar").wait_for(state="visible", timeout=5000)

    app.evaluate("window.api = async () => null")   # every fetch fails
    app.evaluate("refreshLibraries()")
    app.wait_for_timeout(400)
    assert app.locator("#library-bar").is_visible()


# ── Holding playback while the drive is missing (2026-11-19) ─────────
# 2026-11-18 13:15: the gateway came up before /Volumes/MEDIA was
# unlocked, every music track got a 403 from the file server, and the
# skip-on-error rule ran through ten queued tracks in fifteen seconds.
# A missing drive is not a bad track: hold, and resume when it is back.

def _localfs_queue(app, n=3, book=False):
    tracks = [{"url": f"http://192.168.1.10:8200/localfs/stream/id{i}",
               "title": f"T{i}", "artist": "A", "album": "B",
               "art": "", "mime": ""} for i in range(n)]
    app.evaluate(f"""
      browserQueue = {tracks!r};
      browserIdx = 0;
      browserQueueIsBook = {str(book).lower()};
      activeDevice = "browser";
      document.getElementById('browser-audio').src =
        '/stream?url=' + encodeURIComponent({tracks[0]['url']!r});
    """)
    return tracks


def _fail_current(app, code=4):
    app.evaluate(f"""
      const a = document.getElementById('browser-audio');
      Object.defineProperty(a, 'error', {{
        configurable: true, get: () => ({{ code: {code}, message: 'synthetic' }})
      }});
      a.dispatchEvent(new Event('error'));
    """)


def _count_plays(app):
    # A trailing statement on purpose: an expression that evaluates to a
    # function is CALLED by Playwright, which would count as a play.
    app.evaluate("browserAudio.play = () => { window.__played = true; "
                 "return Promise.resolve(); }; 0")


def _hold(app, gateway):
    """Music waiting, a music track fails → held on track 0."""
    gateway.libraries = [_waiting()]
    app.reload()
    _localfs_queue(app)
    _fail_current(app)
    app.wait_for_function("_libHold !== null", timeout=3000)


def test_a_missing_music_drive_holds_instead_of_skipping(gateway, app):
    _hold(app, gateway)
    app.wait_for_timeout(2000)            # longer than the 1.5 s skip delay
    assert app.evaluate("browserIdx") == 0
    assert app.evaluate("_libHold.label") == "Music"
    assert "drive is back" in app.locator("#toast").text_content()
    assert "paused" in app.locator("#library-bar").inner_text()
    req = gateway.wait_for_request("/api/client_log", method="POST",
                                   match=lambda r: "library_hold" in r["body"],
                                   timeout=2.0)
    assert req is not None


def test_a_bad_track_on_a_mounted_drive_still_skips(gateway, app):
    """The hold is for a missing drive only — a genuinely broken file
    must not freeze the queue."""
    gateway.libraries = [_active("Music", "/Volumes/MEDIA/Music")]
    app.reload()
    _localfs_queue(app)
    _fail_current(app)
    app.wait_for_timeout(2200)
    assert app.evaluate("browserIdx") == 1
    assert app.evaluate("_libHold") is None


def test_a_stream_that_is_not_a_library_file_still_skips(gateway, app):
    gateway.libraries = [_waiting()]
    app.reload()
    app.evaluate("""
      browserQueue = [{url:'http://radio.example/a', title:'R0'},
                      {url:'http://radio.example/b', title:'R1'}];
      browserIdx = 0; browserQueueIsBook = false; activeDevice = 'browser';
      document.getElementById('browser-audio').src = '/stream?url=x';
    """)
    _fail_current(app)
    app.wait_for_timeout(2200)
    assert app.evaluate("browserIdx") == 1


def test_a_book_is_not_held_for_the_music_drive(gateway, app):
    """The roots are independent: books on a mounted disk play on (and a
    broken chapter skips) while only the music drive is missing."""
    gateway.libraries = [_active(), _waiting()]
    app.reload()
    _localfs_queue(app, book=True)
    _fail_current(app)
    app.wait_for_timeout(2200)
    assert app.evaluate("browserIdx") == 1
    assert app.evaluate("_libHold") is None


def test_playback_resumes_by_itself_when_the_drive_is_back(gateway, app):
    _hold(app, gateway)
    gateway.libraries = [_active("Music", "/Volumes/MEDIA/Music")]
    app.evaluate("refreshLibraries()")    # what the `libraries` SSE event does
    app.wait_for_function("_libHold === null", timeout=3000)
    assert "back" in app.locator("#toast").text_content()
    assert app.evaluate("browserIdx") == 0
    assert "id0" in app.evaluate("decodeURIComponent(browserAudio.src)")
    app.wait_for_selector("#library-bar", state="hidden", timeout=3000)


def test_a_long_hold_loads_the_track_but_does_not_start_it(gateway, app):
    """Music suddenly starting an hour after you walked away is worse
    than one tap."""
    _hold(app, gateway)
    _count_plays(app)
    app.evaluate("_libHold.at -= 16 * 60 * 1000")
    gateway.libraries = [_active("Music", "/Volumes/MEDIA/Music")]
    app.evaluate("refreshLibraries()")
    app.wait_for_function("_libHold === null", timeout=3000)
    assert app.evaluate("!!window.__played") is False
    assert "press ▶" in app.locator("#toast").text_content()
    assert "id0" in app.evaluate("decodeURIComponent(browserAudio.src)")


def test_play_while_held_retries_the_held_track(gateway, app):
    _hold(app, gateway)
    app.evaluate("browserAudio.removeAttribute('src')")
    app.evaluate("control({action:'pause'})")
    assert "id0" in app.evaluate("decodeURIComponent(browserAudio.src)")
    assert app.evaluate("browserIdx") == 0


def test_stop_while_held_means_nothing_resumes(gateway, app):
    _hold(app, gateway)
    app.evaluate("control({action:'stop'})")
    _count_plays(app)
    gateway.libraries = [_active("Music", "/Volumes/MEDIA/Music")]
    app.evaluate("refreshLibraries()")
    app.wait_for_timeout(500)
    assert app.evaluate("!!window.__played") is False
    assert app.evaluate("browserQueue.length") == 0
