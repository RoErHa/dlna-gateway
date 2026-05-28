# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

DLNA Gateway is a Python-based UPnP/DLNA music library gateway. It discovers UPnP MediaServers (AssetUPnP, MinimServer, Jellyfin, Plex) on the local network, indexes their music into a local SQLite DB, and exposes a PWA web UI for browsing and playback. Playback targets: UPnP MediaRenderers (Naim Uniti, etc.) and browser audio. The gateway also announces itself as a UPnP MediaServer so UPnP renderers can browse its playlists directly.

## Running the Gateway

```bash
./setup.sh --run                   # set up venv + start on :8765
./setup.sh --run --no-browser      # skip auto-open
./setup.sh --run --debug           # verbose logging
./setup.sh --run --probe http://...  # add a server manually
./setup.sh --run --list-devices    # show known devices table
./setup.sh --run --reset-devices   # clear device DB
```

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

# Layer 3 — chaos simulator (live gateway, randomized + adversarial):
python3 tests/chaos.py --iterations 500 --workers 4
python3 tests/chaos.py --seed 42 --quiet    # reproduce a past failure
```

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

`chaos.py` hard-fails if it sees any 5xx, `/tmp/dlna-gateway.err` grows (= silent thread death), or a snapshot takes >5s. Its first real-world find was the `playlist_tracks.duration` HH:MM:SS-string `ValueError` that was killing the renderer-queue daemon thread invisibly.

Each core module also has a standalone self-test:

```bash
python dlna_config.py              # config/logging
python dlna_discovery.py           # SSDP discovery (20s live scan)
python dlna_content.py <control-url>  # UPnP SOAP
python dlna_library.py             # DB operations
python db_pool.py                  # concurrent DB stress test
python dlna_player.py              # QueueRegistry + duration-parser self-test
python dlna_server.py              # HTTP server (30s on :8766)
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
| `dlna_gateway.py` | Main entry point, wires modules, starts all threads |
| `dlna_server.py` | Threaded HTTP server; delegates routing to `dlna_routes` |
| `dlna_routes.py` | `GET_ROUTES` / `POST_ROUTES` path → handler maps |
| `dlna_discovery.py` | SSDP listener, probe, subnet scanner, server heartbeat |
| `dlna_registry.py` | Data classes + `ServerRegistry` / `RendererRegistry` thread-safe stores |
| `dlna_library.py` | `LibraryDB` — SQLite index + FTS5 search + playlists; composition root for DB-owning singletons |
| `dlna_indexer.py` | `Indexer` — background crawler that walks a MediaServer and populates LibraryDB |
| `dlna_art_fetcher.py` | `AlbumArtFetcher` — Phase B MusicBrainz + Cover Art Archive lookup |
| `dlna_devices.py` | `DeviceRoleCache` — in-memory mirror of device_roles for zero-latency classification |
| `db_pool.py` | SQLite connection pool — WAL mode, thread-local connections, write serialization |
| `dlna_config.py` | Constants (`DB_FILE`, `CFG_FILE`, `LOG_FILE`), logging setup, config load/save |
| `dlna_content.py` | UPnP ContentDirectory SOAP client (`cd_browse`, `cd_search`) |
| `dlna_avtransport.py` | UPnP AVTransport SOAP client (send/stop/pause/state/position) |
| `dlna_player.py` | `RendererQueue` (sequential playback per renderer) + `QueueRegistry` (one queue per UDN) |
| `dlna_stream_proxy.py` | Browser-audio HTTP proxy (`/stream`) with 5-min idle timeout |
| `api_browse.py` | Browse/search API endpoints |
| `api_playback.py` | Playback, stream proxy route, `/art`, `/api/client_log`, state, indexer management |
| `api_playlists.py` | Playlist CRUD endpoints |
| `api_upnp.py` | UPnP service descriptors + SOAP ContentDirectory (for Naim Uniti browsing gateway playlists) |

### Key Module-Level Singletons

These are shared state across all request handler threads:

- `dlna_discovery.SERVERS` / `RENDERERS` — device registries
- `dlna_library.DB` / `INDEXER` / `DEVICE_ROLES` — library DB, crawler, device role cache
- `dlna_player.QUEUES` — `QueueRegistry` holding one `RendererQueue` per renderer UDN (lazily created). Replaces the prior single-queue singleton so multiple users/renderers can play concurrently.

### Database Schema

SQLite at `library.db`, WAL mode, accessed via `db_pool.Pool`:

```
tracks(id, udn, obj_id, url, title, artist, album, duration, art, mime, genre, file_path, bit_depth, sample_rate, year)
  UNIQUE(udn, artist, album, title, bit_depth, sample_rate)
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
metadata_overrides(url, artist, album, title, genre, year, updated_at, source)
  source ∈ {'manual', 'acoustid', 'notfound', 'video_skip'}
  year is the MUSICBRAINZ original release year (release-group's
  first-release-date), captured by AcoustIDFetcher.
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

- Hard-caps at 5 MB per image to prevent memory abuse.
- Validates Content-Type starts with `image/` — an upstream HTML 404 page won't poison the SW cache.
- 10-second timeout; slow upstream fails fast.
- The handler is in `api_playback.art()` routed at `/art` in `dlna_server.py`.

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
| `ffmpeg` | `LoudnessScanner` (per-track true-peak analysis) | `brew install ffmpeg` |
| `fpcalc` (Chromaprint) | `AcoustIDFetcher` (fingerprint generation) | `brew install chromaprint` |

Both workers `_find_*()` walk Homebrew install locations explicitly because launchd-spawned processes have a minimal PATH; missing binaries are detected at scan-start and the worker bails without poisoning its sticky-negative cache.

**`.env` caveat:** if `python-dotenv` isn't installed in the runtime Python, `dlna_config.py` silently catches the ImportError and the `.env` file is **never loaded** — env vars must then come from the process environment (`launchctl setenv` on macOS, systemd `EnvironmentFile`, shell `export`). The warnings `GATEWAY_CONTACT_EMAIL not set in .env — using placeholder` at startup are a symptom of dotenv-not-installed, even if the `.env` file looks populated.

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

Same persistence pattern as `album_art` / `play_counts` / `track_loudness`.
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
`play_counts`, `lyrics`, `track_loudness`.

### `album_favourites` table

```sql
CREATE TABLE IF NOT EXISTS album_favourites (
  artist     TEXT NOT NULL,
  album      TEXT NOT NULL,
  added_at   INTEGER NOT NULL,
  PRIMARY KEY (artist, album)
);
```

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
- **Right column**: a synthetic first row (`#album-fav-pl-item`,
  "⭐ Favourite Albums") at the top of the Playlists list — above the
  existing track-level "⭐ Favourites" and any user playlists.
  Clicking it swaps the `pl-list` / `pl-tracks` panels (same UX as
  opening any playlist) and renders the favourites as
  `.album-fav-row` rows with thumbnail + artist/album. Click a row
  → drills into `showAlbumTracks(artist, album)` (same destination as
  Browse → Artist → Album).
- **Module state**: `albumFavouritesCache` in app.js is set to `null`
  on add/remove so the next view-open refetches; this avoids
  add-then-immediately-view showing stale state.

### UPnP exposure (Naim)

`api_upnp._gw_browse` exposes "⭐ Favourite Albums" as the **first**
top-level container under root (above the existing "Playlists"). One
level deeper: a container per favourited album titled
`"<album> — <artist>"`. One more level deeper: the album's tracks,
resolved against `album_fav_list()[i]['udn']` via `DB.album_tracks`.

ObjectID encoding for individual albums:
`favalbum:{base64-urlsafe(artist + "\x00" + album)}` — round-trips
arbitrary unicode (non-ASCII names, ampersands, slashes, NUL bytes
fine) through SOAP/XML. See `_encode_album_id` / `_decode_album_id`
in `api_upnp.py`. Garbled / non-base64 IDs decode to `("", "")` and
return an empty container rather than 500.

### Tests

| File | What it covers |
|---|---|
| `tests/test_album_favourites.py` | DB round-trip + idempotent add, dedupe, ordering newest-first, orphan-album survival, `clear(udn)` invariant, handler 400/200 paths. 14 tests. |
| `tests/test_upnp_album_favourites.py` | Album-id codec round-trip (incl. unicode/specials), root browse lists fav-albums first, "favalbums" lists each favourite, "favalbum:{...}" lists tracks, unknown album → empty container. 9 tests. |
| `tests/frontend/test_album_favourites.py` | Star button gated by `track_count>1`, initial state from `/check`, click → /add or /remove with optimistic flip, "⭐ Favourite Albums" rendered first in `pl-list`, clicking it opens album-list view, clicking a row drills into `showAlbumTracks()`. 9 Playwright tests. |

## External services (outbound HTTP)

The gateway is LAN-only except for album-art, lyrics, radio-catalogue, and (in flight) AcoustID metadata lookups. All over TLS:

| Host | Purpose | Method + path |
|---|---|---|
| `musicbrainz.org` | Resolve `(artist, album)` → release-group MBID | `GET /ws/2/release-group/?query=…&fmt=json&limit=5` |
| `coverartarchive.org` | Confirm a front cover exists for that MBID | `HEAD /release-group/{mbid}/front-500` — 200/301/302/307 counts as "have it", 404 counts as "no cover" |
| `lrclib.net` | On-demand lyrics for the currently-playing track | `GET /api/get?track_name=&artist_name=&album_name=&duration=` — 200 with body or 404 |
| `*.api.radio-browser.info` | Internet-radio station catalogue search | `GET /json/stations/search?name=&tagList=&countrycode=&hidebroken=true` — see the "Internet radio" section |
| `api.acoustid.org` | AcoustID fingerprint → MusicBrainz metadata enrichment | `POST /v2/lookup` form body `client=&meta=recordings+releasegroups&duration=&fingerprint=` — see the "Metadata enrichment" section |

Required contract:

- **User-Agent** — `_MB_USER_AGENT = "DLNAGateway/1.0 ( hintt@me.com )"` in `dlna_library.py`. MusicBrainz's ToS demands an identifying UA with contact info; anonymous calls get 403-blocked.
- **Rate limit** — `_MB_RATE_LIMIT_SEC = 1.1` between calls, enforced in `AlbumArtFetcher.run_once()`. MB allows 1 req/sec sustained; 1.1s gives a small safety margin.
- **Timeout** — `_MB_TIMEOUT = 10.0` per connection. Exceptions inside `_mb_lookup_cover()` are caught and returned as `None` (album gets cached as `notfound`).
- **No retries** — a transient failure ends up as `notfound` and stays sticky; see the "Sticky notfound cache" subsection above for how to force a retry.
- **AcoustID has TWO key types — use the right one.** `https://acoustid.org/api-key` gives you a **user** key, intended for *submitting* fingerprints to the AcoustID database. `https://acoustid.org/applications` (register a new application) gives you an **application** key, which is what `/v2/lookup` requires. Calling `/v2/lookup` with a user key returns `HTTP 400 {"error":{"code":4,"message":"invalid API key"}}` — distinctive failure mode worth remembering. `ACOUSTID_API_KEY` in `.env` must be the application key.

## Loudness normalization (Phase 1, in flight)

Every track gets analysed once with `ffmpeg -af ebur128=peak=true`, the
true peak (dBTP) is stored, and a per-track gain is applied on playback
so all tracks sit just below a common ceiling. **Phase 1 covers UPnP
renderers only** (Naim Uniti is the primary device). Browser-audio Web
Audio gain is deferred to Phase 2.

The integrated LUFS value is captured from the same ffmpeg run as
informational metadata only; **peak drives the gain**, not LUFS.

### Mode: peak normalisation (chosen 2026-05-06)

Trade-off vs. EBU R128:
- **Pro:** very small per-track adjustments. Modern masters all peak
  near 0 dBFS, so corrections typically land within fractions of a dB.
  Minimal interference with the user's chosen volume; the Naim's
  hardware volume swings barely at all between tracks.
- **Con:** does **not** equalise *perceived* loudness. Quiet classical
  vs. loud rock both get ~0 dB correction even though they sound very
  different. If perceptual loudness equalising becomes the goal, switch
  back to LUFS-driven gain (the `lufs` column is still captured, so no
  re-scan needed for that switch).

### Reference target

`TARGET_PEAK_DBTP = -1.0` — typical audiophile choice; keeps 1 dB of
safety headroom under 0 dBFS so inter-sample peaks can't clip the DAC
or the renderer's downstream chain. Defined in `dlna_loudness.py`.

`_MAX_ABS_GAIN_DB = 2.0` — hard clamp on per-track adjustment. Prevents
an outlier (e.g. an unusually quiet vinyl rip at -10 dBFS peak) from
producing a +9 dB jump that would be jarring between tracks.

### `track_loudness` table — survives `clear(udn)`

```sql
CREATE TABLE IF NOT EXISTS track_loudness (
  url        TEXT PRIMARY KEY,   -- matches tracks.url; orphans harmless
  lufs       REAL,               -- integrated loudness (informational); NULL ok
  peak_db    REAL,               -- true peak (dBTP); NULL on scan failure
  gain_db    REAL DEFAULT 0.0,   -- = TARGET_PEAK_DBTP - peak_db (clamped ±2)
  scanned_at INTEGER NOT NULL    -- epoch seconds
);
```

Same persistence pattern as `album_art` and `play_counts` — independent
of `tracks`, so a rebuild-index doesn't trigger a full re-scan.
**`clear(udn)` deliberately leaves this table alone.**

The `peak_db` column was added on 2026-05-06 when the gateway switched
from LUFS-based to peak-based normalisation; the migration in
`dlna_library._setup` adds the column AND wipes existing rows so the
scanner re-analyses every track and stores both values together.

### `LoudnessScanner` background worker

Mirrors `AlbumArtFetcher` (see `dlna_art_fetcher.py:98-212`). Public
surface in `dlna_loudness.py`:

- `bare_tracks() → [(url,)]` — tracks with no `track_loudness` row.
- `run_once()` — drain in batches of 50; re-queries between batches so
  triggers arriving mid-run are absorbed into the current pass.
- `_analyze(audio_src) → (lufs, peak_db)` — subprocess
  `ffmpeg -nostats -i {audio_src} -af ebur128=framelog=quiet:peak=true
  -f null -`, parses both the `Integrated loudness:` and `True peak:`
  summary blocks. Either field may be `None` on parse failure;
  **only `peak_db is None` marks the scan as a failed/sticky-negative.**
- `trigger()` / `start_initial_scan(delay=120)` / `stop()` — same
  contract as `ART_FETCHER`.

CPU posture: **single thread, `os.nice(10)`.** ~1 sec per track. A
5000-track library is ~80 min once; subsequent runs hit only new tracks.

### Sticky negative cache

Failed scans (unreadable file, ffmpeg crash) get a row with
`peak_db=NULL, gain_db=0.0` so we don't retry every restart — same
convention as `album_art.source='notfound'`. To force a retry on a
single track:

```sql
DELETE FROM track_loudness WHERE url = '...' AND peak_db IS NULL;
```

### UPnP `RenderingControl` — new SOAP helpers

Added to `dlna_avtransport.py` (the service is distinct from
AVTransport):

- `set_volume(rc_url, level: int) → bool` — clamped 0-100; sends
  `urn:schemas-upnp-org:service:RenderingControl:1#SetVolume` with
  `Channel=Master`.
- `get_volume(rc_url) → int | None` — used **once per RendererQueue**
  on first play to adopt whatever the user has set on the renderer's
  own remote as the reference.

The renderer's RenderingControl URL is sourced from the device
description XML during discovery (`dlna_discovery.py` extends
`_RendererInfo`).

### Per-track gain via SetVolume

`dlna_player.py` constant: `GAIN_TO_VOLUME_RATIO = 2` (one Naim
volume-unit ≈ 0.5 dB; **approximation** — the renderer's curve is
logarithmic and renderer-specific). Tune by ear after first listen.

`RendererQueue._send_current()` (the per-track hook):

1. If `self._user_volume is None` → `get_volume(rc_url)` once, cache.
2. Look up `gain_db = DB.gain_db_for_url(t["url"])`.
3. `level = clamp(0, 100, _user_volume + round(gain_db *
   GAIN_TO_VOLUME_RATIO))`.
4. `set_volume(rc_url, level)` → then `avtransport_send` (SetURI+Play).

### `/api/control` UPnP volume

Previously rejected with "Unknown device" (api_playback.py:273).
Implemented:

```
POST /api/control
{"action": "volume", "value": 65, "device": "upnp:<udn>"}
```

→ `QUEUES.get(udn).set_user_volume(65)` — pushes immediately to the
renderer AND updates the reference for next-track gain math.

### `/api/loudness/status` — visibility endpoint

```json
{ "scanned": 1234, "total": 4321, "in_progress": true,
  "target_peak_dbtp": -1.0 }
```

Routed via `dlna_routes.GET_ROUTES`; future PWA work will surface the
progress in the index bar.

### Caveats

- **Gateway is not in the audio path for Naim** — we can only adjust
  SetVolume; we cannot apply DSP. This is a hardware reality, not a
  limitation of the approach.
- **The Naim's own remote will fight us.** A nudge on the Naim's remote
  is undone on the next track's `set_volume`. Document; not solvable
  without a poll-then-adjust loop that itself would lag.
- **Renderer volume curve is non-linear** — `GAIN_TO_VOLUME_RATIO = 2`
  is a guess; tune by ear.

### Tests

| File | What it covers |
|---|---|
| `tests/test_loudness.py` | `_parse_ebur128` + `_parse_true_peak` (incl. `+`-prefixed positive peaks), `bare_tracks` query (excludes already-scanned / negative-cache), `clear(udn)` survival, `run_once` writes lufs + peak_db + gain, failed-scan negative cache (`peak_db IS NULL`), gain clamped ±2 dB, `trigger()` idempotent, `start_initial_scan`, `gain_db_for_url` helper |
| `tests/test_avtransport_volume.py` | 9 tests — `set_volume` body shape (RenderingControl namespace, `<Channel>Master</Channel>`, `<DesiredVolume>`), clamping 0/100, SOAP-fault and connection-error paths; `get_volume` parses `<CurrentVolume>`, returns None on fault/garbled/error |
| `tests/test_player_volume.py` | 9 tests — first play calls GetVolume once then SetVolume, subsequent tracks skip GetVolume, gain math with RATIO=2, clamp at 0/100, no-row → reference passed through, `set_user_volume` updates reference + fires SetVolume immediately and is sticky for next track |
| `tests/frontend/test_vol_extras.py` | Extended: tighter UPnP volume body assertion (`device="upnp:<udn>"` required); new `test_loudness_status_endpoint` asserts `/api/loudness/status` shape |
| `tests/run_all.py` | Live-gateway integration: `GET /api/loudness/status` returns the four expected fields with right types |

## Metadata enrichment via AcoustID (Phase 1, in flight)

Background worker that fingerprints library tracks via Chromaprint's
`fpcalc`, resolves the fingerprint to MusicBrainz metadata through the
AcoustID API, and writes the result back into `metadata_overrides`.
Fixes mistagged or untagged tracks (`title="Track 03"`, missing artist,
etc.) **without ever rewriting the on-disk file tags** — only the
gateway's SQLite cache is touched. This is deliberate: confirmed
2026-05-25 that nothing browses AssetUPnP directly, so the gateway
PWA / Subsonic clients / Naim-via-gateway-playlists are the only
consumers, and they all see `metadata_overrides` via the existing
COALESCE pass in `upsert_tracks`. File-tag rewriting via mutagen is
deferred to a possible Phase 2.

### `dlna_acoustid.AcoustIDFetcher` — background worker

Mirrors `LoudnessScanner` (`dlna_loudness.py`) and `AlbumArtFetcher`
(`dlna_art_fetcher.py`) line-for-line in lifecycle surface: `trigger()`,
`stop()`, `start_initial_scan(delay=120)`, `status()`. Daemon thread,
batched drain (`_BATCH_SIZE=50`) with `bare_tracks` re-queried between
batches so triggers arriving mid-run get absorbed into the current
pass. `os.nice(10)` so it doesn't steal CPU from playback.

### What enters `metadata_overrides`

Four sources, distinguished by the `source` column (migration in
`LibraryDB._init_schema`):

| source | Written by | Carries data |
|---|---|---|
| `manual` | `LibraryDB.update_track_meta()` (user edits in the PWA) | yes |
| `acoustid` | `AcoustIDFetcher` on a confident match | yes (artist / album / title) |
| `notfound` | `AcoustIDFetcher` on a permanent miss (no confident match, fpcalc-fail, AcoustID HTTP 4xx) | sentinel; all metadata columns NULL |
| `video_skip` | `AcoustIDFetcher` when the URL ends in a video extension (`.mp4` etc.) | sentinel; not fingerprinted |

**Existence of a row of ANY source means "we've processed this URL".**
That's how `bare_metadata_tracks()` decides what to fingerprint — it
excludes URLs that already have any override row, including all
sticky sentinels. There is intentionally no separate `meta_update`
flag on `tracks`; the override row is the single source of truth.

### Transient AcoustID failures stay bare

**Critical correctness rule**, regression-guarded by
`TestRunOnceTransientBehaviour`:

- HTTP 5xx responses from AcoustID → `_lookup` raises `AcoustIDTransientError` → the worker leaves the URL bare. NO row is written.
- Network-level errors (DNS, refused, socket timeout, broken pipe) → same.
- HTTP 4xx (invalid key, malformed request) → permanent, sticky `notfound`. Retrying won't help.
- Garbled JSON from a 200 response → permanent, sticky `notfound`.

Why this matters: on 2026-05-25 an AcoustID 503 burst during the first
24k-track pass cached ~10 transient failures as permanent `notfound`
rows, requiring manual cleanup via
`tools/retry_notfound_metadata.py`. The split exists so a future
outage doesn't repeat the bug.

Per-run isolation: `run_once()` keeps an in-memory
`transient_this_run` set so a URL that fails transiently can't
re-queue forever inside the same run (the outer while loop re-queries
`bare_tracks()` between batches; without the set, transient URLs
would spin). User re-runs later when the upstream is healthy.

### Music-video skip (`video_skip`)

`_VIDEO_EXTENSIONS = {.mp4 .m4v .avi .mkv .mov .mpeg .mpg .wmv}`. URLs
with these extensions short-circuit BEFORE fpcalc — the worker writes
a `video_skip` sentinel row, logs `AcoustIDFetcher ⊘ video_skip <url>
— video extension, not fingerprinted`, and moves on. No rate-limit
penalty (no AcoustID call) and the row makes the skip sticky.

To list all skipped videos for manual cleanup:

```sql
SELECT url FROM metadata_overrides WHERE source='video_skip';
```

### Greppable anomaly markers in `gateway.log`

Every error / skip writes a recognisable line:

```
AcoustIDFetcher ✓ <url> → 'artist — title' (score=…)        — success
AcoustIDFetcher ✗ no_match <url> — no confident match, …    — sticky notfound (real miss)
AcoustIDFetcher ✗ fpcalc_fail <url> — fingerprint failed, … — sticky notfound (corrupt source / network)
AcoustIDFetcher ⊘ video_skip <url> — video extension, …     — sticky video_skip
AcoustIDFetcher ↺ transient <url> — HTTP 503, leaving bare  — bare, retry next run
AcoustIDFetcher: HTTP 4xx from AcoustID …                   — permanent (key invalid etc.)
```

Greppable by source token: `grep 'fpcalc_fail\|video_skip\|transient' gateway.log`.

### Confidence threshold

`ACOUSTID_CONFIDENCE_THRESHOLD = 0.85` in `dlna_acoustid.py`. AcoustID
returns matches with 0-1 scores; covers, live versions, and remasters
routinely score in 0.4–0.7 territory, and a wrong-match write is more
damaging than no write. Sub-threshold results are treated as
`notfound`. Tune by ear once running — lowering admits more questionable
matches; raising costs hit rate on genuinely-good fingerprints.

### Partial matches are honoured

AcoustID sometimes returns a title but no album, or an artist but no
title. `LibraryDB.metadata_override_set()` only updates the fields the
caller supplied; the others are left as the indexer originally found
them. So a track that has `title="Track 03"` but a correct
`(artist, album)` gets only the title fixed.

### UNIQUE-constraint collisions

As of 2026-05-25 the `tracks` UNIQUE is **widened** to
`(udn, artist, album, title, bit_depth, sample_rate)` so a 16-bit + 24-bit
copy of the same album coexist as distinct rows. See the
"16/24-bit duplicate handling" section below for the full design.

A residual collision case still exists: AcoustID resolves two different
URLs **with the same bit_depth + sample_rate** to identical metadata
(e.g. the same recording appearing on both an original album and a
compilation, both at 16-bit/44.1kHz). `metadata_override_set` catches
`sqlite3.IntegrityError` on the in-place `tracks` UPDATE and continues —
**the override row is saved either way** (source of truth); only the
live UPDATE is skipped, with a WARN. `upsert_tracks` uses
`UPDATE OR IGNORE` for the same reason — a future re-index's COALESCE
pass silently skips the collider too. User-visible impact: one of the
colliding duplicates keeps its old `tracks`-row metadata in browse
until manually resolved.

### 16/24-bit duplicate handling (`bit_depth` + `sample_rate`)

**Problem (surfaced 2026-05-25):** the user's library has 16-bit and
24-bit copies of several albums. Pre-AcoustID, their metadata differed
enough (different tag-cleanliness) that the narrow
`UNIQUE(udn, artist, album, title)` accepted both. After the AcoustID
worker normalised both to identical metadata, the second copy collided
on every COALESCE update and every re-index — a single collision aborts
the whole indexer-side bulk UPDATE → indexer crashes → empty `tracks`
table.

**Solution (option 7, accepted 2026-05-25):** widen the UNIQUE to
include audio-quality columns, so the two copies are legitimately
distinct rows; then dedup at the **browse-view** layer so the user only
sees the higher-quality version.

Schema:

```
tracks(... , bit_depth INTEGER, sample_rate INTEGER)
  UNIQUE(udn, artist, album, title, bit_depth, sample_rate)
```

`bit_depth` and `sample_rate` are parsed from the URL at insert time
by `_parse_audio_params` (regex `/b(\d+)/f(\d+)/` over the URL). This
is AssetUPnP-specific; non-AssetUPnP servers leave both columns NULL.
SQLite treats NULL as distinct in UNIQUE, so NULL tracks don't collide
with each other either.

**Browse-side filter (`_dedup_clause`):** a SQL `NOT EXISTS` fragment
that excludes the current row when a same-(udn,artist,album,title) row
with strictly higher (bit_depth, sample_rate) exists. NULL is treated
as 0 (lowest) via `COALESCE`, so any non-NULL beats a NULL.

Applied to **browse-view query methods** in `dlna_library`:
`search` (tracks/albums/artists subqueries), `album_tracks`,
`all_albums`, `artist_albums`, `all_artists`, `all_genres`,
`genre_albums`, `genre_tracks`. Track counts in album/artist listings
also reflect the deduped (browse-visible) count.

**NOT applied to:**
- **`playlist_tracks` queries** — playlists already pointing at 16-bit
  URLs stay valid; per user policy "if it's already in the playlists,
  leave it there" (2026-05-25).
- **`radio_tracks`** — the play-count-biased shuffle wants the full
  pool, not the browse-deduped subset.
- **`bare_metadata_tracks`** — the AcoustID worker needs to process
  every URL (both 16-bit and 24-bit copies should get metadata).
- **The UPnP/Subsonic API code paths** still pass through the
  LibraryDB methods, so they pick up dedup automatically.

**Migration (two steps):**

`_migrate_widen_tracks_unique` detects the old narrow UNIQUE via
`sqlite_master`, drops FTS5 triggers, renames the old `tracks` table,
creates the new wide-UNIQUE table, INSERTs every old row with parsed
bit_depth/sample_rate, drops the renamed table, recreates FTS5
triggers, and rebuilds FTS5. Idempotent.

`_migrate_unique_url` follows: when the widened UNIQUE is in place but
no `idx_tracks_udn_url` exists, the indexer can accumulate same-URL
phantom dupes (one row has AcoustID-corrected metadata, the other has
raw-from-AssetUPnP — both legitimate under the wide UNIQUE since their
title/album differ). The migration `DELETE`s duplicates keeping
`MIN(id) per (udn,url)` (the older, corrected row), then `CREATE UNIQUE
INDEX idx_tracks_udn_url ON tracks(udn, url)` to prevent recurrence.
After this, `INSERT OR IGNORE` in the indexer dedups by URL
automatically. Idempotent.

Both migrations run at `LibraryDB.__init__`. **Their log lines don't
appear in `gateway.log`** because LibraryDB is constructed as a
module-level singleton at import time, before `setup_logging` runs.
Verify they ran by counting: `SELECT COUNT(*) = COUNT(DISTINCT url) FROM tracks`
(must be true) and `SELECT name FROM sqlite_master WHERE name='idx_tracks_udn_url'`
(must return a row).

**Caveat — UPDATE OR IGNORE collisions during indexing:** for tracks
that *do* have identical (udn, artist, album, title, bit_depth,
sample_rate) after override application — e.g. the same recording at
the same bit-depth appearing on multiple compilations — the COALESCE
UPDATE in `upsert_tracks` uses `UPDATE OR IGNORE`. This was a separate
fix (2026-05-25) for an indexer crash. Without it, one collision
aborts the entire bulk UPDATE and leaves `tracks` empty after a
`clear(udn)`.

Tests: `tests/test_dedup.py` — 30 tests covering URL parser
(`_parse_audio_params`), widen-UNIQUE migration (idempotent, preserves
rows, backfills, rebuilds FTS, no-op on fresh DB), URL-unique
migration (dedup keeps MIN(id), UNIQUE index created, subsequent
same-URL INSERT skipped, idempotent, fresh DB has the index), browse
dedup (`album_tracks` / `search` / `all_albums` / `artist_albums` /
`all_artists` hide 16-bit when 24-bit exists, higher sample-rate wins
within same bit-depth, NULL loses to non-NULL, two NULLs both
survive), upsert population, and `_dedup_clause` smoke.

### Sticky-negative cache

The `'notfound'` row is sticky by design — same convention as
`album_art.source='notfound'` and `lyrics.source='notfound'`. If you've
since cleaned up the source metadata in AssetUPnP and want the worker
to retry one track:

```sql
DELETE FROM metadata_overrides WHERE source='notfound' AND url='…';
```

Or wholesale retry every miss:

```sql
DELETE FROM metadata_overrides WHERE source='notfound';
```

Those URLs become bare again on the next `ACOUSTID_FETCHER.trigger()`.

### Survives `clear(udn)`

`metadata_overrides` is independent of `tracks` (keyed by URL, no FK),
so a rebuild-index doesn't trigger a full re-fingerprint. Same
invariant as `album_art` / `play_counts` / `lyrics` / `track_loudness`.

### Dependencies

- **Chromaprint `fpcalc`** — required CLI binary. Install via
  `brew install chromaprint`. Worker bails (no rows written) if
  missing; mirror of the `ffmpeg`-missing guard in `LoudnessScanner`.
- **`ACOUSTID_API_KEY`** — free key from acoustid.org, stored in
  `.env`. If unset, the singleton constructs but every `run_once()`
  is a no-op with a one-time WARN — `start_initial_scan` is a no-op
  too so an unconfigured deployment is silent.
- Reuses `GATEWAY_CONTACT_EMAIL` (already set in `.env` for
  MusicBrainz) for the AcoustID User-Agent.

### Wiring (event-driven, no periodic poll)

Matches the `ART_FETCHER` / `LOUDNESS_SCANNER` model — two hooks
only, no background ticker:

1. **Startup mop-up.** `dlna_gateway.main()` calls
   `ACOUSTID_FETCHER.start_initial_scan()` (120s delay) — catches
   tracks left bare by a previous interrupted run. Dormant when
   `ACOUSTID_API_KEY` is unset (logs `initial scan disabled` and
   returns).
2. **Per-crawl tail.** `Indexer._run()` calls
   `ACOUSTID_FETCHER.trigger()` at the success tail (right after
   the `ART_FETCHER` and `LOUDNESS_SCANNER` triggers) so new bare
   tracks from a fresh crawl get fingerprinted immediately. Lazy
   import + `try/except Exception: pass` so a test harness that
   imports `Indexer` without booting the full library doesn't crash.

Manual one-shot (when needed):

```bash
ACOUSTID_API_KEY=… python3 -c "from dlna_library import ACOUSTID_FETCHER; ACOUSTID_FETCHER.run_once()"
```

HTTP endpoints / frontend surface for AcoustID status are not in
this slice.

### Weekly notfound retry — `com.roha.dlna-acoustid-retry`

Weekly LaunchAgent + `retry-acoustid-weekly.sh` wrapper:

1. Run `tools/retry_notfound_metadata.py --all -y` to drop every
   `source='notfound'` row from `metadata_overrides`.
2. `launchctl kickstart -k gui/$(id -u)/com.roha.dlna-gateway` so
   the gateway restarts and its 120s-post-startup
   `ACOUSTID_FETCHER.start_initial_scan()` re-fingerprints all the
   now-bare tracks. MusicBrainz's database keeps growing, so a
   miss six months ago may match today; legit ongoing misses get
   re-cached as notfound automatically.

Script no-ops when `ACOUSTID_API_KEY` is unset (no point clearing
notfound if we can't look anything up). All output appended to
`acoustid-retry.log` (separate from `gateway.log`, parallel to
`cert-renewal.log`).

Schedule: every Monday 05:30 local — one hour after the cert
renewer to avoid two gateway restarts within the same minute.

Install (one-time, after first clone, edit the path placeholder
inside the plist first):

```bash
cp com.roha.dlna-acoustid-retry.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.roha.dlna-acoustid-retry.plist
```

Manual override:

```bash
./retry-acoustid-weekly.sh                                         # do it now
./retry-acoustid-weekly.sh --dry-run                               # preview
launchctl kickstart gui/$(id -u)/com.roha.dlna-acoustid-retry      # dry-run the weekly job
```

### Tests

| File | What it covers |
|---|---|
| `tests/test_acoustid.py` | 64 tests — `_parse_fpcalc_output` (JSON shape, float-rounding, garbled-input → None), `_reconstruct_artist` (joinphrase, ' & ' fallback, blank skip), `_extract_best_match` (threshold gating at boundary, album-over-single preference, multi-result top-score selection, malformed-response → None); `_is_video_url` (every video ext flagged, case-insensitive, audio not flagged, no-extension and substring-in-path not flagged); `_lookup` HTTP transient-vs-permanent split (HTTP 500/503/504 + OSError + socket.timeout + HTTPException → `AcoustIDTransientError`; HTTP 4xx + garbled JSON → None; happy path); `bare_metadata_tracks` excludes `'manual'` / `'acoustid'` / `'notfound'` / `'video_skip'` rows; `clear(udn)` survival; `run_once` writes `'acoustid'` on hit, `'notfound'` on miss, treats fingerprint failure as notfound, transient → leaves URL bare (no row written) + tracked in per-run set so no infinite loop, video URLs skipped before fpcalc + sticky `video_skip` row; `ACOUSTID_API_KEY` unset → no-op; `fpcalc`-missing-at-start and `fpcalc`-disappears-mid-run guardrails don't poison cache; `trigger()` idempotent; `start_initial_scan` skipped when disabled; `status()` shape; partial-match only overwrites supplied fields; LibraryDB method tests: `metadata_override_set` merges with existing row + updates tracks, `mark_notfound` is sticky + never overwrites manual edits |
| `tools/test_retry_notfound_metadata.py` | 8 tests — default mode reports stats only (no deletions); `--all` deletes only `source='notfound'` rows and leaves `manual` / `acoustid` / `video_skip` untouched; `--since TIMESTAMP` deletes only newer notfound rows; `--dry-run` doesn't mutate; `--all` + `--since` rejected as mutually exclusive; missing DB fails cleanly; `_scan_log_for_5xx` counts only HTTP 5xx lines in a synthetic log, returns empty when log absent |
| `tests/test_year.py` | 22 tests — DIDL-Lite year parsing (`<dc:date>` full ISO, year-only, year+month, `<upnp:originalTrackDate>` fallback, garbage → None, out-of-range rejected); schema columns present after migration; upsert stores year; `metadata_override_set` keeps year SEPARATE from tracks.year (override = MB original, tracks = file-tag edition); `track_meta_by_url` returns both; `_renderNpYear` display logic — original preferred, "(remastered)" annotation at 3+ year gap, file-only fallback, no annotation for negative gaps |

### Year (file-tag + MusicBrainz original)

Two-field model in the schema:

- **`tracks.year`** — the **file-tag year** from DIDL-Lite (`<dc:date>` or `<upnp:originalTrackDate>`, first 4 digits, range-clamped 1900–2100). Populated by the indexer at crawl time via `dlna_content._parse_didl`. Reflects the edition of the file the user has on disk (e.g. 2001 for a remastered CD of a 1987 album).
- **`metadata_overrides.year`** — the **MusicBrainz original release year** (release-group `first-release-date`). Populated by `AcoustIDFetcher`:
  - In `run_once`: `_extract_best_match` now also returns `rg_id`; the worker calls `_mb_release_group_year(rg_id)` to fetch the date and writes it through `metadata_override_set(year=...)`.
  - In `run_year_backfill`: walks existing acoustid overrides where `year IS NULL`, searches MB by `(artist, album)` via `_mb_search_year`, writes year to all overrides with that pair. Per-run cache keyed by lowercase `(artist, album)` so a 12-track album costs 1 MB query.
  - Rate-limited to `_MB_RATE_LIMIT_SEC = 1.1` per MB's ToS.

The fields are deliberately stored separately — they mean different things and the frontend renders both:

- **`GET /api/track_meta?url=…`** returns `{title, artist, album, duration, year, year_original}`. `year` is `tracks.year` (edition); `year_original` is `metadata_overrides.year` (MB).
- Frontend (`_renderNpYear` in `static/app.js`):
  - Prefer `year_original` if set.
  - If both set and `year - year_original >= 3`, render as `"YYYY (remastered)"` (e.g. `1987 (remastered)` for a 2001 remaster of a 1987 album).
  - Otherwise show whichever is available; empty string if neither.
  - Race-guarded by `_npYearReqUrl` — successive setNpTrack() calls only let the most recent URL write the field.

### Backfilling year for an existing library

After this feature is deployed onto a DB that pre-dates it, both year columns are `NULL` for every row. Two backfill steps:

1. **`tracks.year`** (file-tag) — triggered by re-running the indexer. Either:
   - PWA → settings → "Rebuild Index" (preferred), or
   - `POST /api/index/rebuild` directly.

   Crawls AssetUPnP, re-parses DIDL-Lite, populates every row's year. ~25 min for 24k tracks. Safe to run alongside playback / other workers.

2. **`metadata_overrides.year`** (MB original) — call `ACOUSTID_FETCHER.run_year_backfill()`:
   ```bash
   ACOUSTID_API_KEY=… GATEWAY_CONTACT_EMAIL=… \
     python3 -c "from dlna_library import ACOUSTID_FETCHER; ACOUSTID_FETCHER.run_year_backfill()"
   ```
   Queries MB once per unique `(artist, album)` pair. ~3 hours for ~9k pairs at MB's 1.1s rate limit.

Both can run concurrently with normal gateway operation. The indexer rebuild repopulates `tracks`; the MB backfill writes only to `metadata_overrides` — no conflict.

## Bit-perfect notes

**Verdict:** the gateway is byte-perfect on every path it controls.
No resampler, EQ, mixer, or DSP exists anywhere in the playback
code. Confirmed by audit on 2026-05-11.

**Naim / UPnP path.** The gateway is not in the audio path. AssetUPnP
serves bytes directly to the renderer; the gateway only sends
`AVTransport::SetURI` + `Play` SOAP. Loudness gain and the user trim
slider are applied via `RenderingControl::SetVolume` SOAP, which
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

**Loudness scanner.** ffmpeg is invoked only for measurement
(`ebur128=peak=true -f null -`) in `dlna_loudness.py:258-295`;
output bytes are discarded, only stderr is parsed. Never on the
playback path.

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

### `tools/retry_notfound_metadata.py`

Cleanup script that deletes bogus `'notfound'` rows from
`metadata_overrides` so the AcoustID worker can re-try them on the
next pass. Written 2026-05-25 after an AcoustID 503 outage left
several transient lookup failures cached as permanent `notfound`
rows. The runtime fix in `dlna_acoustid._lookup` prevents future
poisoning (see "Transient AcoustID failures stay bare" above); this
tool is for cleaning up the once-only damage from the pre-fix run.

#### Defaults & safety

- **Default mode = report only.** No deletions unless `--all` or
  `--since TIMESTAMP` is passed explicitly.
- The tool also scans `acoustid-firstpass.log` (or `--log <path>`)
  for `HTTP 5xx` lines so you can size the bogus-notfound count
  before acting.
- `--all` and `--since` are mutually exclusive (one or the other).
- `--dry-run` shows what would be deleted without acting.
- `-y` / `--yes` skips the confirmation prompt before deleting.

#### Usage

```bash
# Just report what's in metadata_overrides, plus HTTP 5xx count from log:
python3 tools/retry_notfound_metadata.py

# Surgical — delete only notfound rows updated after a known outage start:
python3 tools/retry_notfound_metadata.py --since '2026-05-25 14:30:00'

# Wholesale — delete every notfound (re-runs legit misses too, ~1.5s each):
python3 tools/retry_notfound_metadata.py --all

# Preview before acting:
python3 tools/retry_notfound_metadata.py --all --dry-run

# Non-interactive cleanup (CI / scripts):
python3 tools/retry_notfound_metadata.py --all -y
```

#### Tests

`tools/test_retry_notfound_metadata.py` — 8 tests over a throw-away
SQLite, covering default report-only mode, `--all` scope (only
`notfound` deleted), `--since` scope (only newer notfound deleted),
`--dry-run` no-mutation, mutually-exclusive flag rejection, missing-DB
clean failure, and the `_scan_log_for_5xx` helper.

```bash
python3 -m unittest tools.test_retry_notfound_metadata -v
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
3. **`metadata_overrides` is unaffected** — keyed by URL; the rows for deleted files become orphans, harmless. (Use `tools/retry_notfound_metadata.py` or manual SQL to prune orphans later if desired.)

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
`lyrics` / `track_loudness` / `metadata_overrides`: survives
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

## Subsonic API (Phase 1, in flight)

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
- Album:    `al:<base64(artist + \x00 + album)>`
- Artist:   `ar:<base64(artist)>`
- Playlist: `pl:<plid>` (already opaque in the DB)

Round-trips arbitrary unicode through XML/JSON/URL transports.

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
  `DLNAGateway/1.0 ( hintt@me.com )` pattern.
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
