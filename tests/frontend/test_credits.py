"""Songwriting credits on the now-playing panel (step 1, 2026-09-20).

The credits ride on the SAME `/api/track_meta` response the year line
already fetches — `test_credits_cost_no_extra_request` is what keeps it
that way, because the obvious implementation is a second fetch per
track and nothing else would notice.
"""

TRACK = "http://stub/bo.flac"


def _play(app, gateway, meta):
    gateway.track_meta[TRACK] = meta
    app.evaluate("""(url) => startPlay({url, title:'Bohemian Rhapsody',
        artist:'Queen', album:'A Night at the Opera', art:'', type:'audio'},
        null);""", TRACK)
    app.wait_for_function(
        "document.getElementById('np-actions').style.display === 'flex'",
        timeout=2000)


def _credits(app):
    app.wait_for_timeout(350)          # let the single fetch settle
    return app.locator("#np-credits").text_content().strip()


def test_composer_is_shown(app, gateway):
    _play(app, gateway, {"title": "Bohemian Rhapsody", "year": 1975,
                         "composer": "Freddie Mercury", "lyricist": ""})
    assert "Freddie Mercury" in _credits(app)


def test_composer_and_lyricist_both_shown_when_different(app, gateway):
    _play(app, gateway, {"title": "Your Song", "year": 1970,
                         "composer": "Elton John",
                         "lyricist": "Bernie Taupin"})
    txt = _credits(app)
    assert "Elton John" in txt
    assert "Bernie Taupin" in txt


def test_identical_composer_and_lyricist_render_once(app, gateway):
    """Real data: MusicBrainz returns Freddie Mercury as BOTH composer
    and lyricist for Bohemian Rhapsody. Printing the name twice reads
    like a bug."""
    _play(app, gateway, {"title": "Bohemian Rhapsody", "year": 1975,
                         "composer": "Freddie Mercury",
                         "lyricist": "Freddie Mercury"})
    assert _credits(app).count("Freddie Mercury") == 1


def test_no_credits_leaves_the_line_empty(app, gateway):
    """68% of this library has no composer tag — the line must collapse
    rather than show a stray label or a blank bullet."""
    _play(app, gateway, {"title": "Untagged", "year": 1999,
                         "composer": "", "lyricist": ""})
    assert _credits(app) == ""


def test_year_line_still_renders(app, gateway):
    """Credits share the year's fetch; a mistake there takes out a
    feature that already worked."""
    _play(app, gateway, {"title": "Bohemian Rhapsody", "year": 1975,
                         "composer": "Freddie Mercury", "lyricist": ""})
    app.wait_for_timeout(350)
    assert "1975" in app.locator("#np-year").text_content()


def test_credits_cost_no_extra_request(app, gateway):
    """One /api/track_meta per track, not two."""
    gateway.clear_requests()
    _play(app, gateway, {"title": "Bohemian Rhapsody", "year": 1975,
                         "composer": "Freddie Mercury", "lyricist": ""})
    app.wait_for_timeout(400)
    n = sum(1 for r in gateway.requests if r["path"] == "/api/track_meta")
    assert n == 1, f"expected exactly 1 track_meta request, saw {n}"


def test_stale_response_cannot_overwrite_a_newer_track(app, gateway):
    """Same race the year line guards: switching tracks quickly must
    not let the first response paint over the second."""
    gateway.track_meta["http://stub/a.flac"] = {
        "title": "A", "composer": "Composer A", "lyricist": ""}
    _play(app, gateway, {"title": "B", "composer": "Composer B",
                         "lyricist": ""})
    app.wait_for_timeout(350)
    assert "Composer B" in _credits(app)
    assert "Composer A" not in _credits(app)
