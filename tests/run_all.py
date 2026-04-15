#!/usr/bin/env python3
"""
tests/run_all.py — DLNA Gateway regression test suite.

Runs against a live gateway instance. Takes ~5 seconds.

Usage:
    python tests/run_all.py                          # defaults to http://localhost:8765
    python tests/run_all.py http://192.168.1.125:8765
    python tests/run_all.py --offline                # file-only checks, no running server needed

Exit code: 0 if all pass, 1 if any fail.
"""
import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "http://localhost:8765"
OFFLINE = "--offline" in sys.argv
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(PROJECT, "static")

passed = 0
failed = 0
errors = []


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        failed += 1
        msg = f"  \033[31m✗\033[0m {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        errors.append(name)


def fetch(path, expect_json=False):
    """GET a path from the running gateway. Returns (status, body) or (0, None) on error."""
    try:
        url = BASE_URL.rstrip("/") + path
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=5)
        body = resp.read()
        if expect_json:
            return resp.status, json.loads(body)
        return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, None


def section(title):
    print(f"\n\033[1m{title}\033[0m")


# ══════════════════════════════════════════════════════════════════
# FILE-LEVEL CHECKS (always run, no server needed)
# ══════════════════════════════════════════════════════════════════

section("T1.1 — Static files exist")
check("static/index.html exists", os.path.isfile(os.path.join(STATIC, "index.html")))
check("static/app.css exists", os.path.isfile(os.path.join(STATIC, "app.css")))
check("static/app.js exists", os.path.isfile(os.path.join(STATIC, "app.js")))
check("static/sw.js exists", os.path.isfile(os.path.join(STATIC, "sw.js")))

section("T1.1b — HTML references")
html = open(os.path.join(STATIC, "index.html")).read() if os.path.isfile(os.path.join(STATIC, "index.html")) else ""
check("HTML links app.css", "/static/app.css" in html)
check("HTML links app.js", "/static/app.js" in html)
check("HTML has title", "DLNA Gateway" in html)
check("HTML has viewport", "viewport-fit=cover" in html)
check("HTML has manifest link", "manifest.json" in html)
check("HTML has apple-touch-icon", "apple-touch-icon" in html)

section("T1.6 — Mobile responsive (CSS)")
css = open(os.path.join(STATIC, "app.css")).read() if os.path.isfile(os.path.join(STATIC, "app.css")) else ""
check("CSS has mobile breakpoint", "@media" in css and "768px" in css)
check("CSS has safe-area support", "safe-area-inset" in css)
check("CSS has letter-bar", "letter-bar" in css or "letter-btn" in css)
check("CSS has browse-modes", "browse-mode" in css)

section("T1.EXTRA — Feature preservation (JS)")
js = open(os.path.join(STATIC, "app.js")).read() if os.path.isfile(os.path.join(STATIC, "app.js")) else ""
features = {
    "Letter bar":        "buildLetterBar",
    "Browse modes":      "setBrowseMode",
    "Drill-down back":   "drillBack",
    "Genre albums":      "showGenreAlbums",
    "Artist albums":     "showArtistAlbums",
    "Edit modal":        "openEditModal",
    "Chromecast":        "castDevices",
    "Cast queue":        "cast_queue",
    "Cast state poll":   "cast_state",
    "SW registration":   "serviceWorker",
    "Shuffle":           "shuffle",
    "MediaSession":      "mediaSession",
    "Visibility poll":   "startPolling",
}
for label, needle in features.items():
    check(f"JS: {label}", needle in js)

section("T1.EXTRA — Gateway is slim")
gw_path = os.path.join(PROJECT, "dlna_gateway.py")
if os.path.isfile(gw_path):
    gw = open(gw_path).read()
    gw_lines = gw.count("\n")
    check(f"Gateway is slim ({gw_lines} lines)", gw_lines < 350, f"got {gw_lines} lines")
    check("No embedded HTML in gateway", "<html" not in gw)
else:
    check("dlna_gateway.py exists", False)

section("T1.5a — Server endpoints (file check)")
srv_path = os.path.join(PROJECT, "dlna_server.py")
if os.path.isfile(srv_path):
    srv = open(srv_path).read()
    endpoints = [
        "/api/servers", "/api/renderers", "/api/browse", "/api/artists",
        "/api/albums", "/api/genres", "/api/genre_albums", "/api/genre_tracks",
        "/api/artist_albums", "/api/browse_letter", "/api/search",
        "/api/album_tracks", "/api/play", "/api/play_tracks",
        "/api/playlist", "/api/playlists", "/api/playlist/add",
        "/api/playlist/create", "/api/playlist/delete", "/api/playlist/remove",
        "/api/render", "/api/render_queue", "/api/renderer_state",
        "/api/state", "/api/capabilities", "/api/index/rebuild",
        "/api/index/status", "/api/cast_devices", "/api/cast_state",
        "/api/cast_queue", "/api/control", "/api/edit_track",
    ]
    for ep in endpoints:
        check(f"Endpoint {ep} in server code", f'"{ep}"' in srv)
else:
    check("dlna_server.py exists", False)

# ══════════════════════════════════════════════════════════════════
# LIVE SERVER CHECKS (skip with --offline)
# ══════════════════════════════════════════════════════════════════

if OFFLINE:
    print(f"\n\033[33m⚠ Skipping live server tests (--offline)\033[0m")
else:
    section("T1.1c — Static file serving (live)")
    status, body = fetch("/")
    check("GET / returns 200", status == 200)
    check("GET / contains DLNA Gateway", body and b"DLNA Gateway" in body)

    status, body = fetch("/static/app.js")
    check("GET /static/app.js returns 200", status == 200)

    status, body = fetch("/static/app.css")
    check("GET /static/app.css returns 200", status == 200)

    section("T1.2 — Service Worker (live)")
    status, body = fetch("/sw.js")
    check("GET /sw.js returns 200", status == 200)
    check("sw.js contains APP_CACHE", body and b"APP_CACHE" in body)

    section("T1.3 — PWA manifest (live)")
    status, data = fetch("/manifest.json", expect_json=True)
    check("GET /manifest.json returns 200", status == 200)
    if data:
        check("Manifest has name", data.get("name") == "DLNA Gateway")
        check("Manifest has start_url", data.get("start_url") == "/")
        check("Manifest has standalone", data.get("display") == "standalone")
        check("Manifest has icons", len(data.get("icons", [])) >= 2)

    section("T1.4 — Icons (live)")
    status, body = fetch("/icon-192.png")
    check("GET /icon-192.png returns 200", status == 200)
    check("icon-192 is valid PNG", body and body[:4] == b"\x89PNG")

    status, body = fetch("/icon-512.png")
    check("GET /icon-512.png returns 200", status == 200)
    check("icon-512 is valid PNG", body and body[:4] == b"\x89PNG")

    section("T1.5b — API endpoints (live)")
    live_endpoints = [
        ("/api/servers", True),
        ("/api/renderers", True),
        ("/api/cast_devices", True),
        ("/api/playlists", True),
        ("/api/capabilities", True),
    ]
    for ep, is_json in live_endpoints:
        status, data = fetch(ep, expect_json=is_json)
        check(f"GET {ep} returns 200", status == 200, f"got {status}")

    # Check server has tracks
    status, data = fetch("/api/servers", expect_json=True)
    if status == 200 and data and len(data) > 0:
        tracks = data[0].get("tracks", 0)
        check(f"Server has tracks indexed ({tracks})", tracks > 0, f"got {tracks}")


# ══════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════

total = passed + failed
print(f"\n{'='*50}")
if failed:
    print(f"\033[31mFAILED: {failed}/{total} test(s)\033[0m")
    for e in errors:
        print(f"  ✗ {e}")
    sys.exit(1)
else:
    print(f"\033[32mALL {total} TESTS PASSED\033[0m")
    sys.exit(0)
