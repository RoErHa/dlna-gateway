"""Transport controls — including the playlist-first-track regression."""


def _seed_playlist(gateway, name="My Mix", n_tracks=3):
    tracks = [gateway.add_track("Artist A", "Album X", f"Track {i+1}",
                                url=f"http://stub/track{i+1}.flac")
              for i in range(n_tracks)]
    gateway.add_playlist("pl-1", name, tracks)
    return tracks


def test_all_transport_buttons_present(app):
    for sel in ("#btn-prev", "#btn-rew", "#btn-stop", "#btn-pp",
                "#btn-fwd", "#btn-next"):
        assert app.locator(sel).count() == 1, f"missing {sel}"


def test_play_button_initial_label(app):
    assert "Play" in app.locator("#btn-pp").text_content()


def test_stop_resets_player(app):
    # Start a track, then stop
    app.evaluate("""
      startPlay({url:'http://stub/x.flac', title:'Test', artist:'A',
                 album:'B', art:'', type:'audio'}, null);
    """)
    app.locator("#btn-stop").click()
    app.wait_for_timeout(200)
    assert "Nothing playing" in app.locator("#np-title").text_content()


def test_next_idle_is_noop(app, gateway):
    # No queue — next should not error and not POST
    app.locator("#btn-next").click()
    app.wait_for_timeout(200)
    # No API control posted in browser mode
    posts = [r for r in gateway.captured(method="POST")
             if r["path"] == "/api/control"]
    assert posts == []


def test_seek_back_30_posts_control_for_upnp(page, stub, gateway):
    gateway.renderers = [{"udn": "uuid:naim", "name": "Naim"}]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#output-sel option').length >= 2", timeout=4000)
    page.select_option("#output-sel", "upnp:uuid:naim")
    page.locator("#btn-rew").click()
    req = gateway.wait_for_request("/api/control", method="POST", timeout=2.0)
    assert req is not None
    body = req["body"]
    assert "seek" in body and "-30" in body


def test_seek_fwd_30_posts_control_for_upnp(page, stub, gateway):
    gateway.renderers = [{"udn": "uuid:naim", "name": "Naim"}]
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#output-sel option').length >= 2", timeout=4000)
    page.select_option("#output-sel", "upnp:uuid:naim")
    page.locator("#btn-fwd").click()
    req = gateway.wait_for_request("/api/control", method="POST", timeout=2.0)
    assert req is not None
    body = req["body"]
    assert "seek" in body and "30" in body and "-30" not in body


# ───── REGRESSION GUARD: playlist play must auto-start track 1 ─────
def test_playlist_play_starts_first_track(page, stub, gateway):
    """Pressing the rendered ▶ Play button on a playlist must begin
    streaming the first track, not require the user to press Next.

    This goes through the SAME path the user takes: render the playlists
    panel, click the playlist row to open it, click its ▶ Play button.

    Acceptable outcomes:
      a) <audio>.currentTime > 0 (real playback started), OR
      b) a `play_rejected` POST appears (autoplay was blocked but the
         frontend correctly surfaced it). The second branch IS still a
         bug, but it's a *visible* bug; the regression we guard against
         here is silent failure (audio.src never even set).
    """
    tracks = _seed_playlist(gateway, "Mix", 3)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('source-sel') && !document.getElementById('source-sel').textContent.includes('Scanning')",
        timeout=5000)
    # Wait for the playlist row to render in the right-hand panel
    page.wait_for_function(
        "document.querySelectorAll('#pl-list .pl-item').length >= 2", timeout=4000)
    # Click the playlist to open it
    page.locator("#pl-list .pl-item", has_text="Mix").click()
    # Wait for the ▶ Play button to render in the actions strip
    page.wait_for_selector("#pl-actions button:has-text('Play')", timeout=2000)
    gateway.clear_requests()
    # Click the actual rendered ▶ Play button — same gesture chain a user does
    page.locator("#pl-actions button", has_text="Play").click()
    # Within 2s: audio.src must point at one of the queued tracks
    page.wait_for_function(
        "document.getElementById('browser-audio').src.includes('/stream?url=')",
        timeout=2000)
    src = page.evaluate("document.getElementById('browser-audio').src")
    track_urls = [t["url"] for t in tracks]
    assert any(t.split("/")[-1] in src for t in track_urls), (
        f"audio.src {src!r} does not match any queued track")
    # And either it started playing OR a play_rejected event was logged
    page.wait_for_timeout(1500)
    started = page.evaluate(
        "document.getElementById('browser-audio').currentTime > 0")
    rejected = any(r["path"] == "/api/client_log" and "play_rejected" in r["body"]
                   for r in gateway.captured(method="POST"))
    assert started or rejected, (
        "Playlist ▶ Play must either start audio OR surface autoplay rejection; "
        "silent failure (neither) is the regression.")


def test_next_advances_browser_queue(page, stub, gateway):
    tracks = _seed_playlist(gateway, "Mix", 3)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('source-sel') && !document.getElementById('source-sel').textContent.includes('Scanning')", timeout=5000)
    # Build the queue directly to remove dependency on first-track auto-start
    page.evaluate(f"""
      browserQueue = {tracks!r};
      browserIdx = 0;
      _browserPlayIdx(0);
    """)
    page.wait_for_function(
        "document.getElementById('browser-audio').src.includes('/stream')",
        timeout=2000)
    page.locator("#btn-next").click()
    page.wait_for_timeout(300)
    idx = page.evaluate("browserIdx")
    assert idx == 1


def test_prev_returns_browser_queue(page, stub, gateway):
    tracks = _seed_playlist(gateway, "Mix", 3)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('source-sel') && !document.getElementById('source-sel').textContent.includes('Scanning')", timeout=5000)
    page.evaluate(f"""
      browserQueue = {tracks!r};
      browserIdx = 1;
      _browserPlayIdx(1);
    """)
    page.wait_for_function(
        "document.getElementById('browser-audio').src.includes('/stream')",
        timeout=2000)
    page.locator("#btn-prev").click()
    page.wait_for_timeout(300)
    idx = page.evaluate("browserIdx")
    assert idx == 0
