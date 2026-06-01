"""C7 — AcoustID metadata-enrichment status surfaced in the index bar,
plus the manual "🔎 Enrich" trigger button.

Contract:
  * The index bar carries an Enrich button (#btn-enrich).
  * Clicking it POSTs /api/acoustid/enrich.
  * When enrichment is in_progress (and indexing isn't), the index-bar
    label shows enrichment progress (processed / remaining) instead of
    the idle "Library: N tracks" line.
"""


def test_enrich_button_present(app):
    assert app.locator("#btn-enrich").count() == 1


def test_enrich_button_posts(app, gateway):
    gateway.clear_requests()
    app.evaluate("acoustidEnrich()")
    req = gateway.wait_for_request("/api/acoustid/enrich", method="POST",
                                   timeout=2.0)
    assert req is not None, "Enrich must POST /api/acoustid/enrich"


def test_enrichment_progress_in_index_bar(page, stub, gateway):
    gateway.index_status = {"status": "done", "progress": 100, "total": 100,
                            "tracks": 4321, "db_tracks": 4321}
    gateway.acoustid_status = {"enabled": True, "fpcalc": True,
                               "in_progress": True, "processed": 50,
                               "remaining": 200, "threshold": 0.85,
                               "last_match": "Miles Davis — So What",
                               "last_url": "http://x/1"}
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('index-label') && "
        "/[Ee]nrich/.test(document.getElementById('index-label').textContent)",
        timeout=5000)
    label = page.locator("#index-label").text_content()
    assert "50" in label and "200" in label, f"got: {label!r}"


def test_idle_enrichment_does_not_hijack_index_bar(page, stub, gateway):
    # Enrichment NOT in progress → the bar shows the normal library line.
    gateway.index_status = {"status": "done", "progress": 100, "total": 100,
                            "tracks": 4321, "db_tracks": 4321}
    gateway.acoustid_status = {"enabled": True, "fpcalc": True,
                               "in_progress": False, "processed": 0,
                               "remaining": 0, "threshold": 0.85,
                               "last_match": "", "last_url": ""}
    page.goto(stub.base_url + "/")
    page.wait_for_selector("#index-bar:visible", timeout=5000)
    page.wait_for_function(
        "/tracks indexed/.test(document.getElementById('index-label').textContent)",
        timeout=4000)
    assert "Enrich" not in page.locator("#index-label").text_content()
