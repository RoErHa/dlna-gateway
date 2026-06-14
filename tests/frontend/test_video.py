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
         "playUrl": f"/video/{vid}", "posterUrl": f"/video_poster?id={vid}"}
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


def test_close_video_clears_player(app, gateway):
    gateway.videos = [_vid("v1", "Holiday")]
    _open_videos(app)
    app.locator(".video-row").first.click()
    app.wait_for_selector("#video-modal.open", timeout=2000)
    app.locator("#video-close").click()
    app.wait_for_function(
        "!document.getElementById('video-modal').classList.contains('open')",
        timeout=2000)
