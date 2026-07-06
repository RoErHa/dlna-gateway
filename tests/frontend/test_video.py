"""
Playwright tests for the "📹 Videos" PWA view (V2-PWA).

Contract:
  • The Playlists panel renders a synthetic "📹 Videos" entry
    (id=videos-pl-item).
  • Clicking it opens a video view in #pl-tracks (#video-list) populated
    from GET /api/videos.
  • Each video (.video-row) shows title + folder/duration + a poster
    thumbnail (or a 📹 placeholder).
  • Clicking a video opens #video-modal (class 'open') with #video-player
    src = the SAME-ORIGIN /video/<id> (mixed-content-safe over HTTPS).
  • Empty library → a "No videos" message.

Uses the shared StubGateway (app + gateway fixtures from conftest).
"""
from __future__ import annotations


def _vid(vid, title, **kw):
    d = {"id": vid, "title": title, "folder": "2026", "duration": 65,
         "width": 1920, "height": 1080, "vcodec": "h264", "acodec": "aac",
         "container": "mp4", "mime": "video/mp4",
         "created": "2026-06-14T14:30:00Z", "location_name": "Amsterdam",
         "country": "NL",
         "playUrl": f"/video/{vid}", "transcodeUrl": f"/video_transcode/{vid}",
         "hlsUrl": f"/video_hls/{vid}/index.m3u8",
         "posterUrl": f"/video_poster?id={vid}"}
    d.update(kw)
    return d


def _open_videos(page):
    page.evaluate("showPlaylists()")
    page.wait_for_function("document.getElementById('videos-pl-item')",
                           timeout=2000)
    page.locator("#videos-pl-item").click()
    # Wait for the async fetch+render to populate #video-list (rows or the
    # "No videos" message), not just the empty container.
    page.wait_for_function(
        "document.getElementById('video-list') && "
        "document.getElementById('video-list').children.length > 0",
        timeout=3000)


def test_videos_row_present(app, gateway):
    app.evaluate("showPlaylists()")
    app.wait_for_function("document.getElementById('videos-pl-item')",
                          timeout=2000)
    assert app.locator("#videos-pl-item").is_visible()


def test_open_videos_lists_items(app, gateway):
    gateway.videos = [_vid("v1", "Holiday"), _vid("v2", "Beach Day")]
    _open_videos(app)
    assert app.locator(".video-row").count() == 2
    txt = app.locator("#video-list").inner_text()
    assert "Holiday" in txt and "Beach Day" in txt


def test_empty_videos_message(app, gateway):
    gateway.videos = []
    _open_videos(app)
    assert "No videos" in app.locator("#video-list").inner_text()


def test_click_plays_same_origin_video(app, gateway):
    gateway.videos = [_vid("v1", "Holiday")]
    _open_videos(app)
    app.locator(".video-row").first.click()
    app.wait_for_selector("#video-modal.open", timeout=2000)
    src = app.locator("#video-player").get_attribute("src")
    assert src and src.endswith("/video/v1"), src   # same-origin, not :8200


def test_unplayable_container_uses_hls(app, gateway):
    # An MKV (video/x-matroska) can't play natively → SEEKABLE on-demand HLS
    # (Chromium → hls.js; Safari → native HLS), not the progressive stream.
    gateway.videos = [_vid("mkv1", "Concert", mime="video/x-matroska",
                           container="matroska", vcodec="hevc", acodec="eac3")]
    _open_videos(app)
    app.locator(".video-row").first.click()
    app.wait_for_selector("#video-modal.open", timeout=2000)
    app.wait_for_function(
        "['hls','native-hls'].includes("
        "document.getElementById('video-player').dataset.mode)", timeout=3000)


def test_playable_mp4_uses_native(app, gateway, browser_name):
    gateway.videos = [_vid("v1", "Holiday")]   # video/mp4 (H.264)
    _open_videos(app)
    app.locator(".video-row").first.click()
    app.wait_for_selector("#video-modal.open", timeout=2000)
    # playVideo() attempts an mp4 NATIVELY first (mode=native, src=playUrl) —
    # unlike FORCE_TRANSCODE containers (mkv/avi/ts) that go straight to
    # transcode. On Chromium the native attempt sticks. On REAL WebKit the
    # stub's placeholder body can't be decoded, so the <video> fires 'error'
    # and the code correctly falls back to the seekable HLS transcode
    # (native-hls) — a valid Safari path, not a force-transcode. So webkit may
    # land on either; the default engine still asserts the strict native path.
    mode = app.locator("#video-player").get_attribute("data-mode")
    if browser_name == "webkit":
        assert mode in ("native", "native-hls"), f"unexpected mode {mode!r}"
    else:
        assert app.locator("#video-player").get_attribute("src").endswith("/video/v1")
        assert mode == "native", f"unexpected mode {mode!r}"


def test_close_video_clears_player(app, gateway):
    gateway.videos = [_vid("v1", "Holiday")]
    _open_videos(app)
    app.locator(".video-row").first.click()
    app.wait_for_selector("#video-modal.open", timeout=2000)
    app.locator("#video-close").click()
    app.wait_for_function(
        "!document.getElementById('video-modal').classList.contains('open')",
        timeout=2000)


# ── search + date/location browse (2026-07-06) ────────────────────────
# The Videos view gets the same affordances as audio browse: a filter box
# (#video-search, matches title + location) and a sort toggle —
# #video-sort-date (default: newest first, grouped by month) vs
# #video-sort-loc (locations A-Z, newest first within, "(no location)"
# last). Group headers are .video-group divs.

def test_video_search_filters_rows(app, gateway):
    gateway.videos = [_vid("v1", "Holiday in Rome", location_name="Rome"),
                      _vid("v2", "Beach Day", location_name="Nice")]
    _open_videos(app)
    app.fill("#video-search", "beach")
    app.wait_for_function(
        "document.querySelectorAll('.video-row').length === 1", timeout=2000)
    assert "Beach Day" in app.locator("#video-list").inner_text()
    app.fill("#video-search", "")
    app.wait_for_function(
        "document.querySelectorAll('.video-row').length === 2", timeout=2000)


def test_video_search_matches_location(app, gateway):
    gateway.videos = [_vid("v1", "Clip One", location_name="Rome"),
                      _vid("v2", "Clip Two", location_name="Nice")]
    _open_videos(app)
    app.fill("#video-search", "rome")
    app.wait_for_function(
        "document.querySelectorAll('.video-row').length === 1", timeout=2000)
    assert "Clip One" in app.locator("#video-list").inner_text()


def test_default_date_mode_groups_by_month_newest_first(app, gateway):
    gateway.videos = [
        _vid("v1", "Old", created="2021-08-16T14:48:35Z"),
        _vid("v2", "New", created="2026-06-10T09:52:44Z"),
        _vid("v3", "Mid", created="2026-05-01T08:00:00Z"),
    ]
    _open_videos(app)
    groups = app.locator(".video-group").all_inner_texts()
    assert groups == ["2026-06", "2026-05", "2021-08"], groups
    rows = app.locator(".video-row").all_inner_texts()
    assert "New" in rows[0] and "Old" in rows[-1]


def test_location_mode_groups_country_then_location(app, gateway):
    """2026-07-06 v2: location mode = COUNTRY blocks (A-Z, specials last),
    location sub-blocks inside, newest-first within a location."""
    gateway.videos = [
        _vid("v1", "U-clip", location_name="Utrecht", country="NL",
             created="2026-06-10T09:52:44Z"),
        _vid("v2", "A-clip", location_name="Amsterdam", country="NL",
             created="2026-05-01T08:00:00Z"),
        _vid("v3", "F-clip", location_name="Faro", country="PT",
             created="2026-03-01T08:00:00Z"),
        _vid("v4", "Lost", location_name="Atlantis", country="",
             created="2026-02-01T08:00:00Z"),
        _vid("v5", "Nowhere", location_name="", country="",
             created="2026-04-01T08:00:00Z"),
    ]
    _open_videos(app)
    app.locator("#video-sort-loc").click()
    app.wait_for_function(
        "document.querySelectorAll('.video-group').length === 4",
        timeout=2000)
    groups = app.locator(".video-group").all_inner_texts()
    assert groups == ["NL", "PT", "(no country)", "(no location)"], groups
    subs = app.locator(".video-subgroup").all_inner_texts()
    assert subs == ["Amsterdam", "Utrecht", "Faro", "Atlantis"], subs
    rows = app.locator(".video-row").all_inner_texts()
    assert "A-clip" in rows[0] and "Nowhere" in rows[-1]


def test_sort_toggle_back_to_date(app, gateway):
    gateway.videos = [
        _vid("v1", "One", location_name="Utrecht", country="NL",
             created="2026-06-10T09:52:44Z"),
        _vid("v2", "Two", location_name="Amsterdam", country="NL",
             created="2021-08-16T14:48:35Z"),
    ]
    _open_videos(app)
    app.locator("#video-sort-loc").click()
    app.locator("#video-sort-date").click()
    app.wait_for_function(
        "document.querySelectorAll('.video-group').length === 2",
        timeout=2000)
    groups = app.locator(".video-group").all_inner_texts()
    assert groups == ["2026-06", "2021-08"], groups
