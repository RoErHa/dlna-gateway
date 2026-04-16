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

# Project must be on sys.path so local modules (dlna_*, api_*) can be imported
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

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
# T2 — SERVER SPLIT CHECKS (file-level)
# ══════════════════════════════════════════════════════════════════

section("T2.1 — API module files exist")
api_modules = ["api_browse.py", "api_playback.py", "api_playlists.py", "api_upnp.py"]
for mod in api_modules:
    check(f"{mod} exists", os.path.isfile(os.path.join(PROJECT, mod)))

section("T2.2 — API module imports (no circular deps)")
if all(os.path.isfile(os.path.join(PROJECT, m)) for m in api_modules):
    import importlib.util as _ilu2
    for mod in api_modules:
        try:
            _spec = _ilu2.spec_from_file_location(mod[:-3], os.path.join(PROJECT, mod))
            _m    = _ilu2.module_from_spec(_spec)
            _spec.loader.exec_module(_m)
            check(f"{mod} imports cleanly", True)
        except Exception as _e:
            check(f"{mod} imports cleanly", False, str(_e)[:80])

section("T2.3 — api_browse.py: all browse functions present")
browse_path = os.path.join(PROJECT, "api_browse.py")
if os.path.isfile(browse_path):
    bc = open(browse_path).read()
    for fn in ["servers", "renderers", "browse", "artists", "search",
               "album_tracks", "albums", "genres", "genre_albums",
               "genre_tracks", "artist_albums", "browse_letter"]:
        check(f"  api_browse.{fn}", f"def {fn}(" in bc)

section("T2.4 — api_playback.py: all playback functions present")
pb_path = os.path.join(PROJECT, "api_playback.py")
if os.path.isfile(pb_path):
    pc = open(pb_path).read()
    for fn in ["play", "state", "renderer_state", "capabilities",
               "index_status", "index_rebuild", "stream",
               "cast_devices", "cast_state", "cast_queue",
               "render_queue", "render", "control", "edit_track",
               "play_tracks"]:
        check(f"  api_playback.{fn}", f"def {fn}(" in pc)

section("T2.5 — api_playlists.py: all playlist functions present")
pl_path = os.path.join(PROJECT, "api_playlists.py")
if os.path.isfile(pl_path):
    plc = open(pl_path).read()
    for fn in ["playlists", "playlist", "playlist_create",
               "playlist_delete", "playlist_add", "playlist_remove"]:
        check(f"  api_playlists.{fn}", f"def {fn}(" in plc)

section("T2.6 — api_upnp.py: UPnP gateway present")
upnp_path = os.path.join(PROJECT, "api_upnp.py")
if os.path.isfile(upnp_path):
    uc = open(upnp_path).read()
    check("  GW_UDN defined", "GW_UDN" in uc)
    check("  GW_NAME defined", "GW_NAME" in uc)
    for fn in ["device_xml", "cd_desc_xml", "cd_events", "cd_control",
               "gw_ssdp_announcer", "gw_ssdp_byebye"]:
        check(f"  api_upnp.{fn}", f"def {fn}(" in uc)

section("T2.7 — dlna_server.py is slim router")
srv_path = os.path.join(PROJECT, "dlna_server.py")
if os.path.isfile(srv_path):
    srv = open(srv_path).read()
    srv_lines = srv.count("\n")
    check(f"dlna_server.py is slim ({srv_lines} lines)", srv_lines < 400,
          f"got {srv_lines} lines")
    check("imports api_browse",    "import api_browse"    in srv)
    check("imports api_playback",  "import api_playback"  in srv)
    check("imports api_playlists", "import api_playlists" in srv)
    check("imports api_upnp",      "import api_upnp"      in srv)
    check("re-exports GW_UDN",     "GW_UDN" in srv)
    check("no domain logic in router", "_gw_browse" not in srv)

section("T2.8 — dlna_server.py endpoint routing")
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
        check(f"  {ep} routed", f'"{ep}"' in srv)


# ══════════════════════════════════════════════════════════════════
# T3 — DATABASE POOL CHECKS (file-level)
# ══════════════════════════════════════════════════════════════════

section("T3.1 — db_pool.py exists and imports")
pool_path = os.path.join(PROJECT, "db_pool.py")
check("db_pool.py exists", os.path.isfile(pool_path))
if os.path.isfile(pool_path):
    pool_code = open(pool_path).read()
    check("Pool class defined", "class Pool" in pool_code)
    check("Pool has read() method", "def read(self)" in pool_code)
    check("Pool has write() method", "def write(self)" in pool_code)
    check("Pool has close() method", "def close(self)" in pool_code)
    check("Pool sets WAL mode", "journal_mode=WAL" in pool_code)
    check("Pool sets busy_timeout", "busy_timeout" in pool_code)

section("T3.2 — LibraryDB uses pool")
lib_path = os.path.join(PROJECT, "dlna_library.py")
if os.path.isfile(lib_path):
    lib_code = open(lib_path).read()
    # Extract LibraryDB class only
    lib_start = lib_code.find("class LibraryDB:")
    lib_end = lib_code.find("\nclass ", lib_start + 10)
    lib_class = lib_code[lib_start:lib_end] if lib_end > 0 else lib_code[lib_start:]

    check("LibraryDB imports Pool", "from db_pool import Pool" in lib_code)
    check("LibraryDB creates Pool", "Pool(" in lib_class)
    check("No self._lock in LibraryDB", "self._lock" not in lib_class)
    check("No self._connect in LibraryDB", "self._connect" not in lib_class)
    check("No self._db_file in LibraryDB", "self._db_file" not in lib_class,
          "stale attribute — use self._pool.db_file instead")
    check("No self._local in LibraryDB", "self._local" not in lib_class,
          "stale attribute — pool handles thread-local connections")
    check("Uses pool.read()", "self._pool.read()" in lib_class)
    check("Uses pool.write()", "self._pool.write()" in lib_class)

    reads = lib_class.count("self._pool.read()")
    writes = lib_class.count("self._pool.write()")
    check(f"Read/write balance ({reads}r/{writes}w)", reads > 0 and writes > 0)

section("T3.3 — Pool concurrent test (standalone)")
if os.path.isfile(pool_path):
    import subprocess
    # Run db_pool.py standalone test
    result = subprocess.run(
        [sys.executable, pool_path],
        capture_output=True, text=True, timeout=15, cwd=PROJECT
    )
    pool_passed = "PASS" in result.stdout
    check("Pool concurrent test passes", pool_passed,
          result.stdout.strip().split('\n')[-1] if result.stdout else result.stderr[:100])


# ══════════════════════════════════════════════════════════════════
# T4 — CHROMECAST MIME NORMALISATION
# ══════════════════════════════════════════════════════════════════

section("T4.1 — dlna_cast.py MIME normalisation table")
cast_path = os.path.join(PROJECT, "dlna_cast.py")
if os.path.isfile(cast_path):
    import sys as _sys
    _sys.path.insert(0, PROJECT)
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("dlna_cast", cast_path)
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        norm = _mod.CAST_MIME_NORM

        check("CAST_MIME_NORM exported", isinstance(norm, dict))

        # Every alias must map to a canonical type (no self-mapping needed, but
        # the canonical itself must not be in the table as a key pointing elsewhere)
        _expected = {
            # MP3
            "audio/mp3":           "audio/mpeg",
            "audio/x-mpeg":        "audio/mpeg",
            "audio/x-mp3":         "audio/mpeg",
            "audio/mpeg3":         "audio/mpeg",
            "audio/mpg":           "audio/mpeg",
            # FLAC
            "audio/x-flac":        "audio/flac",
            # AAC / M4A / ALAC
            "audio/aac":           "audio/mp4",
            "audio/x-aac":         "audio/mp4",
            "audio/x-m4a":         "audio/mp4",
            "audio/x-alac":        "audio/mp4",
            "audio/m4a":           "audio/mp4",
            "audio/vnd.dlna.adts": "audio/mp4",
            # OGG / Opus / Vorbis
            "audio/vorbis":        "audio/ogg",
            "audio/x-ogg":         "audio/ogg",
            "audio/x-vorbis":      "audio/ogg",
            "audio/opus":          "audio/ogg",
            "audio/x-opus":        "audio/ogg",
            # WAV
            "audio/x-wav":         "audio/wav",
            "audio/wave":          "audio/wav",
            "audio/vnd.wave":      "audio/wav",
            # AIFF
            "audio/x-aiff":        "audio/aiff",
            "audio/aif":           "audio/aiff",
            # WMA
            "audio/x-ms-wma":      "audio/x-ms-wma",
            "audio/wma":           "audio/x-ms-wma",
            # WebM
            "audio/x-webm":        "audio/webm",
        }
        for alias, canonical in _expected.items():
            check(f"  {alias} → {canonical}", norm.get(alias) == canonical,
                  f"got {norm.get(alias)!r}")

        # Canonical pass-through: types already correct should not be remapped
        _passthrough = ["audio/mpeg", "audio/flac", "audio/mp4",
                        "audio/ogg", "audio/wav", "audio/aiff", "audio/webm"]
        for t in _passthrough:
            check(f"  {t} passes through unchanged",
                  norm.get(t, t) == t,
                  f"remapped to {norm.get(t)!r}")

    except Exception as _e:
        check("dlna_cast imports cleanly", False, str(_e))
else:
    check("dlna_cast.py exists", False)


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
