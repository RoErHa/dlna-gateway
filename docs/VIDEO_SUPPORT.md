# Video support — design runbook + as-built record (incl. smartphone movies)

> **Status: SHIPPED AND LIVE (all phases V0–V5).** This began as a
> forward-looking runbook; the plan below is kept because its design
> rationale is still the best explanation of *why* the feature is shaped
> this way. **Read the phase ledger immediately below for what actually
> landed and where** — the phase sections themselves are the original
> plan and still use future tense.
>
> Video browsing + playback for **mobile, computer, and TV** clients —
> explicitly **NOT the Naim** (audio-only renderer). The approach chosen
> and built: **Option C — hybrid, capability-aware**: serve the original
> bytes to clients that can play them (Safari, modern TVs, any H.264) and
> **transcode on demand only when the client can't** (the iPhone-HEVC
> case). Video transcoding does NOT violate the bit-perfect rule — that
> rule is about *audio* fidelity.

### As-built ledger (checked 2026-08-20)

| Phase | Status | Where it landed |
|---|---|---|
| V0 — deps + scaffolding | ✅ | `dlna_ffmpeg.py` (ffprobe/ffmpeg helpers, both **optional** — absent degrades, never crashes). |
| V1 — index | ✅ | `videos` table + `dlna_video_index.scan_videos`, a 5-min periodic scan kicked off from `dlna_localfs_wiring`. Enabled by `LOCALFS_VIDEO_ROOT`. |
| V2 — serve + browser playback | ✅ | `GET /localfs/video/<id>` (Range) + the same-origin `/api/video…` routes in `dlna_asgi_video.py`, so iOS will play them. PWA player in `static/app.js`. |
| V3 — capability-aware transcode | ✅ | On-demand HLS via `dlna_ffmpeg` + `dlna_asgi_video.py`. |
| V4 — TV browse (DMS) | ✅ | The 📹 Videos tree in `api_upnp_browse_video.py` — by date / location / person / all — browsed by the LG WebOS TV. Kept out of the Naim's audio tree. |
| V5 — polish | ✅ | Reverse-geocoded titles (`dlna_geocode.py`, `dlna_countries.py`), location inference for GPS-less clips (`tools/infer_video_locations.py`), Immich person tags (`tools/immich_people_sync.py`), the importer (`tools/immich_import.py`). |

**Tests as built** (the names below in the phase sections were provisional):
`tests/test_video_db.py` (10), `tests/test_video_scan.py` (21),
`tests/test_video_loc_overrides.py` (20), `tests/test_video_people.py` (7),
`tests/test_upnp_videos_browse.py` (29) — 87 in total, all in the offline gate.

**Config:** `LOCALFS_VIDEO_ROOT` (enables the feature), `VIDEO_SCAN_INTERVAL_SEC`
(default 300), `VIDEO_POSTER_DIR`, and — for the Immich tools only —
`IMMICH_URL` / `IMMICH_API_KEY`. All in `.env`; see `.env.example`.

**Goal:** a "📹 Videos" section in the PWA that plays your library's video —
crucially the **iPhone/Android clips** (DCIM footage) — in the browser, with
optional cast-to-TV. The byte-serving is nearly free (the LocalFs server already
does Range); the real work is metadata, the UI player, and the HEVC transcode
fallback.

---

## Design decisions (locked for this runbook)

1. **Separate `videos` table** (NOT extending `tracks`). Videos have distinct
   metadata (resolution, codecs, capture date) and must stay out of audio
   browse + the Naim's UPnP tree.
2. **id = `sha1(rel_path)`** — same path-stable scheme as LocalFs tracks
   (`_track_id_for` in `dlna_providers/localfs_tags.py`).
3. **`ffprobe` + `ffmpeg` are OPTIONAL binaries** — discovered via the existing
   Homebrew-path `_find_*()` pattern (like `fpcalc`). Absent → metadata falls
   back to filename/mtime and the transcode endpoint 503s (native-only still
   works). Graceful degradation, matching the project's optional-binary ethos.
4. **Serving reuses the LocalFs Range machinery** (`dlna_localfs_server.py`).
5. **Capability-aware playback**: the PWA tries the **native** stream first
   (pre-checked with `video.canPlayType()` / `MediaSource.isTypeSupported`) and
   falls back to the **transcoded** stream on a decode/`error` event.
6. **Naim/`/gw` untouched** — videos are NOT exposed in the gateway-as-Media­
   Server ContentDirectory (audio-only for the Naim). A video-capable DMS for
   TVs that browse the gateway is an optional later add (V5+), not in scope here.
7. **Separate video root** — videos live in their OWN folder,
   **`/Volumes/SAMDATA/GWMovies`**, distinct from the music root
   `/Volumes/SAMDATA/Music` (`LOCALFS_MUSIC_ROOT`). New config var
   **`LOCALFS_VIDEO_ROOT`** (env) / `localfs.video_root` (config.json), default
   **unset = video disabled** (mirrors how `LOCALFS_MUSIC_ROOT` gates audio).
   The video scan walks this root ONLY — fully independent of the music scan, so
   there's no extension-filtering against the music tree and the two never mix.
   Videos get their own synthetic udn (`uuid:localfs-movies`) to keep them
   cleanly separable from the audio LocalFs source.

### Video extensions
`.mp4 .m4v .mov .mkv .webm .avi .3gp .m2ts .mts` (phone footage is `.mov`
[iPhone HEVC] or `.mp4` [Android H.264]). `.mp4` is currently *excluded* from
audio indexing — keep that; video indexing is a separate pass.

### The codec reality (why Option C)
- **iPhone** → HEVC/H.265 in `.mov`. Safari plays it; **Chrome/Firefox + many
  TVs do not** → transcode needed for those clients.
- **Android** → usually H.264/AAC `.mp4` → plays natively everywhere.
- Transcode target: **H.264 (High/Main) + AAC in fragmented MP4** — universal.

### Metadata + display title
Extract per video (via `ffprobe`, with graceful fallback when absent):
- **date + time (capture):** `format.tags.creation_time` (ISO 8601). iPhone/
  Android set this. Fallback: file mtime.
- **duration:** `format.duration`.
- **location:** GPS from `format.tags.location` / the Apple QuickTime
  `com.apple.quicktime.location.ISO6709` tag — these are **coordinates** (e.g.
  `+52.3676+004.9041/`), NOT a place name. Store the raw coords in `location`.
  **ALWAYS reverse-geocode to a place name (`location_name`, e.g. "Amsterdam")
  when online** — not optional. Use an online geocoder (Nominatim/OSM:
  free, no key) with the same discipline as the MusicBrainz/art fetchers — a
  contact User-Agent, ~1 req/sec rate limit, and a persistent cache (a
  `geocode_cache` table keyed by rounded coords, sticky like
  `album_art`/`song_year_cache`) so each place is fetched once, ever. Only when
  genuinely offline / the lookup fails does it degrade to coords-or-nothing.
- **title:** `format.tags.title` if present (rare on phone footage).

**Display title rule (country added 2026-07-06):** use the embedded `title`
if present; otherwise build
**`<country>_<location>_<YYYYMMDD>_<HHMM>.<ext>`** from the above —
`<country>` = the ISO country code, uppercase, from the same Nominatim
response (`geocode_cache.country`; pre-country cache rows are upgraded with
one re-fetch on next use); `<location>` = `location_name` if geocoded, else
the raw coords, else omit; `<YYYYMMDD>_<HHMM>` from capture time (or mtime);
`<ext>` = the file extension. Examples: `NL_Amsterdam_20260614_1430.mov`,
`PT_São Vicente_20250729_1106.mov`, `20260614_1430.mp4` (no GPS).
Compute the fallback once at index time and
store it in `videos.title` so browse/sort/search are simple.

---

## Phase V0 — deps + scaffolding
- **Config:** add `LOCALFS_VIDEO_ROOT` (env) / `localfs.video_root`
  (config.json) → `/Volumes/SAMDATA/GWMovies`. Unset = video disabled. Wire it
  in `dlna_config.py` + the LocalFs wiring (`dlna_localfs_wiring.py`) alongside
  `LOCALFS_MUSIC_ROOT`; add it to the LaunchAgent plist when enabling. (The two
  roots are independent; a missing/unmounted GWMovies just disables video, like
  an unmounted music drive disables audio.)
- Add `ffprobe`/`ffmpeg` discovery helper (mirror `fpcalc` finding in the
  enrichment tools: explicit Homebrew paths + `PATH`, since launchd has a minimal
  PATH). One module, e.g. `dlna_ffmpeg.py` with `find_ffmpeg()` / `find_ffprobe()`
  + `probe(path) -> dict` + `extract_poster(path, out, t)` + a `transcode_cmd(path)`
  builder. Pure-ish, unit-testable (mock `subprocess`).
- Decide poster cache location: reuse the `dlna_art_cache.py` on-disk-cache
  pattern → `video_posters/` (gitignored), keyed by video id.

## Phase V1 — index videos (backend, TEST-FIRST)
- **Schema** (`dlna_library.py`): new `videos` table —
  `id TEXT PK, udn, url, title, file_path, folder, duration, width, height,
   vcodec, acodec, container, mime, size, mtime, created, location,
   location_name, poster, added_at`. `created` = capture date/time (ISO),
   `location` = raw GPS coords (ISO6709), `location_name` = geocoded place
   (nullable), `title` = embedded title OR the constructed
   `<location>_<YYYYMMDD>_<HHMM>.<ext>` fallback. Migration in `_init_schema`
   (idempotent). Run `tools/regen_schema.py` + `tests/test_schema_sync.py` gate.
- **Extractor** (`dlna_ffmpeg.probe`): ffprobe `-show_format -show_streams
  -print_format json` → duration / width / height / vcodec(`hevc`/`h264`) /
  acodec / container / **creation_time** / **location (ISO6709 GPS)** /
  **embedded title**. Then compute the **display title** (embedded, else
  `<location>_<YYYYMMDD>_<HHMM>.<ext>`). No-ffprobe fallback: title from
  filename, `created` from mtime, codecs `unknown`, location empty.
- **Reverse-geocode (always when online):** resolve `location_name` from the GPS
  coords via Nominatim — contact UA + ~1 req/sec + a persistent `geocode_cache`
  (keyed by rounded coords, one fetch per place ever). Run it at index time so
  the constructed title gets the place name. Only an offline/failed lookup falls
  back to coords (or omits location).
- **Poster**: `ffmpeg -ss <10%> -frames:v 1` → JPEG into the poster cache; skip
  if ffmpeg absent (UI shows a generic film icon).
- **Scan**: a SEPARATE pass that walks **`LOCALFS_VIDEO_ROOT`
  (`/Volumes/SAMDATA/GWMovies`)** only — its own scanner (own mtime/size cache,
  own `uuid:localfs-movies` udn), fully independent of the music scan over
  `LOCALFS_MUSIC_ROOT`. Reuse the LocalFs walk/cache helpers but point them at
  the video root; no extension-filtering against the music tree. **Runs
  PERIODICALLY**: an initial scan at boot, then every `VIDEO_SCAN_INTERVAL_SEC`
  (default 300 s) — incremental (skips unchanged, prunes removed) so new clips
  appear without a restart and a steady library is near-free.
- **DB methods**: `upsert_videos(udn, rows)`, `all_videos(udn)` (newest-first /
  by folder), `video_by_id(id)`, `clear_videos(udn)`.
- **Tests** (planned name; shipped as `tests/test_video_db.py` +
  `tests/test_video_scan.py`): probe-JSON parse (h264/hevc/garbled/
  no-ffprobe) incl. creation_time / duration / ISO6709 location; **display-title
  construction** (embedded title wins; else `<location>_YYYYMMDD_HHMM.ext`; coords
  fallback; no-location omits the prefix; no-creation_time → mtime); `videos`
  round-trip + migration idempotent; scan finds video + skips audio;
  **reverse-geocode cache** (mocked Nominatim: hit cached once, sticky, offline
  → coords); schema-sync gate.

> **PRIVACY (added after the 2026-08-20 security audit).** Reverse-geocoding
> means your clips' **GPS coordinates leave the machine automatically** —
> every video scan, at ~1 m precision, with the contact email from `.env` in
> the User-Agent as Nominatim's ToS requires. For home videos that is your
> home and holiday locations, correlatable with an identifying email, held by
> a third party. It is inherent to turning coordinates into place names, and
> it is bounded (cache-first, so one lookup per distinct coordinate ever;
> ~1 req/sec). The opt-out is not enabling video at all — leave
> `LOCALFS_VIDEO_ROOT` unset — or pre-tagging locations manually via
> `video_location_overrides` and never letting a GPS-bearing file be scanned.
>
> **New outbound host:** Nominatim/OSM (`nominatim.openstreetmap.org`) for
> reverse-geocoding — add it to CLAUDE.md's "External services" table at V5 docs,
> with the contact-UA + 1 req/sec usage-policy note.

## Phase V2 — serve + native browser playback (TEST-FIRST backend, then UI)
- **Serve** (`dlna_localfs_server.py`): `GET /localfs/video/<id>` — resolve via
  `videos`, stream original bytes with the SAME Range/206/416 machinery as
  `/localfs/stream`, correct `Content-Type` (video/mp4, video/quicktime,
  video/x-matroska…) + DLNA `contentFeatures.dlna.org`/`transferMode`. (Reuse the
  existing range helper; just a second id resolver + a video MIME map.)
- **Read API** (`api_browse.py` / `api_playback.py` + `dlna_asgi.py` native
  routes): `GET /api/videos` (list: id, title, folder, duration, w/h, codecs,
  poster url, native-playable hint), `GET /api/video_meta?id=`, `GET /video_poster?id=`.
- **PWA** (`static/index.html` + `app.js`): a "📹 Videos" entry (synthetic row
  like "📡 Stations", or a new tab) → poster grid → a `<video>` modal player
  (native source `/localfs/video/<id>`, fullscreen, scrub, `MediaSession`).
- **Tests** (planned name; the shipped coverage lives in
  `tests/test_video_scan.py` + `tests/test_localfs_server.py`) — Range 206/416, MIME, DLNA headers,
  unknown id 404); `tests/frontend/test_video.py` (Videos panel renders, click →
  player opens with the right `src`, requests assert).

## Phase V3 — capability-aware transcode (Option C core, TEST-FIRST)
- **Transcode endpoint** (`dlna_localfs_server.py` or a small relay in
  `dlna_asgi`): `GET /localfs/transcode/<id>` → `ffmpeg -i <file> -c:v libx264
  -preset veryfast -c:a aac -movflags frag_keyframe+empty_moov+default_base_moof
  -f mp4 pipe:1` streamed to the response (`video/mp4`, `Connection: close`).
  Start with **progressive fragmented MP4** (simple; limited mid-file seek);
  note **HLS** (`-f hls` + segment serving) as the seek-robust V5 upgrade.
  ffmpeg absent → **503** (native-only still works).
- **Client decision** (`app.js`): from `/api/video_meta` codecs, pre-check with
  `video.canPlayType()`/`MediaSource.isTypeSupported`. If native-playable → use
  `/localfs/video/<id>`. Else (or on a native `error`/decode event) → swap `src`
  to `/localfs/transcode/<id>`. Belt-and-braces: pre-check **and** `onerror`
  fallback.
- **Tests**: transcode-cmd builder (codecs, flags) unit-tested with mocked
  subprocess; endpoint 503 when ffmpeg missing; `tests/frontend/test_video.py`
  asserts the fallback swaps to `/localfs/transcode/...` when canPlayType says no.

## Phase V4 — cast to TV (UPnP renderer; NOT the Naim)
- Reuse the OUT picker + `dlna_avtransport`/`dlna_player`: `SetURI` the video URL
  (native `/localfs/video/<id>` for capable TVs, else `/localfs/transcode/<id>`)
  + `Play`, with DLNA **video** `protocolInfo` / `contentFeatures` (e.g.
  `DLNA.ORG_PN=AVC_MP4_…`) — fiddly per-TV, iterate against the real LG.
- **Exclude audio-only renderers** (the Naim): don't offer "cast video" to a
  renderer not flagged video-capable (or let the user try and surface the TV's
  failure). Keep video out of the gateway's `/gw` tree so the Naim never sees it.
- **Tests**: protocolInfo string, renderer-send path, audio-only exclusion.

## Phase V5 — polish (optional / iterative)
- Group by **capture date / folder** (DCIM); newest-first default; date headers.
- `MediaSession` (poster as artwork, title), fullscreen, orientation handling.
- **HLS** transcode for robust seeking (replaces progressive for the fallback).
- Optional: a **video-capable DMS** so TVs can browse the gateway for videos
  directly (separate from the Naim audio tree).
- Docs (CLAUDE.md "Video" section) + `tools/gen_architecture_pdf.py` + regen PDF.

---

## Process reminders (project standards)
- **Test-first** for the frontend + regression-prone backend (write tests, then
  code). Run the full gate before every commit:
  `python tests/run_all.py --offline` + `.venv/bin/pytest tests/frontend`.
- ffprobe/ffmpeg stay **optional** — every path must degrade gracefully when
  they're absent (no hard dependency; the gateway is audio-first).
- Commit on `2.0`, merge to `main`, deploy via `launchctl kickstart -k`.
- Keep videos entirely out of the **Naim** path (UPnP `/gw` + audio renderers).

## First-session starting point
Phase V0 + V1 (deps helper + `videos` table + ffprobe extractor + scan, all
test-first) is the clean first chunk — backend-only, no UI risk, fully gated.
V2 (serve + a minimal `<video>` player) is the first visible result.
