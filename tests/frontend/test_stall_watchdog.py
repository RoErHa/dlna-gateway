"""The stall watchdog — the "it just stopped mid-playlist" guard.

The browser queue only ever advanced on `ended` or `error`. An <audio>
element has a third resting state that fires NEITHER: buffer starved, no
more bytes arriving, and — from the element's point of view — no error.

That state was reached in the wild on 2026-08-25. gateway.log shows the last
byte of a track delivered at 10:22:32, then no further request, no
client_log entry, and nothing until the track was restarted by hand six
minutes later. Whatever starves the buffer (a dropped fetch, an iOS media
interruption, a relay that dumped the whole file into memory and lost it),
the player must not sit there wedged.

The watchdog polls currentTime while it believes it is playing. No progress
for _STALL_GRACE_MS → one recovery nudge (re-seek, which re-issues the
range request); still nothing → report and advance the queue.

`_STALL_GRACE_MS` is a const, so instead of shortening it these tests push
`_stallSince` into the past — the same thing from the watchdog's point of
view, and it keeps the shipped timing values under test rather than a
test-only copy of them.
"""


def _arm(app, n=3, poll_ms=100):
    """Build a browser queue and drive _stallCheck on a fast interval, with
    the element frozen in a "playing but not progressing" state."""
    tracks = [{"url": f"http://stub/t{i}.flac", "title": f"T{i}",
               "artist": "A", "album": "B", "art": "", "mime": ""}
              for i in range(n)]
    app.evaluate(f"""
      browserQueue = {tracks!r};
      browserIdx = 0;
      activeDevice = "browser";
      _stallWatchdogStop();
      // Freeze the element in a "playing but not progressing" state.
      const a = document.getElementById('browser-audio');
      Object.defineProperty(a, 'paused',      {{configurable:true, get:()=>false}});
      Object.defineProperty(a, 'ended',       {{configurable:true, get:()=>false}});
      Object.defineProperty(a, 'currentTime', {{configurable:true,
                                                get:()=>window.__pos,
                                                set:(v)=>{{window.__seeked=v;}}}});
      Object.defineProperty(a, 'buffered',    {{configurable:true,
                                                get:()=>({{length:0}})}});
      a.play = () => Promise.resolve();
      window.__pos = 10;
      window.__seeked = null;
      window.__stallTicks = setInterval(_stallCheck, {poll_ms});
    """)
    return tracks


def test_progress_never_triggers_the_watchdog(app, gateway):
    """A slow-but-advancing stream must never be skipped. This is the
    regression that matters most: a watchdog that fires on a healthy track is
    worse than no watchdog."""
    _arm(app)
    for _ in range(8):
        app.evaluate("window.__pos += 1;")
        app.wait_for_timeout(150)
    assert app.evaluate("browserIdx") == 0
    assert app.evaluate("window.__seeked") is None


def test_a_paused_element_is_not_a_stall(app, gateway):
    """The user pressing pause stops currentTime too. That is not a fault."""
    _arm(app)
    app.evaluate("""
      const a = document.getElementById('browser-audio');
      Object.defineProperty(a,'paused',{configurable:true,get:()=>true});
    """)
    app.wait_for_timeout(1200)
    assert app.evaluate("browserIdx") == 0


def test_stall_nudges_before_it_gives_up(app, gateway):
    """First response to a stall is recovery, not a skip — a dropped fetch
    usually resumes on a re-seek and the listener never notices."""
    _arm(app)
    app.evaluate("_stallPos = window.__pos; _stallSince = Date.now() - 99999;")
    app.wait_for_timeout(600)
    assert app.evaluate("window.__seeked") is not None, "no recovery attempt"
    assert app.evaluate("browserIdx") == 0, "skipped without trying to recover"


def test_stall_that_survives_the_nudge_advances_the_queue(app, gateway):
    """The actual bug: nothing ever un-wedged the queue."""
    _arm(app)
    app.evaluate("_stallPos = window.__pos; _stallSince = Date.now() - 99999;")
    app.wait_for_timeout(400)          # nudge
    app.evaluate("_stallSince = Date.now() - 99999;")
    app.wait_for_timeout(600)          # give up
    assert app.evaluate("browserIdx") == 1, "queue still wedged after a stall"


def test_stall_on_the_last_track_stops_cleanly(app, gateway):
    """No next track to move to — reset the button rather than run past the
    end of the queue."""
    _arm(app, n=1)
    app.evaluate("_stallPos = window.__pos; _stallSince = Date.now() - 99999;")
    app.wait_for_timeout(400)
    app.evaluate("_stallSince = Date.now() - 99999;")
    app.wait_for_timeout(600)
    assert app.evaluate("browserIdx") == 0
    assert "Play" in app.evaluate("document.getElementById('btn-pp').textContent")


def test_stall_is_reported_to_the_gateway(app, gateway):
    """A stall that nobody logged is how this went undiagnosed for so long."""
    _arm(app)
    gateway.clear_requests()
    app.evaluate("_stallPos = window.__pos; _stallSince = Date.now() - 99999;")
    req = gateway.wait_for_request("/api/client_log", method="POST", timeout=3.0)
    assert req is not None
    assert "audio_stall" in req["body"]


def test_watchdog_does_not_run_for_upnp_output(app, gateway):
    """With a renderer selected the gateway owns advancing the queue; the
    browser element is idle and must not be second-guessed."""
    _arm(app)
    app.evaluate("activeDevice = 'upnp';")
    app.evaluate("_stallPos = window.__pos; _stallSince = Date.now() - 99999;")
    app.wait_for_timeout(800)
    assert app.evaluate("browserIdx") == 0
