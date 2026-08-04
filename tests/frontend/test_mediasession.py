"""MediaSession.playbackState tracks the <audio> element's play/pause.

A correct playbackState is what lets CarPlay / the lock screen show the right
control and deliver a dependable one-tap PLAY back to us after a phone-call
interruption (iOS won't let a backgrounded web page auto-resume without that
gesture). We drive it off the element's own play/pause events so it's correct
regardless of what triggered the change.
"""


def _state(app):
    return app.evaluate(
        "('mediaSession' in navigator) ? navigator.mediaSession.playbackState : 'unsupported'")


def test_play_event_sets_playing(app, gateway):
    if _state(app) == "unsupported":
        return  # engine without MediaSession — nothing to assert
    app.evaluate("""
      const a = document.getElementById('browser-audio');
      a.dispatchEvent(new Event('play'));
    """)
    assert app.evaluate("navigator.mediaSession.playbackState") == "playing"


def test_pause_event_sets_paused(app, gateway):
    if _state(app) == "unsupported":
        return
    app.evaluate("""
      const a = document.getElementById('browser-audio');
      a.dispatchEvent(new Event('play'));    // start from a known 'playing'
      a.dispatchEvent(new Event('pause'));
    """)
    assert app.evaluate("navigator.mediaSession.playbackState") == "paused"


def test_interruption_then_resume_round_trip(app, gateway):
    """Simulate a phone call: playing → pause (call) → play (one-tap resume)."""
    if _state(app) == "unsupported":
        return
    seq = app.evaluate("""
      (() => {
        const a = document.getElementById('browser-audio');
        const out = [];
        a.dispatchEvent(new Event('play'));   out.push(navigator.mediaSession.playbackState);
        a.dispatchEvent(new Event('pause'));  out.push(navigator.mediaSession.playbackState);
        a.dispatchEvent(new Event('play'));   out.push(navigator.mediaSession.playbackState);
        return out;
      })()
    """)
    assert seq == ["playing", "paused", "playing"]
