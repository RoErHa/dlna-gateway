"""HTML simplifications agreed with the user — these tests will fail until
the index.html / app.js cleanup lands."""


def test_server_dropdown_removed(app):
    assert app.locator("#server-sel").count() == 0


def test_settings_button_removed(app):
    assert app.locator("#btn-settings").count() == 0


def test_settings_modal_removed(app):
    assert app.locator("#settings-modal").count() == 0


def test_header_status_label_visible(app):
    # Disc label remains the source of truth for server status
    assert app.locator("#disc-label").is_visible()
    assert app.locator("#out-sel, #output-sel").first.is_visible()
