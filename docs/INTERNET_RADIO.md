# Internet radio

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

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
