"""One-tap resume after an interruption (phone call, Siri, another app).

iOS pauses <audio> for a call and never resumes a web page by itself, so the
best the PWA can do is make the ONE tap that follows work — from the lock
screen, CarPlay, AirPods or the app's own ▶. On iOS 26 a home-screen web app
answers a plain play() on an interrupted element with a promise that
resolves and no sound (WebKit bug 295518): nothing errors, nothing reaches
gateway.log, and the owner has to open the app and start over.

The fix: tell iOS's pauses apart from our own, and on resume after one of
them (or after any long pause) assign the source again at the position it
stopped, then play(). These tests drive a real <audio> element against the
stub's silent WAV, muted so the engine allows playback without a gesture.
Calling the element's own pause() stands in for iOS: that is exactly what an
interruption looks like from the page — a pause event none of our code made.
"""
import json
import re

import pytest

TRACK = {"url": "http://stub/t0.flac", "title": "T0", "artist": "A",
         "album": "B", "art": "", "mime": ""}


@pytest.fixture
def playing(app, gateway):
    """One 30 s track, playing, at ~12 s. MediaSession handlers are captured
    into window.__ms so a test can press the lock screen's ▶."""
    gateway.stream_seconds = 30
    app.evaluate("""async (track) => {
      window.__ms = {};
      if ('mediaSession' in navigator) {
        navigator.mediaSession.setActionHandler = (a, h) => { window.__ms[a] = h; };
      }
      browserQueue = [track]; browserIdx = 0; activeDevice = 'browser';
      const a = document.getElementById('browser-audio');
      a.muted = true;
      _browserPlayIdx(0);
      if (a.readyState < 1) {
        await new Promise(r => a.addEventListener('loadedmetadata', r, {once: true}));
      }
      a.currentTime = 12;
      if (a.paused) await a.play();
      await new Promise(r => setTimeout(r, 300));
    }""", TRACK)
    assert not app.evaluate("document.getElementById('browser-audio').paused")
    return app


def _interrupt(app):
    """What a phone call looks like to the page: a pause nobody here made."""
    app.evaluate("document.getElementById('browser-audio').pause()")


def _press_lock_screen_play(app):
    if app.evaluate("typeof window.__ms.play === 'function'"):
        app.evaluate("window.__ms.play()")
    else:                       # engine without MediaSession: the app's ▶
        app.evaluate("control({action: 'pause'})")


def _src(app):
    return app.evaluate("document.getElementById('browser-audio').getAttribute('src')")


def _reports(gateway, kind):
    out = []
    for r in gateway.requests:
        if r["path"] == "/api/client_log" and r["method"] == "POST":
            body = json.loads(r["body"] or "{}")
            if body.get("kind") == kind:
                out.append(body)
    return out


def test_an_interruption_is_told_apart_from_our_own_pause(playing):
    _interrupt(playing)
    assert playing.evaluate("_pausedBySystem") is True
    _press_lock_screen_play(playing)
    playing.wait_for_timeout(500)
    playing.evaluate("control({action: 'pause'})")      # the app's own ⏸
    assert playing.evaluate("_pausedBySystem") is False


def test_resume_after_an_interruption_reloads_at_the_position(playing, gateway):
    """The fix itself: the tap re-assigns the source where it stopped —
    a plain play() is what stays silent on iOS 26."""
    _interrupt(playing)
    gateway.clear_requests()
    _press_lock_screen_play(playing)
    assert re.search(r"#t=1[23]\.\d$", _src(playing)), _src(playing)
    assert gateway.wait_for_request("/stream", method="GET", timeout=3.0), \
        "resume did not fetch the source again"
    playing.wait_for_timeout(800)
    a = "document.getElementById('browser-audio')"
    assert playing.evaluate(f"!{a}.paused")
    assert playing.evaluate(f"{a}.currentTime") >= 11.5, "resumed from the start"


def test_a_quick_pause_by_hand_is_a_plain_resume(playing, gateway):
    """Pausing and resuming by hand must not rebuffer."""
    playing.evaluate("control({action: 'pause'})")
    gateway.clear_requests()
    playing.evaluate("control({action: 'pause'})")      # toggles back to play
    assert "#t=" not in _src(playing)
    gateway.wait_for_request("/api/client_log", method="POST",
                             match=lambda r: '"resume"' in (r["body"] or ""),
                             timeout=3.0)
    assert _reports(gateway, "resume")[0]["reload"] is False


def test_a_long_pause_by_hand_reloads(playing):
    """After a long pause the relay's connection may be gone as well."""
    playing.evaluate("control({action: 'pause'})")
    playing.evaluate("_pausedAt -= 30000")
    playing.evaluate("control({action: 'pause'})")
    assert "#t=" in _src(playing)


def test_resume_and_its_outcome_reach_the_gateway_log(playing, gateway):
    """'It didn't resume' must leave a line: whether the tap arrived, whether
    the element was reloaded, and whether time then actually moved."""
    _interrupt(playing)
    gateway.clear_requests()
    _press_lock_screen_play(playing)
    assert gateway.wait_for_request(
        "/api/client_log", method="POST",
        match=lambda r: '"resume_check"' in (r["body"] or ""), timeout=8.0)
    resume = _reports(gateway, "resume")[0]
    assert resume["by_system"] is True and resume["reload"] is True
    assert resume["position"] >= 12
    assert "standalone" in resume and "visibility" in resume
    check = _reports(gateway, "resume_check")[0]
    assert check["advanced"] is True and check["paused"] is False


def test_a_network_retry_keeps_the_position(playing):
    """A network error used to re-arm `src = src`, restarting the track at
    0:00 — what a dropped connection during a long call turned into."""
    playing.evaluate("""
      const a = document.getElementById('browser-audio');
      Object.defineProperty(a, 'error', {configurable: true,
        get: () => ({code: 2, message: 'synthetic'})});
      a.dispatchEvent(new Event('error'));
    """)
    playing.wait_for_timeout(1200)                      # retry fires at 800 ms
    assert re.search(r"#t=1[23]\.\d$", _src(playing)), _src(playing)


def test_the_retry_counter_ignores_the_resume_position(playing):
    """Keyed by the full URL, every retry would carry a new `#t=`, count as a
    new source, and retry forever."""
    _interrupt(playing)
    _press_lock_screen_play(playing)
    assert "#t=" in _src(playing)
    keys = playing.evaluate("""(() => {
      const a = document.getElementById('browser-audio');
      Object.defineProperty(a, 'error', {configurable: true,
        get: () => ({code: 2, message: 'synthetic'})});
      a.dispatchEvent(new Event('error'));
      return [..._audioRetryCount.keys()];
    })()""")
    assert keys and all("#" not in k for k in keys), keys


def test_live_radio_rejoins_the_stream_rather_than_seeking(app):
    app.evaluate("""
      const a = document.getElementById('browser-audio');
      a.muted = true;
      a.setAttribute('src', '/radio_stream?url=' + encodeURIComponent('http://x/live'));
      _reloadAt(42);
    """)
    assert "#t=" not in _src(app)


def test_a_pause_on_a_paused_element_does_not_mislabel_the_next_interruption(playing):
    """pause() on a paused element fires no event; a flag left set would make
    the next real interruption look like ours — and resume without a reload."""
    playing.evaluate("control({action: 'pause'})")
    playing.evaluate("_pauseBrowserAudio()")            # already paused: no event
    playing.evaluate("control({action: 'pause'})")      # resume by hand
    playing.wait_for_timeout(400)
    _interrupt(playing)
    assert playing.evaluate("_pausedBySystem") is True
