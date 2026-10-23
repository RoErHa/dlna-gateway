# Frontend (PWA) — the full write-ups

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

### Frontend

`static/index.html` + `static/app.js` (PWA, ~3,000 lines). Communicates with backend via `/api/*` JSON endpoints. Features: letter bar, browse modes, playlist management, MediaSession API, Service Worker offline support. Dark theme with amber accents (`static/app.css`).

**A missing control must not kill `app.js` (2026-09-21).** `app.js` is
ONE long script, so a top-level `$("id").addEventListener(...)` on an
element that isn't there throws and **kills every line below it** — the
app paints its full chrome and never loads content, in every library,
with nothing in the log. That is not hypothetical: a half-updated
Service Worker cache pairs an OLD `index.html` with a NEW `app.js`, and
adding the ℹ️ button did exactly this to a live client. The tell was
"works on iOS but not on Mac" — iOS had a matching pair of files.
**New controls bind through `bindClick(id, handler)`**, which warns and
skips when the element is absent: a missing button is cosmetic, a dead
`app.js` is the whole application. Bump `APP_CACHE` when `index.html`
gains an element `app.js` reaches for — the guard makes the mismatch
survivable, the bump is what ends it. Guarded by
`tests/frontend/test_artist_panel.py::test_a_missing_control_cannot_kill_app_js`.

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

**Source picker (`#source-sel`).** When more than one MediaServer is in `SERVERS` (e.g. music + audiobooks, or a UPnP server alongside LocalFs), the header carries a `SRC` dropdown next to the `OUT` (renderer) picker. `selectSource(udn)` swaps the active `curServer`, resets browse navigation, and reloads the library (or re-runs the active search). `refreshServers()` populates it via `rebuildSourceSel()` (💾 icon for `uuid:localfs-*`, 🗄 otherwise) and `updateDiscStatus()` keeps the header disc-dot tracking the active source. Regression-guarded by `tests/frontend/test_source_picker.py`.

### `/art` — lock-screen artwork proxy

iOS MediaSession refuses to load cross-origin artwork on the lock screen. The PWA rewrites every track art URL to `/art?url=<external>` so the lock-screen fetch is same-origin. Service Worker cache-firsts these (art rarely changes).

- **Size-bucketed downscaling (2026-08-07).** `/art?url=…&size=N` now honours
  `size` via `api_playback.art_fetch_scaled` — the same 96/256/512/1024 bucket
  ladder and per-bucket disk cache the Subsonic `getCoverArt` route uses. The
  PWA previously asked for the **full-resolution original everywhere**: a 36px
  list thumbnail and a 130px grid card both pulled the multi-MB embedded
  cover, and the album grid put a dozen on screen at once. Measured on the
  real library, a **772 KB cover is 12 KB at `size=256` (1.6%) and 42 KB at
  `size=512` (5.5%)**. `artUrl(raw, size)` in `app.js` names one bucket per
  surface — `ART_THUMB` 256 (36px rows, mini-player, station logos),
  `ART_COVER` 512 (grid cards), `ART_FULL` 1024 (now-playing panel **and** the
  MediaSession lock-screen artwork, deliberately the same url so it's one
  fetch and one cache entry per track; iOS never shows lock-screen art above
  ~600px). Sizes are **fixed, not devicePixelRatio-derived** — every device
  then asks for the same few URLs and shares one on-disk scaled copy per
  bucket, where a dpr-derived size would fragment the cache per device for no
  visible gain. `size` absent/0 is byte-for-byte the old behaviour, and Pillow
  stays optional (absent → original served, so it degrades to the old
  bandwidth, never to a broken image). Browse/grid `<img>`s also carry
  `loading="lazy"`. `ART_CACHE` bumped to v3 to evict the originals cached
  under the old size-less URLs. Guarded by
  `tests/test_asgi.py::TestArtProxy` (size forwarded, absent-size unchanged,
  junk size degrades) and the `/art` bucket tests in
  `tests/frontend/test_layout.py`.
- Hard-caps at 12 MB per image to prevent memory abuse (raised from 5 MB
  2026-07-03 — real embedded covers exceed 5 MB and the cap made
  getCoverArt 404 deterministically for every candidate of such an
  album; 12 MB matches `dlna_localfs_server`'s embedded-art cap).
- Validates Content-Type starts with `image/` — an upstream HTML 404 page won't poison the SW cache.
- 10-second timeout; slow upstream fails fast.
- The handler is in `api_playback.art()` routed at `/art` in `dlna_asgi.py`.
- **Subsonic `getCoverArt?size=N` downscaling (2026-08-04).** The native
  `getCoverArt` route in `dlna_asgi.py` honours the client's `size` box via
  `api_playback.art_fetch_scaled(url, size)` — Amperfy asks for ~100–600 px
  thumbnails per list row, so serving the full multi-MB embedded original was
  the main "cover art slow on Amperfy" cause. Sizes snap to a 96/256/512/1024
  bucket ladder; each scaled JPEG (Pillow, quality 85) is cached per bucket in
  `dlna_art_cache` under a `s<box>` variant, so a bucket is scaled at most once
  and the original is fetched at most once regardless of how many sizes are
  requested. `size=0`/absent, a size above the top bucket, an already-small
  image, or Pillow-missing all serve the unmodified original. **The PWA `/art`
  route now uses the same ladder** (2026-08-07 — see the `/art` section above;
  it used to serve full-res everywhere). Guarded by
  `tests/test_art_cache.py` (`TestSizeBucket`, `TestArtFetchScaled`,
  `TestCacheVariants`).

### UI — navy palette, per-device layouts, album grid (2026-08-07)

Guarded by `tests/frontend/test_layout.py` (26 tests). Design proposal that
was approved: the artifact linked from that session; the numbers below are
measured, not estimated.

**Palette.** The warm near-black set is replaced by a deep navy one. The
change that matters is **`--ink-dim`**: it carries every artist line,
duration, count and label, and at the old `#9a907f` it sat at **6.2:1**
against the ground — that, not the background, is what washed out in
sunlight. It is now `#B4C9E6` at **10.8:1**. Body `#F4F8FF` 17.2:1, amber
`#FFC24A` 11.4:1, green `#4FD98C` 10.1:1 — all AAA. The amber accent is
kept deliberately (it's the app's identity and reads better on navy than on
the warm black). `--ink-dim-soft` is the one sub-AAA ink, for decorative
labels that never carry information alone. Also repainted: the PWA
`theme-color`, the manifest `background_color`/`theme_color`, and the
generated app icon (`_make_icon_png` in `dlna_asgi.py`).

**Type.** `DM Mono` is no longer requested at weight **300** — thin small
type is what actually disappears outdoors, roughly as much as low contrast.
Secondary text went 11px → 12px via `--fs-sub`; uppercase micro-labels
10px → 11px via `--fs-micro`.

**Breakpoints.** There was ONE (768px). There are now four zones:

| Zone | Query | Layout |
|---|---|---|
| Phone upright | `≤767px` | one column + bottom nav |
| Tablet upright | `768–1023px` | **two** columns; playlists via a tab |
| Desktop / tablet sideways | `≥1024px` | three columns, fluid `clamp()` |
| Phone sideways | `max-height:500px and min-width:768px` | list beside player + side rail |

Two situations were on the wrong side of the old single breakpoint:
- **iPad upright** (Air/Pro 11 = 820–834px) took the desktop layout, where
  `#browser` 360px + `#pl-panel` 260px = 620px of fixed chrome left the
  player **~200px** — too narrow for the six transport buttons. Now two
  columns: measured **426px** player on an 820px iPad.
- **Every iPhone on its side is 852px wide** (932 Pro Max), so it also took
  the desktop layout — three columns and a status bar inside 393px of
  height. The rule that catches it is keyed on **height**, so it works on
  any model and never catches an iPad sideways (1024×768). **This block must
  stay LAST in the file** — a landscape phone also matches the tablet query
  and has to override it.

**Non-obvious things that bit during this work — do not re-introduce:**
- `#bottom-nav` is a **sibling of `.workspace`**, at the end of `<body>`.
  Making it `position:static` to build the landscape side rail dropped it
  into the body's column flow *below* the panes and squeezed them to
  nothing. It stays `position:fixed`, with `.workspace{margin-left:54px}`.
- `.tab.tablet-only{display:none}` **must be declared before** the tablet
  media query. A media query adds no specificity, so the same selector
  written afterwards simply wins and hides the tabs at every width.
- The inline `style="…"` attributes on `#source-sel`/`#output-sel` were
  moved into CSS. An inline style beats any selector short of `!important`,
  so the responsive rules could not reach them.
- The manifest said `"orientation": "portrait"`, which an **installed PWA
  obeys** — it would have locked the home-screen app upright and made the
  landscape layout unreachable from the one place it matters. Now `"any"`.

**Album cover grid.** Albums, the artist/genre/decade drill-downs and the ⭐
favourites list render through **one** `renderAlbumRows()` (they were five
near-identical copies that had already drifted). Putting `grid` on
`#item-list` turns the *same* markup into a cover card — the markup is
deliberately unchanged (`.row` / `.row-art` / `.row-body` / `.row-actions >
.icon-btn`), so every existing handler, selector and test still applies and
list-vs-grid stays a pure presentation choice. `showSpinner()` clears the
class, which is why every view load resets to a list. Artists and tracks
stay lists — text-first content, a cover adds nothing.

> **`grid-auto-rows:max-content` is NOT optional**, and the card is
> `display:block`, not a flex column. A cover sized `width:100%` +
> `aspect-ratio:1` contributes **no intrinsic height**: as a flex item the
> ratio is circular (the line's height is wanted before the width it depends
> on is known), and inside `#item-list`'s bounded scroll container the auto
> rows were squeezed to share the visible height — 114px rows around 129px
> covers, every title spilling onto the card below. **It only broke once the
> cards needed more than one row**, so a single-row test passes vacuously;
> `test_grid_cards_never_overflow` seeds eight albums and asserts a second
> row exists.

**Also:** the header is one row on the phone (search collapses to a 🔍
button, wordmark hidden — the pickers need that ~90px to show a readable
source name), and the app now boots into `mobileTab("browse")`. Without a
body `m-*` class the phone stylesheet hides nothing, so a fresh load used to
stack the entire playlist panel under the browse list, below the fold.

### iOS battery — what an OPEN but IDLE PWA costs (2026-08-07)

The 2026-07 work fixed the **screen-locked** case (Page Visibility stops the
polls, closes SSE, stops the ICY poll). The follow-up report was that the app
still drains while **open and NOT playing**. Two independent causes, both now
fixed; regression-guarded by `tests/frontend/test_battery.py` (10 tests).

**1. The polls ran at a fixed cadence regardless of activity.**
`startPolling()` set flat intervals — state 1 s, index 2 s, servers 8 s,
renderers 10 s — so an idle-but-open PWA fired **~2,600 requests/hour**, and
**~6,200/hr with a UPnP output selected**, where every `/api/renderer_state`
also costs the gateway **two SOAP round-trips to the renderer**
(`RendererQueue.snapshot()`; `_tracks` is never cleared on stop, so a
finished session keeps answering with live SOAP forever). At better than one
request per second the iPhone radio never reaches low-power idle. None of
that traffic was useful — the library, the device list and the transport
state are all static while idle, and SSE already pushes every change.

Polling is now **two-tier**, one self-scheduling loop per poll
(`_LOOPS` / `_armLoop` / `_runLoop` / `kickPoll` in `app.js`). The **fast tier
is the original interval**, so anything "active" behaves exactly as before:

| loop | fast (when) | slow |
|---|---|---|
| `state` | 1 s — audio genuinely playing (`_playbackActive()`) | 20 s |
| `index` | 2 s — a rebuild is running, or no source adopted yet | 60 s |
| `servers` | 8 s — until SSE connects | 60 s |
| `renderers` | 10 s — until SSE connects | 60 s |

The slow tiers are a **safety net for a dropped event, not the update path**:
an SSE push calls `kickPoll(...)`, which polls *then* re-arms, so a change is
still reflected instantly. `bumpPolling()` does the same from every path that
starts/stops playback (`sendRenderQueue`, `control`, the `<audio>`
play/pause events) — arming without polling first would size the delay off
stale flags. **If SSE never connects, every loop stays in its fast tier and
behaviour is identical to before.** A throwing poll re-arms its own loop
(guarded) — otherwise one blip would freeze the UI until the next visibility
change.

Backend counterpart: `_SSE_HEARTBEAT_SEC` 15 s → **45 s** (`dlna_asgi.py`).
With the polls throttled, the keepalive was the most frequent thing left on
the wire — 240 inbound frames/hour to say "still here". Kept under 60 s so a
future reverse proxy's idle timeout can't close the stream.

**2. The vinyl rotation never stopped.** `#player.playing.is-audio .art` is an
**8 s infinite rotation**, and nothing removed `playing` on the browser-output
path (`pollState` only clears it on the UPnP branch). Pausing therefore left a
full-size album cover spinning at display refresh rate **forever** — a
compositor/GPU wake every frame for as long as the app stayed open, and the
biggest **non-network** idle drain. The class is now driven off the `<audio>`
element's own `play`/`pause` events, the same single-source-of-truth as
`MediaSession.playbackState` below.

**3. Orphaned spinners.** Every loader is "show spinner → fetch →
`if(!r) return;`", so any failed fetch left `.spinner`'s infinite CSS rotation
running with no user feedback. All eight sites now go through
`showSpinner(el)`, which arms a `_SPINNER_TIMEOUT_MS` (15 s) watchdog that
swaps the spinner for a static "⚠ Couldn't load" line. The replacement keeps
the `spinner-wrap` class so the existing "clear the spinner before appending"
checks still remove it.

**Also fixed:** `rebuildSourceSel` / `rebuildOutputSel` reassigned `<select>`
`innerHTML` on every refresh even when the list was identical, destroying and
rebuilding the `<option>` nodes (a style/layout pass, and on iOS it closes an
open native picker). Both now skip the write when nothing changed.

**Known, NOT changed** (deliberate — each is a real trade-off):
- `<audio preload="auto">`: after a pause Safari finishes buffering the
  current track. Bounded by one track (not continuous), and changing
  `preload` risks the "stops after one track" class of regression.
- The PWA `/art` route still serves full-res originals (lock-screen art).
  `art_fetch_scaled` already exists for Subsonic — routing PWA list-row art
  through a small `size=` bucket is the highest-value remaining win (network
  + decode + the memory held by a rotating layer), but it changes the
  documented `/art` contract, so it's a separate decision.

### MediaSession playback state (CarPlay / lock-screen resume)

`_updateMediaSession` (`static/app.js`) wires the lock-screen / CarPlay
metadata + transport handlers (play/pause/prev/next + position scrubber). On
top of that, **`navigator.mediaSession.playbackState` is driven off the
`<audio>` element's own `play`/`pause` events** (2026-08-04) — the single
source of truth, so it's correct no matter what caused the change (a tap,
autoplay, a queue advance, or a phone-call/Siri interruption). A correct
`playbackState` is what makes CarPlay / the lock screen show the right control
and reliably deliver the PLAY command back to us, i.e. a dependable **one-tap
resume after a call**. **Hard platform limit:** iOS forbids a backgrounded web
page from resuming audio without a user gesture, so fully-automatic hands-free
resume is *not* achievable in the PWA — only a native app (Amperfy, via
`AVAudioSession` `.shouldResume`) can do that. Keeping `playbackState` correct
is the realistic best the web path can offer. Guarded by
`tests/frontend/test_mediasession.py` (play→playing, pause→paused, and the
playing→paused→playing call-interruption round-trip).

### Browser audio error handling (MediaError discrimination)

`static/app.js` listens for `error` events on the `<audio>` element and branches on `MediaError.code`:

| code | name              | behavior                                                      |
|------|-------------------|--------------------------------------------------------------- |
| 1    | ABORTED           | ignored (we or the UA told it to stop)                        |
| 2    | NETWORK           | retry the same track ONCE (transient network), then skip      |
| 3    | DECODE            | retry the same track ONCE (could be a bad Range chunk), skip  |
| 4    | SRC_NOT_SUPPORTED | skip immediately — the format genuinely isn't playable        |

Prior to 2026-04-23 every `error` event was treated as code 4 and auto-skipped, producing false-positive "unsupported format" skips whenever the network hiccupped. Every event (including ignored-code-1) is now POSTed to `/api/client_log` so real-world incidents land in `gateway.log` for diagnosis.

### The stall watchdog — "it just stopped mid-playlist" (2026-08-25)

**The browser queue only ever advanced on `ended` or `error`.** An `<audio>`
element has a third resting state that fires **neither**: buffer starved, no
more bytes arriving, and — from the element's own point of view — no error.
Nothing in `app.js` was listening for `stalled`/`waiting`/`suspend`, so when
that happened the queue simply sat there showing "⏸ Pause" with silence, and
the only way out was restarting a track by hand.

It happened in the wild: the last byte of a track was delivered at 10:22:32,
then **no further request, no `client_log` entry, and nothing at all until
10:28:38** when the next track was started manually. What starved the buffer
was the relay bug above (eleven concurrent Range requests against a relay
that had already dumped the whole file into memory) — that is fixed, **but
the player must not depend on the network being well-behaved.** Any stall
that nothing reports still has to un-wedge the queue.

`_stallCheck` polls `currentTime` every `_STALL_POLL_MS` (2 s) while the
element believes it is playing. No progress for `_STALL_GRACE_MS` (12 s,
comfortably longer than a tailnet rebuffer) → **one recovery nudge** (re-seek
to where it died, which re-issues the Range request; a dropped fetch usually
resumes and the listener never notices) → still nothing → report
`kind:"audio_stall"` to `/api/client_log` and **advance the queue**.

Non-obvious things it must keep getting right, each one a test:
- **Progress resets everything.** A watchdog that fires on a slow-but-healthy
  stream is worse than no watchdog.
- **A user pause is not a stall** — `paused` stops `currentTime` too.
- **It never runs for UPnP output.** With a renderer selected the gateway
  owns advancing the queue and the browser element is idle.
- **The `error` path and `_browserPlayIdx` both stop it**, so an error-driven
  skip and a watchdog-driven skip can't both fire and jump two tracks.

Guarded by `tests/frontend/test_stall_watchdog.py` (7 tests; the give-up case
genuinely fails with the branch disabled — checked).

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
