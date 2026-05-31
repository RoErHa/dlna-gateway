"""LocalFs folder-based album grouping — frontend wiring (Layer 3).

Album rows carry `album_key`; opening/playing an album passes it to
/api/album_tracks so a Various-Artists compilation opens as one folder.
The SRC dropdown surfaces the tracks + albums counts.
"""


def _boot(page, stub):
    page.goto(stub.base_url + "/")
    page.wait_for_function(
        "document.getElementById('source-sel') && "
        "!document.getElementById('source-sel').textContent.includes('Scanning')",
        timeout=5000)


def test_album_row_opens_by_album_key(page, stub, gateway):
    # An album row that carries album_key must open via album_key, not
    # (artist, album) — that's what makes a compilation open as one album.
    gateway.albums_default.append({
        "artist": "Various Artists", "album": "Abbey Comp",
        "album_key": "VA/Abbey Comp", "track_count": 3, "art": ""})
    gateway.album_tracks_by_key["VA/Abbey Comp"] = [
        {"url": "http://x/1", "title": "S1", "artist": "P1", "album": "O1",
         "type": "audio", "id": "1", "mime": "audio/flac"}]
    _boot(page, stub)
    page.locator("#bmode-albums").click()
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length>0", timeout=4000)
    gateway.clear_requests()
    page.locator("#item-list .row", has_text="Abbey Comp").first.click()
    req = gateway.wait_for_request(
        "/api/album_tracks", timeout=2.0,
        match=lambda r: r["query"].get("album_key") == "VA/Abbey Comp")
    assert req is not None, "album row must open via album_key"


def test_play_album_button_uses_album_key(page, stub, gateway):
    gateway.albums_default.append({
        "artist": "Various Artists", "album": "Anthems",
        "album_key": "VA/Anthems", "track_count": 2, "art": ""})
    gateway.album_tracks_by_key["VA/Anthems"] = [
        {"url": "http://x/2", "title": "S2", "artist": "P2", "album": "O2",
         "type": "audio", "id": "2", "mime": "audio/flac"}]
    _boot(page, stub)
    page.locator("#bmode-albums").click()
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length>0", timeout=4000)
    gateway.clear_requests()
    page.locator("#item-list .row", has_text="Anthems").first.locator(
        ".icon-btn").click()
    req = gateway.wait_for_request(
        "/api/album_tracks", timeout=2.0,
        match=lambda r: r["query"].get("album_key") == "VA/Anthems")
    assert req is not None, "Play-album button must use album_key"


def test_upnp_album_without_key_opens_by_artist_album(page, stub, gateway):
    # A row with no album_key (UPnP) must still open by (artist, album).
    gateway.albums_default.append({
        "artist": "Adele", "album": "Aaa Album", "track_count": 2, "art": ""})
    gateway.album_tracks[("Adele", "Aaa Album")] = [
        {"url": "http://x/3", "title": "S3", "artist": "Adele",
         "album": "Aaa Album", "type": "audio", "id": "3", "mime": "audio/flac"}]
    _boot(page, stub)
    page.locator("#bmode-albums").click()
    page.wait_for_function(
        "document.querySelectorAll('#item-list .row').length>0", timeout=4000)
    gateway.clear_requests()
    page.locator("#item-list .row", has_text="Aaa Album").first.click()
    req = gateway.wait_for_request(
        "/api/album_tracks", timeout=2.0,
        match=lambda r: r["query"].get("album") == "Aaa Album"
        and not r["query"].get("album_key"))
    assert req is not None, "UPnP album must open by (artist, album), no album_key"


def test_source_dropdown_shows_track_and_album_counts(page, stub, gateway):
    gateway.servers = [{"udn": "uuid:localfs-x", "name": "RoHaLocalFS",
                        "online": True, "tracks": 23863, "albums": 2109}]
    _boot(page, stub)
    opts = page.locator("#source-sel").text_content()
    assert "tracks" in opts and "albums" in opts, f"got: {opts!r}"
    assert "23,863" in opts, f"track count missing: {opts!r}"
    assert "2,109" in opts, f"album count missing: {opts!r}"
