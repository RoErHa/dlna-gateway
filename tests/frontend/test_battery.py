"""iOS battery — what an OPEN but IDLE PWA is allowed to cost.

The 2026-08 report was "the app drains the battery even when it's open and
NOT playing". Two independent causes, both guarded here:

1. Fixed poll intervals. state/index/servers/renderers ran at a flat
   1s/2s/8s/10s no matter what the app was doing, so an idle-but-open PWA
   fired ~2,600 requests/hour — ~6,200/hr with a UPnP output selected,
   where each /api/renderer_state also costs the gateway two SOAP
   round-trips to the renderer. At better than one request per second the
   iPhone radio never reaches its low-power idle state. The polls now run
   in a slow tier whenever nothing is happening (SSE pushes the changes),
   and are promoted back to their original interval the moment something
   is.

2. `#player.playing.is-audio .art` is an 8s INFINITE rotation, and nothing
   removed `playing` on the browser-output path — pausing left a full-size
   album cover spinning at display refresh rate forever, a compositor wake
   every frame for as long as the app stayed open.

Plus the spinner watchdog: every loader is "show spinner → fetch →
`if(!r) return;`", so a failed fetch used to leave an infinite CSS rotation
running with no feedback.
"""


def _settle(app, ms=2500):
    """Let boot traffic (first polls, SSE handshake, playlists) drain."""
    app.wait_for_timeout(ms)


# ── 1. Idle poll cadence ──────────────────────────────────────────


def test_idle_does_not_poll_index_status(app, gateway):
    """No rebuild running → /api/index/status is a static line. At the old
    flat 2s it cost 30 requests/minute forever; the slow tier is 60s, so a
    6s idle window must see at most one."""
    _settle(app)
    gateway.clear_requests()
    app.wait_for_timeout(6000)
    n = len(gateway.captured(path_contains="/api/index/status"))
    assert n <= 1, f"idle PWA polled /api/index/status {n}× in 6s (was ~3)"


def test_idle_upnp_output_does_not_poll_renderer_state(app, gateway):
    """The expensive one: with a UPnP renderer selected and nothing playing,
    the old 1 Hz state poll meant 3,600 requests/hour AND 7,200 SOAP calls
    to the renderer. A stopped renderer must fall to the slow tier."""
    app.evaluate("""
      activeDevice = 'upnp:test-renderer';
      _upnpAlive = false;
      bumpPolling();
    """)
    _settle(app, 1500)
    gateway.clear_requests()
    app.wait_for_timeout(6000)
    n = len(gateway.captured(path_contains="/api/renderer_state"))
    assert n <= 1, f"idle PWA polled /api/renderer_state {n}× in 6s (was ~6)"


def test_playing_upnp_renderer_keeps_the_1hz_tier(app, gateway):
    """The throttle must not cost responsiveness: a LIVE renderer still gets
    the 1 Hz seek-bar poll it always had. The tier is re-read from each
    snapshot, so the stub has to keep reporting the renderer as alive."""
    gateway.renderer_state = {**gateway.renderer_state,
                              "state": "playing", "alive": True,
                              "duration": 300, "position": 10}
    app.evaluate("""
      activeDevice = 'upnp:test-renderer';
      _upnpAlive = true;
      bumpPolling();
    """)
    app.wait_for_timeout(500)
    gateway.clear_requests()
    app.wait_for_timeout(4000)
    n = len(gateway.captured(path_contains="/api/renderer_state"))
    assert n >= 2, f"playing renderer only polled {n}× in 4s — expected ~4"


def test_hot_tiers_match_the_original_intervals(app):
    """The fast tier is the pre-throttle interval, so anything the app
    considers 'active' behaves exactly as it always did."""
    tiers = app.evaluate(
        "Object.fromEntries(Object.entries(_LOOPS).map(([k,v]) => [k, v.fast]))")
    assert tiers == {"state": 1000, "index": 2000,
                     "servers": 8000, "renderers": 10000}


def test_a_failed_poll_does_not_kill_its_loop(app, gateway):
    """A rejected fetch inside a self-scheduling loop must still re-arm —
    otherwise one blip freezes the UI until the next visibility change."""
    app.evaluate("""
      activeDevice = 'upnp:test-renderer';
      _upnpAlive = true;
      window._origApi = window.api;
      window.api = async () => { throw new Error('boom'); };
      bumpPolling();
    """)
    app.wait_for_timeout(2500)
    app.evaluate("window.api = window._origApi;")
    gateway.clear_requests()
    app.wait_for_timeout(3000)
    n = len(gateway.captured(path_contains="/api/renderer_state"))
    assert n >= 1, "state loop died after a throwing poll"


# ── 2. The vinyl rotation ─────────────────────────────────────────


def _has_playing(app):
    return app.evaluate(
        "document.getElementById('player').classList.contains('playing')")


def test_pause_stops_the_vinyl_animation(app, gateway):
    app.evaluate("""
      activeDevice = 'browser';
      document.getElementById('player').className = 'playing is-audio';
      document.getElementById('browser-audio').dispatchEvent(new Event('pause'));
    """)
    assert not _has_playing(app), \
        "#player kept .playing after pause — the 8s art rotation never stops"


def test_play_restores_the_vinyl_animation(app, gateway):
    app.evaluate("""
      activeDevice = 'browser';
      document.getElementById('player').className = 'is-audio';
      document.getElementById('browser-audio').dispatchEvent(new Event('play'));
    """)
    assert _has_playing(app), "#player must regain .playing on play"


def test_vinyl_round_trip(app, gateway):
    seq = app.evaluate("""
      (() => {
        const a = document.getElementById('browser-audio');
        const p = document.getElementById('player');
        activeDevice = 'browser';
        const out = [];
        a.dispatchEvent(new Event('play'));  out.push(p.classList.contains('playing'));
        a.dispatchEvent(new Event('pause')); out.push(p.classList.contains('playing'));
        a.dispatchEvent(new Event('play'));  out.push(p.classList.contains('playing'));
        return out;
      })()
    """)
    assert seq == [True, False, True]


# ── 3. Spinner watchdog ───────────────────────────────────────────


def test_spinner_watchdog_stops_an_orphaned_spinner(app, gateway):
    """A loader whose fetch never lands must not leave an infinite CSS
    rotation running (and must tell the user something went wrong)."""
    app.evaluate("""
      _SPINNER_TIMEOUT_MS = 300;
      showSpinner(document.getElementById('item-list'));
    """)
    assert app.evaluate(
        "!!document.getElementById('item-list').querySelector('.spinner')")
    app.wait_for_timeout(900)
    assert not app.evaluate(
        "!!document.getElementById('item-list').querySelector('.spinner')"), \
        "orphaned spinner kept animating past the watchdog"
    assert "Couldn't load" in app.evaluate(
        "document.getElementById('item-list').textContent")


def test_spinner_watchdog_does_not_fire_when_content_arrives(app, gateway):
    """The watchdog must never overwrite real content."""
    app.evaluate("""
      _SPINNER_TIMEOUT_MS = 300;
      const el = document.getElementById('item-list');
      showSpinner(el);
      el.innerHTML = '<div class="row">Real content</div>';
    """)
    app.wait_for_timeout(900)
    assert "Real content" in app.evaluate(
        "document.getElementById('item-list').textContent")
    assert "Couldn't load" not in app.evaluate(
        "document.getElementById('item-list').textContent")
