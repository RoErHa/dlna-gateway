"""Edit-metadata modal."""


def _open_modal(app, *, title="Test", artist="A", album="B", genre="Rock"):
    app.evaluate(f"""
      openEditModal({{
        url:'http://stub/x.flac',
        title:{title!r}, artist:{artist!r}, album:{album!r}, genre:{genre!r},
      }});
    """)
    app.wait_for_selector("#edit-modal.open", timeout=1500)


def test_modal_hidden_by_default(app):
    cls = app.locator("#edit-modal").get_attribute("class") or ""
    assert "open" not in cls


def test_modal_opens_prefilled(app):
    _open_modal(app)
    assert app.locator("#edit-title").input_value() == "Test"
    assert app.locator("#edit-artist").input_value() == "A"
    assert app.locator("#edit-album").input_value() == "B"
    assert app.locator("#edit-genre").input_value() == "Rock"


def test_cancel_closes(app):
    _open_modal(app)
    app.locator("#edit-cancel").click()
    app.wait_for_function(
        "!document.getElementById('edit-modal').classList.contains('open')",
        timeout=1500)


def test_overlay_click_closes(app):
    _open_modal(app)
    # Click on the overlay (outside the modal) — coordinates near top-left
    app.locator("#edit-modal").click(position={"x": 10, "y": 10})
    app.wait_for_function(
        "!document.getElementById('edit-modal').classList.contains('open')",
        timeout=1500)


def test_save_with_no_changes_does_not_post(app, gateway):
    _open_modal(app)
    app.locator("#edit-save").click()
    app.wait_for_timeout(400)
    assert not gateway.captured(path_contains="/api/edit_track", method="POST")


def test_save_with_change_sends_only_changed_fields(app, gateway):
    _open_modal(app, title="Old", artist="A", album="B", genre="Rock")
    app.locator("#edit-title").fill("New Title")
    app.locator("#edit-save").click()
    req = gateway.wait_for_request("/api/edit_track", method="POST", timeout=2.0)
    assert req is not None
    body = req["body"]
    assert "New Title" in body
    # Untouched fields should NOT be in the changed payload
    # The body looks like {"title":"New Title","url":"..."} — just check no
    # spurious "artist":"A"
    import json
    parsed = json.loads(body)
    assert "title" in parsed
    assert "artist" not in parsed
    assert "album" not in parsed
    assert "genre" not in parsed


def test_enter_in_input_triggers_save(app, gateway):
    _open_modal(app, title="Old")
    app.locator("#edit-title").fill("Edited")
    app.locator("#edit-title").press("Enter")
    req = gateway.wait_for_request("/api/edit_track", method="POST", timeout=2.0)
    assert req is not None
