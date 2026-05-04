"""Pagination — Prev / Next enabled-disabled state."""


def _seed_pages(gateway, total=250):
    # Override the default browse_pages with an exact total
    gateway.browse_pages[("artists", "A")] = {
        "items": [{"artist": f"Artist {i}", "album_count": 1, "track_count": 5,
                   "art": ""} for i in range(min(100, total))],
        "total": total, "offset": 0, "limit": 100,
    }


def test_pager_hidden_when_total_le_limit(page, stub, gateway):
    gateway.browse_pages[("artists", "A")] = {
        "items": [{"artist": "Solo", "album_count": 1, "track_count": 1, "art": ""}],
        "total": 1, "offset": 0, "limit": 100,
    }
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length > 0", timeout=4000)
    assert page.locator("#browse-pager").is_hidden()


def test_pager_visible_with_info(page, stub, gateway):
    _seed_pages(gateway, total=250)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "!document.getElementById('browse-pager').classList.contains('hidden')",
        timeout=4000)
    info = page.locator("#pager-info").text_content()
    assert "1" in info and "250" in info


def test_prev_disabled_at_offset_zero(page, stub, gateway):
    _seed_pages(gateway, total=250)
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "!document.getElementById('browse-pager').classList.contains('hidden')",
        timeout=4000)
    assert page.locator("#pager-prev").is_disabled()
    assert not page.locator("#pager-next").is_disabled()


def test_next_disabled_on_last_page(page, stub, gateway):
    # Total exactly fills offset+limit
    gateway.browse_pages[("artists", "A")] = {
        "items": [{"artist": f"A{i}", "album_count": 1, "track_count": 1, "art": ""}
                  for i in range(50)],
        "total": 250, "offset": 200, "limit": 100,
    }
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "!document.getElementById('browse-pager').classList.contains('hidden')",
        timeout=4000)
    assert page.locator("#pager-next").is_disabled()
