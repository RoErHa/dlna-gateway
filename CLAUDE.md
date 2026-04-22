# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

DLNA Gateway is a Python-based UPnP/DLNA music library gateway. It discovers UPnP MediaServers (AssetUPnP, MinimServer, Jellyfin, Plex) on the local network, indexes their music into a local SQLite DB, and exposes a PWA web UI for browsing and playback. Playback targets: IINA (Mac), Chromecast, UPnP MediaRenderers (Naim Uniti, etc.), and browser audio. The gateway also announces itself as a UPnP MediaServer so UPnP renderers can browse its playlists directly.

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

```bash
python tests/run_all.py            # full suite (requires gateway running on localhost:8765)
python tests/run_all.py --offline  # file-level checks only (no server needed)
python tests/run_all.py http://192.168.1.x:8765  # custom gateway URL
```

Each core module also has a standalone self-test:

```bash
python dlna_config.py              # config/logging
python dlna_discovery.py           # SSDP discovery (20s live scan)
python dlna_content.py <control-url>  # UPnP SOAP
python dlna_library.py             # DB operations
python db_pool.py                  # concurrent DB stress test
python dlna_player.py              # IINA/mpv control
python dlna_cast.py                # Chromecast discovery
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
6. Chromecast discovery (mDNS/zeroconf via pychromecast)
7. HTTP server (ThreadingMixIn, handles all API and static file requests)
8. Optional HTTPS server (separate thread, redirects HTTP→HTTPS)

### Module Responsibilities

| File | Responsibility |
|---|---|
| `dlna_gateway.py` | Main entry point, wires modules, starts all threads |
| `dlna_server.py` | Threaded HTTP server, routes `/api/*` to api_* modules, serves static PWA files |
| `dlna_discovery.py` | SSDP discovery; `ServerRegistry`/`RendererRegistry` thread-safe device stores |
| `dlna_library.py` | SQLite library index + FTS5 search; `LibraryDB` (db_pool-backed) + `Indexer` crawler |
| `db_pool.py` | SQLite connection pool — WAL mode, thread-local connections, write serialization |
| `dlna_config.py` | Constants (`DB_FILE`, `CFG_FILE`, `LOG_FILE`), logging setup, config load/save |
| `dlna_content.py` | UPnP ContentDirectory SOAP client (`cd_browse`, `cd_search`) + AVTransport sender |
| `dlna_player.py` | IINA/mpv launcher, JSON IPC control, HTTP stream proxy |
| `dlna_cast.py` | Chromecast registry + queue playback; lazy-loads pychromecast |
| `api_browse.py` | Browse/search API endpoints |
| `api_playback.py` | Playback, streaming, player state, indexer management endpoints |
| `api_playlists.py` | Playlist CRUD endpoints |
| `api_upnp.py` | UPnP service descriptors + SOAP ContentDirectory (for Naim Uniti browsing gateway playlists) |

### Key Module-Level Singletons

These are shared state across all request handler threads:

- `dlna_discovery.SERVERS` / `RENDERERS` — device registries
- `dlna_library.DB` / `INDEXER` / `DEVICE_ROLES` — library DB, crawler, device role cache
- `dlna_cast.CAST_DEVICES` / `CAST_QUEUE` — Chromecast registry + queue player
- `dlna_player.PLAYER` / `RENDERER_QUEUE` — IINA state + UPnP renderer queue

### Database Schema

SQLite at `library.db`, WAL mode, accessed via `db_pool.Pool`:

```
tracks(id, udn, obj_id, url, title, artist, album, duration, art, mime, genre, file_path)
  UNIQUE(udn, artist, album, title)
tracks_fts — FTS5 virtual table over (title, artist, album)
metadata_overrides(url, artist, album, title, genre, updated_at)
index_meta(udn, indexed_at)
playlists(id, name, created_at, sort_order)
playlist_tracks(id, pl_id, url, title, artist, album, duration, art, added_at)
  UNIQUE(pl_id, url)
device_roles(udn, name, location, host, is_server, is_renderer, first_seen, last_seen)
album_art(artist, album, art_url, source, updated_at)
  PRIMARY KEY (artist, album)
  source ∈ {'sibling', 'musicbrainz', 'notfound', 'manual'}
```

### Frontend

`static/index.html` + `static/app.js` (PWA, ~71K lines). Communicates with backend via `/api/*` JSON endpoints. Features: letter bar, browse modes, playlist management, MediaSession API, Service Worker offline support. Dark theme with amber accents (`static/app.css`).

### Concurrency Notes

- All DB writes are serialized through `db_pool`'s write lock; reads use thread-local connections in WAL mode.
- SOAP calls to UPnP servers are throttled by a semaphore (max 3 concurrent) in `dlna_content.py` to avoid overwhelming servers like AssetUPnP.
- Device registries use `threading.Lock` for thread-safe access.

## Dependencies

All optional — the gateway degrades gracefully if missing:

```
rich>=13.7.0              # colored terminal logging
python-json-logger>=2.0.7 # structured JSON logging
PyChromecast>=14.0.0      # Chromecast support
```

Standard library only for core UPnP functionality.

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

`dlna_player.RendererQueue` emits a fixed-format per-track line for every start and end, regardless of the reason. Greppable prefixes:

```
RendererQueue ▶ START [idx/total] 'title' — artist / album (durS) → renderer
RendererQueue ■ END   [idx/total] 'title' played NNs/durS reason=<reason>
RendererQueue ✗ SEND FAILED [idx/total] 'title' — SetURI/Play returned False (url=…)
RendererQueue ⚠ ABORT N consecutive send failures — stopping queue
```

Reason tags on END lines:

| reason | Where it comes from |
|---|---|
| `finished` | Renderer state went PLAYING → STOPPED naturally (end of track) |
| `user_next` | `RENDERER_QUEUE.next_track()` — UI pressed Next |
| `user_prev` | `RENDERER_QUEUE.prev_track()` — UI pressed Prev |
| `user_stop` | `RENDERER_QUEUE.stop()` — UI pressed Stop |
| `queue_replaced` | `RENDERER_QUEUE.start()` — a new queue was posted while the old one was still playing |
| `send_failed` | `avtransport_send()` returned False (SOAP fault on SetURI or Play) |

### Why SEND FAILED matters

Previously `_send_current()` ignored the return value of `avtransport_send()`. When SetURI failed, the renderer stayed STOPPED, the monitor saw STOPPED, called `_advance()`, sent the next track, which also failed — silently chewing through every track in the queue until the user hit kickstart. The symptom was "all 35 songs skipped, nothing in the log, only a restart fixes it."

The fix has two parts:
1. `_send_current()` now checks the SOAP return and emits `✗ SEND FAILED` with the track URL for every failure.
2. `RendererQueue._MAX_CONSECUTIVE_FAILS = 5` caps the damage: after 5 consecutive SetURI/Play failures the queue aborts itself with `⚠ ABORT`. A transient blip (1–4 failures) auto-advances past the bad track and resets the counter on the next success.

## External services (outbound HTTP)

The gateway is LAN-only except for album-art lookups. Two hosts are contacted, both over TLS:

| Host | Purpose | Method + path |
|---|---|---|
| `musicbrainz.org` | Resolve `(artist, album)` → release-group MBID | `GET /ws/2/release-group/?query=…&fmt=json&limit=5` |
| `coverartarchive.org` | Confirm a front cover exists for that MBID | `HEAD /release-group/{mbid}/front-500` — 200/301/302/307 counts as "have it", 404 counts as "no cover" |

Required contract:

- **User-Agent** — `_MB_USER_AGENT = "DLNAGateway/1.0 ( hintt@me.com )"` in `dlna_library.py`. MusicBrainz's ToS demands an identifying UA with contact info; anonymous calls get 403-blocked.
- **Rate limit** — `_MB_RATE_LIMIT_SEC = 1.1` between calls, enforced in `AlbumArtFetcher.run_once()`. MB allows 1 req/sec sustained; 1.1s gives a small safety margin.
- **Timeout** — `_MB_TIMEOUT = 10.0` per connection. Exceptions inside `_mb_lookup_cover()` are caught and returned as `None` (album gets cached as `notfound`).
- **No retries** — a transient failure ends up as `notfound` and stays sticky; see the "Sticky notfound cache" subsection above for how to force a retry.
