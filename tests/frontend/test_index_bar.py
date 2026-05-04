"""Index status bar + ↺ Rebuild."""


def test_hidden_when_idle(app):
    assert app.locator("#index-bar").is_hidden()


def test_visible_when_running(page, stub, gateway):
    gateway.index_status = {"status": "running", "progress": 42, "total": 100,
                            "tracks": 1234, "db_tracks": 0}
    page.goto(stub.base_url + "/")
    # pollIndex runs at startPolling(); first poll happens at +2s, but the
    # bar gets shown on the first response — wait for it.
    page.wait_for_selector("#index-bar:visible", timeout=4000)
    label = page.locator("#index-label").text_content()
    assert "1234" in label or "42" in label


def test_visible_when_done(page, stub, gateway):
    gateway.index_status = {"status": "done", "progress": 100, "total": 100,
                            "tracks": 1234, "db_tracks": 1234}
    page.goto(stub.base_url + "/")
    page.wait_for_selector("#index-bar:visible", timeout=4000)
    assert "✓" in page.locator("#index-label").text_content()


def test_rebuild_click_confirms_and_posts(app, gateway):
    # Force the bar visible first by triggering a status that shows it,
    # OR call reindex() directly via evaluate.
    app.on("dialog", lambda d: d.accept())
    app.evaluate("reindex()")
    req = gateway.wait_for_request("/api/index/rebuild", timeout=2.0)
    assert req is not None


def test_rebuild_cancel_does_not_post(app, gateway):
    app.on("dialog", lambda d: d.dismiss())
    app.evaluate("reindex()")
    # Give it time to NOT post
    app.wait_for_timeout(500)
    assert not gateway.captured(path_contains="/api/index/rebuild")
