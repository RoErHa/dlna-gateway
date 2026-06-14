# Video support — runbook (incl. smartphone movies)

> **Status: READY TO IMPLEMENT (next session).** Add video browsing + playback
> to the gateway for **mobile, computer, and TV** clients — explicitly **NOT the
> Naim** (audio-only renderer). Chosen approach: **Option C — hybrid,
> capability-aware**: serve the original bytes to clients that can play them
> (Safari, modern TVs, any H.264) and **transcode on demand only when the client
> can't** (the iPhone-HEVC case). Video transcoding does NOT violate the
> bit-perfect rule — that rule is about *audio* fidelity.

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
   (`dlna_providers/localfs.py`).
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

### Video extensions
`.mp4 .m4v .mov .mkv .webm .avi .3gp .m2ts .mts` (phone footage is `.mov`
[iPhone HEVC] or `.mp4` [Android H.264]). `.mp4` is currently *excluded* from
audio indexing — keep that; video indexing is a separate pass.

### The codec reality (why Option C)
- **iPhone** → HEVC/H.265 in `.mov`. Safari plays it; **Chrome/Firefox + many
  TVs do not** → transcode needed for those clients.
- **Android** → usually H.264/AAC `.mp4` → plays natively everywhere.
- Transcode target: **H.264 (High/Main) + AAC in fragmented MP4** — universal.

---

## Phase V0 — deps + scaffolding
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
   vcodec, acodec, container, mime, size, mtime, created, poster, added_at`.
  Migration in `_init_schema` (idempotent). Run `tools/regen_schema.py` +
  `tests/test_schema_sync.py` gate.
- **Extractor** (`dlna_ffmpeg.probe`): ffprobe `-show_format -show_streams
  -print_format json` → duration/width/height/vcodec(`hevc`/`h264`)/acodec/
  container/creation_time. No-ffprobe fallback: filename title + mtime, codecs
  `unknown`.
- **Poster**: `ffmpeg -ss <10%> -frames:v 1` → JPEG into the poster cache; skip
  if ffmpeg absent (UI shows a generic film icon).
- **Scan**: extend the LocalFs scan (`dlna_providers/localfs.py`) to also walk
  video extensions into `videos` (same mtime/size cache so re-scan is cheap).
  Keep audio + video passes independent.
- **DB methods**: `upsert_videos(udn, rows)`, `all_videos(udn)` (newest-first /
  by folder), `video_by_id(id)`, `clear_videos(udn)`.
- **Tests** (`tests/test_video_index.py`): probe-JSON parse (h264/hevc/garbled/
  no-ffprobe), `videos` round-trip + migration idempotent, scan finds video +
  skips audio, schema-sync gate.

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
- **Tests**: `tests/test_video_serve.py` (Range 206/416, MIME, DLNA headers,
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
