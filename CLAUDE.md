# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

DLNA Gateway is a Python-based UPnP/DLNA music library gateway. It discovers UPnP MediaServers (AssetUPnP, MinimServer, Jellyfin, Plex) on the local network, indexes their music into a local SQLite DB, and exposes a PWA web UI for browsing and playback. Playback targets: UPnP MediaRenderers (Naim Uniti, etc.) and browser audio. The gateway also announces itself as a UPnP MediaServer so UPnP renderers can browse its playlists directly.

> **Backend migration COMPLETE (2026-05-31).** AssetUPnP has been
> replaced by an in-process indexer + bit-perfect file server
> (**RoHaLocalFS**), and the AssetUPnP-shaped code path was generalised
> into a pluggable **`LibraryProvider` seam** so the gateway can speak to
> any of: AssetUPnP/MinimServer (UpnpProvider, kept), Plex, Jellyfin, or
> the in-process LocalFs backend. See **[Library backend — LocalFs](#library-backend--localfs-migration-complete)**
> below for the architecture; phase history is in `docs/MIGRATION_PLAN.md`.

## Running the Gateway

```bash
./setup.sh --run                   # set up venv + start on :8765
./setup.sh --run --no-browser      # skip auto-open
./setup.sh --run --debug           # verbose logging
./setup.sh --run --probe http://...  # add a server manually
./setup.sh --run --list-devices    # show known devices table
./setup.sh --run --reset-devices   # clear device DB
./setup.sh --restart               # refresh venv/deps + restart launchd gateway
```

`--restart` refreshes the venv/dependencies, then runs
`launchctl kickstart -k gui/$(id -u)/com.roha.dlna-gateway` — the
launchd-correct restart (a bare `kill` races launchd's respawn; see
"Restarting the gateway" below). It aborts with install hints if the
LaunchAgent isn't loaded. `--restart` takes precedence over `--run` if
both are passed.

## Running Tests

Four complementary layers:

```bash
python tests/run_all.py              # full backend suite: grep + live + unit tests
python tests/run_all.py --offline    # file-level checks only (no server needed)
python tests/run_all.py --frontend   # backend suite + Playwright UI suite
python tests/run_all.py --frontend-only  # just the Playwright UI suite (fastest iteration)
python tests/run_all.py http://192.168.1.x:8765  # custom gateway URL

# Layer 1 — behavioural unit tests (no network, <1s):
python3 -m unittest tests.test_player tests.test_api_playback -v

# Layer 2 — Playwright UI suite (~75s, no live gateway needed):
.venv/bin/pytest tests/frontend -v
.venv/bin/pytest tests/frontend -k transport --headed   # visible browser, single panel
.venv/bin/pytest tests/frontend --browser webkit        # engine parity (Playwright WebKit, NOT real Safari)

# Layer 2b — real-Safari smoke (opt-in; opens real Safari, not CI-able):
.venv/bin/python tests/frontend/safari_smoke.py

# Layer 2c — iOS-Simulator smoke (opt-in; needs Appium + a booted sim):
appium >/tmp/appium.log 2>&1 &                 # start Appium server (:4723)
xcrun simctl boot "iPhone 15"                   # boot a sim (see boot cmds below)
.venv/bin/python tests/frontend/ios_sim_smoke.py

# Layer 3 — chaos simulator (live gateway, randomized + adversarial):
python3 tests/chaos.py --iterations 500 --workers 4
python3 tests/chaos.py --seed 42 --quiet    # reproduce a past failure

# Layer 3b — /stream concurrency load test (live gateway, opt-in):
python3 tests/load_stream.py --concurrency 40 --count 80
python3 tests/load_stream.py --gateway https://127.0.0.1:8443 --insecure --max-p95 8

# Layer 3c — Subsonic completeness/performance/art verification (live, opt-in):
python3 tests/subsonic_verify.py
python3 tests/subsonic_verify.py --seed 42 --reps 5 --art-sample 40
```

`tests/load_stream.py` guards the **threadpool-starvation regression** (the 2.0
"stops after one track" origin bug): it fires N concurrent full `/stream` pulls
at the live gateway (real track URLs from `library.db`) and asserts **zero
failures + an optional p95 threshold** (`--max-p95`). Live-gateway + opt-in like
`chaos.py` (NOT in `run_all.py`). Prints p50/p95/max + throughput so before/after
runs compare directly. The fix it guards: the shared threadpool limiter raised
40 → 256 (`dlna_asgi.py`) so audio relays don't starve behind browse/art.

`tests/subsonic_verify.py` (2026-07-03) measures the **Subsonic/Amperfy
surface** against `library.db` ground truth (the same LibraryDB methods the
API wraps): completeness (getArtists / paginated getAlbumList2 / per-album
song counts), per-endpoint latency percentiles, and cover-art health
(status/MIME/bytes over a random album sample, distinguishing "no art in
DB" from real fetch failures). Credentials from `.env`. Run it whenever a
Subsonic client "feels flaky" — its first run pinned four real defects
(no type-ahead prefix search, punctuation terms AND-blanking queries, the
5 MB art cap, search3 ids missing `album_key`). Live + opt-in like
`chaos.py` (NOT in `run_all.py`). Exit 0 = clean.

### Frontend test architecture (`tests/frontend/`)

The Playwright suite never touches the live gateway — it boots a Python
stub (`stub_gateway.py`) on an ephemeral port that serves the real
`static/` files and mocks every `/api/*` endpoint app.js calls. Each test
gets a fresh stub instance via the `gateway` fixture (state) plus a
Playwright `app` (desktop, 1280×800) or `mobile_app` (iPhone-sized,
375×667) page.

Add a button-or-feature test by appending to the right `test_<panel>.py`.
The stub captures every request into `gateway.requests`, so assertions
of the form *"clicking X must POST {body} to /api/Y"* are one-liners:
`gateway.wait_for_request("/api/Y", method="POST", match=lambda r: ...)`.
Call `gateway.clear_requests()` before the user action so stale init
calls don't false-match.

### Real-Safari smoke layer (`tests/frontend/safari_smoke.py`)

The Playwright suite runs **Chromium** by default (and optionally its bundled
**WebKit** via `--browser webkit` — which is desktop WebCore/JSC, *not* real
Safari and *not* iOS: it caught the video `native` vs `native-hls` codec
divergence, but not the SW/PWA/autoplay class). `safari_smoke.py` fills the next
rung: an **opt-in Selenium/safaridriver script that drives the actual Safari on
this Mac**, whose real WebKit Service-Worker lifecycle is closer to iOS than
Chromium's. It boots the same `StubServer` and runs three checks — app boots +
renders, SW reaches `activated`, and **poison-recovery** (poison the app-shell
cache → reload → the app must still render, i.e. network-first on real WebKit —
the exact 2026-06-27 outage condition; see the SW cache-tiers note above).

Deliberately **NOT** in `run_all.py`: safaridriver has no headless mode (a real
Safari window opens), allows only one session, and needs one-time enablement.
Setup: `.venv/bin/pip install selenium` (optional dev dep, not in
`requirements.txt` — same as pytest/playwright) · `safaridriver --enable` ·
Safari → Settings → Advanced → "Show features for web developers" → Develop →
"Allow Remote Automation". Run: `.venv/bin/python tests/frontend/safari_smoke.py`
(exit 0 = pass). **Honest scope:** desktop Safari ≠ iOS Safari — no
standalone-PWA mode, autoplay/audio-session policy, or WKWebView networking, so
the "Mobile / PWA testing checklist" (real device) stays the iOS gate; this
covers the Safari engine + SW class only.

### iOS-Simulator smoke layer (`tests/frontend/ios_sim_smoke.py`)

The highest-fidelity **automated** iOS rung: drives real **Mobile Safari in an
iOS Simulator** via Appium/XCUITest, so it exercises the genuine iOS
Service-Worker lifecycle (the class behind the 2026-06-27 outage) that even
desktop Safari only approximates. Same three checks as `safari_smoke.py` (boot +
render, SW `activated`, and **poison-recovery** — poison the app-shell cache →
reload → app must still render, network-first on Mobile Safari). Verified
passing on iOS 26.5 / iPhone 15. Also opt-in, NOT in `run_all.py` (needs a
booted Simulator + a running Appium server; slow first-run WebDriverAgent
build). Default device `iPhone 15`; override with `IOS_DEVICE` / `IOS_VERSION` /
`APPIUM_URL`. **Same honest scope caveat as above** — even the Simulator can't
script standalone home-screen PWA mode, autoplay/audio-session, or WKWebView
networking; the real-device checklist stays the final iOS gate.

One-time setup:
```bash
xcodebuild -downloadPlatform iOS                 # iOS Simulator runtime (~8.5 GB)
npm install -g appium && appium driver install xcuitest
.venv/bin/pip install Appium-Python-Client        # optional dev dep, not in requirements.txt
```

Appium server (start / stop):
```bash
appium >/tmp/appium.log 2>&1 &                    # start (listens on :4723)
curl -s http://127.0.0.1:4723/status              # health check ({"ready":true})
pkill -f "node.*appium"                           # stop
```

Simulator devices (create-once already done for 15/16/17; boot / shutdown):
```bash
# create (only if a device is missing):
xcrun simctl create "iPhone 16" \
  com.apple.CoreSimulator.SimDeviceType.iPhone-16 \
  com.apple.CoreSimulator.SimRuntime.iOS-26-5
xcrun simctl boot "iPhone 15"                     # boot (also "iPhone 16" / "iPhone 17")
xcrun simctl boot "iPhone 16"
xcrun simctl boot "iPhone 17"
xcrun simctl list devices | grep Booted           # what's running
xcrun simctl shutdown "iPhone 15"                 # stop one (or: shutdown all)
xcrun simctl shutdown all
open -a Simulator                                 # optional: show the sim window
```

`chaos.py` hard-fails if it sees any 5xx, `/tmp/dlna-gateway.err` grows (= silent thread death), or a snapshot takes >5s. Its first real-world find was the `playlist_tracks.duration` HH:MM:SS-string `ValueError` that was killing the renderer-queue daemon thread invisibly.

Each core module also has a standalone self-test:

```bash
python dlna_config.py              # config/logging
python dlna_discovery.py           # SSDP discovery (20s live scan)
python dlna_content.py <control-url>  # UPnP SOAP
python dlna_library.py             # DB operations
python db_pool.py                  # concurrent DB stress test
python dlna_player.py              # QueueRegistry + duration-parser self-test
```

## Architecture

### Entry Point & Thread Model

`dlna_gateway.py:main()` wires all modules and spawns these daemon threads:
1. SSDP multicast listener (MediaServer + MediaRenderer discovery)
2. Pre-prober (re-probes known servers cached in DB on startup)
3. Subnet scanner (fallback if SSDP finds nothing)
4. Heartbeat thread (marks devices offline after 2 consecutive failures)
5. Gateway SSDP announcer (broadcasts gateway itself as a MediaServer)
6. HTTP server (ThreadingMixIn, handles all API and static file requests)
7. Optional HTTPS server (separate thread, redirects HTTP→HTTPS)

### Module Responsibilities

| File | Responsibility |
|---|---|
| `dlna_gateway.py` | Module wiring + `start_background_services()` (spawns the daemon threads). **2.0:** no longer the process entry — the `dlna_asgi` lifespan calls it so `hypercorn dlna_asgi:app` boots the whole gateway. Its own stdlib HTTP edge + TLS were removed. |
| `dlna_asgi.py` | **2.0 — THE server (Hypercorn owns the whole edge).** FastAPI app; terminates **TLS + HTTP/2** (ALPN) on `:8443` + plain on `:8765`, owns the `tailscale cert`. Native routes for the read API, `/art`, `/stream` + `/radio_stream` relays, static/PWA, the Subsonic byte methods, and the **Naim-facing `/gw/*` UPnP surface** (device.xml/desc.xml/events/control on the plain `:8765` bind — Cleanup C folded it in here, retiring the separate `dlna_server.py` device server + `run-2.0.sh`). Remaining legacy handlers run via the bridge. Lifespan boots `start_background_services`. `docs_url=None` (no Swagger CDN call). Run: `./run-2.0-asgi.sh`. |
| `dlna_asgi_bridge.py` | Shim that runs the legacy `(h, params)` handlers unchanged inside the ASGI app (fake `h` captures `_json`/`_html`/`_xml_response`/`send_error`; runs in a threadpool). Routes are rewritten native one batch at a time, then dropped from the bridge. |
| `dlna_art_cache.py` | **2.0.** On-disk cover-art byte cache keyed by source URL. `api_playback.art_fetch_cached()` fronts `art_fetch` so `/art` + Subsonic `getCoverArt` serve repeat covers from disk (across clients + restarts) instead of re-fetching coverartarchive / re-decoding embedded art. TTL + size-capped; `art_cache/` gitignored. `art_fetch` follows redirects (coverartarchive `front-500` 307→archive.org) + rejects <64 B junk bodies. |
| `dlna_events.py` | **2.0.** `EventBus`/`EVENTS` (thread-safe publish → asyncio loop) + native `GET /api/events` (SSE). Publishers: RendererQueue state, index-status transitions, discovery changes. The PWA opens an `EventSource` as a polling accelerator (fallback intact). |
| `dlna_routes.py` | `GET_ROUTES` / `POST_ROUTES` path → handler maps |
| `dlna_discovery.py` | SSDP listener, probe, subnet scanner, server heartbeat |
| `dlna_registry.py` | Data classes + `ServerRegistry` / `RendererRegistry` thread-safe stores |
| `dlna_library.py` | `LibraryDB` — SQLite index + FTS5 search + playlists; composition root for DB-owning singletons |
| `dlna_indexer.py` | `Indexer` — background crawler that walks a MediaServer and populates LibraryDB |
| `dlna_art_fetcher.py` | `AlbumArtFetcher` — Phase B MusicBrainz + Cover Art Archive lookup |
| `dlna_devices.py` | `DeviceRoleCache` — in-memory mirror of device_roles for zero-latency classification |
| `db_pool.py` | SQLite connection pool — WAL mode, thread-local connections, write serialization |
| `dlna_config.py` | Constants (`DB_FILE`, `CFG_FILE`, `LOG_FILE`), logging setup, config load/save |
| `dlna_providers/` | `LibraryProvider` seam (P0). Protocol + dataclasses + registry; `mock.py` for tests; `upnp.py` (P1) wraps the existing UPnP SOAP path; `localfs.py` (P2) is the in-process backend (mutagen + watchdog + a content-hashed track id). `plex.py` / `jellyfin.py` land in P3+ if/when the LocalFs path proves the seam works. |
| `dlna_localfs_server.py` | LocalFs HTTP file server (P3). `ThreadingHTTPServer` on its own port (default 8200, bound `0.0.0.0`). `GET /localfs/stream/<id>` resolves via `library.db` and streams the original bytes in 64 KB chunks. Range-aware (`Accept-Ranges: bytes`, `Content-Range`, 206 / 416), DLNA-headered (`DLNA.ORG_PN`, `transferMode`), bit-perfect. Path-traversal defence via `allowed_roots`. Also serves `GET /localfs/art/<id>` — the file's first embedded cover picture on demand via `_extract_art_bytes` (FLAC/ID3/MP4, MIME sniffed from magic bytes), 12 MB cap, 404 on no-art. |
| `dlna_localfs_wiring.py` | Boot-time wiring of the LocalFs provider (P4). `maybe_start_localfs(get_lan_ip)` is called from `dlna_gateway.main()`; gated on `$LOCALFS_MUSIC_ROOT` / `localfs.root` in `config.json`. Starts the file server, creates a `LocalFsProvider` with the LAN-IP `base_url`, binds it via `dlna_providers.bind_provider`, adds a synthetic `MediaServer` entry to `SERVERS`, kicks off the initial scan in the background. Kept in its own module so the run_all.py "Gateway is slim (<350 lines)" lint stays green. |
| `dlna_content.py` | UPnP ContentDirectory SOAP client (`cd_browse`, `cd_search`). After Phase 1, reached ONLY via `dlna_providers/upnp.py`. |
| `dlna_avtransport.py` | UPnP AVTransport SOAP client (send/stop/pause/state/position) |
| `dlna_player.py` | `RendererQueue` (sequential playback per renderer) + `QueueRegistry` (one queue per UDN) |
| `dlna_stream_proxy.py` | Browser-audio HTTP proxy (`/stream`) with 5-min idle timeout |
| `api_browse.py` | Browse/search API endpoints |
| `api_playback.py` | Playback, stream proxy route, `/art`, `/api/client_log`, state, indexer management |
| `api_playlists.py` | Playlist CRUD endpoints |
| `dlna_ffmpeg.py` / `dlna_geocode.py` / `dlna_video_index.py` | Video feature (see `docs/VIDEO_SUPPORT.md`): optional ffmpeg/ffprobe helpers + HLS transcode cmds; Nominatim reverse-geocode (cache-first, 1.1s rate limit); the periodic GWMovies scanner (`scan_videos`, 5-min loop from `dlna_localfs_wiring`) incl. `apply_location_overrides` (re-lays inferred/manual locations after every scan). |
| `dlna_countries.py` | ISO 3166-1 alpha-2 → English name (`country_name`). **GENERATED** via Node `Intl.DisplayNames` (regen one-liner in its docstring / generating commit) — used so the video country-selection level shows "Netherlands", not "NL" (ids/filenames keep the code). The PWA uses the browser's `Intl.DisplayNames` directly. |
| `api_upnp.py` | The gateway-as-MediaServer: a **complete DLNA Media Server** the Naim/LG browse. Device descriptor (`MediaServer:1` + `X_DLNADOC` + icons + ContentDirectory **and** ConnectionManager), both service SCPDs, SOAP `ContentDirectory#Browse` over the full library (`_gw_browse`) + the pre-browse handshake actions, `ConnectionManager#GetProtocolInfo` etc., GENA SUBSCRIBE + initial NOTIFY, and SSDP announce + **M-SEARCH responder**. See "UPnP exposure (Naim)". |

### Key Module-Level Singletons

These are shared state across all request handler threads:

- `dlna_discovery.SERVERS` / `RENDERERS` — device registries
- `dlna_library.DB` / `INDEXER` / `DEVICE_ROLES` — library DB, crawler, device role cache
  ⚠ `DB = LibraryDB()` is constructed at MODULE IMPORT — merely importing
  `dlna_library` (tests, tools, a REPL in the repo dir) runs `_init_schema`
  and ALL pending migrations against the real `library.db`, even while the
  gateway is live. Safe by design (idempotent migrations, WAL, per-call
  pool connections) but surprising: a new migration "lands" on the live DB
  the first time any test imports the module, not at the next restart.
- `dlna_player.QUEUES` — `QueueRegistry` holding one `RendererQueue` per renderer UDN (lazily created). Replaces the prior single-queue singleton so multiple users/renderers can play concurrently.

### Database Schema

SQLite at `library.db`, WAL mode, accessed via `db_pool.Pool`. The
committed `schema.sql` is a **generated artifact** — it does NOT
auto-update when `LibraryDB._init_schema` / migrations change, and has
drifted before. After any schema change run **`python3
tools/regen_schema.py`** to regenerate it; `tests/test_schema_sync.py`
fails the suite if it's stale (`tools/regen_schema.py --check` is the
same gate).

```
tracks(id, udn, obj_id, url, title, artist, album, duration, art, mime, genre, file_path, bit_depth, sample_rate, year, album_key)
  UNIQUE(udn, artist, album, title, album_key, bit_depth, sample_rate)
  -- album_key joined the UNIQUE 2026-07-12 (_migrate_widen_unique_album_key):
  -- two DISTINCT files in different FOLDERS with identical tags (duplicate
  -- editions, scene comps) both index; UPnP rows keep album_key='' so their
  -- collision semantics are unchanged. Same-folder same-tag files still
  -- collide (true tag ambiguities — fix by retagging).
  UNIQUE(udn, url) via idx_tracks_udn_url  -- created by _migrate_unique_url
  bit_depth + sample_rate are parsed from the URL at index time
  (AssetUPnP pattern /b<N>/f<MMMMM>/). They participate in UNIQUE so a
  16-bit and 24-bit copy of the same album coexist as distinct rows.
  The separate UNIQUE(udn, url) index dedups same-URL inserts (AssetUPnP
  serves each file via multiple browse-container paths).
  Browse-side queries hide lower-quality dupes via _dedup_clause.
  year is the FILE-TAG year (DIDL-Lite dc:date / upnp:originalTrackDate),
  parsed by dlna_content._parse_didl. Frontend prefers
  metadata_overrides.year (the MusicBrainz original year) over this
  edition year — see "Year display" subsection.
tracks_fts — FTS5 virtual table over (title, artist, album)
  Search semantics (2026-07-03): every whitespace term must match
  (implicit AND) and the LAST term matches as a prefix — type-ahead
  per keystroke works (Amperfy / the PWA box). Punctuation-only
  tokens ("-", "&") are dropped (they'd AND-blank the query).
  KNOWN LIMITATION: folder-derived album display names are NOT in
  FTS (it indexes raw tags) — an album whose folder name differs
  from its tag name is searchable only by the tag name.
  AUTO-HEAL (2026-07-03): the recurring shadow-table corruption
  ("database disk image is malformed", 6× since Apr 2026, tripped by
  mass DELETE/INSERT trigger traffic) self-heals on every mass-write
  path — LibraryDB.run_with_fts_heal (repair_fts + one retry) wraps
  clear(), upsert_tracks(), the LocalFs rescan finalize txn, and the
  Indexer crawl. Guarded by tests/test_fts_heal.py.
  SYNC TRIGGERS (2026-07-12): tracks_ai (INSERT), tracks_ad (DELETE),
  and tracks_au (UPDATE OF title/artist/album — added with the
  retagged-file rescan fix; before it, any in-place metadata UPDATE
  desynced FTS until the next full rebuild).
metadata_overrides(url, artist, album, title, genre, year, updated_at, source)
  source ∈ {'manual', 'acoustid', 'notfound', 'video_skip'}
  year is the MUSICBRAINZ original release year (release-group's
  first-release-date) — captured historically by the AcoustID worker
  (removed in 2.0); nowadays written by tools/improve_song_years.py /
  correct_year_drift.py as source='manual'.
index_meta(udn, indexed_at)
playlists(id, name, created_at, sort_order)
playlist_tracks(id, pl_id, url, title, artist, album, duration, art, added_at)
  UNIQUE(pl_id, url)
device_roles(udn, name, location, host, is_server, is_renderer, first_seen, last_seen)
album_art(artist, album, art_url, source, updated_at)
  PRIMARY KEY (artist, album)
  source ∈ {'sibling', 'musicbrainz', 'notfound', 'manual'}
play_counts(url, count, last_played)
  PRIMARY KEY (url)
  Incremented by LibraryDB.radio_tracks(); persists across rebuild-index.
playback_positions(album_key, url, position_sec, duration_sec, finished, updated_at)
  PRIMARY KEY (album_key) — audiobook resume: ONE row per book (its
  folder), holding which chapter + offset. Survives clear(udn) like
  play_counts/lyrics. finished=1 → PWA offers "start over"; any normal
  save clears it. Root-level single-file books key by URL (PWA fallback).
  See "Audiobooks" section.
videos(id, udn, url, title, file_path, ..., created, location,
       location_name, country, poster, ...)
  id = sha1(rel_path)[:16], udn = uuid:localfs-movies. Populated by
  dlna_video_index.scan_videos (5-min periodic). See docs/VIDEO_SUPPORT.md.
video_location_overrides(video_id, location_name, country, source, updated_at)
  source ∈ {'inferred_same_day', 'inferred_window', 'inferred_country',
  'manual'} — locations for GPS-less videos, written by
  tools/infer_video_locations.py. Survives clear_videos (the scanner
  would wipe them otherwise); RE-APPLIED at the end of every video scan
  (dlna_video_index.apply_location_overrides). 'manual' beats inferred;
  a row NEVER overwrites a real-GPS video; embedded titles never rewritten.
video_people(video_id, person, person_id, updated_at)
  PRIMARY KEY (video_id, person) — Immich person tags, synced by
  tools/immich_people_sync.py (per-person REPLACE semantics). Survives
  clear_videos. Feeds the "👤 By person" DLNA container + PWA grouping.
```

### Indexer-side dedup (AssetUPnP virtual-album aliases)

Diagnosed 2026-05-28: AssetUPnP exposes the SAME physical file under
multiple browse paths — the real album AND any "virtual compilation"
albums (Greatest Hits, Best Of, Music From the OC: Mix 5, case-only
album variants like `Live in Armenia` vs `Live In Armenia`, etc.).
HTTP HEAD of paired URLs returns byte-identical Content-Length on
~99% of pairs. Pre-dedup row counts were ~1.82× the actual file
count (40,662 rows for ~22k files).

`upsert_tracks` dedups by `(d_id, _norm_title(title))` keyed against
both existing rows and the in-flight batch:

- `d_id` = the `d-<n>-co` segment of the AssetUPnP URL. d-id is NOT
  a per-file identifier — it collides across distinct files. Pure
  d-id matching would over-collapse; combining with title catches
  the virtual-album case while preserving genuine d-id collisions
  (Kryptonite + Down Poison sharing `d-4591903772373150829` stay
  as 2 rows because their titles differ).
- `_norm_title` = NFKD diacritic stripping + smart-apostrophe/quote
  → ASCII + lower + whitespace collapse. Catches the AcoustID-
  corrected-vs-raw race where the existing tracks.title has a curly
  apostrophe (rewritten by COALESCE) and the incoming raw title has
  ASCII apostrophe. Keeps bracketed annotations IN the key so
  legit-distinct recordings ("Be Like That" / "Be Like That
  (acoustic)" sharing d-id within the same album) survive as 2 rows.

**Known limitation:** when two genuinely distinct files coincidentally
share a d-id AND the same title (Pink Floyd "Comfortably Numb" on
The Wall vs on Shine On compilation — 47 MB vs 42 MB, definitely
different bytes), the dedup currently collapses them. The track is
still playable via every other album, so the cost is one "original
album" browse-row missing per such collision. Accepted as
right-enough; HTTP-HEAD-based dedup with Content-Length in the key
remains an option if this bites more often.

Two rebuilds collapsed 40,662 → 30,292 → 28,868. Remaining ~6,000
"extras" above the ~22k distinct-file count are largely correct
distinctions (live recordings, acoustic versions, extended mixes,
etc.).

### Frontend

`static/index.html` + `static/app.js` (PWA, ~71K lines). Communicates with backend via `/api/*` JSON endpoints. Features: letter bar, browse modes, playlist management, MediaSession API, Service Worker offline support. Dark theme with amber accents (`static/app.css`).

**Service Worker cache tiers (`static/sw.js`).** Three caches: `APP_CACHE`
(app shell — **network-first** as of 2026-06-27, was stale-while-revalidate),
`ART_CACHE` (`/art`, cache-first), and
`API_CACHE` (2026-06-02) — a **stale-while-revalidate** cache for the
`CACHEABLE_API` allowlist of STABLE browse GETs (`/api/browse_letter`,
`album_tracks`, `artist_albums`/`artist_tracks`, `albums`, `search`,
`genres`/`genre_*`, `decades`/`decade_*`). Repeat navigation is instant
over a slow tailnet; the background fetch still refreshes the entry (and
still hits the gateway, so request-assertion tests pass). Everything NOT
on the allowlist — `/api/state`, `/servers`, `/renderers`, `/index/status`,
`/album_favourites` (user-mutated), `/track_meta`,
`/radio/*`, `/stream`, POSTs — stays **network-only** so live/mutable data
is never stale. Bump `API_CACHE`'s version to force-evict if its shape
changes. Measured server cost that this hides: the folder-grouped
`/api/browse_letter` albums query is ~150 ms; cached → ~0.

**Why the app shell is network-first (2026-06-27).** It was
stale-while-revalidate, which served the cached `/` document AND
`/static/app.js` cached-first. A once-broken/truncated cached `app.js`
then pinned the app blank on every load ("full UI, no content",
unrecoverable by refresh). The shell (document + assets) is now
**network-first**: an online load always gets the fresh HTML/JS/CSS;
the cache is the **offline fallback only**. `install` also calls
`self.skipWaiting()` **unconditionally** (was gated behind
`cache.addAll(SHELL)` — a single failing shell entry left the new worker
stuck in `waiting` forever, so updates never activated) and `activate`
calls `clients.claim()`, so a new worker takes over on refresh going
forward. Caveat: a client already wedged on the *pre-fix* worker does
**not** self-heal — it needs a one-time "clear site data" (iOS PWA:
delete + re-add the home-screen icon). Guarded by
`tests/frontend/test_pwa.py::test_poisoned_shell_cache_still_renders`
(poison the live cache → reload → app must still render) and
`test_sw_navigation_is_network_first`.

**Source picker (`#source-sel`).** When more than one MediaServer is in `SERVERS` (e.g. AssetUPnP + LocalFs coexisting), the header carries a `SRC` dropdown next to the `OUT` (renderer) picker. `selectSource(udn)` swaps the active `curServer`, resets browse navigation, and reloads the library (or re-runs the active search). `refreshServers()` populates it via `rebuildSourceSel()` (💾 icon for `uuid:localfs-*`, 🗄 otherwise) and `updateDiscStatus()` keeps the header disc-dot tracking the active source. Regression-guarded by `tests/frontend/test_source_picker.py`.

### Concurrency Notes

- All DB writes are serialized through `db_pool`'s write lock; reads use thread-local connections in WAL mode.
- SOAP calls to UPnP servers are throttled by a semaphore (max 3 concurrent) in `dlna_content.py` to avoid overwhelming servers like AssetUPnP.
- Device registries use `threading.Lock` for thread-safe access.
- `RendererQueue.snapshot()` is cached for 500ms and coalesced across concurrent callers: only the first cache-miss fires SOAP; subsequent callers during the fetch return the stale cache immediately rather than block. Prevents N polling browser tabs from stacking N SOAP round-trips.
- The two SOAP calls inside `snapshot()` (GetTransportInfo + GetPositionInfo) run in parallel threads — halves snapshot latency under load, caps at ~6s on an unresponsive renderer instead of 12s.
- Worker threads kicked off by `/api/render_queue` are wrapped in `_start_safe()` with `log.exception` so any crash inside `RendererQueue.start()` lands in `gateway.log`, not silently in `/tmp/dlna-gateway.err`.

### Concurrent playback model

Each renderer (UDN) owns its own `RendererQueue` in `QUEUES`. Architectural rule: one active stream per physical output, enforced server-side.

- `POST /api/render_queue` with `{udn, tracks}` → 200 if that renderer is idle.
- `POST /api/render_queue` while that UDN is already playing → **409 Conflict** with body `{error: "renderer_busy", busy_with: {title, artist, renderer}}`.
- Client resends with `{udn, tracks, force: true}` to take over — this stops the prior session and starts the new queue.
- The UI (`sendRenderQueue()` in `app.js`) catches the 409 and shows a native `confirm()`: "X is already playing Y — Take over?".
- Different UDNs are fully independent: queuing on renderer A doesn't touch renderer B's state. Per-UDN concurrency is proven by `tests/test_api_playback.py::test_concurrent_renderers_have_independent_state`.

### Browser-audio stream proxy

`proxy_stream()` in `dlna_player.py` relays bytes from the media server to the browser over a single HTTP connection with Range support.

- `PROXY_IDLE_SEC = 300` (module-level so tests can monkey-patch) — if the browser stops consuming bytes for 5 minutes (laptop suspended, tab closed without clean FIN), the proxy tears down the upstream connection. On laptop wake, the user starts playback again from the beginning; there's no resume.
- Every session logs `proxy_stream ▶ START host/path` and `proxy_stream ■ END host/path sent=N bytes in Xs reason=<r>` with reason ∈ `{upstream_eof, client_idle_timeout, client_closed, error:<Type>}`.
- The gateway is NOT in the audio path for UPnP renderers (the renderer streams directly from AssetUPnP); the proxy only matters for browser-audio playback.
- No HTTP keep-alive — `Connection: close` on both ends. Each browser Range request opens a new upstream TCP connection. On LAN this is ~1ms; on Tailscale it's ~50-100ms per seek. Acceptable for current load; would need a connection pool if users start complaining about seek latency over the tailnet.

### `/art` — lock-screen artwork proxy

iOS MediaSession refuses to load cross-origin artwork on the lock screen. The PWA rewrites every track art URL to `/art?url=<external>` so the lock-screen fetch is same-origin. Service Worker cache-firsts these (art rarely changes).

- Hard-caps at 12 MB per image to prevent memory abuse (raised from 5 MB
  2026-07-03 — real embedded covers exceed 5 MB and the cap made
  getCoverArt 404 deterministically for every candidate of such an
  album; 12 MB matches `dlna_localfs_server`'s embedded-art cap).
- Validates Content-Type starts with `image/` — an upstream HTML 404 page won't poison the SW cache.
- 10-second timeout; slow upstream fails fast.
- The handler is in `api_playback.art()` routed at `/art` in `dlna_asgi.py`.

### Browser audio error handling (MediaError discrimination)

`static/app.js` listens for `error` events on the `<audio>` element and branches on `MediaError.code`:

| code | name              | behavior                                                      |
|------|-------------------|--------------------------------------------------------------- |
| 1    | ABORTED           | ignored (we or the UA told it to stop)                        |
| 2    | NETWORK           | retry the same track ONCE (transient network), then skip      |
| 3    | DECODE            | retry the same track ONCE (could be a bad Range chunk), skip  |
| 4    | SRC_NOT_SUPPORTED | skip immediately — the format genuinely isn't playable        |

Prior to 2026-04-23 every `error` event was treated as code 4 and auto-skipped, producing false-positive "unsupported format" skips whenever the network hiccupped. Every event (including ignored-code-1) is now POSTed to `/api/client_log` so real-world incidents land in `gateway.log` for diagnosis.

### `_playBrowserAudio()` — autoplay-rejection-aware play()

Every call to `browserAudio.play()` now routes through this helper instead of `audio.play().catch(()=>{})`. When the browser rejects with `NotAllowedError` (autoplay blocked, first-play without gesture, iOS standalone resume) or `AbortError`, the helper:
1. Resets the play button UI so the user can manually re-trigger.
2. Toasts "⚠ Browser blocked playback — tap ▶ Play to start".
3. POSTs a `play_rejected` event to `/api/client_log` with the error name + UA.

Before this, blocked autoplay was invisible: the UI showed "⏸ Pause", no audio, no error surfaced.

### `/api/client_log` — browser-side observability

POST-only endpoint that logs free-form JSON reports from the PWA into `gateway.log` under the `dlna.client` logger:

```
grep "dlna.client" gateway.log         # all browser-reported events
grep "client_log\[audio_error" gw.log  # just MediaError events
grep "client_log\[play_rejected" gw.log # just autoplay blocks
```

Payload fields are clamped defensively (40 chars for `kind`, 120 for fields, 200 for `message`, 80 for `ua`) so a broken or malicious client can't flood the log. Handler in `api_playback.client_log()`.

### HTTPS / Tailscale cert

The gateway auto-detects `*.crt` + `*.key` in the working directory on startup. Currently uses a Tailscale-issued Let's Encrypt cert: `ronsmacmini.tail5be6ad.ts.net.{crt,key}`. Mobile devices on the tailnet get a trusted cert with no install or exception needed.

**Cert renewal is automated** via `renew-cert.sh` + `com.roha.dlna-cert-renew` LaunchAgent (Mondays 04:30). The script no-ops unless the cert has < 30 days left, then runs `tailscale cert …` and `launchctl kickstart` of the gateway. All output appended to `cert-renewal.log` (separate from `gateway.log` for grep-ability). Belt-and-braces: on every successful HTTPS bind the gateway logs a WARN if the cert has < 14 days left (`_warn_if_cert_expiring_soon` in `dlna_gateway.py`) — surfaces a silently-dead LaunchAgent.

Manual override:

```bash
./renew-cert.sh --force        # force a renewal NOW regardless of remaining days
launchctl kickstart gui/$(id -u)/com.roha.dlna-cert-renew   # dry-run the weekly job
```

Install (one-time, after first clone):

```bash
cp com.roha.dlna-cert-renew.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.roha.dlna-cert-renew.plist
```

### HTTP/2 · HTTP/3 · TLS — DONE in 2.0 (roadmap retained for history)

> **✅ 2.0 — SHIPPED (cutover 2026-06-08/09).** HTTP/2 + app-owned TLS is
> **done**: the gateway is a **Hypercorn + FastAPI ASGI app (`dlna_asgi.py` +
> `dlna_asgi_bridge.py`)** that **terminates TLS and negotiates HTTP/2 (ALPN)
> natively** on `:8443` (plain HTTP on `:8765`), using a `tailscale cert`-issued
> cert (cert-renewal machinery kept, now Hypercorn-owned). The Naim-facing
> `/gw/*` UPnP surface (on the plain `:8765` bind, folded into the ASGI app by
> Cleanup C) and RoHaLocalFS (`:8200`) stay plain HTTP for the Naim.
> HTTP/3 (QUIC) is a later `--quic-bind` add. **`tailscale serve` was tried and
> dropped** (broken on this mini's Tailscale `:443`) — the app owning TLS is the
> chosen end-state. Verified trusted h2 over the tailnet hostname. Full detail:
> `docs/BUILDING_2.0.md`, `docs/CUTOVER_RUNBOOK.md`, `docs/ARCHITECTURE.PDF`.
> **Everything below this line is the pre-2.0 (1.x stdlib) state, kept for
> historical context — it no longer describes what runs.**

> *(Historical pre-2.0 detail — the stdlib HTTP/1.1 analysis, the h2/h3*
> *option matrix, and the reverse-proxy recommendation — stubbed
> 2026-07-08 to keep CLAUDE.md under 150k. Full text:*
> *`git show c7e7c42:CLAUDE.md`.)*

### Mobile / PWA testing checklist

Because there's no automated test coverage for on-device behavior (iOS Safari, Android Chrome, PWA standalone mode), before believing a browser-mode change is shipped, manually verify on an actual mobile device over Tailscale:

1. **Fresh PWA install**: Safari → Share → Add to Home Screen. Icon + title use manifest values.
2. **First-play autoplay**: Tap a track. Audio starts. (If it doesn't, and a toast says "Browser blocked playback", that's the correct new behavior — the `catch` surfaces it.)
3. **Lock screen artwork**: Lock the phone during playback. Album art shown on lock screen. Previous/Next/Play/Pause work.
4. **Tab backgrounded**: Switch to another app. Audio continues. (If it pauses, that's iOS's audio-session policy, not a bug.)
5. **Laptop/phone suspend during stream**: Close lid (or long-press sleep). Wait 5+ min. Check `gateway.log` shows `proxy_stream ■ END ... reason=client_idle_timeout` within 6 minutes.
6. **Error reporting**: After playback fails (e.g., play an unsupported-format track), `grep dlna.client gateway.log` should show the event with `codeName=unsupported` etc.
7. **Rebuild / force-refresh**: Pull-to-refresh in PWA. Service Worker updates (check DevTools Application tab → SW version).

When something goes wrong, the diagnostic order is (1) `gateway.log` `dlna.client` entries for browser-side events, (2) `/tmp/dlna-gateway.err` for server-thread crashes, (3) Safari DevTools Web Inspector for pre-report JS errors.

## Library backend — LocalFs (migration complete)

> **Status: migration complete (2026-05-31).** AssetUPnP is
> decommissioned; **RoHaLocalFS** (in-process indexer + bit-perfect file
> server) is the live backend. This section is now architecture
> reference — the seam, the providers, the non-negotiable rules — plus a
> condensed **Status — migration COMPLETE** summary below. The
> blow-by-blow phase history (P0–P6) lives in `docs/MIGRATION_PLAN.md`.

**Original goal.** Replace AssetUPnP with an in-process indexer + file server,
**while generalising the AssetUPnP-shaped code into a `LibraryProvider`
seam** so the gateway can speak to any of: AssetUPnP, MinimServer,
Plex, Jellyfin, or our own in-process backend, modularly.

### Why we're doing this

Current chain:

```
dlna-gateway  --SOAP Browse-->  AssetUPnP  -->  files  -->  Naim (renderer)
```

There are **two sources of truth** — AssetUPnP's internal index and
the gateway's view of it — coupled over UPnP, which is a coarse,
lossy channel. On rescan, AssetUPnP can renumber object IDs,
mishandle `UpdateID`/`SystemUpdateID`, and serve a half-built tree.
The gateway then has to defensively re-walk and retry SOAP. This is
the source of the recurring "hours fixing the gateway" pain, and is
the cause of every category in the d-id-collision / aliasing / orphan
saga that produced `tools/relink_orphan_overrides.py`,
`tools/audit_override_mismatches.py`, and the `upsert_tracks` dedup
work (see those sections below).

Target chain:

```
dlna-gateway (owns index + serves files for the LocalFs provider)
                    ↓ AVTransport
                  Naim (renderer)
```

One index, owned by us, scanned on our terms.

### Non-negotiable rules

1. **Bit-perfect.** Serve the **original file bytes, unmodified.
   Never transcode.** A checksum of served bytes must equal the
   source file. The same rule that applies to the existing browser
   `/stream` proxy (`dlna_stream_proxy.py:45-138`) applies to the
   new file server.
2. **Additive & parallel.** The new backend runs *alongside*
   AssetUPnP against the same (read-only) music folder, on its own
   HTTP port. AssetUPnP is untouched until we choose to stop it.
3. **Reversible.** Backend selection is a config flag. We can flip
   back to AssetUPnP (or any other provider) at any point until
   final decommission.
4. **No big-bang cutover.** Real listening is not affected until
   Phase 4, and even then AssetUPnP remains as a one-flag fallback.
5. **Modular by default.** Adding a new provider must NOT require
   touching the gateway core — it's a new file implementing
   `LibraryProvider`, registered with `dlna_providers`. The
   migration is the *first* user of the seam, not a permanent
   exception to it.

### Target architecture — the `LibraryProvider` seam

A thin provider interface decouples the gateway from the backend
choice. **Multiple implementations coexist**; each renderer/UDN
can be browsed via whichever provider its source server is bound
to. The gateway speaks only the seam, never the wire protocol
directly.

```python
# dlna_providers/__init__.py — abstract seam
from typing import Protocol, Iterator

class LibraryProvider(Protocol):
    """One library source — AssetUPnP, MinimServer, Plex, Jellyfin,
    or our in-process LocalFs implementation. Implementations live
    under dlna_providers/<name>.py and register themselves via
    @register_provider('<name>')."""

    name: str                  # 'upnp' | 'plex' | 'jellyfin' | 'localfs'
    udn: str                   # stable id for this provider instance

    def list_artists(self) -> Iterator[Artist]: ...
    def list_albums(self, artist_id: str) -> Iterator[Album]: ...
    def list_tracks(self, album_id: str) -> Iterator[Track]: ...
    def get_track(self, track_id: str) -> Track: ...
    # Stream URL the *renderer* will fetch. For UPnP/Plex/Jellyfin
    # this is the source server's URL. For LocalFs it's our own
    # HTTP file server's URL. NEVER a /api proxy.
    def stream_url(self, track_id: str) -> str: ...

    # Optional. Providers without native search can leave unimplemented
    # and the gateway falls back to LibraryDB FTS5 on its mirror.
    def search(self, q: str, limit: int) -> Iterator[Track]: ...

    # Health/discovery hooks
    def probe(self) -> bool: ...          # is the backend reachable?
    def watch_changes(self, on_change) -> None: ...   # incremental updates
```

### Key facts that shape the design

- **The renderer fetches bytes directly from `stream_url`** — the
  gateway does **not** proxy audio on the renderer path. So the
  serving endpoint must be reachable by the Naim on the LAN. The
  existing `dlna_stream_proxy.py` proxies only browser-mode audio
  (an iOS Safari same-origin requirement); the Naim never sees it.
- **The Naim issues HTTP Range requests.** Correct
  `206 Partial Content` handling is mandatory (`Accept-Ranges`,
  `Content-Range`), or seeking and sometimes playback start will
  break.
- **DLNA response headers** (`contentFeatures.dlna.org`,
  `transferMode.dlna.org`) are required and will need iteration
  against the real Naim. Handle serving manually rather than via a
  framework static-file helper, so these can be set.
- **SSDP is not required** for the LocalFs file server. The gateway
  pushes URIs to the Naim via `AVTransport`. The new server is just
  an HTTP file server on its own port; no UPnP device advertisement
  needed. (Running both servers is therefore safe — separate index
  DBs, separate HTTP ports, SSDP multicast coexists by design.)
- **The gateway's own SSDP announcer** (the "gateway-as-MediaServer
  for Naim playlists" feature, currently in `api_upnp.py`) is
  unaffected. It announces the *gateway's own* playlist tree, not
  a provider's library.

### Backend implementations

Each provider lives at `dlna_providers/<name>.py`:

| File | Backend | Wire protocol | Notes |
|---|---|---|---|
| `dlna_providers/upnp.py` | AssetUPnP, MinimServer | UPnP SOAP (ContentDirectory) | Wraps the existing `dlna_content.py` + `dlna_discovery.py` paths. Becomes the FIRST provider extracted, by definition (it's all current behaviour). |
| `dlna_providers/plex.py` | Plex Media Server | Plex HTTP API | Native API exposes richer metadata (ratings, playcount, smart playlists). Requires PLEX_TOKEN. |
| `dlna_providers/jellyfin.py` | Jellyfin | Jellyfin/Emby HTTP API | Open-source equivalent to Plex. Token-based auth. |
| `dlna_providers/localfs.py` | The new in-process backend | Filesystem + in-process HTTP file server | The destination of the migration. Owns SQLite index AND audio serving. |

**Discovery vs. configuration.** UPnP providers are still discovered
via SSDP (today's path). Plex / Jellyfin / LocalFs are configured
via `config.json` because they have no SSDP advertisement (or, in
LocalFs's case, no separate device). The provider registry chooses
which implementation handles a given UDN based on the device's
declared `name`/`model` or the explicit config block.

### Mapping today's code to the seam

Phase 0 is mechanical refactoring — no behaviour change:

| Today | After Phase 0 |
|---|---|
| `dlna_content.cd_browse / cd_search` | `dlna_providers/upnp.py` (called via the seam) |
| `dlna_indexer.Indexer._run` | unchanged; just calls `provider.list_albums(...)` instead of `cd_browse` directly |
| `dlna_discovery.SERVERS` | gains a `provider:` field on each entry — points to the constructed `LibraryProvider` |
| `dlna_player.RendererQueue._send_current` | unchanged; AVTransport `SetURI` still takes a `stream_url` string. The string just comes from `provider.stream_url(track_id)`. |

The non-UPnP providers (`plex.py`, `jellyfin.py`, `localfs.py`) only
appear in later phases.

### Status — migration COMPLETE (2026-05-31)

All phases P0–P6 are done and verified. AssetUPnP is decommissioned
(switched off, its `tracks` rows deleted, playlists relinked to LocalFs
via `tools/relink_playlists_to_localfs.py`). **RoHaLocalFS is the live
backend**: in-process indexer + bit-perfect file server (`:8200`),
folder-based album grouping, gapless auto-advance (Naim-verified). The
`UpnpProvider` class is **kept** — it's how MinimServer / any generic
UPnP server is supported going forward; only the AssetUPnP binary is
retired.

The detailed phase-by-phase record lives in `docs/MIGRATION_PLAN.md`.
What shipped:

- **P0–P1** — `LibraryProvider` seam + registry; `UpnpProvider` wraps the
  existing UPnP SOAP path (no functional change).
- **P2–P3** — `LocalFsProvider` (mutagen index, content-hashed track ids,
  mtime/size cache) + `dlna_localfs_server.py` (Range-aware, DLNA-headered,
  bit-perfect file server on `:8200`, embedded-art route).
- **P4** — boot wiring (`dlna_localfs_wiring.maybe_start_localfs`), real
  Naim-fetchable URLs, gapless via `SetNextAVTransportURI` + C6 TrackURI
  auto-advance tracking.
- **P5–P6** — ran live alongside AssetUPnP, then decommissioned it;
  playlists/favourites relinked to LocalFs.

Post-migration follow-ups: split-folder tidiness (optional) and the
non-AssetUPnP providers (Plex/Jellyfin) as weekend projects on the
proven seam — see Open questions below.

**Library-completeness audit (2026-07-12).** LocalFs serves the same
music root AssetUPnP did, so file-level completeness is disk vs index:
26,129 audio files on disk, 25,893 tracks rows → **236 files invisible**
to the gateway, all swallowed by the wide UNIQUE at insert time (140
duplicate-edition copies in sibling folders + 96 distinct files with
colliding tags). **RESOLVED same day** by widening the UNIQUE with
`album_key` (`_migrate_widen_unique_album_key` — a beets-retag attempt
failed first: chroma fingerprints rate-limited to 0 candidates AND the
non-standard editions / off-MB scene comps never reach quiet-mode's
strong-match bar). Post-migration force rescan: 25,893 → 26,051 rows.
The remaining **78** unindexed files are SAME-folder same-tag collisions
(alternate takes tagged identically, disc-subfolder folding putting two
"Jamming"s in one album_key, untagged "NN Track.flac" junk) — genuine
tag ambiguities; fix by real retagging (interactive `beets --timid`
session) if ever worth it. The full audit report — and the proposed
**audiobooks plan** (second LocalFs root + `playback_positions`
cross-session resume; P1–P5) — is `docs/REPORTS.html` (self-contained
HTML, open in any browser). The bigger find: **10,859 of 10,890 manual
`metadata_overrides` are ORPHANED** — 10,810 still key on dead AssetUPnP
`:26125` URLs (the ~10k `improve_song_years` year corrections + user
edits) and 49 on the old `:8201` LocalFs port, so the original-year
display has been inert since the decommission. **RECOVERED same day** by
`tools/relink_overrides_to_localfs.py` (see its section below): 46
port-healed, 8,087 orphans transplanted onto 8,009 live URLs, 650
orphaned notfound rows pruned; live manual overrides went 31 → 7,976 and
`year_original` display works again. 2,726 unmatched orphans kept
(inert). The 2026-05-31 playlist-relink casualties (744 removed rows)
remain unrecoverable — no pre-relink backup survives.

### Open questions

- **Plex/Jellyfin priority.** Build both in Phase 2-3 only if the
  LocalFs path proves the seam works. If the seam's clean,
  third-party providers become weekend projects, not a blocker
  for the migration.
- **Mixed-provider browse.** When the user has both an AssetUPnP
  device AND a LocalFs library, does the PWA show two separate
  trees or merge? Default: **separate trees, switchable from a
  source picker**, mirroring today's per-UDN browse. Merge-view
  is post-MVP.
- ~~ReplayGain / loudness~~ — CLOSED 2026-07-12: pass tags through,
  never act on them (bit-perfect). Loudness normalization was built
  then removed (2026-05-31, negligible benefit in peak mode + broke
  browser bit-perfect — see "Volume control"). Not tracked anymore;
  a future perceptual/LUFS feature would be a fresh decision.

### Audiophile notes

**Sound quality: no change.** The Naim fetches the file over TCP,
buffers it, and clocks it to its own DAC with its own clock. TCP
is error-corrected, so identical bytes arrive regardless of which
server sent them; the buffer absorbs network timing. A server
delivering unmodified files has no path to the analog output
other than "which bytes." Same bytes in → same sound out. The
Phase 3 checksum makes this certain. ("Server X sounds warmer
than server Y" does not apply to bit-identical local serving.)

**The only two ways to make it *worse* than AssetUPnP** — both
completeness, not capability:

1. **Gapless.** AssetUPnP does it well; ours will only be as good
   as the `SetNextAVTransportURI` queueing. This is the one
   audible regression risk, and it bites hardest on segued /
   continuous material. Test ruthlessly in Phase 4.
2. **Format coverage.** AssetUPnP transparently handles DSD,
   high-res PCM, ReplayGain tags, embedded art. The scanner must
   read those tags and the server must serve DSD/high-res with
   correct MIME so the Naim accepts them. Verify the exact Uniti
   model's PCM/DSD ceiling so nothing is silently rejected.

### Testing quick-reference

- **Range / 206**: `curl -r 0-1023 -D - http://<host>:8200/localfs/stream/<id> -o /dev/null`
- **Bit-perfect**: compare `sha256` of served bytes vs source file.
- **Gapless**: a known segued album, listen for gaps/clicks at
  track boundaries.
- **Format**: at least one each of FLAC 16/44, FLAC 24/96, FLAC
  24/192, DSF (DSD64), MP3.
- **Multi-provider**: configure both UpnpProvider (against
  AssetUPnP) and LocalFsProvider; verify both browse trees render
  and both can play to the Naim.

## Dependencies

Python packages — all optional, the gateway degrades gracefully if missing (see `requirements.txt` for the canonical list):

```
rich>=13.7.0              # colored terminal logging
python-json-logger>=2.0.7 # structured JSON logging
python-dotenv>=1.0.0      # .env file loader (see caveat below)
```

Standard library only for core UPnP functionality. Chromecast support was removed in commit `2a8d81e`; `PyChromecast` was dropped from `requirements.txt` accordingly.

External CLI binaries — optional per feature:

| Binary | Used by | Install |
|---|---|---|
| `fpcalc` (Chromaprint) | the **beets** enrichment tool (`tools/beets_enrich.py` via pyacoustid) — the in-process AcoustID worker was removed in 2.0 | `brew install chromaprint` |
| `beet` (beets) | metadata enrichment batch (`tools/beets_enrich.py`) | `brew install beets` — NOT pip (Homebrew python upgrades wipe a pip install); then add `musicbrainzngs` + `pyacoustid` to the keg venv — see `requirements.txt` → "beets enrichment toolchain" |

Both workers `_find_*()` walk Homebrew install locations explicitly because launchd-spawned processes have a minimal PATH; missing binaries are detected at scan-start and the worker bails without poisoning its sticky-negative cache.

**`.env` is THE configuration file (consolidated 2026-07-13).** Every
gateway setting lives in `<repo>/.env` — identity (`GW_UDN`, `GW_NAME`,
`APP_VERSION`), libraries (`LOCALFS_MUSIC_ROOT`/`_PORT`/`_VIDEO_ROOT`,
`AUDIOBOOKS_ROOT`), Subsonic credentials, contact email, Immich keys.
The LaunchAgent plist carries ONLY `PATH` + the launch command; the
committed **`.env.example`** documents every key for clean-slate
installs. Rules that keep it sane:

- **Never put a config key back in the plist** — plist env OVERRIDES
  `.env`, so a stale plist value silently wins.
- `dlna_config._load_env_file` loads `.env` at the TOP of the module
  (before `VERSION` is read — moving the load above that read was a
  real bug fix) and has a **built-in fallback parser**, so `.env` loads
  even without python-dotenv — the old "dotenv missing → `.env`
  silently never loaded" failure mode is gone (guarded by
  `tests/test_env_loader.py`). Real process env (shell export,
  `launchctl setenv`) still wins over `.env` for ad-hoc overrides.
- Editing the plist itself needs a full `launchctl bootout` +
  `bootstrap` (a `kickstart -k` does NOT re-read plist env — and
  bootout is async: retry the bootstrap if it fails with I/O error).
  Editing `.env` only needs the normal `./setup.sh --restart`.

## Album art persistence (Phase A + Phase B)

Album covers are cached in a dedicated `album_art` table, keyed by `(artist, album)`, independent of `tracks`. This means a rebuild-index is non-destructive to art:

```
album_art(artist, album, art_url, source, updated_at)
  PRIMARY KEY (artist, album)
  source ∈ {'sibling', 'musicbrainz', 'notfound', 'manual'}
```

### How it fills up

- **Phase A — sibling harvest** (`dlna_library.LibraryDB._backfill_album_art`): instantaneous SQL pass. Runs at end of every `upsert_tracks(udn, rows)` call (i.e. after each indexer run) and once at startup as a migration. Harvests per-album art from tracks that brought their own art (source = `'sibling'`) and applies it onto sibling tracks of the same `(artist, album)` that were missing one.
- **Phase B — external lookup** (`dlna_library.AlbumArtFetcher`, singleton `ART_FETCHER`): event-driven background worker. Fires on two hooks only — (1) a one-shot startup scan 120s after boot (`ART_FETCHER.start_initial_scan()` in `dlna_gateway.main`) to catch albums left bare by a previous interrupted run, and (2) `ART_FETCHER.trigger()` at the tail of every successful `Indexer._run()` so new bare albums from a fresh crawl get looked up immediately. No periodic poll. Walks `bare_albums()` (tracks with no art AND no `album_art` row of any source), queries MusicBrainz release-group → HEADs `coverartarchive.org/release-group/{mbid}/front-500`, writes hits as `source='musicbrainz'` and misses as `source='notfound'`. Rate-limited to ~1 req/sec (`_MB_RATE_LIMIT_SEC = 1.1`) per MusicBrainz ToS. If a trigger arrives while a scan is in flight, it's a no-op — the ongoing `run_once()` re-queries `bare_albums()` between batches and absorbs the new work into the current pass.

### What survives a rebuild-index

The `album_art` cache persists across rebuild-index operations — only the `tracks` table gets wiped and repopulated. After an `upsert_tracks()` call:

1. The fresh `tracks` rows may bring new per-album art from the server → harvested into `album_art` (source=`'sibling'`).
2. `_backfill_album_art(conn, udn=udn)` applies existing `album_art` URLs onto the new tracks that lack one.
3. Brand-new albums not yet in the cache become bare. At the end of `Indexer._run()` (success path), `ART_FETCHER.trigger()` fires and the MB/CAA lookup runs in a background thread. Hits are written into `album_art` AND straight onto `tracks.art` via an UPDATE within `run_once()`, so the now-playing window and any future playlist queries pick them up without another indexer pass. After a gateway restart, the 120s-post-startup scan picks up anything that an earlier run couldn't finish.

### Sticky "notfound" cache

The negative cache is sticky by design — once MusicBrainz failed to match an album, `AlbumArtFetcher` skips it forever so re-indexing doesn't re-hammer MB for the same misses. If you've since fixed artist/album metadata in the source server and want to retry, delete the notfound row(s):

```sql
-- Retry a single album
DELETE FROM album_art WHERE source='notfound' AND artist='…' AND album='…';

-- Retry everything MB missed previously
DELETE FROM album_art WHERE source='notfound';
```

Those albums become bare again and get looked up on the next `ART_FETCHER.trigger()` — which means either a rebuild-index of any server, or a gateway restart (the 120s startup scan).

## Restarting the gateway

The gateway runs under launchd (LaunchAgent `com.roha.dlna-gateway`). To restart:

```bash
launchctl kickstart -k gui/$(id -u)/com.roha.dlna-gateway
# or, equivalently, with a venv/deps refresh first:
./setup.sh --restart
```

`kill <pid> && ./setup.sh --run` is wrong — launchd will respawn the old copy before the manual one starts, leading to port conflicts.

## Renderer playback diagnostics

Each `RendererQueue` (one per renderer UDN in `QUEUES`) emits a fixed-format per-track line for every start and end, regardless of the reason. Greppable prefixes:

```
RendererQueue ▶ START [idx/total] 'title' — artist / album (durS) → renderer
RendererQueue ■ END   [idx/total] 'title' played NNs/durS reason=<reason>
RendererQueue ✗ SEND FAILED [idx/total] 'title' — SetURI/Play returned False (url=…)
RendererQueue ⚠ ABORT N consecutive send failures — stopping queue
RendererQueue ⚠ WATCHDOG [idx/total] 'title' — renderer state 'UNREACHABLE' …; advancing
RendererQueue ⚠ ABORT renderer out of contact (state UNREACHABLE) for >300s — stopping queue
```

Reason tags on END lines:

| reason | Where it comes from |
|---|---|
| `finished` | Renderer state went PLAYING → STOPPED naturally (end of track) |
| `user_next` | `QUEUES.get(udn).next_track()` — UI pressed Next |
| `user_prev` | `QUEUES.get(udn).prev_track()` — UI pressed Prev |
| `user_stop` | `QUEUES.get(udn).stop()` — UI pressed Stop |
| `queue_replaced` | `QUEUES.get(udn).start()` — a new queue was posted while the old one was still playing (includes the "force take over" path) |
| `send_failed` | `avtransport_send()` returned False (SOAP fault on SetURI or Play) |
| `watchdog` | Monitor stall guard — renderer stopped reporting a usable state mid-track; advanced on a duration-based timeout (see below) |
| `renderer_lost` | Renderer read `UNKNOWN`/`UNREACHABLE` continuously for `UNKNOWN_ABORT_SEC` with no duration for the watchdog; queue aborted |

### `UNREACHABLE` vs `UNKNOWN` — telling a lost renderer from a quiet one

`avtransport_probe_state(av_url) → (state, detail)` in `dlna_avtransport.py` distinguishes two cases the old `avtransport_get_state()` collapsed into a bare `UNKNOWN`:

- **`UNREACHABLE`** — the `GetTransportInfo` SOAP call itself failed (renderer powered off, network drop, HTTP fault). `detail` carries the transport error reason (`[Errno 61] Connection refused`, `timed out`, `HTTP 500`). `_av_soap()` now returns `(text, err)` instead of `Optional[str]` so the cause is preserved.
- **`UNKNOWN`** — the renderer answered (HTTP 200) but reported no usable transport state, or the body didn't parse. The renderer is reachable; `detail` is empty.

`avtransport_get_state()` is kept as a thin wrapper returning just the state string (it now yields `UNREACHABLE` on transport failure). `avtransport_probe_state()` logs a WARN naming the failure reason on the first failure per renderer, then rate-limits to once per `_STATE_FAIL_WARN_SEC` (30 s) — the 2 s monitor poll would otherwise flood `gateway.log` while a renderer stays down — and logs a single INFO when the renderer answers again. Before this change (2026-05-20) `_av_soap` logged failures at `debug` only, so on a normal INFO-level gateway the cause of a `→ UNKNOWN` transition was never recorded.

### Why the monitor watchdog matters

The monitor advances a track on the normal `PLAYING → STOPPED` transition. But when a renderer goes unreachable mid-track (powered off, network drop, Naim HTTP server wedged), `GetTransportInfo` SOAP starts failing and the monitor sees `UNREACHABLE` (formerly an indistinguishable `UNKNOWN`). The `PLAYING → STOPPED` transition is then never observed and the queue stalls on one track forever — the 2026-05-20 incident, where `'Starman'` (242 s) sat "playing" for 36 minutes after the renderer went `STOPPED → UNKNOWN` six seconds into the track.

Two stall guards in `_monitor()`, both decided by the pure helper `_monitor_decision()`:

1. **Watchdog** (`WATCHDOG_GRACE_SEC = 90`): once wall-clock playback runs past the track's own declared duration + grace while the renderer is **not** actively `PLAYING`/`TRANSITIONING`/`PAUSED_PLAYBACK`, advance regardless of observed state. `PAUSED_PLAYBACK` is excluded so a deliberately paused queue is never skipped; a genuinely-playing long track is excluded because it still reports `PLAYING`. If the next `_send_current()` also fails because the renderer is still down, `_MAX_CONSECUTIVE_FAILS` aborts the queue cleanly.
2. **Unknown-abort** (`UNKNOWN_ABORT_SEC = 300`): if the renderer reads `UNKNOWN`/`UNREACHABLE` continuously for 5 minutes and the current track has no duration for the watchdog to act on, the queue aborts with `⚠ ABORT renderer out of contact`.

Regression-guarded by `tests/test_player.py::TestMonitorDecision` (advance logic) and `tests/test_avtransport_volume.py::TestProbeState` (UNREACHABLE/UNKNOWN distinction, WARN rate-limiting, recovery).

### Why SEND FAILED matters

Previously `_send_current()` ignored the return value of `avtransport_send()`. When SetURI failed, the renderer stayed STOPPED, the monitor saw STOPPED, called `_advance()`, sent the next track, which also failed — silently chewing through every track in the queue until the user hit kickstart. The symptom was "all 35 songs skipped, nothing in the log, only a restart fixes it."

The fix has two parts:
1. `_send_current()` now checks the SOAP return and emits `✗ SEND FAILED` with the track URL for every failure.
2. `RendererQueue._MAX_CONSECUTIVE_FAILS = 5` caps the damage: after 5 consecutive SetURI/Play failures the queue aborts itself with `⚠ ABORT`. A transient blip (1–4 failures) auto-advances past the bad track and resets the counter on the next success.

### Duration parsing (`_dur_to_sec`)

Track durations in `playlist_tracks` are stored as TEXT in UPnP `H:MM:SS(.fff)` format, NOT as seconds. `_dur_to_sec()` in `dlna_player.py` tolerates every format the DB stores (int, float, empty/None, `H:MM:SS.fff`, `MM:SS`, malformed strings → 0). Prior to 2026-04-23 this was `int(dur)` and blew up silently inside the daemon thread, killing playback before SetURI was ever sent. Regression-guarded by `tests/test_player.py::TestDurToSec` and `TestRendererQueueDurationSafety`.

## Radio play-count biasing

`GET /api/radio?udn=X&limit=N` (handler: `api_browse.radio` → `LibraryDB.radio_tracks`) picks N tracks biased toward **lowest play count** with random tiebreak, then atomically bumps the count on all selected URLs. The same 100 never keep surfacing: over time the whole library cycles through.

```
SELECT ... FROM tracks t
  LEFT JOIN play_counts p ON p.url = t.url
 WHERE t.udn = ?
 ORDER BY COALESCE(p.count, 0) ASC, RANDOM()
 LIMIT ?
```

The `play_counts` table is intentionally decoupled from `tracks`:

- **Not touched by `clear(udn)`** — rebuild-index doesn't reset play history, same invariant as `album_art`.
- **Keyed by URL** — if the upstream media server ever re-hashes URLs, orphaned rows are harmless and those tracks restart at count=0. Soft preference, not a correctness invariant.
- **Radio-only tracking** — listening via browse / playlists / favourites does NOT increment; only `/api/radio` does. Keeps the feature self-contained and matches the "radio freshness" use case.

To force a full reset (hear early picks again):

```sql
DELETE FROM play_counts;
```

Tests: `tests/test_library.py::TestRadioPlayCountBias` covers the invariants — disjoint-from-prior-call, full-library cycling, persistence across `clear()`.

## On-demand lyrics (lrclib)

📜 button in the now-playing panel fetches lyrics via lrclib.net on
first tap and caches the result in the `lyrics` table forever (sticky
positive AND negative). Re-taps are pure DB reads — lrclib is contacted
**at most once per track URL**.

### `lyrics` table — survives `clear(udn)`

```sql
CREATE TABLE IF NOT EXISTS lyrics (
    url        TEXT PRIMARY KEY,
    plain      TEXT,                  -- NULL only when source='notfound'
    synced     TEXT,                  -- LRC with [mm:ss.xx] timestamps if available
    source     TEXT NOT NULL,         -- 'lrclib' | 'notfound' | 'manual'
    fetched_at INTEGER NOT NULL
);
```

Same persistence pattern as `album_art` / `play_counts`.
Rebuild-index does NOT wipe lyrics.

### Endpoint

```
GET /api/lyrics?url=<track-url>
→ { plain, synced, source, cached }
  source ∈ {'lrclib', 'notfound', 'manual'}
  cached = true → row was already in DB; no network hit
```

Errors: `400 missing url`, `404 track not in library`,
`502 lyrics provider unreachable` (transient; **not cached**, so the
next tap retries).

### Sticky negative cache

A 404 from lrclib (or a 200 with both `plainLyrics` and `syncedLyrics`
null) gets cached as `source='notfound'` so we don't re-hammer lrclib
each time the user taps. To force a retry on a single track:

```sql
DELETE FROM lyrics WHERE source='notfound' AND url='…';
```

### Display

Frontend modal (`#lyrics-modal` in `index.html`, handler in `app.js`)
shows the plain text. If only synced (LRC) is available, the
`[mm:ss.xx]` timestamps are stripped client-side for readable display.
Karaoke-style sync is deferred.

Tests: `tests/test_lyrics.py` — 15 unit tests covering DB round-trip,
`clear(udn)` survival, cache-hit short-circuit, sticky-notfound, and
network-error pass-through.

## Album favourites (whole-album bookmarks)

Distinct from the track-level "⭐ Favourites" playlist (id
`__favourites__`). Album favourites bookmark whole albums by
`(artist, album)`, independent of `tracks` so they survive
`clear(udn)` / re-indexing — same contract as `album_art`,
`play_counts`, `lyrics`.

### `album_favourites` table

```sql
CREATE TABLE IF NOT EXISTS album_favourites (
  artist     TEXT NOT NULL,
  album      TEXT NOT NULL,
  album_key  TEXT NOT NULL DEFAULT '',   -- LocalFs folder identity
  added_at   INTEGER NOT NULL,
  PRIMARY KEY (artist, album, album_key)
);
```

`album_key` (2026-05-31, A2): a LocalFs favourite is identified by its
**folder** (`album_key`), so a Various-Artists compilation favourites as
one album even though every track has a different performer — and two
distinct comps that share `artist='Various Artists'` + a repeatable
display name don't collide (hence `album_key` in the PK). Non-LocalFs
favourites keep `album_key=''` and the legacy `(artist, album)` identity.
`album_fav_add/remove/is` take an optional `album_key`; `album_fav_list`
returns it and matches `track_count`/art by folder when set. Migrated in
`_migrate_album_fav_key` (rebuilds the table, carrying old rows forward
with `album_key=''`). **UPnP fav exposure (A3a) and Subsonic fav/album
ids (A3b) are both album_key-aware — favouriting/browsing a compilation
by folder works across the PWA, the Naim, and CarPlay.**

### LibraryDB methods

- `album_fav_add(artist, album) → bool` — INSERT OR IGNORE; returns
  True if a new row was created. Empty album rejected.
- `album_fav_remove(artist, album) → bool`
- `album_fav_is(artist, album) → bool`
- `album_fav_list() → [{artist, album, added_at, art, track_count, udn}]`
  — joins `album_art` for cover and `tracks` for count + udn; orphan
  favourites (no matching tracks anywhere) still appear with
  `track_count=0, udn=""` so the user can prune them.

### HTTP endpoints

```
GET /api/album_favourites
    → [{artist, album, art, track_count, udn, added_at}, …]
GET /api/album_favourites/check?artist=X&album=Y
    → {is_favourite: bool}
GET /api/album_favourites/add?artist=X&album=Y
    → {ok: true, created: bool}
GET /api/album_favourites/remove?artist=X&album=Y
    → {ok: bool}   (false if it wasn't there)
```

### UI

- **Album header** (`#browse-fav-album`): a star button next to "▶ Play
  all" in `browse-section-hdr`. **Visible only when the album has more
  than one track** — single-track "albums" are typically metadata-less
  orphans and shouldn't be favouritable. `data-fav="0"` shows ☆,
  `data-fav="1"` shows ★. Click toggles via /add or /remove with
  optimistic UI flip.
- **Add:** the album-header ⭐ star (folder-keyed via `album_key`).
- **Browse:** a **⭐ entry at the front of the browse letter bar**
  (`LETTERS[0]` in app.js, before `#`). Selecting it loads
  `/api/album_favourites` and renders the favourites as album rows
  (`renderFavouriteAlbums`); clicking one opens it via `album_key`
  (`showAlbumTracks(..., album_key)`). This replaced the old right-column
  "⭐ Favourite Albums" list view, which was **removed 2026-06-01** — its
  `(artist, album)` entries didn't survive the folder-album migration
  (stale rows, slow joins, spinner-on-open), and the `album_favourites`
  table was cleared for a clean slate. Favourites are also exposed via
  UPnP (Naim) and Subsonic (CarPlay).

### UPnP exposure (Naim)

`api_upnp._gw_browse` exposes the gateway as a MediaServer the Naim browses
directly (no PWA). **Root container "0" lists five children:** `Artists`,
`Albums`, `Genres` (the **full library** — added 2026-06-12, since AssetUPnP's
decommission left nothing for the Naim to browse the whole library over UPnP),
then `⭐ Favourite Albums` and `Playlists` — plus a sixth, `📹 Videos`, only
when GWMovies is enabled + populated.

**Videos sub-tree** (for the LG TV; guarded by
`tests/test_upnp_videos_browse.py`): `videos` → `📅 By date` (year → month),
`📍 By location` (COUNTRY blocks → locations; **titles show FULL country
names** via `dlna_countries.py` while ids/sorting/filenames keep the ISO
code — 2026-07-08), `👤 By person` (Immich person tags via `video_people`;
container only present when persons exist — 2026-07-07), and `🎞 All videos`.
Country drill-downs carry a **"(no city)" bucket** for country-only inferred
locations; the top-level "(no location)" bucket holds videos with NEITHER.
Garbled ids → empty container, never 500.

**DLNA Media Server surface — what makes strict clients browse it (hard-won
2026-06-13).** Browsing only works if the gateway is a *complete, spec-correct*
DLNA DMS. The Naim (control point UA `dLeyna/0.6.0 GUPnP/1.0.2`) and the LG WebOS
TV both refused to browse until every one of these was right — diagnose via the
`GW /gw/…` lines in `gateway.log` (at `debug`; run with `GATEWAY_DEBUG=1`), which
show exactly what a client requests, in order:
- **`device.xml`** (`_gw_device_xml`): `MediaServer:1` device + `friendlyName`
  + `UDN` + **`<dlna:X_DLNADOC>DMS-1.50`** + an **`<iconList>`** (192/512 PNG,
  served by the ASGI app — TVs won't list an icon-less server) + a `serviceList`
  with **BOTH** `ContentDirectory:1` **and** `ConnectionManager:1`
  (ConnectionManager is MANDATORY — its absence was why both clients quit).
- **ContentDirectory SCPD** (`/gw/cd/desc.xml`, `_gw_cd_desc_xml`): MUST use
  `<name>` tags (a stray `<n>` made clients fail to parse the service). Advertises
  `Browse` + the pre-browse handshake actions. `cd_control_soap` dispatches:
  `Browse` + `GetSearchCapabilities` / `GetSortCapabilities` /
  `GetSortExtensionCapabilities` / `GetSystemUpdateID` / `GetFeatureList` /
  `Search` — all returning empty-but-valid 200 (a client runs the handshake
  BEFORE it will browse; 400s there made it give up).
- **ConnectionManager** (`/gw/cm/desc.xml` + `/gw/cm/control`, `cm_control_soap`):
  `GetProtocolInfo` (Source = the audio `protocolInfo` we serve, `_GW_SOURCE_
  PROTOCOLS`), `GetCurrentConnectionIDs` (`0`), `GetCurrentConnectionInfo`.
- **GENA eventing** (`/gw/cd/events` + `/gw/cm/events`): a `SUBSCRIBE` MUST return
  a valid `SID` + `TIMEOUT` **and** then push the initial NOTIFY to the client's
  CALLBACK (`gw_event_subscribe` / `gw_event_initial_notify`). The NOTIFY is fired
  on a **daemon `threading.Thread`, NOT `asyncio.create_task`** — an un-referenced
  task is GC'd before it runs, the NOTIFY never sends, and GUPnP/dLeyna then
  re-SUBSCRIBEs forever and never browses (the final bug; commit `90afef7`).
- **Discovery** (`dlna_gateway.start_background_services`): `gw_ssdp_announcer`
  (NOTIFY alive every 60 s) **plus** `gw_ssdp_responder` — answers SSDP
  `M-SEARCH` so a control point's *active* search finds the server, not only a
  passively-caught NOTIFY.

All four `/gw/*` route groups are native in `dlna_asgi.py` (Cleanup C) on the
plain `:8765` bind. Regression-guarded by `tests/test_upnp_album_favourites.py`
(`TestContentDirectorySCPD`, `TestContentDirectoryActions`, `TestConnectionManager`,
`TestGenaEvents`, `TestMSearchResponder`).

**Full-library tree** — backed by `LibraryDB` on `DB.primary_udn()` (the udn
owning the most tracks = the LocalFs backend):
- `artists` → `gartist:<b64(artist)>` → that artist's albums.
- `albums` → a **#-0-A..Z letter index** (`albumltr:<L>`, only non-empty buckets,
  matching `LibraryDB.browse_letter` / the PWA letter bar) → that letter's albums
  → tracks. (Not one flat ~2,000-entry list — unbrowsable on a Naim remote.)
- `galbum:<b64(artist\x00album\x00album_key)>` → the album's tracks as
  `musicTrack` items whose `<res>` is the `/localfs/stream` URL the Naim plays.
- `genres` → `ggenre:<b64(genre)>` → that genre's albums.
- **Untagged junk is hidden** (`_is_junk_name`, DISPLAY-only): blank names, a
  `NN.`/`NN)`/`NN -` track-number prefix, or a bare 1–2-digit number ("07") are
  dropped from Artists/Albums/Genres + the album sub-lists, and a junk artist is
  suppressed from the `album — artist` title. The raw DB is untouched (the PWA
  still shows it; beets enrichment fixes tags over time).
- Every list **paginates** via `StartingIndex`/`RequestedCount` (the slice is
  applied to the junk-filtered `LibraryDB` result — fine at this library size; a
  future optimisation would push `LIMIT/OFFSET` into the queries). Album ids
  carry the LocalFs `album_key` so folder-albums (incl. Various-Artists comps)
  resolve. Codecs: `_b64e`/`_b64d` (single value) + `_encode/_decode_lib_album_id`
  (`galbum:*`, distinct from the favourites `favalbum:*` codec); garbled ids →
  empty container, never 500.

**Favourites tree** — `⭐ Favourite Albums`: one container per favourited album
titled `"<album> — <artist>"`. One level deeper: the album's tracks, resolved
against `album_fav_list()[i]['udn']` via `DB.album_tracks`.

ObjectID encoding for individual albums:
`favalbum:{base64-urlsafe(artist + "\x00" + album + "\x00" + album_key)}`
— round-trips arbitrary unicode (non-ASCII names, ampersands, slashes,
NUL bytes fine) through SOAP/XML. `album_key` (A3a) is the LocalFs folder
identity so a Various-Artists compilation resolves as one album; it's
empty for (artist, album)-keyed favourites, and legacy 2-field ids decode
with `album_key=''`. The favalbum container resolves tracks via
`DB.album_tracks(udn, artist, album, album_key=…)`. See `_encode_album_id`
/ `_decode_album_id` in `api_upnp.py` (both now 3-field). Garbled /
non-base64 IDs decode to `("", "", "")` and return an empty container
rather than 500.

### Tests

| File | What it covers |
|---|---|
| `tests/test_album_favourites.py` | DB round-trip + idempotent add, dedupe, ordering newest-first, orphan-album survival, `clear(udn)` invariant, handler 400/200 paths. 14 tests. |
| `tests/test_upnp_album_favourites.py` | Album-id codec round-trip (incl. unicode/specials), root browse lists fav-albums first, "favalbums" lists each favourite, "favalbum:{...}" lists tracks, unknown album → empty container. 9 tests. |
| `tests/frontend/test_album_favourites.py` | Album-header star only (the right-column browse view was removed 2026-06-01): star gated by `track_count>1`, initial state from `/check`, click → /add or /remove with optimistic flip, album_key-aware check/add. 8 Playwright tests. |

## External services (outbound HTTP)

The gateway is LAN-only except for album-art, lyrics, and radio-catalogue lookups. All over TLS:

| Host | Purpose | Method + path |
|---|---|---|
| `musicbrainz.org` | Resolve `(artist, album)` → release-group MBID | `GET /ws/2/release-group/?query=…&fmt=json&limit=5` |
| `coverartarchive.org` | Confirm a front cover exists for that MBID | `HEAD /release-group/{mbid}/front-500` — 200/301/302/307 counts as "have it", 404 counts as "no cover" |
| `lrclib.net` | On-demand lyrics for the currently-playing track | `GET /api/get?track_name=&artist_name=&album_name=&duration=` — 200 with body or 404 |
| `*.api.radio-browser.info` | Internet-radio station catalogue search | `GET /json/stations/search?name=&tagList=&countrycode=&hidebroken=true` — see the "Internet radio" section |

(`api.acoustid.org` was in this table until 2026-07-12 — the in-process
AcoustID worker was removed in 2.0; beets calls AcoustID from its own
process now. The key-types bullet below is kept for beets debugging.)

Required contract:

- **User-Agent** — `_MB_USER_AGENT` in `dlna_art_fetcher.py` is built as `DLNAGateway/1.0 ( <GATEWAY_CONTACT_EMAIL> )` from the `.env` value (single source for the contact email; never hardcoded). MusicBrainz's ToS demands an identifying UA with contact info; anonymous/placeholder calls get throttled (HTTP 503) or 403-blocked.
- **Rate limit** — `_MB_RATE_LIMIT_SEC = 1.1` between calls, enforced in `AlbumArtFetcher.run_once()`. MB allows 1 req/sec sustained; 1.1s gives a small safety margin.
- **Timeout** — `_MB_TIMEOUT = 10.0` per connection. Exceptions inside `_mb_lookup_cover()` are caught and returned as `None` (album gets cached as `notfound`).
- **No retries** — a transient failure ends up as `notfound` and stays sticky; see the "Sticky notfound cache" subsection above for how to force a retry.
- **AcoustID has TWO key types — use the right one.** `https://acoustid.org/api-key` gives you a **user** key, intended for *submitting* fingerprints to the AcoustID database. `https://acoustid.org/applications` (register a new application) gives you an **application** key, which is what `/v2/lookup` requires. Calling `/v2/lookup` with a user key returns `HTTP 400 {"error":{"code":4,"message":"invalid API key"}}` — distinctive failure mode worth remembering. `ACOUSTID_API_KEY` in `.env` must be the application key.

## Volume control (UPnP renderer)

> **Loudness normalization removed (2026-05-31).** The peak-mode per-track
> gain gave negligible perceptual benefit (modern masters peak near 0
> dBFS), was already disabled in the playback path, and would have broken
> bit-perfect on the browser path. `LoudnessScanner`, the `track_loudness`
> table, `gain_db_for_url`, `/api/loudness/status`, and the `ffmpeg`
> dependency are all gone; the slider is now purely a volume control.
> (Perceptual/LUFS normalization, if ever genuinely wanted, would be a
> fresh feature.)

The gateway is NOT in the audio path for UPnP renderers — it sets the
renderer's **hardware volume** via `RenderingControl::SetVolume`, never
PCM. So everything here is **bit-perfect** (the Naim attenuates in its own
DAC/analog domain).

### Startup volume + user trim

- **`STARTUP_VOLUME = 22`** (`dlna_player.py`): set ONCE per queue on the
  first track via `RendererQueue._apply_startup_volume()`. We deliberately
  do NOT read the renderer's current volume first — a STOPPED Naim reports
  0 via GetVolume, and adopting that baseline silenced playback (the
  2026-05-30 bug). After this one-shot set, volume is never re-asserted
  per-track, so a change on the Naim's own remote sticks for the session.
- **User trim** — `RendererQueue.set_user_trim_db(db)`: a relative ±dB
  offset around the startup baseline, clamped ±`MAX_USER_TRIM_DB` (5 dB),
  applied IMMEDIATELY via SetVolume so the change is audible mid-track.
  `GAIN_TO_VOLUME_RATIO = 2` converts slider dB → Naim volume units
  (≈0.5 dB/unit; the renderer curve is logarithmic — approximate, tune by
  ear). A fresh queue resets trim to 0 → plays at exactly `STARTUP_VOLUME`.

### `RenderingControl` SOAP helpers (`dlna_avtransport.py`)

- `set_volume(rc_url, level)` — clamped 0–100; `RenderingControl:1#SetVolume`
  with `Channel=Master`.
- `get_volume(rc_url)` — parses `<CurrentVolume>`; kept as a helper but
  **not used on the playback path** (see the STOPPED-reports-0 note).
- The renderer's `rc_url` is sourced from the device description XML at
  discovery time (`dlna_discovery.py`).

### `/api/control` UPnP volume

```
POST /api/control
{"action": "volume", "value": <-5..+5 dB>, "device": "upnp:<udn>"}
```
→ `QUEUES.get(udn).set_user_trim_db(value)` — pushes to the renderer
immediately and is sticky for the rest of the queue.

### Caveats

- **Gateway can't apply DSP** — only SetVolume (hardware). A nudge on the
  Naim's own remote is undone on the next trim change; not solvable
  without a poll-then-adjust loop that would lag.
- **Renderer volume curve is non-linear** — `GAIN_TO_VOLUME_RATIO = 2` is
  an approximation; tune by ear.

### Tests

| File | What it covers |
|---|---|
| `tests/test_avtransport_volume.py` | `set_volume` body shape (RenderingControl namespace, `Channel=Master`, `<DesiredVolume>`), 0/100 clamp, SOAP-fault + connection-error paths; `get_volume` parses `<CurrentVolume>`, None on fault/garbled/error |
| `tests/test_player_volume.py` | first track sets `STARTUP_VOLUME` once, GetVolume never called, no per-track re-assert, trim composes around the baseline, trim resets on a new queue |
| `tests/frontend/test_vol_extras.py` | UPnP volume body asserts `action=trim_db` (relative offset, not absolute) + `device="upnp:<udn>"` |

## Metadata enrichment via AcoustID (REMOVED in 2.0)

> **⛔ REMOVED (2026-06-11).** The in-process AcoustID worker is gone — under
> Option A, **beets is the sole metadata authority** (see "beets vs the
> AcoustID worker"). Deleted: `dlna_acoustid.py`, the `ACOUSTID_FETCHER`
> singleton + wiring, the `/api/acoustid/*` endpoints, the worker-only
> LibraryDB methods, and the weekly retry agent. **Still present (data, not
> the worker):** the `metadata_overrides` table incl. historical
> `source='acoustid'` rows, and the tools that operate on them
> (`tools/post_beets_reindex.py` clears acoustid rows after a beets run;
> `audit_override_mismatches.py`, `correct_year_drift.py`,
> `improve_song_years.py`, `find_duplicate_audio.py` read them).
>
> Still-relevant invariants that OUTLIVED the worker:
> - `metadata_overrides.source ∈ {manual, acoustid, notfound, video_skip}`;
>   a row of ANY source means "processed"; `manual` always wins; sticky
>   negatives are released by deleting the row.
> - The 16/24-bit duplicate handling (wide UNIQUE incl. bit_depth/
>   sample_rate + `_dedup_clause` browse filter + `idx_tracks_udn_url`) is
>   ACTIVE schema behaviour documented in the "Database Schema" section
>   comments and `tests/test_dedup.py`.
> - Year model: `tracks.year` = file-tag year; `metadata_overrides.year` =
>   original release year; display prefers the override
>   (`_renderNpYear`, "(remastered)" at 3+ year gap); guarded by
>   `tests/test_year.py`.
>
> The full historical write-up (worker design, transient-vs-permanent
> failure split, confidence threshold, weekly retry agent, backfill
> procedures) is in git history: `git show 7478064^:CLAUDE.md`.

## Bit-perfect notes

**Verdict:** the gateway is byte-perfect on every path it controls.
No resampler, EQ, mixer, or DSP exists anywhere in the playback
code. Confirmed by audit on 2026-05-11.

**Naim / UPnP path.** The gateway is not in the audio path. AssetUPnP
serves bytes directly to the renderer; the gateway only sends
`AVTransport::SetURI` + `Play` SOAP. The startup volume and the user
trim slider are applied via `RenderingControl::SetVolume` SOAP, which
adjusts the renderer's **hardware volume** — never PCM modification.
See `RendererQueue` in `dlna_player.py:57-591` and
`dlna_avtransport.py:29-94`/`278-287`.

**Browser stream proxy (`/stream`).** Byte-perfect Range pass-through.
`dlna_stream_proxy.proxy_stream` (`dlna_stream_proxy.py:45-138`)
relays bytes verbatim; the only mutation is a `Content-Type` header
normalisation (`audio/x-flac` → `audio/flac`) for Safari quirks.

**Browser `<audio>` caveat (not a gateway behaviour).** In
browser-output mode the trim slider sets
`browserAudio.volume = 10^(db/20)` (`static/app.js:1396-1403`). When
non-zero, the **browser** scales every PCM sample by that factor —
HTML5 `<audio>` behaviour, not the gateway. Default = 0 dB
(volume = 1.0 = no scaling), so unmodified playback is the default.
**Keep the slider at 0 dB for bit-perfect browser playback.** UPnP
output is unaffected — the same slider then routes through
`/api/control` and the gateway calls `SetVolume` on the renderer
hardware.

## Bit-perfect on macOS

**Best chain.** Play to the Naim via UPnP. Gateway is not in the
audio path; macOS / CoreAudio is not in the audio path; AssetUPnP
serves directly to the renderer. This is the recommended setup when
bit-perfect matters.

**For browser playback on macOS,** the chain is:
AssetUPnP → gateway `/stream` proxy → browser HTML5 `<audio>` →
CoreAudio → output device. macOS still applies sample-rate conversion
if the output device's rate doesn't match the source.

### Audio MIDI Setup

`/System/Applications/Utilities/Audio MIDI Setup.app`:
- Select the output device.
- Set **Format** sample rate to match the source material:
  - CD rips: 44100 Hz / 16-bit
  - Hi-res FLAC: 96000 Hz / 24-bit
  - DXD: 192000 Hz / 24-bit
- Mismatched rates cause CoreAudio SRC. With a mixed-rate library
  the only "right" choice is to either pick the rate of your majority
  format, or switch to UPnP/Naim for hi-res sources.

### System Settings → Sound → Output

- Turn off any **Spatial Audio** / **Audio Enhancements** on the
  selected output device.
- Bluetooth / AirPods are **always lossy** on macOS (codec
  re-compression); use wired output for bit-perfect listening.

### Limits of browser bit-perfect on macOS

Browsers always go through CoreAudio's shared mixer; you can minimise
but not fully eliminate it. For true hog-mode / exclusive-mode
bit-perfect listening from a Mac, use Audirvana or Roon — outside the
gateway's scope.

### iOS / iPadOS PWA

Ensure these are **off**:
- Settings → Accessibility → Audio/Visual → **Headphone
  Accommodations**.
- Settings → Music → EQ (set to **Off**, not "Flat").
- Settings → Sounds & Haptics → Headphone Audio → Volume Limit.
- Spatial Audio toggles on connected headphones.

## Library housekeeping tools

### `tools/prune_empty_music_dirs.py`

Walks the music root and **moves to Trash** any directory whose entire
subtree contains zero music files. The user's library lives at
`/Volumes/SAMDATA/Music` (external drive — see project memory).

Music extensions (default): `.mp3 .flac .ogg .opus .m4a .aac .wav .wma
.ape .aiff .aif .dff .dsf .alac`. **`.mp4` is deliberately excluded**
so a music-video MP4 in a folder doesn't mark that folder as "kept".

#### Protection rule (Rule B — subtree-protect)

As soon as the walker descends into a directory whose **subtree** (any
depth) contains a music file, that directory is "an album root" and
*all* of its descendants — including non-music siblings of music — are
preserved. This protects:
- `Album/scans/`, `Album/coverart/`, `Album/booklet/` next to music.
- Multi-disc albums: `Album/CD1/track.flac` AND `Album/scans/cover.jpg`
  both survive even though the cover-art subdir is a sibling of (not
  under) the music subdir.

The root music folder itself is **never** treated as an album root —
loose music files at root don't grant blanket protection to root's
other subdirs.

**Documented trade-off:** the rule cannot tell apart a multi-disc
album's `scans/` (you want kept) from a junk subdir next to a music
subdir (you might want deleted). If you have a folder that mixes
music subfolders and pure-junk subfolders at the same level, the
junk is **preserved**. The rule errs on the side of preservation;
clean such mixed dirs manually.

#### Defaults & safety

- **Trash, not delete.** Default behaviour moves directories to the
  macOS Trash via `osascript` — recoverable from Finder for ~30 days.
- **Confirmation prompt** — shows the first 20 dirs that would be
  deleted + total count, then `Y/n`. Pass `-y` to skip.
- **Limit acts as a safety belt** — if `--limit N` halts the walk
  early, the script does NOT execute deletions for that run (you're
  not seeing the full picture, so we never act on a partial list).

#### Usage

```bash
# Required first step — preview without acting:
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --dry-run

# Verbose preview (logs every kept directory too):
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --dry-run -v

# Stop after 200 dirs (safety/sanity check; no deletions when limit hit):
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --dry-run -v --limit 200

# Real run — Trash, with confirmation prompt:
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music

# Real run — non-interactive (e.g. cron / a script):
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music -y

# Override extension list:
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --exts mp3,flac,ogg

# Permanent rm -rf (NOT recoverable — use only if you're sure):
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --hard-delete -y
```

#### Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Print decisions without acting |
| `-v` / `--verbose` | Log every kept directory too (default: only deletions print) |
| `--limit N` | Stop after evaluating N directories (no deletions executed when limit is hit) |
| `--exts a,b,c` | Override the music extension list (commas, with or without leading dot) |
| `--hard-delete` | Permanent `rm -rf` instead of Trash. NOT recoverable. |
| `-y` / `--yes` | Skip the confirmation prompt |

#### Why this is safe for the gateway

The gateway frontend never references files in the music root
directly — every `tracks.art` and `album_art.art_url` value in
`library.db` is `http://` or `https://` (AssetUPnP-served or Cover
Art Archive). Album art is fetched through `/art` proxying an HTTP
URL, never a filesystem path. Music-less directories are also
not indexed by AssetUPnP (it indexes albums = folders with audio
files), so they have no entry in the gateway DB at all. Pruning
them is invisible to the gateway.

#### Tests

`tools/test_prune_empty_music_dirs.py` — 14 unit tests over
throw-away tempdirs. Cover: album-with-music kept, multi-disc
support dirs (`scans/`, `booklet/`) kept, root-level music doesn't
protect siblings, branch with deep-only music kept, split-branch
trade-off (junk-next-to-music preserved), symlinks not followed,
case-insensitive extension match, `mp4` treated as non-music,
`--limit` halts cleanly. Run standalone:

```bash
python3 -m unittest tools.test_prune_empty_music_dirs -v
```

### `tools/find_corrupt_audio.py`

Walks the music root and flags audio files whose first 16 bytes are
wrong for their extension — catches the failure mode the AcoustID
worker surfaced on 2026-05-25 (13 MB FLAC files starting with `\x00`
instead of `fLaC` magic). Per-format magic-byte validators cover the
full default extension set: `.flac .mp3 .ogg .opus .m4a .alac .aac
.wav .aiff .aif .wma .ape .dsf .dff`.

#### Three reasons a file gets flagged

| reason | What it means |
|---|---|
| `zero-size` | File is 0 bytes on disk |
| `zero-header` | First 16 bytes are all `\x00` — the actual failure mode found in the user's library |
| `magic-mismatch:.<ext>` | Header isn't valid for the file's extension (e.g. a `.flac` not starting with `fLaC` or `ID3`) |

#### Defaults & safety

- **Scan-only by default.** No file is touched unless `--trash` or
  `--hard-delete` is explicitly passed. Default run only PRINTS the
  findings and WRITES them to `./corrupt-audio.txt` (one
  `<reason>\t<path>` per line) — usable as input to any downstream
  cleanup script the user wants to write.
- **`--trash` and `--hard-delete` are mutually exclusive** and both
  prompt for confirmation unless `-y` is passed.
- **`--limit N` is a safety belt**: when the limit is hit, the run
  is reported as PARTIAL and any delete flags are ignored — you only
  act on a complete picture.
- **Symlinks are not followed** (`os.walk(followlinks=False)`) —
  avoids loops and accidental scans of foreign volumes.
- **Unreadable files** (permission errors) go into a separate
  `read_errors` bucket, never `files_corrupt` — we don't delete what
  we couldn't even open.

#### Usage

```bash
# Default — scan only, write list to ./corrupt-audio.txt:
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music

# Verbose preview (logs every OK file too):
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music -v

# Stop after scanning 1000 files (no deletions even if --trash passed):
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --limit 1000

# Move flagged files to Trash, with confirmation prompt:
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --trash

# Non-interactive permanent delete (NOT recoverable):
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --hard-delete -y

# Restrict to one or two formats:
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --exts flac,mp3

# Different output path for the list:
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --out /tmp/broken.txt

# Suppress the list file (just print):
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --out /dev/null
```

#### Flags

| Flag | Effect |
|---|---|
| `-v` / `--verbose` | Also log every OK file (default: only corrupt) |
| `--limit N` | Stop after scanning N files (0 = no limit); halts the delete step |
| `--exts a,b,c` | Override the audio extension list (commas, with or without leading dot) |
| `--out path` | Write the corrupt-paths list here (default: `./corrupt-audio.txt`; pass `/dev/null` to suppress) |
| `--trash` | Move flagged files to macOS Trash via `osascript` |
| `--hard-delete` | Permanent `unlink()` instead of Trash. NOT recoverable. Mutually exclusive with `--trash` |
| `-y` / `--yes` | Skip the confirmation prompt before deleting |

#### Tests

`tools/test_find_corrupt_audio.py` — 23 unit tests over throw-away
tempdirs. Cover: every format's valid-magic happy path, raw-MP3-sync
without ID3 prefix, AIFC variant of AIFF, zero-size flagged,
zero-header flagged across multiple extensions, per-format magic-
mismatch, non-audio extensions skipped by the walker, case-insensitive
extension match, `--limit` halts cleanly, symlinks not followed,
unreadable files recorded separately (not auto-deleted). Run standalone:

```bash
python3 -m unittest tools.test_find_corrupt_audio -v
```

### `tools/find_duplicate_audio.py`

Finds **duplicate audio FILES on disk** — two or more physical files
that AcoustID identified as the same recording (same post-correction
`(artist, album, title)` in `metadata_overrides`). Surfaced 2026-05-27
after the full re-fingerprint pass left ~20k unsynced "phantom"
tracks (same recording on disk multiple times — different folders /
formats / sources, all matched by fingerprint to the same metadata).

#### Winner-selection ranking

Within each duplicate group the tool picks ONE winner to keep; the
rest are loser candidates:

1. Higher `bit_depth` (24 > 16)
2. Higher `sample_rate` (96000 > 44100)
3. Larger `file_size` (proxy for less compression / higher bitrate)
4. Alphabetical URL (deterministic tiebreaker)

NULL bit_depth / sample_rate count as 0 (lowest) so any quality-tagged
row beats an untagged one.

#### "Lose nothing" guarantee

- **Single-file groups** (unique recordings) are NEVER touched.
- A 16-bit file is kept whenever it's the only copy of its recording.
- Default mode is REPORT ONLY — writes `duplicate-audio.txt` and exits.
- `--trash` moves loser files to macOS Trash via `osascript` (recoverable from Finder for ~30 days).
- `--hard-delete` is opt-in permanent `unlink()` (NOT recoverable).
- `--trash` / `--hard-delete` both require confirmation prompt unless `-y`.
- Ambiguous URL→path mappings (multiple disk files with the same Content-Length) and missing matches are flagged in the report and SKIPPED from any action.

#### URL→path mapping

AssetUPnP URLs don't expose file paths. The tool reconstructs the
mapping by:
1. HTTP HEAD each URL → `Content-Length`.
2. Walk SAMDATA → `{file_size_bytes: [Path, ...]}` index.
3. Match each URL's Content-Length to disk files. Exactly one match → confident. Multiple → ambiguous, skip with warning. None → missing.

File size is essentially unique for multi-MB audio files; collisions
on short MP3s are rare and reported rather than guessed.

#### Usage

```bash
# Default — scan, build the report, no action:
python3 tools/find_duplicate_audio.py /Volumes/SAMDATA/Music

# Verbose (per-URL ambiguity / not-found logs):
python3 tools/find_duplicate_audio.py /Volumes/SAMDATA/Music -v

# Move losers to Trash, with confirmation:
python3 tools/find_duplicate_audio.py /Volumes/SAMDATA/Music --trash

# Non-interactive permanent delete (NOT recoverable):
python3 tools/find_duplicate_audio.py /Volumes/SAMDATA/Music --hard-delete -y
```

#### Post-cleanup workflow

After trashing duplicate files:
1. **Rescan AssetUPnP** so it drops the now-missing URLs from its UPnP responses.
2. **Trigger a gateway rebuild-index** (`POST /api/index/rebuild` or PWA → settings → Rebuild). The indexer's `clear(udn)` wipes the old `tracks` rows and re-crawls AssetUPnP cleanly.
3. **`metadata_overrides` is unaffected** — keyed by URL; the rows for deleted files become orphans, harmless. (Prune orphans later with manual SQL if desired.)

#### Tests

`tools/test_find_duplicate_audio.py` — 17 tests over a throw-away DB
+ tempdir. Cover: duplicate-group identification (only acoustid source,
only ≥2 members, NULL/empty metadata excluded), ranking algorithm
(bit_depth wins > sample_rate within bit-depth > file size within
quality > alpha tiebreaker, NULL counted as 0), disk walk (size index
correctness, non-audio skipped, zero-size skipped, symlinks not
followed), URL→path resolution (unique-size confident, ambiguous-size
skipped, missing-size reported), report file shape (KEEP / TRASH
markers, metadata + path columns).

```bash
python3 -m unittest tools.test_find_duplicate_audio -v
```

### `tools/immich_import.py`

Copies **new videos from Immich's originals** (`library/` + `upload/` on
the Immich data volume; `encoded-video/` transcodes never touched) into
the gateway video root (`/Volumes/SAMDATA/GWMovies`). The gateway does
the rest automatically: the 5-minute periodic scan picks new files up,
reverse-geocodes their GPS and titles them `<location>_<YYYYMMDD>_<HHMM>`
(see `docs/VIDEO_SUPPORT.md`) — filenames are preserved because titles
come from metadata, not names.

Dedup is by **content hash** (BLAKE2b), remembered in
`<dest>/.immich-import.db`: files already in GWMovies (any name, any
origin) are registered on first run and never re-copied; processed
sources are skipped by (path, size, mtime) without re-reading. Re-running
after new phone uploads only copies what's new — safe as a habit. A name
collision with different content gets a ` (2)` suffix. DRY-RUN by
default (`--apply` copies; the hash cache is warmed even in dry-run so
the apply is fast). `--min-seconds 10` skips Live-Photo motion clips;
`--limit N` stops early.

**The dedup cache is deliberately STICKY** — `dest_files` keeps every
content hash EVER seen in dest, so a clip you deleted from GWMovies never
comes back on a re-run. That stickiness once silently withheld 1,783
PHOTOS-ALL videos whose content had been in GWMovies before (2026-07-07);
**`--reimport-deleted`** narrows the dedup set to files PRESENT now (and
re-evaluates stale 'duplicate' source verdicts) when you DO want deleted
content back. **`--subdirs ""`** walks the source root as a plain folder
tree (e.g. Immich's external library, PHOTOS-ALL) instead of the default
`library,upload` Immich storage layout.

```bash
python3 tools/immich_import.py               # preview
python3 tools/immich_import.py --apply       # copy new videos
python3 tools/immich_import.py --source /Volumes/SAMDATA-1TB/PHOTOS-ALL \
    --subdirs "" --reimport-deleted --apply  # external library, full
python3 -m unittest tools.test_immich_import -v   # 18 tests
```

### `tools/infer_video_locations.py`

Infers locations for **GPS-less videos from their temporal neighbors**
(2026-07-07): a clip shot the same day (or within ±`--window` days,
default 3) as real-GPS-located clips gets their location. Tiers, strongest
first: `same_day` unanimous → `±1d` → `±window` → **country-only** (cities
differ but country agrees — browsable via the "(no city)" bucket).
Evidence = real-GPS rows ONLY (no chaining off previous inferences).
Results go to `video_location_overrides` (source=`inferred_*`), which the
end-of-scan hook re-applies after every rescan; `manual` rows always win.
`--retry-geocode` also re-asks Nominatim for GPS-but-cached-empty rows
(evicts the sticky '' cache entry). DRY-RUN by default.

**Device check caveat:** `--no-device-check` was needed on the real
library — the check (quicktime make/model of video + neighbors must
match) blocked 92/113 targets because GPS-less videos are almost all
re-encoded (WhatsApp/exports) with NO device tags at all. First real run
(user-approved, check off): 64 inferred + 8 geocode retries → located
videos 1,050 → 1,111. Undo one:
`DELETE FROM video_location_overrides WHERE video_id='…'`.

```bash
python3 tools/infer_video_locations.py                     # preview
python3 tools/infer_video_locations.py --no-device-check --retry-geocode --apply
python3 -m unittest tools.test_infer_video_locations -v    # 16 tests
```

### `tools/immich_people_sync.py`

Syncs **Immich's person recognition** onto gateway videos (2026-07-07):
Immich keeps face/person data in ITS Postgres (never in the files), so
this pulls named persons over the REST API (`GET /api/people`, then
`POST /api/search/metadata {personIds, type: VIDEO}`; auth header
`x-api-key`; `IMMICH_URL` + `IMMICH_API_KEY` from `.env`) into the
`video_people` table → the "👤 By person" DLNA container + PWA grouping.

Matching, in order: (1) **content checksum** — Immich returns base64
SHA1; GWMovies files are SHA1'd once (cached in `.immich-import.db`
table `video_sha1`); (2) **(normalized filename, exact byte size)**
fallback via a per-asset GET — needed because **Immich's stored checksums
go stale** when files are metadata-edited in place after indexing (same
size, different bytes; proven live — checksum-only matched 8/349, the
fallback took it to 337/349, 20 persons). The importer's ` (N)` collision
suffix is normalized; ambiguous (name, size) keys are dropped, never
guessed. Per-person writes are a SYNC (replace, not merge) — re-run after
new Immich face-tagging, safe as a habit. DRY-RUN by default.

```bash
.venv/bin/python tools/immich_people_sync.py            # preview
.venv/bin/python tools/immich_people_sync.py --apply    # sync
python3 -m unittest tools.test_immich_people_sync -v    # 14 tests
```

### `tools/openlibrary_books.py`

Enriches **audiobook metadata from the OpenLibrary API** (2026-07-13):
canonical author/title + **series name & number-in-series** per book
(a book = its folder = `album_key` under the audiobooks UDN) into the
`book_meta` display overlay. Never touches files or `tracks`.

Per book: (author, title) guessed from majority tags, falling back to
folder-name parsing ("57 - The Reality Dysfunction - Peter F Hamilton -
1996"); `search.json` behind a fuzzy title floor (0.60) + author-token
overlap guard; series majority-voted from the work's editions'
`series` strings ("Discworld (13)", "Name #3", "Name, Book 3", "Book 3
of Name"; number is REAL so novellas are #1.5). Hard-won guards from
the first live batches: **catalog numbers > 100 reject the whole entry**
(a "Frye annotated #1249" publisher series is bookkeeping, not a story
series); **short titles (<3 words) never retry title-only** (the
narrator-in-artist fallback matched "Alpha" to Lisi Harrison's
"Alphas"); **publisher/imprint series rejected** (curated blocklist +
the "Series (Publisher)" wordy-paren form — "SF Masterworks",
"Historia (DeBolsillo)"); **multi-series edition strings split** into
separate candidates; and **≥2 non-ASCII chars hard-reject a series
name** — the USER RULE that books are English/Dutch ONLY means a
translated edition's series ("Madaʻ bidyoni", Armenian Narnia, pinyin)
is never valid data. Transport failures are transient (NOT cached — retried next
run); a confident miss is a sticky `notfound`; `manual` rows are never
overwritten. ~1 req/s, identifying UA (contact email from `.env`).
Root-level single-file books (album_key='') are skipped — give them a
folder. Deliberately does NOT import dlna_library (module import runs
migrations on the live DB); creates `book_meta` itself if missing.

```bash
python3 tools/openlibrary_books.py                # dry-run preview
python3 tools/openlibrary_books.py --apply -v     # fetch + write
python3 tools/openlibrary_books.py --refetch --apply   # redo non-manual
python3 -m unittest tools.test_openlibrary_books -v    # 27 tests
# retry one book:
sqlite3 library.db "DELETE FROM book_meta WHERE source='notfound' AND album_key='…'"
```

### `tools/compilation_playlists.py`

Creates playlists for **scattered compilation albums** — an album TAG
("2 meter sessies Volume 1", "Billboard Top 100 of 1970") shared by
tracks that live in many different folders (one per contributing
artist). Folder-based album grouping can't reunite them, so they're
invisible as albums; this exposes each as a playlist named after the
tag, tracks ordered artist → title.

Selection (defaults tuned on the real library, 2026-07-03): ≥`--min-tracks`
(5) tracks share the exact album tag, by ≥`--min-artists` (3) distinct
artists, and **no single folder holds ≥`--max-per-folder` (5) of them**.
The artist floor excludes single-artist albums (Supertramp "Paris"); the
per-folder ceiling excludes generic-title collisions ("Greatest Hits" =
20 different artists' separate albums). Existing playlists are skipped by
case-insensitive name, so re-running after new rips only adds what's new
— safe as a post-rip habit. DRY-RUN by default; `--apply` mutates.

```bash
python3 tools/compilation_playlists.py               # preview
python3 tools/compilation_playlists.py --apply       # create
python3 tools/compilation_playlists.py --min-tracks 8
python3 -m unittest tools.test_compilation_playlists -v   # 10 tests
```

First real run (2026-07-03) created 13 playlists (the 2 meter sessies
family, Essential Classical Chillout, Toen Was Het Stil Op Straat,
Cohen Covered, …); the 3 Billboard candidates already existed. Side
fix: `pl_get` now orders by `added_at, id` — `added_at` alone has
second resolution, so bulk adds tied and returned in arbitrary order.

### `tools/relink_orphan_overrides.py`

Recovers orphan `metadata_overrides` rows after an AssetUPnP rescan
rotated co-hashes. AssetUPnP URLs look like
`…/c2/b16/f44100/d<track-id>-co<container-hash>.ext`. After a major
rescan, the indexer crawls in brand-new URLs and every existing
`metadata_overrides` row points at the OLD URL the indexer no longer
sees. The tool matches orphans → current bare tracks by **d-id +
fuzzy (artist, title)** and rewrites `metadata_overrides.url` in place.

Surfaced 2026-05-27 when a duplicate-cleanup-driven AssetUPnP rescan
left 37,943 metadata_overrides rows orphaned; the d-id relink
recovered 19,149 of them (the other 18,794 were genuinely
trashed-file casualties, since pruned).

#### d-id is NOT a per-file identifier

Surfaced 2026-05-28: d-id collides systematically. Of 22,394 distinct
d-ids in the live library, ~9,200 (~41%) appear in tracks with
multiple `(artist, title)` pairs. Categories: same song on two
albums (compilations), same album's adjacent tracks (e.g. 3 Doors
Down's Kryptonite + Down Poison sharing `d-4591903772373150829`),
and same file indexed via two browse-tree paths with different
tags. AssetUPnP's d-id is closer to "(album-bucket, track-position)
hash" than to "physical-file hash". 

Therefore relinking by d-id alone WOULD silently re-attach overrides
to wrong tracks. The tool now requires BOTH d-id match AND a fuzzy
similarity ≥ `_FUZZY_FLOOR` (0.55) on normalised `(artist, title)`.
Punctuation/diacritics/bracketed annotations are stripped before
comparison; collaboration-credit variation (`"A; B"` vs `"A feat. B"`)
passes; truly-different songs fail.

For damage from earlier d-id-only relinks, see
`tools/audit_override_mismatches.py`.

#### Safety

- **Dry-run by default.** `--apply` is opt-in.
- Idempotent — a second run finds no orphans and does nothing.
- d-ids that resolve to multiple bare tracks (shouldn't happen given
  `UNIQUE(udn,url)`, but defended against) are reported as
  `ambiguous` and skipped.
- Two orphans claiming the same new URL: first wins, second skipped
  as `ambiguous`.
- URLs without a recognisable d-id (non-AssetUPnP / hand-edited) are
  counted as `no_d` and left alone.
- **mismatch** (NEW): d-id matched but fuzzy (artist, title) check
  failed — skipped, reported as `mismatch`.

#### Usage

```bash
# Dry-run (default) — preview what would be relinked:
python3 tools/relink_orphan_overrides.py

# Apply the relinks:
python3 tools/relink_orphan_overrides.py --apply

# Custom DB path:
python3 tools/relink_orphan_overrides.py --db /path/to/library.db --apply
```

#### Tests

`tools/test_relink_orphan_overrides.py` — 16 unit tests over a
throw-away temp DB. Cover: `_d_id` parsing (positive + negative
ids, non-AssetUPnP URLs); co-hash rotation relink; dry-run does
not mutate; no-match (trashed file) reported as `no_match`; missing
d-id reported as `no_d`; idempotent (second run is a no-op);
ambiguous d-id skipped; two orphans for the same d-id → one
relinks, one ambiguous; **fuzzy-match guard** — genuine collision
blocked, punctuation difference accepted, collaboration-credit
variation accepted, bracketed annotation ignored, completely-
different song blocked, empty metadata treated as mismatch.

```bash
python3 -m unittest tools.test_relink_orphan_overrides -v
```

### `tools/relink_playlists_to_localfs.py`

Repoints `playlist_tracks` (and the `__favourites__` playlist) at
RoHaLocalFS after AssetUPnP is decommissioned. When the UPnP backend is
switched off, every playlist row still holds its dead
`http://<host>:26125/...` URL and playback times out
(`proxy_stream … reason=error:TimeoutError`). The tool rewrites each
row's `url` + `art` to the matching LocalFs track by NORMALISED metadata —
(artist, album, title) strong, then (artist, title) song-level (album
differs: compilation vs original, AcoustID-corrected album) — and
**removes** rows with no LocalFs match (the migration consequence: those
files aren't in the LocalFs library, which is a subset of what AssetUPnP
served). Rows that would collide on `UNIQUE(pl_id, url)` after a relink
are removed as duplicates. Also prunes `album_favourites` that no longer
match any LocalFs album.

A "LocalFs row" is any url containing `/localfs/stream/`, so already-
relinked rows are skipped — **idempotent**. DRY-RUN by default; `--apply`
mutates and auto-backs-up `library.db` first (`--no-backup` to skip,
`-y` to skip the prompt, `--no-prune-favs` to keep orphan album favs).

First real run (2026-05-31, 2,044 dead rows): relinked 1,260 (367 strong
+ 893 song), removed 784 (744 no-match + 40 dup), pruned 95 album favs.

```bash
python3 tools/relink_playlists_to_localfs.py            # dry-run
python3 tools/relink_playlists_to_localfs.py --apply    # backup + commit
python3 -m unittest tools.test_relink_playlists_to_localfs -v
```

### `tools/relink_overrides_to_localfs.py`

Recovers the ORPHANED `metadata_overrides` found by the 2026-07-12
completeness audit: 10,859 of 10,890 manual rows keyed on dead URLs
(10,810 AssetUPnP `:26125` — the improve_song_years / correct_year_drift
original-year corrections — plus 49 on the pre-cutover `:8201` LocalFs
port), all inert since the decommission because the COALESCE pass and
`/api/track_meta` match by exact URL.

Two modes, deliberately different:

- **Port-heal** (`…/localfs/stream/<id>` orphans): the id is the stable
  LocalFs obj_id → exact same-file match; the WHOLE row is repointed,
  fields intact.
- **Year transplant** (all other manual orphans): matched by normalised
  (artist, title) against current LocalFs tracks; ONLY THE YEAR moves —
  as a fresh year-only `source='manual'` row on every matching track's
  URL (multi-match fans out, same per-recording semantic as
  improve_song_years; capped at 25 to skip junk keys). The orphan's
  artist/album/title/genre are **deliberately dropped**: they're mostly
  AcoustID-era values the improve_song_years merge carried along, and
  relinking them would re-lay stale metadata over beets' file tags on the
  next rescan (the masking problem post_beets_reindex.py fights). Year is
  safe — display-only, never COALESCEd into `tracks`. Trade-off: genuine
  pre-decommission hand edits to artist/title text are not recovered
  (indistinguishable from the merged AcoustID fields).

Never overwrites: an existing row with a non-NULL year always wins (a
NULL-year row gets filled, other fields untouched). Consumed orphans are
deleted; unmatched manual orphans are kept (inert) unless
`--prune-unmatched`; orphaned `notfound` rows are pruned by default
(`--no-prune-notfound` to keep). Idempotent — a second run no-ops.
DRY-RUN by default; `--apply` auto-backs-up `library.db`.

First real run (2026-07-12, 11,509 orphans): 46 port-healed, 8,087
transplanted onto 8,009 live URLs (7,899 inserts + 110 fills), 650
notfound pruned, 2,726 unmatched kept. Live manual overrides went
31 → 7,976; `year_original` display verified working immediately (no
restart needed — read path). Backup:
`library.db.bak-ovrelink-20260712-162540`.

```bash
python3 tools/relink_overrides_to_localfs.py            # dry-run
python3 tools/relink_overrides_to_localfs.py --apply    # backup + commit
python3 -m unittest tools.test_relink_overrides_to_localfs -v   # 16 tests
```

### `tools/audit_override_mismatches.py`

Repairs damage from past d-id-collision-driven mis-relinks (see the
**d-id is NOT a per-file identifier** note in
`tools/relink_orphan_overrides.py`). For each `acoustid` override
row, computes a fuzzy similarity (artist + title) against the joined
`tracks` row. Rows where **both** scores fall below the floor (0.55)
are deleted, freeing the URL so the AcoustID worker re-fingerprints
it on the next pass.

#### Conservative-by-design

- Source `manual` is **never** touched (the user knows best).
- BOTH artist AND title must score < 0.55 — a same-artist-but-wrong-
  title mismatch (e.g. 3 Doors Down's Kryptonite override on a
  Down Poison track) is NOT flagged. That asymmetry is intentional:
  these cases overlap with AcoustID's natural fingerprint mismatches,
  and the worker will resolve them on its own next pass.
- Dry-run by default; `--clean` deletes the suspect rows.
- The fuzzy floor is shared verbatim with
  `tools/relink_orphan_overrides.py` so a row that this tool flags
  as suspect is *also* one that the relink tool would now refuse
  to create. The two stay in sync.

#### Usage

```bash
python3 tools/audit_override_mismatches.py             # dry-run, top 30
python3 tools/audit_override_mismatches.py --top 0     # full list
python3 tools/audit_override_mismatches.py --clean     # confirm prompt
python3 tools/audit_override_mismatches.py --clean -y  # non-interactive
```

#### Tests

`tools/test_audit_override_mismatches.py` — 9 tests covering exact
match / punctuation difference / collaboration-credit variation /
bracketed annotations (all NOT suspect), completely-different-song
suspect, same-album-different-song NOT suspect (conservative),
manual override never touched, `delete_suspects` removes only
`source='acoustid'`.

```bash
python3 -m unittest tools.test_audit_override_mismatches -v
```

### `tools/correct_year_drift.py`

Fixes the case where AcoustID resolves a fingerprint to a later
re-release recording on MusicBrainz (e.g. a 2001 compilation's
recording entry instead of the 1979 studio original), and BOTH
`tracks.year` and `metadata_overrides.year` agree on the reissue date
so the in-display MIN logic can't recover. The tool walks
`(artist, title)` groups and rewrites `metadata_overrides.year` on
the later instances to the earliest plausible year evidenced by
another instance of the same song in the user's library.

#### Identification (v3)

- **Effective year** per row: `MIN(file_year, mb_year)` when both
  present, else whichever is set.
- **earliest_plausible** per `(lower(artist), lower(title))` group:
  `MIN(eff)` among non-live rows with `eff >= 1950`. The floor
  excludes file-tag errors like the Bowie `1905` / Trammps `1927`
  cases observed 2026-05-28.
- **Live filter**: row excluded from being a candidate (and from
  being someone else's earliest_plausible) if its album OR title
  matches any `LIVE_MARKERS` substring: `live`, `in concert`,
  `on tour`, `at the …`, `pulse`, `earls court`, `wembley`,
  `madison square`, `fillmore`, `royal albert`, `wall live`,
  `unplugged`, `(live …)`, `[live …)`, `- live`, `session`,
  `bootleg`. Conservative on the "live" side — a studio album
  named "Live At Carnegie Hall" by mistake would be excluded;
  worth tuning per-corpus if it bites.
- **Candidate** when `eff - earliest_plausible >= 3` AND the row
  is non-live.

#### Persistence

Writes `metadata_overrides.year = earliest_plausible,
source='manual'`. Same column the PWA's edit modal writes to.
Survives re-index and AcoustID re-runs (`manual` beats `acoustid`
in `metadata_override_set`). Idempotent — a second run finds zero
candidates because the freshly-applied year now IS the earliest.

#### Library-only correction limit

The tool can only correct to the earliest year *available in the
library*. If a song only exists in your library on a 1997
compilation (no 1972 original copy), the tool will surface a
candidate but its target year is 1997 — better than 2001, but not
the MB-true 1972. There's no MusicBrainz fallback in this tool by
design; it's the AcoustID worker's job to ask MB.

#### Usage

```bash
# Dry-run preview (default top=30):
python3 tools/correct_year_drift.py

# Full preview, all candidates:
python3 tools/correct_year_drift.py --top 0

# Apply with confirmation prompt:
python3 tools/correct_year_drift.py --apply

# Non-interactive (cron / scripts):
python3 tools/correct_year_drift.py --apply -y

# Custom DB:
python3 tools/correct_year_drift.py --db /path/to/library.db
```

#### Tests

`tools/test_correct_year_drift.py` — 13 tests over a throw-away
SQLite. Covers: studio-vs-live exclusion (Pulse, MTV Unplugged),
drift threshold boundary, pre-1950 floor excludes bogus tags,
case-insensitive `(artist, title)` grouping, MIN logic on file +
mb_year, no candidate when only one album, apply creates a new
override / merges with an existing one and forces source='manual',
apply is idempotent.

```bash
python3 -m unittest tools.test_correct_year_drift -v
```

### `tools/improve_song_years.py`

External-lookup counterpart to `correct_year_drift.py`. Where the
drift tool can only correct songs the user owns multiple copies of
(~2.4k songs in this library), this tool queries MusicBrainz for
the **earliest known recording year** of every song in the library
(~25k unique `(artist, title)` groups). Targets the case where the
user only owns one copy of a song and that copy is a later
compilation/remaster — e.g. "Louis Armstrong / What a Wonderful
World" on a 2017 compilation, true original 1967.

#### Algorithm

Per `(artist, title)` group:

1. MB `recording?query=artist:"X" AND recording:"Y"&fmt=json&limit=100`
2. Paginate up to 5 pages (max 500 recordings) at 1.1s/req rate limit
3. Collect every recording's `first-release-date` year
4. Cache `MIN(years)` in `song_year_cache` with `source='mb_recording'`
5. If no usable date → cache as `source='notfound'` (sticky)

The pagination matters: the top-15 result set often surfaces
later-edition recordings before the 1967 original. Full pagination
on "What a Wonderful World" → 234 matches across 3 pages → MIN
year = 1967.

#### Cache table

```sql
CREATE TABLE song_year_cache (
  artist_key TEXT,           -- _norm_title(artist)
  title_key  TEXT,           -- _norm_title(title)
  year       INTEGER,        -- MIN MB year, NULL on no-match
  source     TEXT,           -- 'mb_recording' | 'notfound'
  n_matches  INTEGER,
  fetched_at INTEGER,
  PRIMARY KEY (artist_key, title_key)
)
```

Same persistence invariant as `album_art` / `play_counts` /
`lyrics` / `metadata_overrides`: survives
`clear(udn)` and rebuild-index. Sticky-negative cache prevents
re-hammering MB for songs with no MB entry.

#### Apply step

After lookups complete, walks cached hits. For each track of the
matching `(artist, title)` whose current effective year is later
than the cached year, writes `metadata_overrides.year = cached_year`
with `source='manual'`. The display rules and AcoustID worker
already treat `manual` as the highest-trust source, so subsequent
worker passes won't overwrite. User-edited manual rows are NEVER
touched.

#### Phantom-row skip

(Unknown Artist) and track-number-prefixed titles ("01 - …") are
skipped before MB queries — they're filename-derived metadata that
will never match. Saves ~700 queries on this library.

#### Cost

~25k uncached groups × ~1.5s avg (rate limit + most songs need
only 1 page) ≈ **10 hours** for a full library sweep. Most songs
need 1 page; long-tail ("Yesterday", "Stairway to Heaven") may
hit the 5-page cap. Subsequent incremental runs only hit newly-
indexed songs.

#### Usage

```bash
# Dry-run preview, no MB calls, no DB writes
python3 tools/improve_song_years.py

# Query MB for uncached groups (cap at N for testing)
python3 tools/improve_song_years.py --lookup --limit 100 -v

# Apply cached hits to tracks
python3 tools/improve_song_years.py --apply

# Combined: lookup then apply in one invocation
python3 tools/improve_song_years.py --lookup --apply

# Force re-query a single song:
sqlite3 library.db "DELETE FROM song_year_cache WHERE artist_key='louis armstrong' AND title_key='what a wonderful world'"
```

Validated 2026-05-28 — first 10 live queries hit 9/10, recovered
correct original years for 10cc "Donna" (1972, was 1997 in library),
"Art for Art's Sake" (1975), "Dreadlock Holiday" (1978).

#### Tests

`tools/test_improve_song_years.py` — 26 tests covering: normalisation
(curly apostrophe, diacritics, case, whitespace, empty); schema
creation (idempotent); candidate selection (distinct groups, dedup
via norm, cache exclusion, empty fields, **(Unknown Artist) phantom
exclusion, track-number-prefix exclusion**); lookup (cache hit /
notfound / transient error leaves uncached / `--limit` honored);
apply (write when later, skip when at/before cached, skip manual
overrides, overwrite acoustid override, skip notfound entries,
apostrophe normalisation); MB query string escape.

```bash
python3 -m unittest tools.test_improve_song_years -v
```

### `tools/beets_enrich.py`

Runs the **beets tag-in-place enrichment batch** described in
`docs/enrichment.md`. beets reads MusicBrainz + AcoustID and writes clean
tags + MBIDs **into the files**, in place; the existing mutagen indexer
then picks them up on the next re-index. beets is an *upstream batch
stage*, never a live metadata authority (routing the serve path at a
beets/Jellyfin DB would re-create the AssetUPnP dual-source-of-truth
problem). This tool is a thin, safe wrapper around the external `beet`
CLI — it does not reimplement beets.

#### The one non-negotiable invariant — tag IN PLACE

```
import.write = yes    import.copy = no    import.move = no
```

`verify_inplace()` is a hard gate: any write run **aborts** unless the
config is verified in-place safe. A *missing* key counts as unsafe (beets'
`import.copy` default is `yes`, which would duplicate files), so the user
is pushed to `--write-config` rather than relying on beets defaults. beets'
own `library.db` (`~/.config/beets/library.db`) is kept deliberately
separate from the gateway's `library.db`. The `scrub` plugin is
intentionally NOT enabled (it strips existing tags — `docs/enrichment.md`
§3).

The generated config sets `timid: no` (not §3's `yes`): beets refuses to
run `-q`/`--quiet` while timid is on (*"can't be both quiet and timid"*),
so timidity is a **CLI choice** here — `--quiet` for the auto-accept bulk
pass, `--timid` for a cautious per-match review pass. If a hand-edited
config still has `timid: yes`, `--quiet` aborts early with a clear message
pointing at `--write-config`.

**beets 2.x pluginized MusicBrainz.** MB is no longer built into the
autotagger — it is the `musicbrainz` metadata-source plugin, which the
generated `plugins:` line now enables and which needs the `musicbrainzngs`
package. Without **both**, beets has no metadata source and silently
matches nothing ("No matching release found" for every album → 0 imports,
exit 0 — the failure mode that wasted two multi-hour runs on 2026-06-03).
The tool guards this: it aborts before running if the config's `plugins:`
line lacks `musicbrainz`, or if `musicbrainzngs` isn't importable in
beets' own environment (probed via the `beet` console-script shebang),
each with the exact fix. One-time deps (2026-07-02 — **install beets via
Homebrew, NOT pip**; a `pip3 install beets` gets wiped by every Homebrew
python upgrade, which is how the 2026-06 install died):

```bash
brew install chromaprint beets
# the formula ships WITHOUT these two plugin deps; put them in the keg
# venv — and RE-RUN this after any `brew upgrade beets`:
BEETS_KEG=$(brew --prefix beets)/libexec
$BEETS_KEG/bin/python -m pip install --prefix $BEETS_KEG musicbrainzngs pyacoustid
```

(Same block lives in `requirements.txt` → "beets enrichment toolchain".
The tool's start-up guards print these exact fixes when either is missing.)

**Quiet mode says just "Skipping." on an untagged album?** Two known
causes, diagnosable with `beet -v import -q <dir>`: (1) `chroma: acoustid
album candidates: 0` — beets' fingerprint lookups use a SHARED bundled
AcoustID key that gets rate-limited (error 14), every lookup fails
silently, and text-matching bare tags yields garbage; (2) the release
isn't on MusicBrainz at all. Proven fix for bare files (Nena, 2026-07-02):
pre-tag minimal TEXT tags with mutagen (artist, exact MB album title,
title = filename stem), then re-run `--quiet --revisit` — the MB text
search matches without fingerprints and beets writes full canonical tags.

#### Flags

| Flag | Effect |
|---|---|
| `--write-config` | Write the prog-tuned, tag-in-place `~/.config/beets/config.yaml` (backs up any existing) and exit |
| `--music-root PATH` | Library root (default `/Volumes/SAMDATA/Music`) |
| `--album PATH` | Import a single album dir instead of the whole root |
| `--quiet` | Auto-accept strong matches, no prompts — the bulk pass (`beet import -q`) |
| `--timid` | Prompt per match (more granular than the default per-album prompt) |
| `--revisit` | Re-import a dir already recorded done (`-I` / noincremental) |
| `--reindex` | After import, discover the LocalFs server UDN via `/api/servers` and POST `/api/index/rebuild` |
| `--gateway URL` | Gateway base for `--reindex` (default `http://127.0.0.1:8765`) |
| `--udn UDN` | Server to reindex (default: auto-pick the `uuid:localfs-*` server) |
| `-n` / `--dry-run` | Print the resolved command + safety report; do not invoke beets |
| `-y` / `--yes` | Skip the §7 backup-warning confirmation before in-place writes |

#### Safety

- Hard-fails fast if `beet` isn't installed (`brew install chromaprint
  beets` + the keg plugin deps — see the one-time-deps block above) or the
  config is missing/not in-place.
- Refuses to run if the music drive isn't mounted (external SAMDATA).
- Confirms before any in-place write (with the backup warning) unless `-y`.
- Warns when DSD (`.dsf`/`.dff`) files are under the target or when fpcalc
  is absent — Chromaprint can't decode DSD (`docs/enrichment.md` §6); those
  tag by existing metadata or fall to Picard.

#### Usage

```bash
# one-time deps (brew, NOT pip — see the block above for the keg plugin deps)
brew install chromaprint beets
# write the tag-in-place config, then interactive review, then bulk
python3 tools/beets_enrich.py --write-config
python3 tools/beets_enrich.py                 # interactive (prompts per album)
python3 tools/beets_enrich.py --quiet         # auto-accept strong matches
python3 tools/beets_enrich.py --quiet --reindex   # then re-index LocalFs
python3 tools/beets_enrich.py --dry-run       # show command + safety report
```

#### Tests

`tools/test_beets_enrich.py` — 23 unit tests over the pure helpers (never
invokes `beet` or the network): config carries the in-place invariant +
separate beets library + no scrub + original-year bias and passes its own
gate; `verify_inplace` flags copy/move/write violations and missing keys;
`build_import_cmd` argv for interactive/quiet/timid/revisit; LocalFs UDN
selection (override / prefer-localfs / sole / ambiguous / none);
`find_binary` fallback.

```bash
python3 -m unittest tools.test_beets_enrich -v
```

### `tools/post_beets_reindex.py`

The "make beets' work visible" step that runs AFTER `beets_enrich.py` has
tagged files in place. Two things must happen for the gateway to show the
new tags, and this tool does both in the right order:

1. **Clear the AcoustID `metadata_overrides`.** LocalFs track URLs are
   PATH-based (`sha1(rel_path)`, see `dlna_providers/localfs.py:97`), so a
   beets-tagged file keeps the SAME url. The COALESCE pass in
   `LibraryDB.upsert_tracks` (dlna_library.py:940-945) therefore re-lays
   the old `source='acoustid'` override straight back on top of beets'
   fresh tags and masks them. Deleting the acoustid rows lets the file
   tags show through.
2. **Reindex LocalFs** (`POST /api/index/rebuild?udn=…` → for LocalFs this
   dispatches to `provider.rescan(force=True)`, which re-reads every file's
   tags but does **NOT clear** the tracks table first).

   > **✅ FIXED 2026-07-12 (was the 2026-07-03 caveat): a rescan now
   > updates the metadata of EXISTING-URL rows in place.** `upsert_tracks`
   > gained a URL-keyed metadata-refresh pass (title/artist/album/year/
   > duration/etc.; change-guarded so untouched rows are no-ops;
   > incoming-empty genre/art never blank an existing value; UNIQUE
   > collisions skipped via `OR IGNORE`) plus the previously-missing
   > `tracks_au` AFTER-UPDATE FTS trigger — the old schema only synced FTS
   > on INSERT/DELETE, so ANY in-place metadata UPDATE (including the
   > overrides COALESCE pass) desynced search until the next full rebuild;
   > the trigger's migration does a one-time FTS rebuild on existing DBs.
   > In-place retagging (beets) is therefore visible to a plain rescan —
   > the old `DELETE FROM tracks` + rebuild workaround is obsolete.
   > Regression-guarded by `tests/test_library.py::TestUpsertMetadataRefresh`
   > and the retag/force tests in `tests/test_provider_localfs.py`.

**Manual-override safety (the core invariant, regression-guarded):** ONLY
`source='acoustid'` rows are deleted. `source='manual'` (user edits + the
year-drift / `improve_song_years` corrections) is NEVER touched — those
legitimately win over beets. `notfound` / `video_skip` rows carry NULL
metadata so they mask nothing and are left alone.

**ACOUSTID_API_KEY guard.** Refuses to clear while the AcoustID worker is
live (checks this process's env AND `launchctl getenv` — the gateway
inherits the launchd-domain env). Clearing then would be futile: the 120s
startup scan re-fingerprints every now-bare track and re-creates the
overrides, re-masking beets. Under **Option A** (beets is the sole
metadata authority) the key stays unset, so this never trips; override
with `--ignore-acoustid-key` if you must.

DRY-RUN by default (prints the override breakdown + planned actions);
`--apply` deletes + reindexes, auto-backing-up `library.db` first.

```bash
python3 tools/post_beets_reindex.py            # preview
python3 tools/post_beets_reindex.py --apply    # backup + clean + reindex
python3 tools/post_beets_reindex.py --apply --no-reindex   # clean only
python3 tools/post_beets_reindex.py --apply --no-clean     # reindex only
python3 -m unittest tools.test_post_beets_reindex -v
```

Flags: `--apply` / `-n`/`--dry-run` · `--no-clean` · `--no-reindex` ·
`--no-backup` · `--ignore-acoustid-key` · `--udn` · `--gateway` · `--db` ·
`-y`/`--yes`. Tests: `tools/test_post_beets_reindex.py` — 12 tests
(delete-only-acoustid, manual/notfound/video_skip survival, dry-run no-op,
backup-on-apply, the key guard + `--ignore-acoustid-key` override + skip
under `--no-clean`).

### beets vs the AcoustID worker — Option A (chosen 2026-06-03)

beets and the in-process `AcoustIDFetcher` do the **same job** (fingerprint
→ MusicBrainz → metadata) and **collide**: both are metadata authorities,
and the AcoustID override outranks beets' file tags via the COALESCE pass.
beets is the strictly-better tagger (writes real tags + MBIDs into the
canonical place — the files — with album-level matching and art embedding),
and AcoustID can't do better on the hard cases (same `fpcalc`+MusicBrainz
path; also can't fingerprint DSD). So they are **not** complementary —
pick one.

**Option A is chosen: beets is the sole metadata authority; the AcoustID
worker stays OFF.** Concretely:

- `ACOUSTID_API_KEY` is left **unset** in launchd, so `AcoustIDFetcher`
  no-ops (its startup scan is disabled, `run_once` returns immediately).
- The weekly `com.roha.dlna-acoustid-retry` LaunchAgent is **unloaded**
  (`launchctl bootout gui/$(id -u)/com.roha.dlna-acoustid-retry`).
- Steady-state workflow for new music: drop files → `beets_enrich.py
  --quiet` (incremental — only new folders; already-seen dirs need
  `--revisit`) → `post_beets_reindex.py --apply`.

**Fully removed in 2.0 (2026-06-11):** the `dlna_acoustid.py` module, the
`ACOUSTID_FETCHER` singleton + all its wiring, the `/api/acoustid/*` endpoints,
and the weekly retry agent are gone — beets is now the only metadata path. The
~52k historical `acoustid` `metadata_overrides` rows remain as data, and
`post_beets_reindex.py` still clears them after a beets run so the file tags show
through. (Because the worker no longer exists, that clear is now unconditionally
safe — the old "refuse while the worker is live" guard was removed too.)

## Audiobooks (P1+P2 shipped 2026-07-13; P3–P5 planned)

Full plan/design: `docs/REPORTS.html` Part II. What's live:

- **Second LocalFs library** — `AUDIOBOOKS_ROOT` in `.env`
  (`/Volumes/SAMDATA-1TB/Audio_Books`; or `localfs.audiobooks_root` in
  config.json). `maybe_start_localfs` starts a second `LocalFsProvider`
  (name "RoHaAudioBooks", own UDN, `dlna_localfs_wiring.AUDIOBOOKS_UDN`),
  served by the same `:8200` file server (root joins `allowed_roots`).
  Per-UDN isolation keeps books out of music browse/radio/search for
  free. First scan: 11,548 chapters / 509 books (163 malformed files
  skipped — worth a `find_corrupt_audio.py` pass someday). `.m4b` added
  to `AUDIO_EXTENSIONS` (MIME `audio/mp4`).
- **Two multi-root safety fixes that matter for ANY future third root**:
  `_load_cache` is scoped to the provider's own root (the shared
  `localfs_files` cache + removed-detection would otherwise purge the
  OTHER provider's rows every rescan), and secondary-root track ids are
  salted (`_track_id_for(rel, namespace)` — the file server resolves
  obj_id across ALL localfs UDNs, so identical rel_paths under two roots
  would collide; music keeps namespace='' so its ids/URLs are unchanged).
- **`kind` field** on `/api/servers` entries (`audiobooks` | `music`) —
  the PWA gates all resume behaviour on it, never on track shape, so a
  music playlist can't trigger position writes.
- **Resume positions** — `playback_positions` (see schema), LibraryDB
  `position_set/get/clear/positions_list`, native `POST /api/position` +
  `GET /api/position?album_key=` + `GET /api/positions` (NOT on the SW
  CACHEABLE_API allowlist — live state, network-only by default; POST
  parses raw body so sendBeacon works).
- **PWA** (`static/app.js`): a book queue (`opts.book` through
  `playTracklist` → `browserQueueIsBook`) never shuffles; positions save
  every ~20s + on pause/chapter-end + `sendBeacon` on visibilitychange;
  playback-rate select `#ab-rate` (0.8–2×, browser output only,
  localStorage-persisted, re-asserted per chapter). Root-level
  single-file books (album_key='') key their position by file URL.
  **CONTINUE IS THE DEFAULT (2026-07-14, after the first real Naim/iPad
  test):** the bookmark is SERVER-side per book, so EVERY play entry
  point resumes on every device — `playAlbumFromDB` auto-resumes for
  the audiobooks source (covers the header button AND the ▶ on book
  rows), the header PRIMARY button relabels to "▶ Resume · Ch N —
  H:MM:SS" with `#browse-resume` as the explicit "↻ Start over", and
  tapping a chapter row routes through `_playBookFromChapter` (a real
  book queue from that chapter — the old single-track path silently
  never saved positions). A finished book behaves like an unread one.
  Caveat: playback started from the Naim's OWN controller (browsing the
  📖 UPnP tree) can't resume — the renderer fetches files directly and
  the gateway isn't in that loop; resume applies to playback initiated
  from the PWA/CarPlay.
- **Tests**: `tests/test_positions.py` (20 — DB contract incl.
  clear(udn) survival, payload validation, wiring config),
  multi-root provider tests in `test_provider_localfs.py`,
  `tests/frontend/test_audiobooks.py` (9 Playwright — button states,
  source gating, unshuffled resume queue, pause-POST assertions,
  URL-fallback, rate control).

**OpenLibrary metadata overlay (P6, shipped 2026-07-13).** Canonical
author/title + **series name & number-in-series** per book, fetched by
`tools/openlibrary_books.py` (see its section under Library housekeeping
tools) into the `book_meta` table — a DISPLAY-layer overlay: files and
`tracks` are never written; sticky `notfound`; `manual` wins
(`LibraryDB.book_meta_set/get/all`). The PWA fetches
`GET /api/book_meta_all` once per switch onto the audiobooks source
(`abMeta` map in app.js) and renders 📚 series chips on album rows
(`renderBrowseItems`) plus a `#browse-series` header line
("📚 Night's Dawn #1 · ✒️ Peter F. Hamilton"). `series_seq` is REAL —
novellas are #1.5.

**P3 — Naim resume (shipped 2026-07-14).** `avtransport_seek()`
(AVTransport#Seek, `Unit=REL_TIME`) + `RendererQueue.start(...,
start_at_sec=, is_book=)`: the resume offset rides the
`/api/render_queue` POST (`book` + `start_at_sec` keys, sent by
`_resumeBook`), and the Seek fires async ~1.5s after Play with one
retry (a Seek during TRANSITIONING faults on the Naim; failure =
non-fatal, chapter plays from 0:00). While an `is_book` queue is
PLAYING, the existing 2s monitor persists the position every ~15s
(`playback_positions`, lazy `dlna_library.DB` import) — stop on the
Naim, resume in the PWA/CarPlay. `tests/test_renderer_resume.py`.

**P4 — CarPlay bookmarks (shipped 2026-07-14).** Subsonic
`getBookmarks` / `createBookmark` / `deleteBookmark` in `api_subsonic`
map onto the SAME `playback_positions` table (track id → its
`album_key` = the book; root-level single files key by URL;
`createBookmark` position is ms). `getBookmarks` lists unfinished books
whose chapter still resolves (`LibraryDB.track_by_url`); finished +
orphan rows are skipped. Cross-path consistency is regression-tested
(a CarPlay bookmark resumes in the PWA). `TestBookmarks` in
`tests/test_subsonic.py`.

**P5 — polish (shipped 2026-07-14).**
- **m4b chapter atoms**: `dlna_ffmpeg.probe_chapters/parse_chapters`
  (ffprobe `-show_chapters`; [] when no ffprobe/no chapters) behind
  `GET /api/chapters?url=` (in-memory cache keyed (url, mtime)). PWA:
  `#ab-chapters` picker in book mode (browser output) — choosing a
  chapter seeks.
- **Continue-listening shelf**: `GET /api/positions` rows are enriched
  with the chapter's track fields (book/author/art/chapter_title;
  orphans still listed bare). PWA: a **📖 letter-bar entry** (front,
  audiobooks source only) rendering in-progress books with a chapter
  progress bar; click opens the book.
- **Naim tree**: root gains "📖 Audiobooks" (only when the source has
  authors) → `abauthor:*` → `abbook:*` (3-field codec like `galbum:*`,
  resolved against `dlna_localfs_wiring.AUDIOBOOKS_UDN`) → chapter
  items. Book titles carry the OpenLibrary series ("… 📚 Culture #2").
  Garbled ids → empty container. `tests/test_upnp_audiobooks.py`.
- **Sleep timer**: `#ab-sleep` select (off/15/30/45/60 min, book mode)
  → pause + position save when it fires.

Open question still standing: whether the Naim accepts m4b/AAC over
AVTransport — the tree exposes the books either way; test with one
file on the real device.

## Subsonic API (shipped — browse/playlists/starring/stream/radio all live)

A read-only-ish Subsonic-compatible HTTP API that lets any third-party
Subsonic iOS client (Amperfy, substreamer, play:Sub, …) browse the
gateway's library and stream from it. **The primary motivator is
CarPlay**: those clients have polished CarPlay implementations, the
gateway PWA fundamentally can't (CarPlay is a closed iOS-native
framework). Subsonic API is also pure HTTP, so it traverses Tailscale
cleanly — unlike UPnP's SSDP multicast discovery which doesn't.

### What the iOS client sees

The Subsonic client connects to **the gateway**, not AssetUPnP. The
flow:

```
iPhone Subsonic client (CarPlay)
    ↓ HTTPS over Tailscale
dlna-gateway  /rest/*
    ↓ library.db reads for browse/playlists/favourites/search
    ↓ proxies audio bytes from AssetUPnP for /rest/stream
AssetUPnP (invisible to the client; just a byte source)
```

Every piece of gateway state is exposed:
- Tracks, artists, albums (as indexed from AssetUPnP into library.db).
- User playlists + the existing `__favourites__` track-level playlist.
- `album_favourites` is exposed as Subsonic's "starred albums".
- `play_counts` increments via `/rest/scrobble` so radio bias keeps
  working from cars too.
- Cover art (gateway's `album_art` cache + AssetUPnP-served art via
  the existing `/art` proxy).

Lyrics aren't exposed (not in the Subsonic spec); still work in the
PWA.

### Endpoints

Mounted under `/rest/*`. Both response formats are supported: XML is
the spec default (and what Amperfy and other clients send when no `f=`
is given), `?f=json` selects JSON. The format is resolved once in
`handle()` and stashed on the handler as `_subsonic_format`. JSON
wrapper:

```json
{"subsonic-response":
  {"status":"ok", "version":"1.16.1",
   "type":"dlna-gateway", "serverVersion":"1.0", ...payload}}
```

Errors: `"status":"failed"` with `{"error": {"code": N, "message": "…"}}`.

| Endpoint | Maps to | Purpose |
|---|---|---|
| `/rest/ping` | — | Connectivity test |
| `/rest/getLicense` | — | Hard-coded `{valid:true}` |
| `/rest/getMusicFolders` | — | Single folder, id=1, name="Music" |
| `/rest/getIndexes` | `DB.all_artists` aggregated A-Z | Legacy artist index |
| `/rest/getArtists` | `DB.all_artists` | Modern artist list |
| `/rest/getArtist?id=` | `DB.artist_albums(udn, artist)` | Albums under an artist |
| `/rest/getAlbum?id=` | `DB.album_tracks(udn, artist, album)` | Tracks in an album |
| `/rest/getAlbumList2?type=&size=&offset=` | `DB.all_albums` + sort by `type` | type ∈ newest/alphabeticalByName/recent/random/frequent/starred |
| `/rest/search3?query=` | `DB.search` | Existing FTS5 search |
| `/rest/getPlaylists` | `DB.pl_list()` | All playlists |
| `/rest/getPlaylist?id=` | `DB.pl_get(pl_id)` | One playlist's tracks |
| `/rest/createPlaylist` `/updatePlaylist` `/deletePlaylist` | `DB.pl_create / pl_add_track / pl_remove_track / pl_delete` | Bidirectional sync |
| `/rest/star` `/unstar` | `DB.album_fav_add / album_fav_remove` | Album-level starring → reuses Album Favourites |
| `/rest/getStarred2` | `DB.album_fav_list()` | Starred albums |
| `/rest/stream?id=` | track ID → URL → `dlna_player.proxy_stream` | Audio (Range supported via existing proxy) |
| `/rest/getCoverArt?id=` | album/track ID → art URL → `api_playback.art` | Cover image |
| `/rest/scrobble?id=&submission=true` | `play_counts.count += 1` | Bumps radio bias from cars |

### ID encoding

Subsonic clients treat IDs as opaque strings. The gateway uses
base64-urlsafe-encoded payloads (same pattern as
`api_upnp._encode_album_id`):

- Track:    `tr:<base64(track.url)>`
- Album:    `al:<base64(artist + \x00 + album [+ \x00 + album_key])>`
- Artist:   `ar:<base64(artist)>`
- Playlist: `pl:<plid>` (already opaque in the DB)

Round-trips arbitrary unicode through XML/JSON/URL transports.

**`album_key` in album ids (A3b):** for a LocalFs source the album id
carries the FOLDER (`album_key`) as a third NUL-delimited field, so a
Various-Artists compilation round-trips as one album (getAlbum / star /
getStarred2 / coverArt resolve by folder). The third field is appended
**only when `album_key` is set**, so non-LocalFs album ids stay
byte-identical to the pre-A3b 2-field form (no client/cache churn);
`_album_id_decode` returns `(artist, album, album_key)` and tolerates
legacy 2-field ids with `album_key=''`. `_so_song` carries the track's
`album_key` (added to `album_tracks` / `search` output) so track→album
navigation lands on the folder album. **search3's album entries carry
`album_key` too** (fixed 2026-07-03 — `_search3` used to rebuild the album
dicts without it, so tapping a search result resolved `getAlbum` by
`(artist, album)` only: wrong/partial track list for folder-grouped
compilations; found live by `tests/subsonic_verify.py`). Search results
show the RAW tag album name while browse shows the folder-derived display
name — match by id/`album_key`, not by name.

### Authentication

Single-user, single shared-secret. Read at startup from environment:

```
SUBSONIC_USER=user        # default "user" if unset
SUBSONIC_PASSWORD=<set>   # REQUIRED; no default
can be set using 
launchctl unsetenv SUBSONIC_PASSWORD (delete old password)
launchctl setenv SUBSONIC_PASSWORD=password (set new password)
launchctl getenv SUBSONIC_PASSWORD (show new password)
```

The gateway accepts either the modern token+salt flow:

```
?u=<user>&t=MD5(password+salt)&s=<salt>&v=1.16.1&c=<clientname>
```

…or the legacy plaintext flow (clients can negotiate either):

```
?u=<user>&p=<password>            # plain
?u=<user>&p=enc:<hex(password)>   # hex-encoded
```

If `SUBSONIC_PASSWORD` is unset the API returns 503 on every call —
deliberate, prevents accidental auth-disabled exposure. Auth is a
defence-in-depth layer; the primary access control is Tailscale (the
gateway is not exposed to the public internet).

### Observability (2026-07-02)

Every `/rest/*` request logs **one INFO line** in `gateway.log` (added
after an undiagnosable "Amperfy flaky in the car" afternoon — Subsonic
traffic was previously visible only at `debug`):

```
Subsonic getAlbum client='amperfy' ip=100.x.y.z → 200 in 12ms   # bridged JSON/XML methods
Subsonic stream id=tr:… client='amperfy' ip=100.x.y.z           # byte methods
Subsonic ping client='…' ip=… → refused                         # auth-gate refusals
```

The shared audio-relay `stream ▶ START` / `■ END` lines also carry
`client=<peer-ip>`. **A `100.x` ip = tailnet (CarPlay/Amperfy or remote
PWA); `192.168.x` = LAN.** Diagnosis shortcut: `grep Subsonic gateway.log`
— if it's EMPTY during a flaky window, the requests never reached the
gateway (phone-side Tailscale/cellular), not a gateway problem. Heads-up:
`getCoverArt` logs per cover, so a client's first art sync is chatty.

### What's intentionally NOT implemented

Subsonic's full spec has 60+ endpoints; about 45 are out of scope for
this user / this gateway. Notable omissions:

- Multiple users / roles / per-user playlists.
- Podcasts, bookmarks, chat, shares, jukebox mode, video,
  transcoding, server-side resampling (`maxBitRate` ignored —
  always serve the original). *(Internet radio IS implemented — see
  the "Internet radio" section.)*
- `getNowPlaying` (gateway isn't a player from Subsonic's POV — the
  iPhone is the player).
- Track-level starring (only album-level via `getStarred2`). Could
  extend with a `track_favourites` table later if needed; for now,
  starring a track no-ops gracefully.

### Connecting an iOS client

Recommended: **Amperfy** (free, open source, well-rated, CarPlay).

1. Set `SUBSONIC_PASSWORD` in the gateway's environment (LaunchAgent
   plist or `.env`) and restart.
2. Install Amperfy on the iPhone.
3. Add server:
   - URL: `https://ronsmacmini.tail5be6ad.ts.net:8443/rest`
   - Username: `user` (or whatever `SUBSONIC_USER` is set to)
   - Password: the value of `SUBSONIC_PASSWORD`
4. Tap a playlist or starred album. Plug into CarPlay. Drive.

Phone-call interruption recovery works because Amperfy is a native
iOS app — it uses `AVAudioSession` properly, which the PWA can't.

### Tests

| File | What it covers |
|---|---|
| `tests/test_subsonic.py` | 38 tests — auth: token+salt / plaintext / enc:hex / wrong-password / wrong-user / no-env-503; ID round-trip (track/album/artist incl. unicode); ping/getLicense/getMusicFolders hard-coded responses; unimplemented method → 404; `.view` legacy suffix routes the same; getArtists/getIndexes/getArtist/getAlbum/search3 against seeded DB; getAlbumList2 alphabetical + starred; getPlaylists includes `__favourites__`; getPlaylist round-trip; star → album_favourites add, unstar → remove, getStarred2 returns favs; scrobble bumps play_counts; submission=false doesn't; getCoverArt resolves to art_url and delegates to /art proxy; unknown cover ID → 404; XML format — default-when-no-`f`, `f=json` vs `f=xml`, special-char escaping, nested-array repeated elements, `<error>` on failed status |

## Internet radio (complete — all 3 phases done)

> Status: **fully implemented, all phases.** `radio_favourites` table,
> `LibraryDB.radio_fav_*` (incl. `radio_fav_update`), all
> `/api/radio/*` endpoints, radio-browser search,
> `proxy_radio_stream()` ICY de-interleaving, `/radio_stream`,
> `/api/radio/nowplaying`, the `is_stream` monitor guard, the
> "📡 Radio Stations" frontend (search + genre chips + favourites +
> now-playing radio variant), **and** the Subsonic radio methods
> (`getInternetRadioStations` / `create` / `update` /
> `deleteInternetRadioStation`) — radio works in CarPlay via Amperfy.
>
> Naming note: the pre-existing "📻 Radio" button is a *different*
> feature (a play-count-biased shuffle of the local library); internet
> radio is deliberately branded "📡 Stations" to keep them distinct.
> A genre-chip row (Prog / Prog-rock / Jazz / Pop / Rock / Classical,
> → `/api/radio/search?tag=`) was added on top of the original spec at
> the user's request; station rows show their genre tags.

Internet radio (Icecast/Shoutcast streams) is in scope; **commercial
streaming services (Spotify, Tidal, Apple Music, Qobuz) are not** —
DRM-encrypted audio, proprietary closed protocols, and licensing make
them a non-starter. The Naim already speaks Tidal/Qobuz/Spotify
Connect natively; that belongs on the renderer, not the gateway.

An internet-radio station is just an HTTP URL serving an endless
MP3/AAC byte stream — which the existing playback paths already
handle: `avtransport_send()` (`SetURI`+`Play`) for the Naim, and the
`/stream` proxy for browser audio. So this is mostly a data-model +
UI feature, not a protocol feature.

The catalogue comes from **radio-browser.info** (a free community
directory of ~50k stations). The gateway does **not** persist the
catalogue — only the user's favourites. Search results are transient
(proxied + briefly cached in memory).

### `radio_favourites` table — capped at 25, survives `clear(udn)`

```sql
CREATE TABLE IF NOT EXISTS radio_favourites (
  station_uuid TEXT PRIMARY KEY,   -- radio-browser stationuuid (stable)
  name         TEXT NOT NULL,
  stream_url   TEXT NOT NULL,      -- radio-browser url_resolved
  homepage     TEXT,
  favicon      TEXT,               -- station logo URL
  codec        TEXT,               -- 'MP3' | 'AAC' | 'OGG' | ...
  bitrate      INTEGER,            -- kbps, informational
  country      TEXT,
  tags         TEXT,               -- comma-separated genre tags
  added_at     INTEGER NOT NULL,
  sort_order   INTEGER NOT NULL DEFAULT 0
);
```

Same persistence contract as `album_favourites` / `play_counts` /
`lyrics` — independent of `tracks`, so a rebuild-index / `clear(udn)`
leaves it untouched. Radio has no `udn`. Stations carry their own art
via `favicon`, so `album_art` is not involved.

**The 25-cap is enforced server-side** in `LibraryDB.radio_fav_add()`,
never trusted to the client:

```
RADIO_FAV_MAX = 25
if not radio_fav_is(uuid) and radio_fav_count() >= RADIO_FAV_MAX:
    return 'full'      # caller turns this into HTTP 409
INSERT OR IGNORE …  →  'ok' / 'exists'
```

Re-adding a station already favourited is idempotent and never counts
against the cap. When full, the user must remove a favourite before
adding another — there is no auto-eviction.

### `LibraryDB` methods

```
radio_fav_add(station: dict) -> str    # 'ok' | 'exists' | 'full'
radio_fav_remove(station_uuid) -> bool
radio_fav_is(station_uuid)     -> bool
radio_fav_list()               -> [ {…all columns…}, … ]   # ordered
radio_fav_reorder(uuid_list)   -> bool
radio_fav_count()              -> int
```

### Native endpoints (`/api/*`)

| Endpoint | Purpose |
|---|---|
| `GET /api/radio/search?q=&country=&tag=&limit=` | Proxy radio-browser `/json/stations/search`; **filters out `hls=1`**; returns normalized station objects |
| `GET /api/radio/favourites` | The ≤25 saved stations, ordered |
| `POST /api/radio/favourites/add` | Body = the full station object as returned by `/api/radio/search`. Inserts; **409 `{error:"favourites_full", limit:25}`** when at the cap. Missing `station_uuid`/`name`/`stream_url` → 400 |
| `POST /api/radio/favourites/remove` `{station_uuid}` | Delete; `{ok:false}` if absent |
| `POST /api/radio/favourites/reorder` `{order:[uuid,…]}` | Preset ordering for now-playing prev/next |
| `GET /api/radio/nowplaying?udn=` *(or* `?stream=`*)* | Current ICY metadata for the live screen |

Station logos route through the **existing `/art?url=` proxy** — same
same-origin requirement for iOS lock-screen artwork, already solved.

### Playback — reuse `/api/render_queue`, no new path

A station is modelled as a single "track" with `is_stream: true` and
no duration:

```json
{ "url": "<stream_url>", "title": "<station name>", "artist": "Radio",
  "art": "<favicon>", "duration": "", "is_stream": true }
```

- **Naim/UPnP:** `POST /api/render_queue {udn, tracks:[<station>]}` →
  existing `SetURI`+`Play`. The watchdog (`_monitor_decision`) is
  already safe — it needs `dur > 0` to fire, and a stream has
  `dur = 0`. The unknown-abort guard still protects against a renderer
  genuinely dropping.
- **Browser:** `<audio src="/stream?url=…">` routed to a new
  `proxy_radio_stream()` (see below).

`RendererQueue` change required: when `is_stream` is set, don't treat
an empty-after-current queue as "finished" (a 1-station queue stays
live) and don't surface a progress bar.

### ICY metadata + the radio now-playing screen

Radio "now playing track" = **ICY metadata** interleaved in the
Icecast byte stream (`StreamTitle='Artist - Title';` every
`icy-metaint` bytes).

**Browser path — `proxy_radio_stream()`** (new function in
`dlna_stream_proxy.py`, kept separate from the byte-perfect
`proxy_stream()` Range relay):
1. Request upstream with header `Icy-MetaData: 1`.
2. Read the `icy-metaint: N` response header.
3. Body is then `[N audio bytes][1 length byte][metadata block]`
   repeating. Relay **only** the audio bytes to the browser (`<audio>`
   cannot handle interleaved metadata); parse `StreamTitle` out of
   each metadata block.
4. Stash the current title in a module-level `{stream_url → title}`
   dict that `/api/radio/nowplaying` reads.

**Naim/UPnP path:** the gateway is not in the audio path — but the
Naim parses ICY itself and exposes the current title in
`CurrentTrackMetaData`, which `avtransport_get_position()` **already
returns** as `title`. So `/api/radio/nowplaying?udn=` just reads the
existing snapshot; no new SOAP.

*Caveat:* ICY title works for MP3/AAC. OGG/FLAC streams carry metadata
as in-band Vorbis comments — no ICY title there; the screen falls back
to the station name only.

**The screen** — the now-playing panel detects `is_stream` and
switches layout:

| Standard now-playing | Radio variant |
|---|---|
| album art | station logo (`favicon` via `/art`) |
| progress bar + seek | "📻 LIVE" badge, no seek |
| title / artist / album | scrolling ICY `StreamTitle` + station name + `codec/bitrate` line |
| prev / next = queue tracks | prev / next = **cycle the 25 favourites like radio presets** |
| MediaSession art = album | MediaSession art = station logo, title = ICY title (lock screen) |

Using prev/next to step through the favourites is what makes the
25-cap behave like physical preset buttons.

### Subsonic exposure (`/rest/*`)

Subsonic has native radio methods; they map straight onto
`radio_favourites`:

| Subsonic method | Maps to |
|---|---|
| `getInternetRadioStations` | `radio_fav_list()` |
| `createInternetRadioStation` `?streamUrl=&name=&homepageUrl=` | `radio_fav_add` (honours the 25-cap) |
| `updateInternetRadioStation` `?id=&streamUrl=&name=` | update row |
| `deleteInternetRadioStation` `?id=` | `radio_fav_remove` |

Subsonic clients (Amperfy/CarPlay) play a station by streaming its
`streamUrl` **directly** — they do not go through the gateway proxy,
and Amperfy parses ICY metadata itself. So radio "just works" in
CarPlay once these four methods exist.

**ID encoding:** `rs:<station_uuid>` — radio-browser UUIDs are already
URL/XML-safe, so no base64 wrapping (unlike `tr:` / `al:`).

### radio-browser.info integration

A **4th outbound host** (also listed in the "External services" table above):

| Host | Purpose | Method + path |
|---|---|---|
| `*.api.radio-browser.info` | Station catalogue search | `GET /json/stations/search?name=&tagList=&countrycode=&limit=&hidebroken=true&order=clickcount&reverse=true` |

- **Mirror selection** — the API is DNS round-robin; resolve
  `all.api.radio-browser.info` and pick a server, or hard-code
  `de1`/`nl1` with failover. Do not pin a single host.
- **User-Agent required** — same contract as MusicBrainz; reuse the
  `DLNAGateway/1.0 ( <GATEWAY_CONTACT_EMAIL> )` pattern (email from `.env`).
- **`hidebroken=true`** drops dead streams; `order=clickcount` surfaces
  popular stations first.
- **HLS filter** — exclude records where `hls == 1`; UPnP renderers
  can't play HLS and browser `<audio>` only does on Safari.
- Optional courtesy: `GET /json/url/{stationuuid}` on play
  (radio-browser's click counter) — nice-to-have, skippable.

### Implementation phases

1. **Phase 1** ✅ *done* — `radio_favourites` table + `LibraryDB`
   methods + native endpoints + `/api/radio/search`. Playback is the
   caller's job via the existing `/api/render_queue` (a station is a
   single `is_stream` "track"); no `RendererQueue` change was needed
   since an extra dict key is harmless and a 0-duration single-track
   queue already behaves. No ICY yet (title = station name).
2. **Phase 2** ✅ *done* — backend: `proxy_radio_stream()` ICY
   de-interleaving, the `/radio_stream` route, `/api/radio/nowplaying`
   (browser `?stream=` ICY path + UPnP `?udn=` snapshot path), and the
   `is_stream` guard in `_monitor_decision` (radio never auto-advances
   — a momentary `STOPPED` is a rebuffer). Frontend: the "📡 Radio
   Stations" synthetic row in `#pl-list`, the search/genre-chips/
   favourites view, `playStation()` (browser → `/radio_stream`, UPnP →
   `is_stream` render-queue track), and the now-playing radio variant
   (LIVE badge, no seek bar, `⏮/⏭` cycle favourites as presets).
3. **Phase 3** ✅ *done* — the four Subsonic radio methods
   (`getInternetRadioStations` / `create` / `update` /
   `deleteInternetRadioStation`) in `api_subsonic.py`, mapped onto
   `radio_favourites` via the `rs:<station_uuid>` id codec. A
   Subsonic-created station gets a synthesised `uuid4`; `create`
   honours the 25-cap. Radio now works in CarPlay via Amperfy.

### Tests

| File | What it covers |
|---|---|
| `tests/test_radio.py` | DB round-trip; **25-cap enforced / `'full'` returned / re-add idempotent and doesn't count**; `radio_fav_update`; `clear(udn)` survival; reorder; handler 400/409/200; HLS filtered from search; ICY parse / `_read_exact` / de-interleave round-trip |
| `tests/test_subsonic.py` | `TestInternetRadio` — `rs:` id round-trip, `getInternetRadioStations` lists favourites, `create` (+ missing-param fail, 25-cap fail), `update` changes fields, `delete` removes (+ bad-id fail) |
| `tests/frontend/test_radio.py` | 16 Playwright tests — synthetic `#radio-pl-item` placement, opening the view, debounced name search, genre-chip tag search, clearing→favourites, optimistic ☆→★ add, cap-full 409 toast, favourite list + ✕ remove, genre shown on rows, browser vs UPnP playback, radio now-playing layout (LIVE badge, no seek bar), ICY poll into `#np-artist`, `⏮/⏭` cycle favourites |
| ICY parser unit test | Feed a synthetic `icy-metaint` byte stream through the `proxy_radio_stream` parser; assert `StreamTitle` extraction + clean audio passthrough |
