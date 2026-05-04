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
