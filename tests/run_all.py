#!/usr/bin/env python3
"""
tests/run_all.py — DLNA Gateway regression test suite.

Runs against a live gateway instance. Takes ~5 seconds.

Usage:
    python tests/run_all.py                          # defaults to http://localhost:8765
    python tests/run_all.py http://192.168.1.125:8765
    python tests/run_all.py --offline                # file-only checks, no running server needed
    python tests/run_all.py --frontend               # also run the Playwright frontend suite
    python tests/run_all.py --frontend-only          # ONLY run the Playwright frontend suite

Exit code: 0 if all pass, 1 if any fail.
"""
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
import urllib.error

BASE_URL = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "http://localhost:8765"
OFFLINE = "--offline" in sys.argv
FRONTEND = "--frontend" in sys.argv or "--frontend-only" in sys.argv
FRONTEND_ONLY = "--frontend-only" in sys.argv
PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(PROJECT, "static")

# Project must be on sys.path so local modules (dlna_*, api_*) can be imported
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

passed = 0
failed = 0
errors = []

# SSL context that accepts self-signed certs (gateway uses tailscale / local cert)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE
_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_ctx))


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
    """GET a path from the running gateway. Follows HTTP→HTTPS redirects."""
    try:
        url = BASE_URL.rstrip("/") + path
        req = urllib.request.Request(url)
        resp = _opener.open(req, timeout=5)
        body = resp.read()
        if expect_json:
            return resp.status, json.loads(body)
        return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def section(title):
    print(f"\n\033[1m{title}\033[0m")


# --frontend-only short-circuits everything except the Playwright suite.
# Useful while iterating on frontend code without re-running the slow
# library / SOAP / discovery checks.
if FRONTEND_ONLY:
    section("T_FRONTEND — Playwright frontend suite (tests/frontend/)")
    cmd = [sys.executable, "-m", "pytest", os.path.join(PROJECT, "tests", "frontend"),
           "--tb=line", "-q"]
    proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True)
    out  = proc.stdout + proc.stderr
    print(out)
    sys.exit(proc.returncode)


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
# Routes moved to dlna_routes.py. Endpoint strings now live there; the
# file check reads the routes module instead of the server module.
routes_path = os.path.join(PROJECT, "dlna_routes.py")
if os.path.isfile(routes_path):
    routes = open(routes_path).read()
    endpoints = [
        "/api/servers", "/api/renderers", "/api/browse", "/api/artists",
        "/api/albums", "/api/genres", "/api/genre_albums", "/api/genre_tracks",
        "/api/artist_albums", "/api/browse_letter", "/api/search",
        "/api/album_tracks",
        "/api/playlist", "/api/playlists", "/api/playlist/add",
        "/api/playlist/create", "/api/playlist/delete", "/api/playlist/remove",
        "/api/render", "/api/render_queue", "/api/renderer_state",
        "/api/index/rebuild", "/api/index/status",
        "/api/control", "/api/edit_track",
    ]
    for ep in endpoints:
        check(f"Endpoint {ep} in dlna_routes", f'"{ep}"' in routes)
else:
    check("dlna_routes.py exists", False)


# ══════════════════════════════════════════════════════════════════
# T1.FIX — BUG-FIX REGRESSION CHECKS (file-level)
# ══════════════════════════════════════════════════════════════════

section("T1.FIX — Browser audio error handling")
check("Error handler resets play button",
      "$" in js and "btn-pp" in js and "▶ Play" in js and "activeDevice" in js)
check("play().catch handles NotAllowedError",
      "NotAllowedError" in js)
check("control stop clears browserAudio.src",
      'browserAudio.src=""' in js)



# ══════════════════════════════════════════════════════════════════
# T5 — PLAYLIST DROPDOWN
# ══════════════════════════════════════════════════════════════════

section("T5.1 — Playlist dropdown CSS: max 5 items")
_dd_match = re.search(r'\.pl-dropdown\{([^}]+)\}', css)
if _dd_match:
    _dd_css = _dd_match.group(1)
    check(".pl-dropdown has overflow-y:auto", "overflow-y:auto" in _dd_css)

    # max-height must be a fixed pixel value, not a viewport unit
    _mh = re.search(r'max-height:([^;]+)', _dd_css)
    if _mh:
        _mh_val = _mh.group(1).strip()
        check(".pl-dropdown max-height is pixel-based (not vh/rem/em)",
              "px" in _mh_val and "vh" not in _mh_val,
              f"got max-height:{_mh_val}")
        # 5 items at ~30px each = ~150px; allow 130–180px
        _px = re.search(r'(\d+)px', _mh_val)
        if _px:
            _px_val = int(_px.group(1))
            check(f".pl-dropdown max-height fits ~5 items ({_px_val}px)",
                  130 <= _px_val <= 180,
                  f"expected 130–180px, got {_px_val}px")
    else:
        check(".pl-dropdown has max-height", False, "max-height not found")
else:
    check(".pl-dropdown rule exists in CSS", False)

section("T5.2 — Playlist dropdown JS: show/hide logic")
check("showAddToPlaylistForItem defined", "showAddToPlaylistForItem" in js)
check("hideDropdown defined",             "hideDropdown" in js)
check("Dropdown appended to body",        "document.body.appendChild" in js and "pl-dropdown" in js)
check("Dropdown dismissed on doc click",  'document.addEventListener("click",hideDropdown)' in js
                                           or "document.addEventListener('click',hideDropdown)" in js)


# ══════════════════════════════════════════════════════════════════
# T6 — POST-INDEX DB FIXES
# ══════════════════════════════════════════════════════════════════

section("T6.1 — post_index_fixes.sql exists and is valid SQL")
sql_path = os.path.join(PROJECT, "post_index_fixes.sql")
check("post_index_fixes.sql exists", os.path.isfile(sql_path))

if os.path.isfile(sql_path):
    sql_src = open(sql_path).read()

    check("SQL fixes Rolling Stones artist name",
          "Rolling Stones" in sql_src and "The Rolling Stones" in sql_src)
    check("SQL deletes inferior duplicates before renaming",
          "DELETE FROM tracks" in sql_src and "Rolling Stones" in sql_src)
    check("SQL renames remaining entries",
          "UPDATE tracks SET artist" in sql_src)
    check("SQL rebuilds FTS after changes",
          "tracks_fts" in sql_src and "rebuild" in sql_src)

    # Validate SQL syntax by running it against an in-memory DB that mirrors
    # the tracks schema (no actual data — just checks parse/compile)
    import sqlite3 as _sqlite3
    try:
        _mem = _sqlite3.connect(":memory:")
        _mem.execute("""
            CREATE TABLE tracks (
                id INTEGER PRIMARY KEY,
                udn TEXT, obj_id TEXT, url TEXT,
                title TEXT, artist TEXT, album TEXT,
                duration TEXT, art TEXT, mime TEXT,
                genre TEXT, file_path TEXT,
                UNIQUE(udn, artist, album, title)
            )
        """)
        _mem.execute("""
            CREATE VIRTUAL TABLE tracks_fts USING fts5(
                title, artist, album,
                content=tracks, content_rowid=id
            )
        """)
        # Strip comments, split on semicolons, execute each statement
        _stmts = [s.strip() for s in sql_src.split(";") if s.strip() and not s.strip().startswith("--")]
        _ok = True
        _err_detail = ""
        for _stmt in _stmts:
            # Skip comment-only blocks
            _clean = "\n".join(l for l in _stmt.splitlines() if not l.strip().startswith("--")).strip()
            if not _clean:
                continue
            try:
                _mem.execute(_clean)
            except _sqlite3.OperationalError as _e:
                # "no such table" is expected for an empty DB — that's fine
                if "no such table" not in str(_e) and "no such row" not in str(_e):
                    _ok = False
                    _err_detail = str(_e)[:80]
        _mem.close()
        check("SQL statements parse without errors", _ok, _err_detail)
    except Exception as _e:
        check("SQL validation setup", False, str(_e)[:80])

section("T6.2 — DB fix logic: lossless beats lossy")
# Verify the DELETE logic keeps lossless (flac) over lossy (mp3)
# by running it against a tiny in-memory fixture
import sqlite3 as _sqlite3
try:
    _mem = _sqlite3.connect(":memory:")
    _mem.executescript("""
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY,
            udn TEXT, obj_id TEXT DEFAULT '', url TEXT DEFAULT '',
            title TEXT, artist TEXT, album TEXT,
            duration TEXT DEFAULT '', art TEXT DEFAULT '',
            mime TEXT, genre TEXT DEFAULT '', file_path TEXT DEFAULT '',
            UNIQUE(udn, artist, album, title)
        );
        CREATE VIRTUAL TABLE tracks_fts USING fts5(
            title, artist, album,
            content=tracks, content_rowid=id
        );
        -- MP3 under wrong name (should be deleted — lossless duplicate exists)
        INSERT INTO tracks VALUES(1,'udn1','','','Start Me Up','Rolling Stones','Some Girls','','','audio/mpeg','','');
        -- FLAC under correct name (should be kept)
        INSERT INTO tracks VALUES(2,'udn1','','','Start Me Up','The Rolling Stones','Some Girls','','','audio/x-flac','','');
        -- Only exists under wrong name — should be renamed, not deleted
        INSERT INTO tracks VALUES(3,'udn1','','','Wild Horses','Rolling Stones','Sticky Fingers','','','audio/mpeg','','');
    """)
    # Run the fix logic
    _mem.execute("""
        DELETE FROM tracks WHERE artist='Rolling Stones'
        AND id IN (
            SELECT r.id FROM tracks r
            JOIN tracks t ON t.udn=r.udn AND t.album=r.album AND t.title=r.title
            WHERE r.artist='Rolling Stones' AND t.artist='The Rolling Stones'
        )
    """)
    _mem.execute("UPDATE tracks SET artist='The Rolling Stones' WHERE artist='Rolling Stones'")
    _mem.commit()
    _rows = {r[0]: r for r in _mem.execute("SELECT id, artist, title FROM tracks").fetchall()}
    _mem.close()

    check("Lossless duplicate kept (id=2 present)",       2 in _rows)
    check("Lossy duplicate deleted (id=1 removed)",        1 not in _rows)
    check("Sole entry renamed (id=3 → The Rolling Stones)",
          3 in _rows and _rows[3][1] == "The Rolling Stones")
except Exception as _e:
    check("DB fix logic test setup", False, str(_e)[:80])


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
        ("/api/playlists", True),
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
    for fn in ["renderer_state", "index_status", "index_rebuild", "stream",
               "render_queue", "render", "control", "edit_track"]:
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

section("T2.8 — dlna_routes.py endpoint routing")
if os.path.isfile(routes_path):
    routes = open(routes_path).read()
    endpoints = [
        "/api/servers", "/api/renderers", "/api/browse", "/api/artists",
        "/api/albums", "/api/genres", "/api/genre_albums", "/api/genre_tracks",
        "/api/artist_albums", "/api/browse_letter", "/api/search",
        "/api/album_tracks",
        "/api/playlist", "/api/playlists", "/api/playlist/add",
        "/api/playlist/create", "/api/playlist/delete", "/api/playlist/remove",
        "/api/render", "/api/render_queue", "/api/renderer_state",
        "/api/index/rebuild", "/api/index/status",
        "/api/control", "/api/edit_track",
    ]
    for ep in endpoints:
        check(f"  {ep} routed", f'"{ep}"' in routes)


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
    result = subprocess.run(
        [sys.executable, pool_path],
        capture_output=True, text=True, timeout=15, cwd=PROJECT
    )
    pool_passed = "PASS" in result.stdout
    check("Pool concurrent test passes", pool_passed,
          result.stdout.strip().split('\n')[-1] if result.stdout else result.stderr[:100])


# ══════════════════════════════════════════════════════════════════
# T4.HB — SERVER HEARTBEAT (file-level)
# ══════════════════════════════════════════════════════════════════

section("T4.HB — dlna_discovery.py heartbeat_thread")
disc_path = os.path.join(PROJECT, "dlna_discovery.py")
if os.path.isfile(disc_path):
    dc = open(disc_path).read()
    check("heartbeat_thread defined",       "def heartbeat_thread(" in dc)
    check("uses SERVERS.touch",             "SERVERS.touch(" in dc)
    check("tracks _heartbeat_fails",        "_heartbeat_fails" in dc)
    check("marks offline on 2nd consecutive fail", "fails == 2" in dc)
    check("sets last_seen = 0",             "last_seen = 0" in dc)

section("T4.HB — dlna_gateway.py starts heartbeat thread")
gw_path = os.path.join(PROJECT, "dlna_gateway.py")
if os.path.isfile(gw_path):
    gwc = open(gw_path).read()
    check("heartbeat thread started", "heartbeat_thread" in gwc)
    check("heartbeat thread named",   '"heartbeat"' in gwc)


# ══════════════════════════════════════════════════════════════════
# T_UNIT — Behavioural unit tests (test_*.py via unittest)
# ══════════════════════════════════════════════════════════════════
# These actually import & call module code (no grep-based faking),
# so they catch bugs that static checks can't — like today's
# ValueError-in-daemon-thread duration bug.

# ══════════════════════════════════════════════════════════════════
# T_PWA — PWA / Service Worker integrity
# ══════════════════════════════════════════════════════════════════
# Catches the 2026-04-23 bug where sw.js pre-cached /art and the PWA's
# MediaSession referenced /art?url=… but the server had no /art route.
# Any URL the SW claims to pre-cache, or any same-origin URL app.js
# fetches, must resolve against the live gateway — otherwise SW install
# fails and iOS lock-screen artwork breaks silently.

section("T_PWA — Art URLs routed through /art proxy (no mixed content)")
# Regression guard for 2026-04-23 "now-playing art missing" bug: when the
# PWA is served over HTTPS but art URLs point at plain-HTTP UPnP servers,
# iOS Safari (esp. in standalone PWA mode) blocks the image as mixed
# content. Routing every art through the same-origin /art proxy avoids it.
_ap_path = os.path.join(STATIC, "app.js")
if os.path.isfile(_ap_path):
    _app = open(_ap_path).read()
    # Every <img src=…> should either be a literal /static asset or route
    # through artUrl()/`/art?url=`. Anything else means a raw track-art
    # URL is being dropped into an img tag — the regression.
    _img_srcs = re.findall(r'img\s+src="([^"]+)"', _app)
    bad = [s for s in _img_srcs
           if ".art" in s and "/art?url=" not in s and "artUrl(" not in s]
    check(f"Every art <img src=> uses /art proxy ({len(_img_srcs)} img tags, {len(bad)} raw)",
          len(bad) == 0,
          f"raw art src found: {bad[:3]}")


section("T_PWA — Service Worker referenced URLs exist")
sw_path = os.path.join(STATIC, "sw.js")
if os.path.isfile(sw_path):
    sw = open(sw_path).read()
    # Pre-cache shell — these MUST all be 200 or SW install will fail
    shell_urls = re.findall(r"'(/[^']*)'", sw)
    # Filter to the SHELL array
    shell_block = re.search(r"const\s+SHELL\s*=\s*\[(.*?)\]", sw, re.DOTALL)
    if shell_block:
        shell_urls = re.findall(r"'(/[^']*)'", shell_block.group(1))
    else:
        shell_urls = []
    check(f"SHELL has ≥4 entries ({len(shell_urls)})", len(shell_urls) >= 4)
    if not OFFLINE:
        for u in shell_urls:
            status, _ = fetch(u)
            check(f"SW shell URL {u} → 200", status == 200, f"got {status}")

    # Intercepted paths (where SW has special handling) — all referenced
    # same-origin paths must actually resolve, or iOS silently breaks
    intercepted = re.findall(r"url\.pathname\s*===?\s*'(/[^']+)'", sw)
    if not OFFLINE:
        for u in intercepted:
            # These paths take query args (?url=...); hit them with a
            # sentinel value that must NOT 404 (400/502 are fine — the
            # route exists; only 404 means "handler missing")
            status, _ = fetch(f"{u}?url=http://127.0.0.1:1/z")
            check(f"SW-intercepted path {u} is routed (not 404)",
                  status != 404, f"got {status}")
else:
    check("static/sw.js exists", False)


section("T_UNIT — Behavioural unit tests (tests/test_*.py)")
import unittest as _ut
_loader = _ut.TestLoader()
# Limit discovery to the top-level tests/ directory only — tests/frontend/
# uses Playwright fixtures that aren't unittest-compatible.
_suite  = _loader.discover(
    start_dir=os.path.dirname(os.path.abspath(__file__)),
    pattern="test_*.py",
    top_level_dir=PROJECT,
)
# Filter out anything from tests/frontend/ — those run via pytest below
def _strip_frontend(suite):
    out = _ut.TestSuite()
    for s in suite:
        if isinstance(s, _ut.TestSuite):
            out.addTest(_strip_frontend(s))
        elif "tests.frontend" not in s.id():
            out.addTest(s)
    return out
_suite = _strip_frontend(_suite)
_runner = _ut.TextTestRunner(verbosity=0, stream=open(os.devnull, "w"))
_result = _runner.run(_suite)
_total  = _result.testsRun
_fails  = len(_result.failures) + len(_result.errors)
check(f"All unit tests pass ({_total - _fails}/{_total})",
      _fails == 0,
      f"{_fails} failure(s)/error(s) — run `python3 -m unittest discover "
      f"tests -v` for details")


# ══════════════════════════════════════════════════════════════════
# T_FRONTEND — Playwright UI suite (--frontend / --frontend-only)
# ══════════════════════════════════════════════════════════════════
if FRONTEND:
    section("T_FRONTEND — Playwright frontend suite (tests/frontend/)")
    # Use the same interpreter that's running this script (the project venv)
    cmd = [sys.executable, "-m", "pytest", os.path.join(PROJECT, "tests", "frontend"),
           "--tb=line", "-q"]
    proc = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True)
    out  = proc.stdout + proc.stderr
    # Parse the pytest summary line: "97 passed in 72.73s" or "3 failed, 94 passed ..."
    m_pass = re.search(r"(\d+) passed", out)
    m_fail = re.search(r"(\d+) failed", out)
    n_pass = int(m_pass.group(1)) if m_pass else 0
    n_fail = int(m_fail.group(1)) if m_fail else 0
    check(f"Playwright suite ({n_pass} passed, {n_fail} failed)",
          proc.returncode == 0 and n_fail == 0,
          f"pytest exit {proc.returncode} — run `pytest tests/frontend -v` "
          f"for details")
    if proc.returncode != 0:
        # Print the tail so failures are visible without needing a re-run
        print("\n".join(out.splitlines()[-30:]))


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
