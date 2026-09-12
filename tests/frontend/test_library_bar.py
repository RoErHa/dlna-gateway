"""
tests/frontend/test_library_bar.py — telling a person which drive to mount.

The 2026-09-11 outage ran for seventeen hours behind ONE boot-time
WARNING in gateway.log. The gateway now waits for a missing root and
wires it the moment it appears, but waiting is only half the job: from
the sofa the library is still missing, and nothing in the window said
so. These tests are about the half that is visible.
"""


def _waiting(label="Music", root="/Volumes/SAMDATA/Music"):
    return {"label": label, "root": root, "state": "waiting",
            "message": f"{label} library unavailable — mount {root}. "
                       "It will be picked up automatically, no restart needed."}


def _active(label="Audiobooks", root="/Volumes/SAMDATA-1TB/Audio_Books"):
    return {"label": label, "root": root, "state": "active"}


def test_a_waiting_root_names_the_volume_to_mount(gateway, app):
    gateway.libraries = [_waiting()]
    app.reload()
    bar = app.locator("#library-bar")
    bar.wait_for(state="visible", timeout=5000)
    assert "/Volumes/SAMDATA/Music" in bar.inner_text()


def test_the_bar_stays_hidden_when_every_volume_is_there(gateway, app):
    gateway.libraries = [_active(), _active("Music", "/Volumes/SAMDATA/Music")]
    app.reload()
    app.wait_for_timeout(600)
    assert not app.locator("#library-bar").is_visible()


def test_no_configured_roots_means_no_bar(gateway, app):
    """An unconfigured library is OFF, not broken. Warning about a video
    library nobody asked for is the noise that buries the real thing."""
    gateway.libraries = []
    app.reload()
    app.wait_for_timeout(600)
    assert not app.locator("#library-bar").is_visible()


def test_only_the_waiting_root_is_named(gateway, app):
    """Books on a healthy disk are not the problem and must not be
    listed as one — the roots are independent."""
    gateway.libraries = [_active(), _waiting()]
    app.reload()
    bar = app.locator("#library-bar")
    bar.wait_for(state="visible", timeout=5000)
    text = bar.inner_text()
    assert "/Volumes/SAMDATA/Music" in text
    assert "Audio_Books" not in text


def test_two_missing_volumes_are_both_named(gateway, app):
    gateway.libraries = [_waiting(), _waiting("Video", "/Volumes/SAMDATA/GWMovies")]
    app.reload()
    bar = app.locator("#library-bar")
    bar.wait_for(state="visible", timeout=5000)
    text = bar.inner_text()
    assert "/Volumes/SAMDATA/Music" in text
    assert "/Volumes/SAMDATA/GWMovies" in text


def test_the_bar_clears_itself_once_the_drive_is_mounted(gateway, app):
    """The whole point of waiting: the person plugs the drive in and the
    app catches up on its own, with no reload and no restart."""
    gateway.libraries = [_waiting()]
    app.reload()
    app.locator("#library-bar").wait_for(state="visible", timeout=5000)

    gateway.libraries = [_active("Music", "/Volumes/SAMDATA/Music")]
    app.evaluate("refreshLibraries()")
    app.wait_for_selector("#library-bar", state="hidden", timeout=5000)


def test_the_message_is_escaped_not_interpreted(gateway, app):
    """`message` and `root` are server-built strings, but a root is a
    path from config and paths can hold anything. It lands in innerHTML,
    so it must be escaped — same rule as every other untrusted text."""
    gateway.libraries = [_waiting("Music", "/Volumes/<img src=x onerror=x>/M")]
    app.reload()
    bar = app.locator("#library-bar")
    bar.wait_for(state="visible", timeout=5000)
    assert bar.locator("img").count() == 0
    assert "<img" in bar.inner_text()


def test_a_failed_libraries_fetch_never_blanks_the_bar(gateway, app):
    """A dropped tailnet must not read as 'the drive came back'."""
    gateway.libraries = [_waiting()]
    app.reload()
    app.locator("#library-bar").wait_for(state="visible", timeout=5000)

    app.evaluate("window.api = async () => null")   # every fetch fails
    app.evaluate("refreshLibraries()")
    app.wait_for_timeout(400)
    assert app.locator("#library-bar").is_visible()
