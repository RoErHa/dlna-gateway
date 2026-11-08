"""Browser <audio> error discrimination + autoplay-rejection surfacing."""


def _setup_queue(app, n=3):
    """Build a browser queue without actually starting playback."""
    tracks = [{"url": f"http://stub/t{i}.flac", "title": f"T{i}",
               "artist": "A", "album": "B", "art": "", "mime": ""}
              for i in range(n)]
    app.evaluate(f"""
      browserQueue = {tracks!r};
      browserIdx = 0;
      activeDevice = "browser";
      document.getElementById('browser-audio').src =
        '/stream?url=' + encodeURIComponent('http://stub/t0.flac');
    """)
    return tracks


def _fire_error(app, code: int):
    """Inject a synthetic MediaError on the audio element."""
    app.evaluate(f"""
      const a = document.getElementById('browser-audio');
      // Stub error.code to {code}
      Object.defineProperty(a, 'error', {{
        configurable: true, get: () => ({{ code: {code}, message: 'synthetic' }})
      }});
      a.dispatchEvent(new Event('error'));
    """)


def test_code_1_aborted_no_skip(app, gateway):
    _setup_queue(app)
    _fire_error(app, 1)
    app.wait_for_timeout(300)
    # Index should NOT advance
    assert app.evaluate("browserIdx") == 0


def test_code_4_unsupported_skips(app, gateway):
    _setup_queue(app, n=3)
    _fire_error(app, 4)
    # Skip is delayed by 1500ms
    app.wait_for_timeout(2000)
    assert app.evaluate("browserIdx") == 1


def test_every_audio_error_logs_to_client_log(app, gateway):
    _setup_queue(app)
    _fire_error(app, 4)
    req = gateway.wait_for_request("/api/client_log", method="POST", timeout=2.0)
    assert req is not None
    assert "audio_error" in req["body"]
    assert '"code":4' in req["body"] or "'code': 4" in req["body"]


def test_autoplay_block_surfaces_toast(app, gateway):
    """Force browserAudio.play() to reject with NotAllowedError and verify
    the toast + play_rejected POST land."""
    app.evaluate("""
      const a = document.getElementById('browser-audio');
      a.play = () => Promise.reject(Object.assign(new Error('autoplay'), {name:'NotAllowedError'}));
      _playBrowserAudio('test');
    """)
    app.wait_for_function(
        "document.getElementById('toast').classList.contains('show')",
        timeout=2000)
    txt = app.locator("#toast").text_content()
    assert "blocked" in txt.lower()
    req = gateway.wait_for_request("/api/client_log", method="POST", timeout=2.0)
    assert req is not None and "play_rejected" in req["body"]


def test_stop_is_not_reported_as_an_unsupported_track(app, gateway):
    """Stop used to set `src = ""`, which every engine answers with a
    MediaError 4 against the page's own URL. The handler then ran with the
    queue already emptied: an `audio_error` with a blank title went to the
    gateway and the user saw "Can't play … unsupported format" for pressing
    Stop (seen 2026-11-08 12:08:16 from Safari on the Mac)."""
    _setup_queue(app)
    toasts = []
    app.expose_function("__toastSeen", lambda t: toasts.append(t))
    app.evaluate("""
      const _t = toast;
      toast = (m, ...r) => { window.__toastSeen(String(m)); return _t(m, ...r); };
    """)
    gateway.clear_requests()
    app.locator("#btn-stop").click()
    app.wait_for_timeout(1000)
    errors = [r for r in gateway.captured(method="POST")
              if r["path"] == "/api/client_log" and "audio_error" in r["body"]]
    assert errors == [], errors
    assert not any("Can't play" in t or "Skipping" in t for t in toasts), toasts
    assert app.evaluate("browserQueue.length") == 0
