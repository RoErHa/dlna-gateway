"""
test_album_year.py — an album row shows the EDITION, and names the
original when it is meaningfully older (2026-09-21).

The server sends two facts and asserts neither: `year` is the pressing,
`year_original` the oldest recording on it. `albumYearLabel` is the one
place that decides what a reader sees, and it is shared by the album
rows and the album header so the two can never disagree.

The bug it was written for: the three 40th-anniversary discs of *The
Piper at the Gates of Dawn* were all dated 1967 — the same as the
original sitting beside them in the list.
"""


def _label(app, year, original):
    return app.evaluate(
        "([y, o]) => albumYearLabel(y, o)", [year, original])


def test_a_remaster_leads_with_its_own_year(app, gateway):
    assert _label(app, 2007, 1967) == "2007 · orig. 1967"


def test_an_ordinary_album_shows_one_year(app, gateway):
    assert _label(app, 1971, 1971) == "1971"


def test_a_gap_under_three_years_is_not_worth_saying(app, gateway):
    """Same threshold the now-playing panel uses for '(remastered)'.
    A 1969 pressing of a 1967 record is not a reissue worth annotating."""
    assert _label(app, 1969, 1967) == "1969"


def test_an_undated_edition_falls_back_to_the_recording(app, gateway):
    assert _label(app, None, 1967) == "1967"


def test_an_album_with_no_date_at_all_says_nothing(app, gateway):
    assert _label(app, None, None) == ""


def test_the_four_piper_folders_stay_distinguishable(app, gateway):
    """The reported bug, end to end: three anniversary discs and the
    original, which used to render as four rows dated 1967."""
    gateway.artist_albums["Pink Floyd"] = [
        {"album": "The Piper at the Gates of Dawn", "artist": "Pink Floyd",
         "track_count": 11, "folder_tracks": 11, "folder_artists": 1,
         "own": True, "album_key": "PF/Piper 1967", "art": "",
         "year": 1967, "year_original": 1967},
    ] + [
        {"album": f"Piper 40th [Disc {d}]", "artist": "Pink Floyd",
         "track_count": 11, "folder_tracks": 11, "folder_artists": 1,
         "own": True, "album_key": f"PF/Piper 40th {d}", "art": "",
         "year": 2007, "year_original": 1967}
        for d in (1, 2, 3)
    ]
    app.evaluate("() => showArtistAlbums({artist:'Pink Floyd'})")
    app.wait_for_selector("#item-list .row", timeout=3000)
    subs = app.locator("#item-list .row-sub").all_text_contents()
    assert subs[0].startswith("1967 · ")
    for s in subs[1:4]:
        assert s.startswith("2007 · orig. 1967 · ")


def test_the_album_header_agrees_with_the_row(app, gateway):
    """The header derives its date from the tracks it just fetched, so
    it works from entry points that hand it no album row at all."""
    for n in ("One", "Two"):
        t = gateway.add_track("Pink Floyd", "Piper 40th", n, year=1967)
        t["year_edition"] = 2007
    app.evaluate("showAlbumTracks('Pink Floyd','Piper 40th')")
    app.wait_for_selector("#item-list .row", timeout=3000)
    assert "2007 · orig. 1967" in app.locator(
        "#browse-section-title").text_content()
