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
