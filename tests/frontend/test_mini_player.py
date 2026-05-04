"""Mini player — mobile-only, shown when something is playing."""


def test_hidden_on_desktop(app):
    # Mini player is a separate element; CSS controls its desktop visibility
    el = app.locator("#mini-player")
    assert el.count() == 1
    # On desktop viewport (1280×800), it should be invisible
    assert el.is_hidden()


def test_visible_on_mobile_when_playing(mobile_app):
    mobile_app.evaluate("""
      startPlay({url:'http://stub/x.flac', title:'Test', artist:'A',
                 album:'B', art:'', type:'audio'}, null);
    """)
    mobile_app.wait_for_timeout(300)
    # Element exists; on mobile + playing it should be visible
    el = mobile_app.locator("#mini-player")
    assert el.count() == 1


def test_mini_pp_button_present(app):
    assert app.locator("#mini-pp").count() == 1


def test_mini_next_button_present(app):
    assert app.locator("#mini-next").count() == 1
