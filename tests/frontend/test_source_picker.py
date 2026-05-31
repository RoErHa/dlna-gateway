"""
Frontend tests for the SRC (library source) dropdown.

Added when the LocalFs backend started coexisting with AssetUPnP in
SERVERS — the PWA now needs a picker to switch the active source. The
pre-existing suite only seeds a single server, so these tests cover the
genuinely new multi-server *switching* path (selectSource → re-browse
against the new UDN). Uses the page/stub/gateway fixtures (not `app`) so
servers can be seeded BEFORE navigation.
"""
ASSET = {"udn": "uuid:asset-1", "name": "AssetUPnP",
         "online": True, "tracks": 28868}
LOCALFS = {"udn": "uuid:localfs-abc", "name": "LocalFs",
           "online": True, "tracks": 23823}


def _boot(page, stub):
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('source-sel') && "
        "!document.getElementById('source-sel').textContent.includes('Scanning')",
        timeout=5000,
    )


def test_source_dropdown_lists_both_servers(page, stub, gateway):
    gateway.servers = [ASSET, LOCALFS]
    _boot(page, stub)
    page.wait_for_function(
        "document.querySelectorAll('#source-sel option').length >= 2",
        timeout=5000)
    options = " | ".join(page.locator("#source-sel option").all_text_contents())
    assert "AssetUPnP" in options and "LocalFs" in options


def test_selecting_source_browses_new_udn(page, stub, gateway):
    gateway.servers = [ASSET, LOCALFS]
    gateway.add_artist("ABBA")
    _boot(page, stub)
    # First server adopted on init → browse fired against AssetUPnP.
    first = gateway.wait_for_request("/api/browse_letter", method="GET")
    assert first is not None
    assert first["query"].get("udn") == "uuid:asset-1"

    gateway.clear_requests()
    page.select_option("#source-sel", "uuid:localfs-abc")

    # Switching must re-browse against the LocalFs UDN.
    r = gateway.wait_for_request(
        "/api/browse_letter", method="GET",
        match=lambda r: r["query"].get("udn") == "uuid:localfs-abc")
    assert r is not None


def test_disc_status_follows_active_source(page, stub, gateway):
    # The disc-label was removed; the active source and its online/offline
    # state now live in the SRC dropdown (value = active UDN; an "(offline)"
    # suffix on the option when that server is offline).
    gateway.servers = [
        ASSET,
        {"udn": "uuid:localfs-abc", "name": "LocalFs",
         "online": False, "tracks": 0},
    ]
    _boot(page, stub)
    # Active source starts as the first server (AssetUPnP, online → no marker).
    assert page.eval_on_selector("#source-sel", "el => el.value") == "uuid:asset-1"
    asset_opt = page.locator(
        "#source-sel option", has_text="AssetUPnP").text_content()
    assert "(offline)" not in asset_opt

    page.select_option("#source-sel", "uuid:localfs-abc")
    page.wait_for_function(
        "document.getElementById('source-sel').value === 'uuid:localfs-abc'",
        timeout=3000)
    # The LocalFs server is offline → its option carries the offline marker.
    localfs_opt = page.locator(
        "#source-sel option", has_text="LocalFs").text_content()
    assert "(offline)" in localfs_opt
