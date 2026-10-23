"""Now-playing action row: ⭐ Fav, ＋ Playlist, ✏️ Edit."""


def test_actions_hidden_when_idle(app):
    assert app.locator("#np-actions").is_hidden()


def _start_track(app):
    """Trigger a play() in browser mode by calling startPlay() directly via JS."""
    app.evaluate("""
      startPlay({url:'http://stub/x.flac', title:'Test', artist:'A', album:'B',
                 art:'', type:'audio'}, null);
    """)
    app.wait_for_function(
        "document.getElementById('np-actions').style.display === 'flex'",
        timeout=2000)


def test_actions_visible_after_play(app):
    _start_track(app)
    assert app.locator("#np-actions").is_visible()


def test_fav_button_posts_to_favourites(app, gateway):
    _start_track(app)
    app.locator("#np-btn-fav").click()
    req = gateway.wait_for_request("/api/playlist/add", timeout=2.0)
    assert req is not None
    assert req["query"].get("pl") == "__favourites__"


def test_edit_button_opens_modal_prefilled(app):
    _start_track(app)
    app.locator("#np-btn-edit").click()
    app.wait_for_selector("#edit-modal.open", timeout=1500)
    assert app.locator("#edit-title").input_value() == "Test"
    assert app.locator("#edit-artist").input_value() == "A"
    assert app.locator("#edit-album").input_value() == "B"


def test_add_to_playlist_button_with_no_custom_playlists_toasts(app):
    _start_track(app)
    # Wait for the "Streaming in browser" toast to fade so we can read the next one
    app.wait_for_function(
        "!document.getElementById('toast').classList.contains('show')",
        timeout=4000)
    app.locator("#np-btn-add").click()
    app.wait_for_function(
        "document.getElementById('toast').classList.contains('show')",
        timeout=2000)
    txt = app.locator("#toast").text_content()
    assert "playlist" in txt.lower(), f"unexpected toast: {txt!r}"


def test_renderer_advance_moves_np_track_to_the_new_song(app, gateway):
    """A UPnP renderer advances through the queue by itself. npTrack must
    follow the renderer snapshot's `uri`, or Lyrics/Artist keep showing
    the FIRST track of the album/playlist for every later one."""
    gateway.renderer_state = {**gateway.renderer_state,
                              "state": "playing", "alive": True,
                              "duration": 300, "position": 10,
                              "queue_pos": 1, "queue_len": 2,
                              "uri": "http://stub/1.flac",
                              "title": "One", "media_title": "One",
                              "artist": "Artist A", "album": "LP"}
    app.evaluate("""
      setNpTrack({url:'http://stub/1.flac', title:'One', artist:'Artist A',
                  album:'LP', art:'', mime:''});
      activeDevice = 'upnp:test-renderer';
      _upnpAlive = true;
      bumpPolling();
    """)
    gateway.renderer_state = {**gateway.renderer_state,
                              "queue_pos": 2, "uri": "http://stub/2.flac",
                              "title": "Two", "media_title": "Two",
                              "artist": "Artist B"}
    app.wait_for_function("npTrack && npTrack.url === 'http://stub/2.flac'",
                          timeout=4000)
    assert app.evaluate("npTrack.artist") == "Artist B"
    assert app.evaluate("npTrack.title") == "Two"


def test_renderer_advance_swaps_the_cover_art(app, gateway):
    """In a playlist each track has its own cover; the big now-playing
    art must follow the renderer, not stay on the queue's first track."""
    gateway.renderer_state = {**gateway.renderer_state,
                              "state": "playing", "alive": True,
                              "duration": 300, "position": 10,
                              "queue_pos": 1, "queue_len": 2,
                              "uri": "http://stub/1.flac",
                              "title": "One", "media_title": "One",
                              "artist": "A", "album": "LP1",
                              "art": "http://stub/cover1.jpg"}
    app.evaluate("""
      setNpTrack({url:'http://stub/1.flac', title:'One', artist:'A',
                  album:'LP1', art:'http://stub/cover1.jpg', mime:''});
      activeDevice = 'upnp:test-renderer';
      _upnpAlive = true;
      bumpPolling();
    """)
    gateway.renderer_state = {**gateway.renderer_state,
                              "queue_pos": 2, "uri": "http://stub/2.flac",
                              "title": "Two", "media_title": "Two",
                              "album": "LP2", "art": "http://stub/cover2.jpg"}
    app.wait_for_function(
        "(document.querySelector('#art img')?.getAttribute('src')||'')"
        ".includes(encodeURIComponent('http://stub/cover2.jpg'))",
        timeout=4000)
