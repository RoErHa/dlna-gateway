# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

DLNA Gateway is a Python-based UPnP/DLNA music library gateway. It indexes music from its own in-process LocalFs backend (and any UPnP MediaServer such as MinimServer) into a local SQLite DB, and exposes a PWA web UI for browsing and playback. Playback targets: UPnP MediaRenderers (Naim Uniti, etc.) and browser audio. The gateway also announces itself as a UPnP MediaServer so UPnP renderers can browse its playlists directly.

The live library backend is **RoHaLocalFS** (an in-process indexer +
bit-perfect file server) behind a pluggable **`LibraryProvider` seam**; see
**[Library backend — LocalFs](#library-backend--localfs)**.

## Branching

**Work happens on `main`.** There is one branch and one checkout
(`~/dlna-gateway-2.0` — the directory name is vestigial). Commit on `main`, run the gates, deploy with
`launchctl kickstart -k gui/$(id -u)/com.roha.dlna-gateway`.

Releases are tagged (`v2.1.0`). Tags are cheap and mark points worth
returning to.

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

## Code quality gates (added 2026-08-20)

Before this date the repo had **no lint, format, or type configuration
and no CI** — nothing enforced any standard. Five gates now run inside
`python tests/run_all.py --offline`, each sized to sit at **zero
violations** so any hit is a real regression rather than backlog:

| Gate | Config / data | Fix when it fails |
|---|---|---|
| **T0.LINT** — ruff | `pyproject.toml` | `.venv/bin/ruff check . --fix` |
| **T0.SIZE** — per-module line ratchet | `tests/module_size_budget.json` | split the code, or `python3 tools/regen_size_budget.py` to lock in a shrink |
| `tests/test_lock_sync.py` — dependency pins | `requirements.lock` + `requirements-dev.lock` | `python3 tools/regen_lock.py` |
| `tests/test_no_silent_swallows.py` — observability | — | log the exception, or narrow the `except` |
| **T0.BADGES** — the README's numbers (added 2026-08-26) | `tools/badges.py` | `python3 tools/badges.py --write` |

**T0.BADGES** — why it exists and how the checks/unit/browser halves are
verified is in README.md → "Code quality gates". The one thing to know
when touching it: the gate is **last on purpose, it counts itself**
(`passed + failed + 1`), and only an `--offline` run can speak for that
number, because live mode performs extra checks.

**ruff** (`pyproject.toml`): rules chosen because each catches a defect
class, not a style preference — `F, E9, W, B, RET, UP, C4, PIE, ASYNC`.
**ASYNC matters most here**: the whole edge is an ASGI app, and one
blocking call inside an `async def` serialises every request. It already
caught four real ones — `os.path.isfile()` on the **external SAMDATA
volume** inside the video routes, where a spun-down USB disk stalls the
event loop (and the SSE stream) for seconds. Blocking work belongs in
`run_in_threadpool`; `dlna_asgi._isfile()` is the helper.
Rules the codebase deliberately diverges from (E501, RET504-508, B008 for
FastAPI's `Depends()`-in-default, B024 for the provider Protocol) are
ignored with the reason inline. `tests/` and `tools/` get E402 relief —
they set `sys.path` before importing what they test.
ruff is a **dev-only dep**: absent → the gate SKIPS, never reddens a
clean clone. `pip install -r requirements-dev.txt`.

**Module size is a RATCHET, not a flat limit.** Every application module
carries a ceiling in `tests/module_size_budget.json`: it may shrink
freely, never grow. A **new** module is held to the 400-line target from
birth — legacy modules get an allowance, new ones do not. A module that
drops well below its ceiling makes the budget *stale* and the suite says
so, so the gain is locked in instead of silently spent again. The count
of over-target modules is itself capped, so splitting one big file into
three still-big files cannot pass. A flat 400-line rule was rejected
because it would fail on day one for 15 modules and be switched off
within a week. The debt was **paid off on 2026-08-20 (84/84 modules
within target)** and has stayed paid as the tree grew — **107/107 today** and `over_target` is empty — so the ratchet now behaves
as a flat 400-line limit in practice, without ever having needed a
flag-day. Every module above the line was split in the refactor series
that ends at `dlna_providers/localfs.py`; the seams chosen are recorded
in each new module's docstring, so a future reader learns why the file
exists rather than just that it is small.
`tests/` and `tools/` are excluded on purpose — gating test-file growth
puts a thumb on the scale against writing tests.

**The README's badge numbers are a GENERATED artifact too.** They are
hand-written numbers in Markdown, so they drift the moment anyone adds a
test — and they had (the badge still said `1,089 unit · 231 browser`
several commits after both had moved). `python3 tools/regen_badges.py`
reads the counts from the sources that own them (`run_all.py --offline`
for checks + unit, `pytest --collect-only` for browser, `ruff check` for
violations), shows the drift, and rewrites the badges on `--apply`;
`--check` exits 1 if stale. Same pattern as `regen_schema.py` /
`regen_size_budget.py`. Deliberately **NOT** wired into `run_all.py` as a
staleness gate, unlike `test_schema_sync.py`: that gate would have to run
the suite to learn the suite's own numbers, and a suite that fails because
it cannot count itself is a worse failure mode than a stale badge. Run it
when the counts move (it re-execs itself under `.venv`, because counting a
test module means importing the app). `.venv/bin/python -m unittest tools.test_regen_badges -v` —
24 tests, over the shields.io encoding (an unescaped `-` or `,` silently
renders as a broken label) and over the prose checker below.

**`--check` also verifies the counts written in PROSE**, which is where
the drift actually happens: the badges had this gate, while CLAUDE.md's
"# N tests" and "N/N modules" lines had nothing and went stale twice in
one day — both times minutes after the badge itself was refreshed. It
matches three shapes (`unittest <mod> -v  # N tests`, `` `tests/x.py`
(N) ``, `**N/N today**`), counts the real cases by LOADING the module
rather than running it, and reports file:line for each disagreement.
Two deliberate choices: a module that cannot be imported is reported
**unavailable, never counted as 1** (unittest substitutes a single
`_FailedTest`, so a deleted test file could otherwise pass by claiming
"1 test"), and prose is **never rewritten automatically** — these counts
live inside sentences, and a regex that edited them would eventually
mangle the surrounding text. Fix those by hand; `--apply` only ever
touches the badges.

**Dependencies are pinned in two layers.** `requirements.txt` is the
SPEC (loose `>=`, every dep optional, graceful degradation);
`requirements.lock` is the INSTALL (exact transitive closure, 26
packages). `setup.sh` installs from the **lock by default** —
`./setup.sh --unpinned` deliberately resolves the ranges when testing
newer upstreams. fastapi/hypercorn additionally carry `<1.0` caps because
setup.sh falls back to the spec when no lock is present. Dev tooling
lives in `requirements-dev.txt` and is never in `requirements.lock`, so a
production install doesn't pull a browser engine. It has its OWN lock,
`requirements-dev.lock` (2026-10-23): `-r requirements.lock` plus the
dev-only closure, so a fresh clone tests with exactly what passed here.
Before it, only the dev tools' direct deps were pinned; an upgrade moved
certifi (a selenium/requests dep) and no tracked file changed. The gate
SKIPS when dev tools aren't installed, and `regen_lock.py` refuses to
write a truncated dev lock from a runtime-only venv.

**No exception may be caught broadly AND discarded silently.** A handler
may be broad, or silent, but not both — `except Exception as e:
log.debug(...)` or `except ValueError: return 0`, never
`except Exception: pass`. Cleanup paths use
**`dlna_config.close_quietly(resource)`**, which is broad on purpose (it
runs while another exception is propagating and must never replace it)
and logs at debug, so a suspected socket leak is greppable under
`GATEWAY_DEBUG=1`. Use it instead of writing another
`try: conn.close() / except: pass` — the gate rejects those by shape.

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

`tests/load_stream.py` guards the **threadpool-starvation regression** (the
"stops after one track" bug): it fires N concurrent full `/stream` pulls
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

### Opt-in real-browser smoke layers

`tests/frontend/safari_smoke.py` (real desktop Safari via safaridriver) and
`tests/frontend/ios_sim_smoke.py` (real Mobile Safari in an iOS Simulator via
Appium/XCUITest) run the same three checks: app boots + renders, the Service
Worker reaches `activated`, and **poison-recovery** (poison the app-shell
cache → reload → the app must still render — the 2026-06-27 outage). Both are
opt-in and NOT in `run_all.py` (a real Safari window / a booted Simulator +
Appium server). Playwright `--browser webkit` is desktop WebCore, *not*
Safari and *not* iOS. **Honest scope:** none of these can script standalone
home-screen PWA mode, autoplay/audio-session policy or WKWebView networking —
the real-device checklist stays the final iOS gate.

One-time setup (safaridriver, Appium, simulator runtimes), the Appium /
`xcrun simctl` start-stop commands, and the **Mobile / PWA real-device
checklist**: **[docs/TESTING.md](docs/TESTING.md)**.

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
| `dlna_gateway.py` | Module wiring + `start_background_services()` (spawns the daemon threads). Not the process entry — the `dlna_asgi` lifespan calls it so `hypercorn dlna_asgi:app` boots the whole gateway. |
| `dlna_asgi.py` | **THE server (Hypercorn owns the whole edge).** FastAPI app; terminates **TLS + HTTP/2** (ALPN) on `:8443` + plain on `:8765`, owns the `tailscale cert`. Native routes for the read API, `/art`, `/stream` + `/radio_stream` relays, static/PWA, the Subsonic byte methods, and the **Naim-facing `/gw/*` UPnP surface** (device.xml/desc.xml/events/control on the plain `:8765` bind). Remaining legacy handlers run via the bridge. Lifespan boots `start_background_services`. `docs_url=None` (no Swagger CDN call). Run: `./run-2.0-asgi.sh`. |
| `dlna_asgi_bridge.py` | Shim that runs the legacy `(h, params)` handlers unchanged inside the ASGI app (fake `h` captures `_json`/`_html`/`_xml_response`/`send_error`; runs in a threadpool). Routes are rewritten native one batch at a time, then dropped from the bridge. |
| `dlna_asgi_state.py` | The shared runtime handles every ASGI route module binds against. Split from `dlna_asgi.py` 2026-08-20 (it had reached 1,156 lines). |
| `dlna_asgi_browse.py` | The JSON read API: library navigation, index status, playlists, favourites, radio, lyrics, audiobook positions, and the SSE stream. |
| `dlna_asgi_media.py` | The byte relays — cover art and the audio proxies. |
| `dlna_asgi_static.py` | PWA shell serving + the generated manifest/icons. |
| `dlna_asgi_subsonic.py` | The `/rest/*` Subsonic surface (Amperfy/CarPlay). |
| `dlna_asgi_upnp.py` | The `/gw/*` UPnP surface the Naim and the LG TV talk to, on the PLAIN `:8765` bind. |
| `dlna_asgi_video.py` | The video routes the PWA uses (same-origin so iOS will play them), incl. on-demand transcoding. |
| `dlna_asgi_artist.py` | `GET /api/artist_info` — the ℹ️ panel's ONE request. Its own module because `dlna_asgi_browse.py` sits at 397/400, and because the panel is a feature seam that will grow. Resolves the line-up for the track's year and LABELS it, so the PWA can't promote a guess to a fact. |
| `dlna_art_cache.py` | On-disk cover-art byte cache keyed by source URL (+ an optional `variant` for size-scaled copies). `api_playback.art_fetch_cached()` fronts `art_fetch` so `/art` + Subsonic `getCoverArt` serve repeat covers from disk (across clients + restarts) instead of re-fetching coverartarchive / re-decoding embedded art. `art_fetch_scaled()` adds `getCoverArt?size=N` downscaling (Pillow, snapped to a 96/256/512/1024 bucket ladder; scaled copies cached per bucket) so Amperfy/CarPlay pull KB-sized thumbnails instead of multi-MB originals — the dominant cost of a library art-sync over the tailnet. Pillow is optional: absent → the full-res original is served (old behaviour). Also negative-caches deterministic fetch failures (a candidate whose file has no embedded art, a CAA 404) under a short-TTL `__neg__` marker (`get_negative`/`put_negative`, default 1 h) so Amperfy's repeated getCoverArt doesn't re-decode the same dead candidate each time; TRANSIENT failures (art_fetch returns **503** when the upstream is unreachable) are never negative-cached, so a momentary localfs blip is retried at once. TTL + size-capped; `art_cache/` gitignored. `art_fetch` follows redirects (coverartarchive `front-500` 307→archive.org) + rejects <64 B junk bodies. |
| `dlna_events.py` | `EventBus`/`EVENTS` (thread-safe publish → asyncio loop) + native `GET /api/events` (SSE). Publishers: RendererQueue state, index-status transitions, discovery changes. The PWA opens an `EventSource` as a polling accelerator (fallback intact). |
| `dlna_routes.py` | `GET_ROUTES` / `POST_ROUTES` path → handler maps |
| `dlna_discovery.py` | Device-description fetch/registration + the server heartbeat. Composition point: re-exports the SSDP and probe surfaces so `dlna_discovery.<name>` resolves as it always has. |
| `dlna_discovery_ssdp.py` | The SSDP multicast side — periodic M-SEARCH + the NOTIFY listener. Owns the SSDP wire constants. |
| `dlna_discovery_probe.py` | The non-SSDP fallbacks — TCP/HTTP subnet sweep + the "only scan if SSDP found nothing" guard. Both this and the SSDP module reach `_register_location` through a **deliberately lazy import** (it reads the `_on_server_found` hook the gateway injects at runtime, so the binding must resolve at call time). |
| `dlna_registry.py` | Data classes + `ServerRegistry` / `RendererRegistry` thread-safe stores |
| `dlna_library.py` | **Composition root only (~150 lines).** `LibraryDB` is assembled from six mixins (below) — each of which may itself inherit one more after the 2026-08-20 size split, ten in the MRO and still disjoint. The `DB`/`INDEXER`/`DEVICE_ROLES`/`ART_FETCHER` singletons are wired here. Until 2026-08-20 this file was **2,912 lines** and `LibraryDB` a 95-method God Object; the split is MIXIN-based precisely so the public `DB.<method>` surface is byte-identical and none of the ~240 call sites changed. |
| `dlna_library_sql.py` | Pure helpers shared by the mixins — `_norm_title`, `_dedup_clause`, `_parse_audio_params`, `_is_localfs`, the `_localfs_album_*` SQL fragments, `_dur_to_secs`. Dependency-free ON PURPOSE: a mixin importing them from `dlna_library` would be circular AND would fire the `DB = LibraryDB()` module-level singleton (hence every pending migration on the live DB) as a side effect. `dlna_library` re-exports them all, so `from dlna_library import _dedup_clause` still works. |
| `dlna_library_schema.py` | `SchemaMixin` — the startup sequence (create → alter → migrate → seed) + the Phase-A album-art sibling backfill. Regenerate `schema.sql` after changes. |
| `dlna_library_ddl.py` | Composition root for the DDL — `SCHEMA_DDL` (concatenated from the three modules below) + `ADD_COLUMN_SQL`. Split 2026-09-20 at 399/400 lines. No logic. |
| `dlna_library_ddl_index.py` | The DERIVED half: `tracks`, FTS5 + sync triggers, `localfs_files`, `index_meta`. Every row here is regenerable by re-reading the music. Concatenated FIRST — the FTS triggers reference `tracks`. |
| `dlna_library_ddl_user.py` | The half that SURVIVES a rebuild — the thirteen tables `clear(udn)` must never touch. Authored by a person, or earned at real cost (Cover Art Archive, lrclib, ~1.2 s per MusicBrainz id). If a table belongs here, adding it to a clear path is data loss, not tidying. |
| `dlna_library_ddl_video.py` | GWMovies, geocode cache, location overrides, Immich people. Separate because a different client browses it, a different worker fills it, and it carries the project's only privacy-relevant outbound call. |
| `dlna_library_migrations.py` | `MigrationsMixin` — the in-place migrations + `repair_fts`/`run_with_fts_heal` (the FTS5 shadow-table corruption recovery). |
| `dlna_library_unique.py` | `UniqueMigrationsMixin` — the three migrations that WIDEN the `tracks` UNIQUE by rebuilding the table. Grouped because each DROPs the FTS triggers and must recreate them, and all three must run before `tracks_au` is added on top. |
| `dlna_library_tracks.py` | `TracksMixin` — `tracks` writes/reads + the indexer upsert path. |
| `dlna_library_overrides.py` | `OverridesMixin` — the `metadata_overrides` display layer. Protects the two invariants: `manual` always wins, and its `year` is display-only (never COALESCEd back into `tracks`). |
| `dlna_library_browse.py` | `BrowseMixin` — NAVIGATION through the hierarchy: artists, albums, the letter bar, an artist's chronology. Owns the two cross-cutting rules: `_dedup_clause` (browse views only) and `_is_localfs` folder-album identity. |
| `dlna_library_search.py` | `SearchMixin` — the FTS5 free-text question and the query semantics that come with it (implicit AND, last-term prefix, punctuation-only tokens dropped). Split from `browse` 2026-09-21 at exactly 400 lines; the seam is real — navigating a tree and answering a question are different jobs. |
| `dlna_library_credits.py` | `CreditsMixin` — the `track_credits` store (MusicBrainz composer/lyricist for the tracks whose files carry none). Same survives-`clear(udn)` family as `album_art` and `lyrics`: every row cost a rate-limited round-trip. |
| `dlna_library_facets.py` | `FacetsMixin` — the tag-sliced facets (genres, decades), their flat track listings, and the play-count-biased radio picker. Owns `_EFFECTIVE_YEAR`. |
| `dlna_library_videos.py` | `VideosMixin` — the GWMovies index, the date/location/person browse queries, location overrides, Immich person tags, Nominatim geocode cache. |
| `dlna_credits.py` | Is this songwriting credit a NAME or machine junk? Display-only, shared by every surface that shows composer/lyricist. Exists because 8% of this library's credits are scene adverts (`www.t.me/…`). See **[Songwriting credits](docs/LIBRARY_RULES.md#songwriting-credits-composer--lyricist-2026-09-20)**. |
| `dlna_mbid.py` | "Is this MusicBrainz artist OUR artist?", decided purely. The keystone: one wrong id is wrong in four sources at once, so it REFUSES rather than guesses. Owns the `&`-is-never-split and every-dash-folds-to-a-space rules. |
| `dlna_lineup.py` | "Who was in the band when this song was recorded?" — pure date-intersection over band membership. Needed because per-recording credits cover only ~17% of tracks while membership is near-universal. |
| `dlna_artist_fetch.py` | The four artist-metadata sources (MusicBrainz, Wikipedia, Last.fm) with PARSING split from HTTP — the parsers are pure and carry the judgement, so that is where the tests are. Owns the `member of band` DIRECTION rule. |
| `dlna_library_artists.py` | `ArtistsMixin` — the artist → MBID store, the display facts, and the band line-up. Same `clear(udn)` contract as `album_art`: each row cost a rate-limited round-trip. |
| `dlna_artist_infer.py` | The pure decision "can we say who performed this, without guessing?" — SHARED by `tools/artist_from_folder.py` (writes the tag) and the worklist sweep (takes what is left), so the two can never disagree. |
| `dlna_library_worklist.py` | `WorklistMixin` — the "- Unknown Artists -" hand-editing worklist: the one place the gateway admits it cannot do a job in code. Split from `collections` 2026-08-25 when the sweep pushed it past 400 lines. |
| `dlna_library_languages.py` | `LanguagesMixin` — which language each audiobook is in, for the PWA's 🌐 letter-bar entry. ASSERTED languages (a file tag, or set by hand) live in `book_language`; a DETECTED one is derived on every read and never stored. See **[Audiobooks by language](docs/AUDIOBOOKS.md#audiobooks-by-language--the--entry-2026-10-12)**. |
| `dlna_book_language.py` | The pure decision "what language is this book written in?" over its path, album tag and chapter titles — and `normalize_language`, which turns a tag value or a typed code into ISO 639-1. Refuses rather than guesses: a book the words do not decide is Unknown. |
| `dlna_asgi_books.py` | `GET /api/book_languages` + `GET /api/book_language_albums` — the 🌐 view. Its own module because `dlna_asgi_browse.py` sits at its budget. |
| `dlna_library_collections.py` | `CollectionsMixin` — playlists, album favourites, lyrics, audiobook positions, book metadata, device roles. **The invariant this module exists to protect: none of these tables is touched by `clear(udn)`.** |
| `dlna_library_radio.py` | `RadioFavouritesMixin` — the saved internet-radio stations. Enforces the 25-station cap SERVER-side (`DB.RADIO_FAV_MAX`); same `clear(udn)` survival contract. |
| `dlna_indexer.py` | `Indexer` — background crawler that walks a MediaServer and populates LibraryDB |
| `dlna_art_fetcher.py` | `AlbumArtFetcher` — Phase B MusicBrainz + Cover Art Archive lookup. |
| `dlna_art_query.py` | The PURE half of that lookup: `tidy_album()` + `art_queries()` turn one `(artist, album)` into an ORDERED list of attempts. Separate module because a cover query is a guess with a cost, and the ordering rule — exact first, always — is the thing that must never be edited casually. LOOKUP ONLY: never identity, never display. |
| `dlna_devices.py` | `DeviceRoleCache` — in-memory mirror of device_roles for zero-latency classification |
| `db_pool.py` | SQLite connection pool — WAL mode, thread-local connections, write serialization |
| `dlna_config.py` | Constants (`DB_FILE`, `CFG_FILE`, `LOG_FILE`), logging setup, config load/save |
| `dlna_providers/` | `LibraryProvider` seam (P0). Protocol + dataclasses + registry; `mock.py` for tests; `upnp.py` (P1) wraps the existing UPnP SOAP path; `localfs.py` (P2) is the in-process backend (mutagen + watchdog + a content-hashed track id), split into `localfs_tags.py` (what a file SAYS about itself — the album-key folder identity and the namespace-salted track id), `localfs_art.py` (what the album LOOKS like — embedded pictures, and the folder-image fallback that is the only half reading sibling files), `localfs_read.py` (`ReadMixin`, the Protocol read surface) and `localfs.py` itself (the scan/upsert half — which STAYS there because the tests patch `dlna_providers.localfs._read_tags` and friends). `plex.py` / `jellyfin.py` land in P3+ if/when the LocalFs path proves the seam works. |
| `dlna_localfs_http.py` | The PURE helpers behind the file server: building the DLNA response-header pair for a MIME type, parsing a `Range:` header (a malformed range must yield **416**, never a silent full-body 200), and `resolve_within` — the containment test every byte route asks before opening a file (path COMPONENTS, not string prefixes; returns the RESOLVED path so the caller cannot re-open the original through a TOCTOU gap). |
| `dlna_localfs_server.py` | LocalFs HTTP file server (P3). `ThreadingHTTPServer` on its own port (default 8200, bound `0.0.0.0`). `GET /localfs/stream/<id>` resolves via `library.db` and streams the original bytes in 64 KB chunks. Range-aware (`Accept-Ranges: bytes`, `Content-Range`, 206 / 416), DLNA-headered (`DLNA.ORG_PN`, `transferMode`), bit-perfect. Path-traversal defence via `allowed_roots`. Also serves `GET /localfs/art/<id>` — the file's first embedded cover picture on demand via `_extract_art_bytes` (FLAC/ID3/MP4, MIME sniffed from magic bytes), 12 MB cap, 404 on no-art. |
| `dlna_localfs_wiring.py` | Boot-time wiring of the LocalFs provider (P4). `maybe_start_localfs(get_lan_ip)` is called from `dlna_gateway.main()`. REGISTERS THREE **independent** roots — music (`$LOCALFS_MUSIC_ROOT` / `localfs.root`), video, audiobooks — with `dlna_localfs_watch.WATCH`, and owns what BRINGING ONE UP means: ensure the one file server (started by the first root to arrive, widened by each later one), construct a `LocalFsProvider` with the LAN-IP `base_url`, bind it via `dlna_providers.bind_provider`, add a synthetic `MediaServer` to `SERVERS`, and scan it in the background. See **[One unmounted volume must not take the others down](docs/LIBRARY_RULES.md#one-unmounted-volume-must-not-take-the-others-down-2026-09-10)**. Kept in its own module so the run_all.py "Gateway is slim (<350 lines)" lint stays green. |
| `dlna_walk.py` | Can this directory walk be trusted to say a file is GONE? The pure rule (`trusted_removals`) the music rescan asks before deleting `tracks` rows: `os.walk` swallows directory errors, so an unreadable folder reads as deleted; a walk that saw no audio at all is a drive gone wrong, not a library emptied. Holds back only the removals UNDER an errored folder, so one permanently unreadable folder never freezes removal everywhere. See **[A rescan may only call a file gone when the walk looked](docs/LIBRARY_RULES.md#a-rescan-may-only-call-a-file-gone-when-the-walk-looked-2026-10-10)**. |
| `dlna_localfs_watch.py` | WHEN each root gets wired — the half `maybe_start_localfs` used to answer once, at boot, and get wrong on a reboot that outran the drive. Holds each configured root's state (`waiting` / `active`), activates it exactly once when its volume is present, re-checks the rest every `$LOCALFS_ROOT_RECHECK_SEC` (30 s) on a thread that retires when nothing is waiting, and — the part the log alone never achieved — backs `GET /api/libraries` + a `libraries` SSE event so the PWA can name the drive to mount. Deliberately generic: it knows "a labelled path that may appear", never music/video/books. See **[A root is asked for ONCE](docs/LIBRARY_RULES.md#a-root-is-asked-for-once-and-boot-is-the-wrong-moment-2026-09-12)**. |
| `dlna_content.py` | UPnP ContentDirectory SOAP client (`cd_browse`, `cd_search`). After Phase 1, reached ONLY via `dlna_providers/upnp.py`. |
| `dlna_avtransport.py` | UPnP AVTransport SOAP client (send/stop/pause/state/position/seek) |
| `dlna_rendering_control.py` | UPnP **RenderingControl** SOAP client (`SetVolume`/`GetVolume`) — a genuinely separate service with its own control URL, so the split follows the protocol boundary. Re-exported from `dlna_avtransport`. |
| `dlna_player.py` | `RendererQueue` composition root — sequential playback per renderer. Split 2026-08-20 (was 847 lines). |
| `dlna_player_policy.py` | The PURE decision functions behind renderer playback (`_monitor_decision`, `_dur_to_sec`, `_gapless_advanced`) + the tuning constants. Pure = directly unit-testable, which is why the stall guards live here. |
| `dlna_player_monitor.py` | `MonitorMixin` — the per-queue 2-second state-poll loop that advances tracks and enforces the stall guards. |
| `dlna_player_transport.py` | `TransportMixin` — SetURI/Play/Seek and the consecutive-send-failure abort. |
| `dlna_player_volume.py` | `VolumeMixin` — startup volume + user trim, via RenderingControl SOAP. |
| `dlna_player_registry.py` | `QueueRegistry` + the process-wide `QUEUES` singleton (one `RendererQueue` per renderer UDN). |
| `dlna_xml.py` | The ONE place untrusted XML is parsed. Refuses DTDs outright (entity-expansion amplification was measured at 10× per nesting level on the unauthenticated `/gw/*` SOAP endpoints) and caps input size; raises `ParseError` so existing handlers need no new branch. Also `read_capped` — the bounded READ that must happen first, because the size cap above can only judge bytes already in memory. |
| `hypercorn_conf.py` | The server's listen addresses, read from `.env`. Must stay PICKLABLE — hypercorn pickles the config namespace for workers, so no `def`/class/module may survive at module scope. |
| `dlna_ssrf.py` | The outbound-fetch guard fronting every caller-supplied `?url=` (`/art`, `/stream`, `/radio_stream`) and every redirect hop. Refuses private/loopback/link-local destinations unless the host is a device already in `SERVERS`/`RENDERERS`; refuses non-http(s) schemes. See "Security posture". |
| `dlna_stream_proxy.py` | Browser-audio HTTP proxy (`/stream`) with 5-min idle timeout — the **byte-perfect** Range pass-through. |
| `dlna_radio_proxy.py` | The internet-radio relay (`/radio_stream`): opens an ICY upstream and **de-interleaves** the metadata out of the audio (a browser `<audio>` cannot handle it), parking `StreamTitle` for `/api/radio/nowplaying`. Split from the byte-perfect path on purpose — this one must rewrite the stream. |
| `dlna_proxy_common.py` | The constants both relays need (`PROXY_IDLE_SEC`, `_MIME_MAP`, `normalize_audio_ctype`). Exists to keep that split acyclic; dependency-free. |
| `api_browse.py` | Browse/search API endpoints |
| `api_playback.py` | Playback control, the stream-proxy route, `/api/client_log`, indexer management. Split 2026-08-20 (was 748 lines). |
| `api_playback_art.py` | The cover-art subsystem behind `/art` and Subsonic `getCoverArt`: fetch, disk cache, downscale, serve. |
| `api_playback_meta.py` | Per-track metadata, lyrics, audiobook positions, OpenLibrary book metadata. |
| `api_playback_state.py` | Shared runtime handles + request helpers for the `api_playback` family. |
| `api_radio.py` | Internet-radio API handlers (`/api/radio/*` — search, favourites, nowplaying). |
| `dlna_lyrics.py` | On-demand lyrics fetch via lrclib.net (the network half; the cache lives in `LibraryDB`). |
| `dlna_fdmon.py` | Open-file-descriptor watchdog (diagnostic) — logs FD count vs the limit so a leak shows as a rising trajectory BEFORE it crashes the gateway. Its periodic heartbeat is INFO only when the count actually MOVED (2026-08-25); a flat number repeated every few minutes was 40% of `gateway.log` and buried the playback lines you go there to find. The ALERT / rising / high-water branches are what catch a leak; `GATEWAY_DEBUG=1` restores the full heartbeat. Same treatment for `dlna_video_index.scan_videos`: a rescan that changed nothing (`+0 prune 0 overrides 0`) logs at DEBUG, a rescan that moved the library still logs at INFO. **That scanner never prunes on an untrustworthy walk** (2026-10-04): `os.walk` swallows directory errors, so a drive dropping mid-scan reads as an empty tree — on 2026-10-03 a walk that saw 0 files pruned all 4,104 videos, and the next scan re-probed and re-geocoded every one. A walk that hit an error, or saw nothing while the index holds rows, now WARNs and prunes nothing (`force=True` still clears). `tests/test_video_scan.py`. The music scanner got the same guard, finer-grained, via `dlna_walk.py`. Together those two were **295 of 400 lines**. |
| `api_playlists.py` | Playlist CRUD endpoints |
| `dlna_ffmpeg.py` / `dlna_geocode.py` / `dlna_video_index.py` | Video feature (see `docs/VIDEO_SUPPORT.md`): optional ffmpeg/ffprobe helpers + HLS transcode cmds; Nominatim reverse-geocode (cache-first, 1.1s rate limit); the periodic GWMovies scanner (`scan_videos`, 5-min loop from `dlna_localfs_wiring`) incl. `apply_location_overrides` (re-lays inferred/manual locations after every scan). |
| `dlna_countries.py` | ISO 3166-1 alpha-2 → English name (`country_name`). **GENERATED** via Node `Intl.DisplayNames` (regen one-liner in its docstring / generating commit) — used so the video country-selection level shows "Netherlands", not "NL" (ids/filenames keep the code). The PWA uses the browser's `Intl.DisplayNames` directly. |
| `api_upnp.py` | The gateway-as-MediaServer (Browse is a **dispatch table** since 2026-08-20 — `_BROWSE_EXACT` + `_BROWSE_PREFIX` → one `_br_*` handler per container, sharing a `_Browse` context that owns the DIDL envelope and the "`count == 0` means unlimited" pagination rule; `_gw_browse` itself went 491 lines/~99 branches → 10 lines. Adding a container = one handler + one table entry.): a **complete DLNA Media Server** the Naim/LG browse. Device descriptor (`MediaServer:1` + `X_DLNADOC` + icons + ContentDirectory **and** ConnectionManager), both service SCPDs, SOAP `ContentDirectory#Browse` over the full library (`_gw_browse`) + the pre-browse handshake actions, `ConnectionManager#GetProtocolInfo` etc., GENA SUBSCRIBE + initial NOTIFY, and SSDP announce + **M-SEARCH responder**. See "UPnP exposure (Naim)". |
| `api_upnp_ids.py` | Gateway UPnP identity, the ObjectID codecs, the junk-name display filter, and the LibraryDB reads the browse tree needs. Owns `music_udn()` — the music tree follows what is SERVING, not what is merely indexed. |
| `api_upnp_didl.py` | DIDL-Lite renderers + the `_Browse` request context shared by every ContentDirectory handler. |
| `api_upnp_descriptors.py` | The device descriptor and the two service SCPDs that make strict DLNA clients willing to browse us at all. |
| `api_upnp_browse.py` | ContentDirectory Browse — the music, audiobook, playlist and favourite-album handlers, the dispatch tables, and `_gw_browse`. |
| `api_upnp_browse_video.py` | The 📹 Videos tree (GWMovies) as browsed by the LG WebOS TV. |
| `api_upnp_ssdp.py` | SSDP presence (NOTIFY + M-SEARCH) and GENA eventing for the gateway-as-MediaServer. |
| `api_subsonic.py` | Subsonic API composition root. Split 2026-08-20 (was 1,174 lines across auth, wire format, id codecs and 33 handlers). |
| `api_subsonic_proto.py` | The Subsonic wire protocol: authentication, the response envelope, the JSON→XML serialiser. |
| `api_subsonic_ids.py` | Opaque id codecs, source-udn resolution, and the builders turning LibraryDB rows into Subsonic objects. |
| `api_subsonic_browse.py` | The read/browse endpoints: ping, license, music folders, artists, albums, album lists, search, genres, OpenSubsonic capability probes. |
| `api_subsonic_playlists.py` | Playlists, album starring, scrobble. |
| `api_subsonic_media.py` | The byte endpoints: stream and cover art. |
| `api_subsonic_extras.py` | Internet radio stations and audiobook bookmarks. |

### Key Module-Level Singletons

These are shared state across all request handler threads:

- `dlna_discovery.SERVERS` / `RENDERERS` — device registries
- `dlna_library.DB` / `INDEXER` / `DEVICE_ROLES` — library DB, crawler, device role cache

#### `DB` is LAZY — importing the module opens nothing (2026-08-26)

Until this date `dlna_library` ended with `DB = LibraryDB()`, so merely
IMPORTING it — a test, a tool, a REPL in the repo dir, or anything that
reaches it transitively, which is most of the app — ran `_init_schema` and
every pending migration against the real, live `library.db`. It was safe by
design (idempotent migrations, WAL, per-call pool connections) and still
wrong in a way that mattered: a new migration "landed" on the live DB the
first time any test imported the module, not at the next restart.

`dlna_library.get_db()` now opens the handle on FIRST CALL, double-checked
under a lock because `LibraryDB()` runs the migrations and two threads
racing that would run them twice against one file. `DB` is a `_LazyDB`
proxy in front of it.

Three things there are load-bearing:

- **A proxy, not a module-level `__getattr__` (PEP 562).** Nearly every
  consumer spells it `from dlna_library import DB`, which a module
  `__getattr__` resolves — and therefore constructs — the moment that
  consumer is imported. Binding a proxy is what keeps `import dlna_asgi`,
  the widest import in the tree, cold.
- **The proxy forwards WRITES too** (`__setattr__` / `__delattr__`). The
  suite patches methods straight onto it
  (`mock.patch.object(dlna_asgi.DB, "all_artists", ...)`) and mock setattrs
  on entry, delattrs on exit. A first cut refused both — it read as a
  sensible guard and broke 12 existing tests.
- **Boot opens it explicitly.** `start_background_services` calls `get_db()`
  before anything else, so schema creation and migrations still happen at
  startup rather than inside whichever request touches the DB first. That
  ordering is deliberate, not incidental — don't remove the call because
  "the proxy handles it".

**`patch.object` is the SECOND door, and it is closed in the tests
(2026-09-21).** The lazy proxy closed the IMPORT door; it could not
close this one — `mock.patch.object(dlna_asgi.DB, "all_artists", ...)`
must `getattr` the original in order to restore it, and that getattr
resolves the proxy, opening the real `library.db` and running every
pending migration. Observed: a `run_all.py --offline` run applied a
pending `ALTER TABLE` to the live index. Harmless that time; the three
UNIQUE migrations REBUILD `tracks`. The proxy cannot refuse the getattr
(refusing broke 12 tests), so the defence is at the other end —
**`tests/__init__.py` AND the top of `tests/run_all.py`** both point
`DB_FILE` at a throwaway. Both are needed: `tests/__init__.py` runs too
late under `run_all.py`, which imports `api_playback` & friends long
before discovery, and `def __init__(self, db_file: str = DB_FILE)` binds
the live path as a DEFAULT ARGUMENT at import time that no later
reassignment can reach.

Nothing else changed: `DB.<method>` at ~240 call sites, `db._pool`
reach-ins, and the one-module-binds-`DB` family contracts
(`api_upnp_ids`, `api_subsonic_proto`, `api_playback_state`) are all
untouched. `_reset_db_singleton()` exists for tests only and is never
called by the app. Guarded by `tests/test_library_singleton.py` (12) —
the import half runs in a SUBPROCESS on purpose, since this process has
already imported half the app and would be answering for the test runner
rather than for a clean import.
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
tracks(id, udn, obj_id, url, title, artist, album, duration, art, mime, genre, file_path, bit_depth, sample_rate, year, album_key, composer, lyricist)
  UNIQUE(udn, artist, album, title, album_key, bit_depth, sample_rate)
  -- album_key joined the UNIQUE 2026-07-12 (_migrate_widen_unique_album_key):
  -- two DISTINCT files in different FOLDERS with identical tags (duplicate
  -- editions, scene comps) both index; UPnP rows keep album_key='' so their
  -- collision semantics are unchanged. Same-folder same-tag files still
  -- collide (true tag ambiguities — fix by retagging).
  UNIQUE(udn, url) via idx_tracks_udn_url  -- created by _migrate_unique_url
  bit_depth + sample_rate participate in UNIQUE so a 16-bit and
  24-bit copy of the same album coexist as distinct rows.
  The separate UNIQUE(udn, url) index dedups same-URL inserts.
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
  composer + lyricist are read from the file tags (2026-09-20).
  NOT in the UNIQUE: they say who WROTE the song, so two rows
  differing only by composer are a retag of one file, not two
  tracks. Blank-safe on refresh like genre/art.
artist_meta(artist_key, artist, mbid, source, fetched_at,
            mb_type, gender, born, died, birth_place, country,
            genres, disambiguation, bio, bio_url, image_url,
            notable, top_tracks, meta_fetched_at)
  PRIMARY KEY (artist_key) — dlna_mbid.norm_artist(artist), so
  'The Bad Seeds' / 'the bad seeds' / 'Bad Seeds' are ONE row and
  one question to MusicBrainz, not three.
  source ∈ {'tag','search','notfound','manual'} and describes how
  the MBID was RESOLVED, not where the biography came from —
  artist_info_set must never rewrite it. 'manual' always wins;
  'notfound' is sticky so a re-run resumes in minutes.
  Survives clear(udn): each row cost a rate-limited round-trip.
artist_members(artist_key, member_name, member_mbid, instruments,
               begin_date, end_date, updated_at)
  PRIMARY KEY (artist_key, member_name, begin_date)
  begin_date is IN the key because a member can REJOIN: Pink
  Floyd's Richard Wright is 1965-1981 AND 1987-2008, and keying
  on name alone collapses them and loses The Division Bell.
  A re-fetch is a SYNC (replace), not a merge. Survives clear(udn).
metadata_overrides(url, artist, album, title, genre, year, updated_at, source)
  source ∈ {'manual', 'acoustid', 'notfound', 'video_skip'}
  year is the MUSICBRAINZ original release year (release-group's
  first-release-date) — written by tools/improve_song_years.py /
  correct_year_drift.py as source='manual'.
index_meta(udn, indexed_at)
playlists(id, name, created_at, sort_order)
playlist_tracks(id, pl_id, url, title, artist, album, duration, art, added_at)
  UNIQUE(pl_id, url)
device_roles(udn, name, location, host, is_server, is_renderer, first_seen, last_seen)
album_art(artist, album, art_url, source, updated_at)
  PRIMARY KEY (artist, album)
  source ∈ {'sibling', 'musicbrainz', 'notfound', 'manual'}
track_credits(url, composer, lyricist, work_mbid, source, fetched_at)
  PRIMARY KEY (url)
  source ∈ {'musicbrainz', 'notfound', 'manual'}
  Composer/lyricist for the ~68% of tracks whose FILES carry neither,
  browsed off MusicBrainz works (2026-09-21, step 4). Deliberately NOT
  tracks.composer: clear(udn) DELETEs tracks, and a rebuild-index would
  throw away a whole night's fetching. Same survives-a-rebuild family as
  lyrics / album_art / play_counts. THE FILE TAG WINS on read —
  COALESCE(NULLIF(t.composer,''), c.composer) — because MusicBrainz is
  filling gaps here, not correcting what the owner has.
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
  A scan whose walk errored or saw 0 files never prunes (2026-10-04).
video_location_overrides(video_id, location_name, country, source, updated_at)
  source ∈ {'inferred_same_day', 'inferred_window', 'inferred_country',
  'manual'} — locations for GPS-less videos, written by
  tools/infer_video_locations.py. Survives clear_videos (the scanner
  would wipe them otherwise); RE-APPLIED at the end of every video scan
  (dlna_video_index.apply_location_overrides). 'manual' beats inferred;
  a row NEVER overwrites a real-GPS video; embedded titles never rewritten.
book_language(album_key, language, source, updated_at)
  PRIMARY KEY (album_key) — an audiobook's language where something
  ASSERTED it: source ∈ {'tag', 'manual'}, 'manual' always wins.
  language is ISO 639-1. A DETECTED language is never stored — it is
  derived on read (dlna_book_language), so it cannot go stale.
  Survives clear(udn). See "Audiobooks by language".
video_people(video_id, person, person_id, updated_at)
  PRIMARY KEY (video_id, person) — Immich person tags, synced by
  tools/immich_people_sync.py (per-person REPLACE semantics). Survives
  clear_videos. Feeds the "👤 By person" DLNA container + PWA grouping.
```

### Library data rules — what took evidence to learn

Each rule below has a full write-up (the incident, the measurements, the
rejected alternatives, the log lines that identify it) in
**[docs/LIBRARY_RULES.md](docs/LIBRARY_RULES.md)**. Read the relevant section
there before changing the code it names — most of these were "fixed" once in
the obvious way and broke something else.

- **One unmounted volume must not take the others down** (2026-09-10).
  Each LocalFs root (music / video / audiobooks) resolves on its own; the
  `:8200` server starts if at least one resolves and **never with an empty
  `allowed_roots`**; each initial scan is guarded separately. Regression
  symptom: every track skips ~1/s with `SSRF guard: refused stream of …`.
  The Naim side: `api_upnp_ids.music_udn()` offers Artists/Albums/Genres
  only while a music `MediaServer` is *registered* (`tracks` outlives its
  files); `primary_udn(among=[])` → `''`, the opposite of `None`; an EMPTY
  registry is deliberately NOT read as "nothing is live" (boot window).
  Favourites/Playlists are not gated. `tests/test_localfs_wiring.py` (18),
  `tests/test_upnp_music_liveness.py` (16).
- **A missing root is a WAITING root** (2026-09-12, `dlna_localfs_watch.py`).
  Roots register with `WATCH`; absent ones are re-checked every
  `LOCALFS_ROOT_RECHECK_SEC` (30 s) on a thread that retires when nothing
  waits. The file server starts on the FIRST root to arrive; later ones
  widen it via `add_allowed_root` (rebinds the tuple, canonicalised).
  State flips to ACTIVE *before* the callback; a failed activation is NOT
  retried; an unconfigured root is not tracked. `GET /api/libraries` + the
  `libraries` SSE event drive the PWA's `#library-bar`; a failed fetch
  leaves the bar as it was. Tell: `path-traversal blocked: <music path> not
  under (<books root>,)`. `tests/test_localfs_watch.py` (25),
  `tests/frontend/test_library_bar.py` (8).
- **A rescan may only call a file gone when the walk looked**
  (2026-10-10, `dlna_walk.trusted_removals`). Removals under a folder whose
  listing errored are held back (component match — `Lost` ≠ `Lost Tapes`);
  a walk that saw zero audio against a non-empty index removes nothing; a
  file whose `stat` fails is unreadable, not gone. Deleting the LAST audio
  file under a root therefore no longer de-indexes it.
  `tests/test_walk_trust.py` (9).
- **Folder-album identity** (2026-08-25, `_localfs_album_group` in
  `dlna_library_sql.py`, used by all six localfs GROUP BY sites). A folder
  whose album tags are all blank has claimed nothing: blank-album rows group
  **per artist**, the artist branch PREFIXED `'a:'||artist`; `album_tracks`
  narrows on the ROW not the folder; narrowing may never EMPTY an album
  (fall back to the whole folder). `_localfs_album_name` falls through to
  the folder leaf. No rebuild fixes untagged media.
- **Missing artist tags — infer, or hand it over** (2026-08-25).
  `dlna_artist_infer.infer_artist` is the ONE decision shared by
  `tools/artist_from_folder.py` and the `- Unknown Artists -` sweep.
  Evidence order: sibling unanimity → uncontradicted folder name → a folder
  name a sibling confirms; everything else `""`. A blank asks a person, a
  wrong name is never questioned. Refusals: self-named compilations, `VA -`
  folders outrank unanimity, dated slugs, whole-string junk matching, artist
  ends at the earliest of `" - "`, `(`, `[`, `{`. Unattributable = **`Anon`**
  (`ANON_ARTIST`); always test with `is_unattributed()`, never `== ""`.
  `is_a_performer_name()`: known-album check is filename-only (self-titled
  albums), `allow_numeric` (112, 911, 98° are bands), `Various Artists`
  protected before the junk set. The worklist syncs both ways, prunes rows
  whose file is gone (it is generated, unlike a curated playlist), and
  audiobooks opt out. `tests/test_artist_infer.py` (26),
  `tests/test_unknown_artists_playlist.py` (20).
- **`metadata_overrides`: NULL means no override, `''` is an override**
  (2026-08-25). Writer stores `None` for blank fields (`_blank_to_null`);
  reader wraps each subselect in `NULLIF(…,'')` before the COALESCE.
  `tests/test_override_blank_masking.py` (10).
- **"Show me this artist" is the same on every surface** (2026-08-25).
  `artist_albums` reports the QUERIED artist and THEIR track count per
  folder (that `artist` field builds the PWA `onOpen`, the Naim `galbum:`
  and CarPlay `al:` ids, so `album_tracks` narrows everywhere), plus `own`
  (`is_own_album`: ≥ half the folder, or ≤ 2 performers). Non-own rows fold
  behind "Appears on N compilations" (always folded, never remembered);
  Naim gets one `gappears:` container, CarPlay gets order + a display-name
  marker (never change the id). An artist with no own records shows
  appearances directly. `Various Artists` never narrows.
  `tests/test_album_grouping.py` (44), `tests/frontend/test_appears_on.py` (11),
  `tests/test_appears_on_devices.py` (10).
- **An album has TWO dates** (2026-09-21). `year` = EDITION (`MAX` of tag
  years, `_ALBUM_EDITION_YEAR`), `year_original` = oldest recording
  (`MIN(_EFFECTIVE_YEAR)`); `album_tracks` carries `year_edition` per row.
  The client's `albumYearLabel(year, original)` is the ONE display rule
  (`2007 · orig. 1967` at ≥ `REMASTER_GAP_YEARS` 3). Join a NARROWED
  `(SELECT url, year FROM metadata_overrides)`, never the table, and order
  by the expression, not bare `year`. The DECADE facet deliberately keeps
  `_EFFECTIVE_YEAR`. `tests/test_artist_chronology.py` (12),
  `tests/test_album_year.py` (12), `tests/frontend/test_album_year.py` (7).
- **Songwriting credits** (2026-09-20/21). `tracks.composer`/`lyricist`
  from tags (EasyMP4 needs the keys registered in `localfs_tags.py`); new
  columns must join `upsert_tracks`' change-guard AND `_TRACK_COLS` (the
  UNIQUE migrations rebuild `tracks` from their own DDL), backfilled as
  `''`. MusicBrainz fills gaps into `track_credits` (never `tracks`), via an
  artist's WORKS browse; the file tag wins on read. Display filter
  `dlna_credits.clean_credit` — **never require a particular script**
  (a "has A-Z" rule hid every Russian composer). `tests/test_credits.py` (15),
  `tests/test_credit_cleaning.py` (12), `tests/test_track_credits.py` (10).
- **Artist metadata — the MBID keystone** (`dlna_mbid.accept_match`
  refuses rather than guesses): `&` never split, only fallen back to;
  commas never split; every dash folds to a SPACE; aliases only when no name
  matched; two MB artists sharing a name → refused; MB `score` is text
  relevance, not correctness. Line-ups are INFERRED from membership dates
  (`dlna_lineup.lineup_at`; members rejoin — `begin_date` is in the key; no
  year → no answer; undated members always included) and labelled via
  server-side `lineup_tier`. **`member of band` runs in both directions** —
  check `direction`. Wikipedia bio dropped without its url (CC BY-SA);
  Last.fm needs an APPLICATION key; ListenBrainz was dropped.
  `tests/test_mbid_match.py` (27), `tests/test_lineup.py` (13).

### Frontend (PWA)

`static/index.html` + `static/app.js` (~3,000 lines, one script) +
`static/app.css` + `static/sw.js`; talks to `/api/*` JSON. Full write-ups —
measurements, incidents, the things that bit — are in
**[docs/FRONTEND.md](docs/FRONTEND.md)**. Load-bearing rules:

- **New controls bind through `bindClick(id, handler)`.** A top-level
  `$("id").addEventListener` on a missing element kills every line of
  `app.js` below it (a half-updated SW cache pairs an old `index.html` with
  a new `app.js`; tell: "works on iOS, not on Mac"). Bump `APP_CACHE` when
  `index.html` gains an element `app.js` reaches for.
- **SW cache tiers** (`sw.js`): `APP_CACHE` shell is **network-first** (the
  cache is the offline fallback only — stale-while-revalidate once pinned a
  broken `app.js` forever); `install` calls `skipWaiting()`
  unconditionally, `activate` calls `clients.claim()`. `ART_CACHE`
  cache-first. `API_CACHE` stale-while-revalidate ONLY for the
  `CACHEABLE_API` allowlist of stable browse GETs; state / servers /
  renderers / index status / favourites / track_meta / radio / stream /
  POSTs stay network-only. Bump a cache's version to force-evict.
  `tests/frontend/test_pwa.py::test_poisoned_shell_cache_still_renders`.
- **Source picker** `#source-sel` appears with more than one MediaServer:
  `selectSource(udn)`, `rebuildSourceSel()` (💾 localfs / 🗄 other).
  `tests/frontend/test_source_picker.py`.
- **`/art?url=&size=N`** — same-origin artwork proxy (iOS refuses
  cross-origin lock-screen art). `size` snaps to 96/256/512/1024 via
  `art_fetch_scaled` (shared with Subsonic `getCoverArt`); `artUrl(raw,
  size)` uses `ART_THUMB` 256, `ART_COVER` 512, `ART_FULL` 1024 (now-playing
  AND MediaSession — one URL, one cache entry). Sizes are fixed, never
  dpr-derived. 12 MB cap, `image/*` only, 10 s timeout; Pillow optional
  (absent → original). `tests/test_asgi.py::TestArtProxy`.
- **Layout** — four zones: ≤767 phone, 768–1023 tablet (two columns),
  ≥1024 desktop, and `max-height:500px and min-width:768px` phone-sideways,
  which **must stay LAST** in the CSS. `#bottom-nav` stays
  `position:fixed`; `.tab.tablet-only{display:none}` is declared BEFORE the
  tablet query; no inline styles on `#source-sel`/`#output-sel`; manifest
  `"orientation": "any"`. One `renderAlbumRows()` for every album list;
  the grid needs `grid-auto-rows:max-content` and `display:block` cards.
  Navy palette: `--ink-dim` `#B4C9E6` (10.8:1) carries secondary text; no
  DM Mono weight 300. `tests/frontend/test_layout.py` (26 tests).
- **An idle open PWA must be quiet** (battery). Polls are two-tier
  (`_LOOPS` / `_armLoop` / `_runLoop` / `kickPoll`): the fast tier is the
  original interval while active; slow 20–60 s tiers are only a safety net
  because SSE pushes call `kickPoll`. `bumpPolling()` from every
  play/stop path; a throwing poll re-arms itself. The vinyl `playing` class
  follows `<audio>` play/pause; every loader uses `showSpinner(el)` (15 s
  watchdog); `<select>` rebuilds skip identical lists. SSE heartbeat 45 s.
  `<audio preload="auto">` deliberately unchanged.
  `tests/frontend/test_battery.py` (10).
- **MediaSession `playbackState`** is driven off `<audio>` play/pause events
  (single source of truth → dependable one-tap resume on CarPlay / lock
  screen). Hands-free resume after a call is impossible for a web page.
  `tests/frontend/test_mediasession.py`.
- **MediaError**: code 1 ignored, 2/3 retry the track once then skip, 4 skip
  at once; every event POSTs to `/api/client_log`.
- **Stall watchdog** (`_stallCheck`, 2 s poll): no `currentTime` progress
  for 12 s → one re-seek nudge → then `audio_stall` report + advance.
  Progress resets everything; a user pause is not a stall; never runs for
  UPnP output; the error path and `_browserPlayIdx` stop it (no double
  skip). `tests/frontend/test_stall_watchdog.py`.
- **Every `play()` goes through `_playBrowserAudio()`** — on
  `NotAllowedError`/`AbortError` it resets the button, toasts, and POSTs
  `play_rejected`.
- **`/api/client_log`** logs browser reports under `dlna.client`
  (`grep "client_log\[audio_error" gateway.log`); fields are clamped.
  Diagnostic order: `dlna.client` lines → `/tmp/dlna-gateway.err` → Safari
  Web Inspector.

### Concurrency Notes

- All DB writes are serialized through `db_pool`'s write lock; reads use thread-local connections in WAL mode.
- SOAP calls to UPnP servers are throttled by a semaphore (max 3 concurrent) in `dlna_content.py` to avoid overwhelming slow UPnP servers.
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

`proxy_stream()` (defined in `dlna_stream_proxy.py`, re-exported from `dlna_player`) relays bytes from the media server to the browser over a single HTTP connection with Range support.

- `PROXY_IDLE_SEC = 300` (module-level so tests can monkey-patch) — if the browser stops consuming bytes for 5 minutes (laptop suspended, tab closed without clean FIN), the proxy tears down the upstream connection. On laptop wake, the user starts playback again from the beginning; there's no resume.
- Every session logs `proxy_stream ▶ START host/path` and `proxy_stream ■ END host/path sent=N bytes in Xs reason=<r>` with reason ∈ `{upstream_eof, client_idle_timeout, client_closed, error:<Type>}`.
- The gateway is NOT in the audio path for UPnP renderers (the renderer streams directly from the file server — RoHaLocalFS on `:8200`); the proxy only matters for browser-audio playback.
- No HTTP keep-alive — `Connection: close` on both ends. Each browser Range request opens a new upstream TCP connection. On LAN this is ~1ms; on Tailscale it's ~50-100ms per seek. Acceptable for current load; would need a connection pool if users start complaining about seek latency over the tailnet.

#### Two things the relay must keep doing (2026-08-25)

Both were diagnosed from `gateway.log` after a "songs skip at random, then it
just stops mid-playlist" report. Native route: `dlna_asgi_media.
_audio_relay_response` (shared by `/stream` and Subsonic `/rest/stream`).

**1. An upstream error body is NOT media — never relay it.** The relay took
`status = resp.status` and streamed whatever came back, typed as the
upstream's content-type normalised into an audio MIME. A playlist row
pointing at a track the index no longer has resolves to a **404 on the
LocalFs server**, so `<audio>` was handed a 404 page labelled `audio/flac`,
reported `MediaError.code 4` "unsupported format", and the PWA skipped the
track — with nothing in the log saying why. Now only **200/206** stream;
**416** passes through bodyless (a real Range answer); everything else logs
`stream ✗ upstream <status> …` at WARNING and returns the opaque
`502 {"error":"stream unavailable"}`. **Do not "improve" that by forwarding
the upstream status** — same rule and same reason as `/art` (§Security
posture 2): the status would be a probe oracle. The refusal path must also
release the `AUDIO_RELAYS` slot; leaking one per dead row would wedge
`/stream` after 64 of them. Guarded by
`tests/test_asgi.py::TestStreamUpstreamErrors`.

**2. `_MAX_SLICE` caps a ranged response — because HTTP/2 has no
backpressure here.** Hypercorn 0.18's `StreamBuffer.pop()` unpauses the
producer whenever the chunk it popped is under the low-water mark —
**including the empty chunk it pops when the peer's flow-control window is
shut**. A stalled reader therefore does not stop us: the relay generator runs
to EOF and the whole remainder of the file lands in the worker's memory.
Measured on this gateway: three 50 KB/s clients pulling one 70 MB FLAC took
RSS **195 MB → 383 MB**, and every `stream ■ END` was logged **within 0.1 s
while the clients still had minutes of reading left** — so the END line's
`sent=` and `reason=` were describing a buffer fill, not a delivery. HTTP/1.1
backpressures properly; **the PWA is on h2, so the PWA is the case that
breaks**. In the wild Safari was seen opening **eleven concurrent Range
requests for one track** (10:38:24, 2026-08-25) — ~770 MB of buffer for one
70 MB file, with relays lingering 160–208 s after their track was over.

We cannot see how much the client consumed, so we bound what we hand it: a
request **that sent a Range** gets at most `_MAX_SLICE` (8 MB ≈ 70 s of
16/44 FLAC; env `STREAM_SLICE_BYTES`, 0 disables) with a **truthful
`Content-Range`/`Content-Length`**, and asks for the next slice when it wants
more. Serving less of a range than was asked for is ordinary HTTP and is
exactly how `<audio>` already drives this endpoint. After the fix the same
three-slow-client test moved RSS 114 MB → 133 MB (**10× less**).

> ⚠️ **A request with NO Range header is left alone**, deliberately: it has
> not shown itself Range-aware, and truncating it would corrupt a plain file
> download (curl, an Amperfy full-file sync). **Bit-perfect is unaffected** —
> verified after the change: a no-Range pull and a 9-request walk of the
> whole file both `sha256`-match the source file on disk.
> `_clamp_content_range` is pure precisely because an off-by-one there is a
> corrupt audio stream that the browser reports as an unplayable file rather
> than as a bad byte count. Guarded by `TestStreamSliceCap`.

### HTTPS / Tailscale cert

The gateway auto-detects `*.crt` + `*.key` in the working directory on startup. Currently uses a Tailscale-issued Let's Encrypt cert: `ronsmacmini.tail5be6ad.ts.net.{crt,key}`. Mobile devices on the tailnet get a trusted cert with no install or exception needed.

**Cert renewal is automated** via `renew-cert.sh` + `com.roha.dlna-cert-renew` LaunchAgent (Mondays 04:30). The script no-ops unless the cert has < 30 days left, then runs `tailscale cert …`, and **only if the expiry actually MOVED** restarts every LaunchAgent in `CERT_RELOAD_LABELS` (`.env`, comma-separated; unset → the gateway alone). Both rules came from `cert-renewal.log` (2026-10-23): on 2026-07-20 Tailscale answered "Public cert unchanged" and the script still logged "renewal complete" (now a WARN, and nothing is restarted); and epub-reader loads these same files but was never restarted, so after the 2026-09-21 renewal it served the old certificate until a development restart happened along, ~7 h before that one expired. **`.env` is READ by the script, never sourced** (`env_get`; the real environment still wins): sourcing executes it as shell, so `GW_NAME=DLNA Gateway (IINA)` was a syntax error that made `.` abandon every key after line 48, an unquoted `a, b` assigned `a,` and ran `b`, and `KEY=x word` would RUN `word`. Found by the first live `--force` run, 2026-10-23. `tests/test_renew_cert.py` (8) runs the real script in a sandbox under `/bin/bash` 3.2. All output appended to `cert-renewal.log` (separate from `gateway.log` for grep-ability). Belt-and-braces: on every successful HTTPS bind the gateway logs a WARN if the cert has < 14 days left (`_warn_if_cert_expiring_soon` in `dlna_gateway.py`) — surfaces a silently-dead LaunchAgent.

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

### HTTP/2 · TLS

Hypercorn terminates TLS and negotiates HTTP/2 (ALPN) on `:8443`, using the
`tailscale cert` above; `:8765` (incl. the Naim-facing `/gw/*` surface) and
the LocalFs file server (`:8200`) stay plain HTTP for the renderers. HTTP/3
would be a `--quic-bind` add. **`tailscale serve` was tried and dropped**
(broken on this mini's Tailscale `:443`) — the app owning TLS is the chosen
end-state.

## Library backend — LocalFs

**RoHaLocalFS is the live backend**: in-process indexer + bit-perfect file
server (`:8200`), folder-based album grouping, gapless auto-advance via
`SetNextAVTransportURI` (Naim-verified). The gateway owns the one index and
scans on its own terms. `UpnpProvider` is kept — it is how MinimServer / any
generic UPnP server is supported.

### Non-negotiable rules

1. **Bit-perfect.** Serve the **original file bytes, unmodified.
   Never transcode.** A checksum of served bytes must equal the
   source file. The same rule that applies to the existing browser
   `/stream` proxy (`dlna_stream_proxy.proxy_stream`) applies to the
   new file server.
2. **Modular by default.** Adding a new provider must NOT require
   touching the gateway core — it's a new file implementing
   `LibraryProvider`, registered with `dlna_providers`.

### The `LibraryProvider` seam

A thin provider interface decouples the gateway from the backend
choice. **Multiple implementations coexist**; each renderer/UDN
can be browsed via whichever provider its source server is bound
to. The gateway speaks only the seam, never the wire protocol
directly.

```python
# dlna_providers/__init__.py — abstract seam
from typing import Protocol, Iterator

class LibraryProvider(Protocol):
    """One library source — a UPnP server, Plex, Jellyfin,
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
  `transferMode.dlna.org`) are required. Serving is hand-rolled rather
  than a framework static-file helper, so these can be set.
- **SSDP is not required** for the LocalFs file server. The gateway
  pushes URIs to the Naim via `AVTransport`; the file server is just
  an HTTP server on its own port, with no device advertisement.
- **The gateway's own SSDP announcer** (the "gateway-as-MediaServer
  for Naim playlists" feature, currently in `api_upnp.py`) is
  unaffected. It announces the *gateway's own* playlist tree, not
  a provider's library.

### Backend implementations

Each provider lives at `dlna_providers/<name>.py`:

| File | Backend | Wire protocol | Notes |
|---|---|---|---|
| `dlna_providers/upnp.py` | MinimServer, any UPnP server | UPnP SOAP (ContentDirectory) | Wraps `dlna_content.py` + `dlna_discovery.py`. |
| `dlna_providers/localfs.py` | The in-process backend (live) | Filesystem + in-process HTTP file server | Owns the SQLite index AND audio serving. |
| `dlna_providers/plex.py` / `jellyfin.py` | Plex / Jellyfin | Their HTTP APIs | **Not built** — weekend projects on the proven seam. |

**Discovery vs. configuration.** UPnP providers are discovered
via SSDP. Plex / Jellyfin / LocalFs are configured
via `config.json` because they have no SSDP advertisement (or, in
LocalFs's case, no separate device). The provider registry chooses
which implementation handles a given UDN based on the device's
declared `name`/`model` or the explicit config block.

### Open questions

- **Mixed-provider browse.** With more than one source, the PWA shows
  separate trees switchable from the source picker; a merged view is
  not built.
- ~~ReplayGain / loudness~~ — CLOSED 2026-07-12: pass tags through,
  never act on them (bit-perfect). Loudness normalization was built
  then removed (2026-05-31, negligible benefit in peak mode + broke
  browser bit-perfect — see "Volume control"). Not tracked anymore;
  a future perceptual/LUFS feature would be a fresh decision.
- ~~DSD / `.dsf` playback~~ — **CLOSED 2026-09-22: not worth building.**
  Measured before deciding, so it need not be re-derived:
  - **`easy=True` returns RAW ID3 FRAMES for DSF.** A hand-built DSF with
    real tags read back `keys=['TALB','TCOM','TIT2','TPE1']` and
    `title -> None`, so `_read_tags` yielded title=filename-stem and
    artist/album/genre/composer/year all EMPTY. Every DSF would index
    fine and browse as an untagged stray in `- Unknown Artists -`. Same
    class as the EasyMP4 composer trap, but for every field — and
    AcoustID/beets cannot fingerprint DSD, so there is no recovery path
    except hand-tagging.
  - **`_dedup_clause` hides DSD behind PCM.** DSD reports
    `bits_per_sample = 1`, so any 16- or 24-bit copy of the same track
    outranks it: seeding a 24/96 FLAC and a DSD64 of one track stored 2
    rows and browsed **1**, the FLAC. Silent.
  - **It would play on ONE surface.** No browser decodes DSD (`<audio>`
    → `MediaError code 4`), and neither does Amperfy, so the PWA and
    CarPlay are out. Transcoding to PCM would fix that and break
    non-negotiable rule #1. UPnP-to-the-Naim only.
  - **~6× the bytes**: DSD64 stereo is ~5.6 Mbit/s ≈ 42 MB/min, so a
    45-minute album is ~1.9 GB against ~300 MB for 16/44.1 FLAC.

  Three small fixes would have made it work — map the ID3 frames for
  DSF, add `audio/x-dsf`/`audio/x-dff` to `_GW_SOURCE_PROTOCOLS` (which
  advertises `audio/dsd` while the server sends `audio/x-dsf`), and
  decide what the dedup clause should do with a 1-bit format. None were
  built. **`.dsf`/`.dff` stay in `_AUDIO_EXTENSIONS` on purpose**: a
  stray file then indexes quietly instead of erroring, it just will not
  carry tags.

### Audiophile notes

The Naim fetches the file over TCP, buffers it, and clocks it to its own
DAC. Identical bytes in → identical sound out; the `sha256` check below
makes that certain. The only audible risks are completeness, not
capability: **gapless** (only as good as the `SetNextAVTransportURI`
queueing — test on segued material) and **format coverage** (correct MIME
so the Naim accepts high-res).

### Testing quick-reference

- **Range / 206**: `curl -r 0-1023 -D - http://<host>:8200/localfs/stream/<id> -o /dev/null`
- **Bit-perfect**: compare `sha256` of served bytes vs source file.
- **Gapless**: a known segued album, listen for gaps/clicks at
  track boundaries.
- **Format**: at least one each of FLAC 16/44, FLAC 24/96, FLAC
  24/192, DSF (DSD64), MP3.
- **Multi-provider**: configure a UpnpProvider alongside
  LocalFsProvider; verify both browse trees render and both can play
  to the Naim.

## Dependencies

**NOT all optional.** `requirements.txt` is the canonical list and
explains each entry; the short version:

| Package | Needed for |
|---|---|
| `fastapi` · `starlette` · `hypercorn` | **REQUIRED.** The gateway IS `dlna_asgi:app`; without these there is no server at all. |
| `mutagen` | **Required by the LocalFs backend** — i.e. in practice. Absent, the gateway still boots and serves, and the scan fails loudly with the install command. Only a UPnP-provider setup runs without it. |
| `watchdog` | Incremental rescans (`LocalFsProvider.watch_changes`). |
| `Pillow` | Cover-art downscaling. Absent → full-resolution originals, i.e. slower, never broken. |
| `rich` · `python-json-logger` | Log formatting. Genuinely cosmetic. |
| `python-dotenv` | Convenience only: `dlna_config` has a built-in fallback parser, so `.env` loads either way (`tests/test_env_loader.py`). |

Verified on 2026-08-21 by stripping rich, python-dotenv, mutagen, watchdog and
Pillow from a fresh clone's venv and removing ffmpeg from PATH: it boots,
serves the PWA and the API, and `.env` still loads.

The UPnP/DLNA protocol work itself — SSDP, SOAP, DIDL — is standard library only; the dependencies above are the server, the tag reader and the niceties. Chromecast support was removed in commit `2a8d81e`; `PyChromecast` was dropped from `requirements.txt` accordingly.

External CLI binaries — optional per feature:

| Binary | Used by | Install |
|---|---|---|
| `fpcalc` (Chromaprint) | the **beets** enrichment tool (`tools/beets_enrich.py` via pyacoustid) | `brew install chromaprint` |
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
- **`UPNP_SERVER_ALLOW`** (optional) — comma-separated friendly-name
  prefixes of external UPnP MediaServers to index (e.g. `MinimServer`).
  Unset = none; every other LAN MediaServer is dropped at discovery so
  a TV's media share never shows up as a source
  (`dlna_discovery.server_allowed`). Replaced a hardcoded `"Asset UPnP"`
  prefix that, once that server was retired, refused every external
  server.
- **`LASTFM_API_KEY`** (optional) powers the artist panel's "best
  known for". It is an APPLICATION key from
  `https://www.last.fm/api/account/create` — **an account password is
  not an API credential**. Absent, that one block is omitted.
- Editing the plist itself needs a full `launchctl bootout` +
  `bootstrap` (a `kickstart -k` does NOT re-read plist env — and
  bootout is async: retry the bootstrap if it fails with I/O error).
  Editing `.env` only needs the normal `./setup.sh --restart`.

## Album art persistence

`album_art(artist, album, art_url, source)` — keyed independently of
`tracks`, so a rebuild-index never loses covers; `source ∈ {sibling,
musicbrainz, notfound, manual}`. Full design, measurements and the sources
that were evaluated and declined: **[docs/ALBUM_ART.md](docs/ALBUM_ART.md)**.

- **Where covers come from, in order:** the embedded picture → an image
  BESIDE the music (`dlna_providers/localfs_art.py`, inside
  `_extract_art_bytes` so scan and serve share it; chosen by NAME — "biggest
  wins" picks the back cover; rejections match whole words; a `CD1`/`Disc 2`
  folder also looks in its parent; WMP `_large` accepted, `_small` rejected;
  12 MB cap; magic-byte MIME) → **Phase A** sibling harvest
  (`_backfill_album_art`, after every `upsert_tracks` + at startup) →
  **Phase B** `ART_FETCHER` (MusicBrainz + Cover Art Archive; a startup scan
  at +120 s and `trigger()` after each indexer run, no periodic poll;
  `_MB_RATE_LIMIT_SEC = 1.1`). `tests/test_folder_art.py` (23).
- **`notfound` is sticky by design.** Retry:
  `DELETE FROM album_art WHERE source='notfound' [AND artist='…' AND album='…']`,
  then restart (120 s scan) or rebuild.
- **`localfs-art:<sha1>` is a scan-time MARKER, not a URL.** The harvest
  heals markers first, refuses to harvest new ones, LEAVES orphan markers
  (deleting sends the album to MusicBrainz), and only pushes a NON-EMPTY
  `art_url` onto tracks — a boot line reporting the same non-zero `filled=`
  every time is a no-op being counted. `tests/test_album_art_sentinel.py` (9).
- **Queries are ordered attempts** (`dlna_art_query.art_queries`): the exact
  `(artist, album)` ALWAYS first, then a tidied title, then title-only —
  only for multi-artist folders. `tidy_album` is LOOKUP-only, never identity
  or display; an all-cruft name comes back unchanged.
  `tests/test_art_query.py` (14).
- **`bare_albums(skip_udns, min_tracks, with_performers)`** filters are
  opt-in: skip audiobooks, skip 1–2-track strays, report multi-artist so
  title-only can run. Look up **release-group first, then release** (art
  attaches to a release — this is what rescues compilations).
- **Ceiling** ≈ 85% of albums; the rest are compilation-shaped names no
  database holds. `album_art` is keyed `(artist, album)` while a LocalFs
  album is a FOLDER, so art statistics never line up one-to-one. Retagging
  is the remaining lever, not another lookup service.

## FIXES.md — the rolling why-log

`FIXES.md` holds write-ups for bugs where **the diagnosis took longer than
the patch**, or where the symptom looked like something it wasn't. `git log`
has the *what*; this has the *why*, the measurements that proved it, and the
"don't re-introduce this" notes.

**It keeps only the three most recent entries.** Add a new one at the top
(`## <sha> — YYYYMMDD — <title>`, newest first), then:

```bash
python3 tools/rotate_fixes.py                 # what would be dropped
python3 tools/rotate_fixes.py --apply         # rotate
python3 tools/rotate_fixes.py --check         # exit 1 if over the limit
python3 -m unittest tools.test_rotate_fixes -v   # 16 tests
```

Nothing is lost when an entry rotates out — **the sha in the heading is the
point**, not decoration: the entry stays reachable in git history at exactly
the commit it names. The tool's `split_entries`/`rotate` are pure and tested
directly because the risk in a file-trimming script is deleting the wrong
half; `###` subsections inside an entry don't split it, and a `##` heading
without a date (ordinary prose in the preamble) is never treated as an entry.

## Backing up library.db (weekly)

`library.db` holds the index **and everything a person created that
deliberately survives a rebuild** — playlists, album favourites, audiobook
resume positions, play counts, lyrics, radio stations, `metadata_overrides`,
`book_meta`. Re-indexing rebuilds `tracks`; it cannot rebuild those. The file
is gitignored and, since the 2026-08-20 history rewrite, purged from git
history entirely — so **git is not a safety net for it**. This job is.

**`backup-library.sh` + `com.roha.dlna-library-backup` LaunchAgent (Sundays
03:30)** — deliberately a different day/hour from the cert renewal (Mon
04:30) so the two never contend. Output appends to `library-backup.log`.

The one thing that makes it correct: **`sqlite3 .backup`, not `cp`.** The
gateway runs WAL mode and is writing while the job fires; a plain copy can
capture a torn page set or miss a checkpointed WAL and produce a file that
opens cleanly and is subtly wrong. `.backup` uses SQLite's online-backup
API — consistent, concurrent-safe, no downtime.

Every snapshot is verified before it counts: `PRAGMA integrity_check` **and**
a non-zero `tracks` count, because an empty-but-valid database passes
integrity_check happily and would be a silent disaster to keep while pruning
a good one. Snapshots are written to `.part` first, so an interrupted run
can't leave a half-file looking valid. Pruning runs only AFTER a good
snapshot exists, so a failing run never reduces how many backups you have.

Retention: newest 8 (≈2 months, ~64 MB each). `BACKUP_DIR` / `KEEP` override
via env or the plist.

```bash
./backup-library.sh              # snapshot now
./backup-library.sh --list       # what exists
./backup-library.sh --keep 12    # override retention for this run
launchctl kickstart gui/$(id -u)/com.roha.dlna-library-backup   # run the job
```

**Restore is deliberately manual** — clobbering a live index should be a
choice, not a script:

```bash
launchctl bootout gui/$(id -u)/com.roha.dlna-gateway
cp ~/dlna-gateway-backups/library-YYYYMMDD-HHMMSS.db library.db
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.roha.dlna-gateway.plist
```

⚠️ **The script must stay bash 3.2-compatible.** macOS ships bash 3.2 and
that is what launchd runs it under. `mapfile` was used in the first draft and
failed at exactly that point — retention silently never pruned while the
snapshot itself succeeded, which is the worst shape of bug for a backup job.

## Keeping the toolchain current (weekly check)

`tools/upgrade.py` reports outdated pieces plus KNOWN vulnerabilities
(osv.dev) across brew / pip / dev pins / vendored hls.js, and `--apply`
upgrades them into a fresh venv that the test gates must pass, with an
automatic rollback and a `--rollback` afterwards. `upgrade-check.sh` +
`com.roha.dlna-upgrade-check` (Saturdays 05:00) runs `--check` weekly. The
report ends in an Attention list (DO / DECIDE / WAIT / NOTE); it notifies on
a DO item or a DECIDE/WAIT that is NEW since last week, never on the same
nag twice. It never applies; that stays a person's call.
`--python 3.N` without `--apply` dry-runs a minor move (which packages lack
a wheel).
The README's **version strip** (`tools/versions.py`, between
`<!-- versions -->` markers) is read from the software, never typed. Its
PINNED half (locks, hls.js) is gated by `tests/test_versions.py`; its
PLATFORM half (python, sqlite, openssl, node, ffmpeg) moves with brew, so
`upgrade.py` watches it (stale → a DO item; a passing `--apply` rewrites
it) rather than the suite, which would go red on any clone with another
Python. Run it with `.venv/bin/python`, since bare `python3` has no
playwright. The first run, its two refusals and what they caught:
**[docs/UPGRADE.md](docs/UPGRADE.md)**. On a FastAPI bump, remember that
0.161 stopped flattening included routers into `app.routes`, which is why
`tests/test_asgi.py` reads routes through `_flatten`.

## Restarting the gateway

The gateway runs under launchd (LaunchAgent `com.roha.dlna-gateway`). To restart:

```bash
launchctl kickstart -k gui/$(id -u)/com.roha.dlna-gateway
# or, equivalently, with a venv/deps refresh first:
./setup.sh --restart
```

> ⚠ **A restart can fail for a reason that is nothing to do with the
> app.** `SoftResourceLimits → NumberOfProcesses` in the plist is
> RLIMIT_NPROC, which counts **every process this UID owns**, not the
> gateway's. At 512 a busy desktop (72 Safari WebContent helpers alone,
> 547 total) put the machine over it, and hypercorn could not fork its
> resource_tracker: `BlockingIOError: [Errno 35]` from
> `hypercorn/run.py`'s `ctx.Event()` — *before any app code loads*, so
> it looks nothing like an app fault, and `KeepAlive` respawn-loops on
> it (53 runs on 2026-09-20). It only bites at START, which is why a
> long-running gateway survives a busy desktop and only the next restart
> fails. Raised to 2048 (system soft limit is 2666). **Check
> `ps -u $(id -u) | wc -l` before blaming your code.** A plist change
> needs `bootout` + `bootstrap`, not `kickstart`.

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

Previously `_send_current()` ignored the return value of `avtransport_send()`. When SetURI failed, the renderer stayed STOPPED, the monitor saw STOPPED, advanced the queue, sent the next track, which also failed — silently chewing through every track in the queue until the user hit kickstart. The symptom was "all 35 songs skipped, nothing in the log, only a restart fixes it."

The fix has two parts:
1. `_send_current()` now checks the SOAP return and emits `✗ SEND FAILED` with the track URL for every failure.
2. `RendererQueue._MAX_CONSECUTIVE_FAILS = 5` caps the damage: after 5 consecutive SetURI/Play failures the queue aborts itself with `⚠ ABORT`. A transient blip (1–4 failures) auto-advances past the bad track and resets the counter on the next success.

### Duration parsing (`_dur_to_sec`)

Track durations in `playlist_tracks` are stored as TEXT in UPnP `H:MM:SS(.fff)` format, NOT as seconds. `_dur_to_sec()` in `dlna_player.py` tolerates every format the DB stores (int, float, empty/None, `H:MM:SS.fff`, `MM:SS`, malformed strings → 0). Prior to 2026-04-23 this was `int(dur)` and blew up silently inside the daemon thread, killing playback before SetURI was ever sent. Regression-guarded by `tests/test_player.py::TestDurToSec` and `TestRendererQueueDurationSafety`.

## Smaller library features — play counts, lyrics, album favourites

Full specs (tables, endpoints, UI): **[docs/FEATURES.md](docs/FEATURES.md)**.
All three tables survive `clear(udn)` / rebuild-index, like `album_art`.

- **Radio play-count bias** — `GET /api/radio` → `LibraryDB.radio_tracks`
  orders by `COALESCE(play_counts.count,0) ASC, RANDOM()` and bumps the
  picked URLs. Only `/api/radio` (and Subsonic scrobble) increment.
  Reset: `DELETE FROM play_counts`. `tests/test_library.py::TestRadioPlayCountBias`.
- **Lyrics (lrclib)** — `GET /api/lyrics?url=` contacts lrclib at most once
  per URL: positive AND `notfound` results are cached forever; a 502 (provider
  unreachable) is NOT cached. Retry one:
  `DELETE FROM lyrics WHERE source='notfound' AND url='…'`. `tests/test_lyrics.py`.
- **Album favourites** — `album_favourites(artist, album, album_key)`. A
  LocalFs favourite is its FOLDER (`album_key` in the PK), so a VA comp is
  one favourite; others keep `album_key=''`. The album-header ⭐ shows only
  for albums with more than one track; browsed via the ⭐ letter-bar entry;
  exposed to the Naim (`favalbum:` ids) and Subsonic (starred albums).

### UPnP exposure (Naim / LG TV)

`api_upnp*` makes the gateway a complete DLNA Media Server. Root: Artists,
Albums (a `#-0-A..Z` letter index), Genres, ⭐ Favourite Albums, Playlists —
plus 📖 Audiobooks and 📹 Videos when populated. Strict clients (the Naim's
dLeyna/GUPnP, LG WebOS) browse ONLY when all of this holds (full tree and
codec details in docs/FEATURES.md):

- `device.xml`: `MediaServer:1`, `<dlna:X_DLNADOC>DMS-1.50`, an `<iconList>`,
  and BOTH ContentDirectory AND **ConnectionManager** (mandatory).
- SCPDs use `<name>` tags (a stray `<n>` broke parsing); the pre-browse
  handshake actions all return empty-but-valid 200s.
- GENA `SUBSCRIBE` returns `SID` + `TIMEOUT`, and the initial NOTIFY fires on
  a **daemon `threading.Thread`, never `asyncio.create_task`** (an
  unreferenced task is GC'd → endless re-SUBSCRIBE, never a browse).
- SSDP NOTIFY announcer **and** M-SEARCH responder.
- Junk names hidden from display only (`_is_junk_name`); every list
  paginates (`RequestedCount=0` = unlimited); ids are base64 codecs
  (`gartist:`, 3-field `galbum:`/`favalbum:` carrying `album_key`); a garbled
  id → an empty container, never a 500.

Diagnose with `GATEWAY_DEBUG=1` and the `GW /gw/…` lines in `gateway.log`.
Guarded by `tests/test_upnp_album_favourites.py`,
`tests/test_upnp_videos_browse.py`, `tests/test_upnp_audiobooks.py`.

## Security posture (audit 2026-08-20)

Unauthenticated except `/rest/*`; Tailscale / the LAN is the access control,
so these controls are all that stand between a LAN peer and the gateway.
Every fix has a regression test verified to fail on the old code. Full
reasoning, measurements and limits: **[docs/SECURITY_POSTURE.md](docs/SECURITY_POSTURE.md)**
(public policy: `SECURITY.md`). **Do not undo any of these:**

1. **SSRF** — `dlna_ssrf.guard(url)` fronts `/art`, `/stream`,
   `/radio_stream` and EVERY redirect hop: private/loopback/link-local
   refused unless the host is a device in `SERVERS`/`RENDERERS`; non-http(s)
   refused.
2. **No oracle** — every failure returns the same opaque 502
   (`art unavailable` / `stream unavailable`); detail goes to the log only.
   Never "improve" this by forwarding the upstream status.
3. **TLS verification is ON** for outbound calls; a radio cert error is a
   real signal, not something to switch off.
4. **Escaping** — `esc()` escapes both quote characters;
   `dlna_discovery._clean_udn`/`_clean_name` sanitise device text.
   `tests/frontend/test_xss.py`.
5. **XML** — every untrusted parse goes through `dlna_xml.safe_fromstring`
   (DTDs refused, 4 MB cap). `tests/test_xml_safety.py`.
6. **Subsonic auth** compares with `hmac.compare_digest`.
7. **Explicit binds** from `.env` via `hypercorn_conf.py`, never `0.0.0.0`;
   loopback must stay bound; a missing address fails startup on purpose.
   `:8443` on the LAN IP serves a cert that doesn't match (→ no SW, blank
   app); a LAN PWA should use `http://<lan-ip>:8765`.
8. **Containment by path COMPONENTS** — `dlna_localfs_http.resolve_within`,
   and open the path it RETURNS (TOCTOU). `tests/test_localfs_traversal.py`.
9. **SSDP** (`parse_location` + `_spawn_registration`): ≤ 8 registrations in
   flight (excess dropped), http(s) only, description body ≤ 512 KB, and the
   LOCATION host must BE the datagram source (`urlsplit().hostname`). A
   too-strict rule shows up as "the Naim stopped browsing", not a red test.
   `tests/test_ssdp_parsing.py`.
10. **Nothing unbounded** — `dlna_xml.read_capped` for every device read
    (`tests/test_resource_limits.py` asserts no bare read is left);
    `AUDIO_RELAYS` and `SSE_STREAMS` are 64-slot caps → 503 + `Retry-After`.
    Slowloris and threadpool exhaustion were measured and are NOT problems.
11. **A dead renderer is cheap** — `snapshot_all` fans out in parallel and
    `_UNREACHABLE_BACKOFF_SEC` (30 s) stops re-dialling a renderer that
    answered `UNREACHABLE`.

**A media file cannot phone home.** The tag reader reads a fixed allowlist of
scalar fields (never the ID3 URL frames); art MIME is **sniffed from magic
bytes, never the declared MIME** (an `APIC` with MIME `-->` stays opaque
bytes), and the allowlist is raster-only — never add `image/svg+xml`.
`tests/test_art_safety.py`. What legitimately leaves the machine is under
"External services"; the privacy-relevant one is video GPS → Nominatim.

## External services (outbound HTTP)

The gateway is LAN-only except for album-art, artist-metadata, songwriting-credit, lyrics and radio-catalogue lookups. All over TLS:

| Host | Purpose | Method + path |
|---|---|---|
| `musicbrainz.org` | Resolve `(artist, album)` → release-group MBID; resolve an artist name → artist MBID; browse an artist's **works** for composer/lyricist; fetch an artist's life-span, genres and band members | `GET /ws/2/release-group/?query=…`, `/ws/2/release/?query=…`, `/ws/2/artist/?query=…`, `/ws/2/artist/{mbid}?inc=…`, `GET /ws/2/work?artist={mbid}&inc=artist-rels&limit=100` — all `&fmt=json` |
| `coverartarchive.org` | Confirm a front cover exists for that MBID | `HEAD /release-group/{mbid}/front-500`, falling back to `/release/{mbid}/front-500` — 200/301/302/307 counts as "have it", 404 counts as "no cover". Art attaches to a RELEASE; a release-group only reports one when a release is flagged as its cover, which is why compilations need the fallback |
| `en.wikipedia.org` | The biography paragraph + the artist photo behind the ℹ️ panel | `GET /api/rest_v1/page/summary/{title}` — CC BY-SA, so **the text is dropped when its url is missing**: the link IS the attribution and they travel together or not at all |
| `ws.audioscrobbler.com` | "Best known for", by actual listening | `GET /2.0/?method=artist.gettoptracks&api_key=…` — needs a registered APPLICATION key (`LASTFM_API_KEY`), not an account password. Absent → the block is omitted and nothing else changes |
| `lrclib.net` | On-demand lyrics for the currently-playing track | `GET /api/get?track_name=&artist_name=&album_name=&duration=` — 200 with body or 404 |
| `*.api.radio-browser.info` | Internet-radio station catalogue search | `GET /json/stations/search?name=&tagList=&countrycode=&hidebroken=true` — see the "Internet radio" section |

(beets calls AcoustID from its own process; the key-types bullet below is
kept for beets debugging.)

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

## `metadata_overrides` invariants

beets is the sole metadata authority (see `tools/beets_enrich.py`); there is
no in-process tagger. Historical `source='acoustid'` rows remain as data —
`tools/post_beets_reindex.py` clears them after a beets run so file tags show
through.

- `metadata_overrides.source ∈ {manual, acoustid, notfound, video_skip}`;
  a row of ANY source means "processed"; `manual` always wins; sticky
  negatives are released by deleting the row.
- The 16/24-bit duplicate handling (wide UNIQUE incl. bit_depth/
  sample_rate + `_dedup_clause` browse filter + `idx_tracks_udn_url`) is
  documented in the "Database Schema" comments and `tests/test_dedup.py`.
- Year model: `tracks.year` = file-tag year; `metadata_overrides.year` =
  original release year; display prefers the override
  (`_renderNpYear`, "(remastered)" at 3+ year gap); `tests/test_year.py`.

## Bit-perfect notes

**Verdict:** the gateway is byte-perfect on every path it controls.
No resampler, EQ, mixer, or DSP exists anywhere in the playback
code. Confirmed by audit on 2026-05-11.

**Naim / UPnP path.** The gateway is not in the audio path. The file
server (RoHaLocalFS)
serves bytes directly to the renderer; the gateway only sends
`AVTransport::SetURI` + `Play` SOAP. The startup volume and the user
trim slider are applied via `RenderingControl::SetVolume` SOAP, which
adjusts the renderer's **hardware volume** — never PCM modification.
See `RendererQueue` in `dlna_player.py` (its transport/volume/monitor
mixins live in `dlna_player_transport.py`, `dlna_player_volume.py` and
`dlna_player_monitor.py`),
`dlna_avtransport.avtransport_send` and
`dlna_rendering_control.set_volume`.

**Browser stream proxy (`/stream`).** Byte-perfect Range pass-through.
`dlna_stream_proxy.proxy_stream` relays bytes verbatim; the only mutation is a `Content-Type` header
normalisation (`audio/x-flac` → `audio/flac`) for Safari quirks.

**Browser `<audio>` caveat (not a gateway behaviour).** In
browser-output mode the trim slider sets
`browserAudio.volume = 10^(db/20)` (the `volume` branch of `control()` in
`static/app.js`). When
non-zero, the **browser** scales every PCM sample by that factor —
HTML5 `<audio>` behaviour, not the gateway. Default = 0 dB
(volume = 1.0 = no scaling), so unmodified playback is the default.
**Keep the slider at 0 dB for bit-perfect browser playback.** UPnP
output is unaffected — the same slider then routes through
`/api/control` and the gateway calls `SetVolume` on the renderer
hardware.

## Bit-perfect on macOS / iOS

The listener-side settings (Audio MIDI Setup sample rate, Sound output
enhancements, the iOS EQ/accommodation switches, and why browser playback can
never be fully exclusive-mode) are user documentation and live in
**README.md → "Bit-perfect listening"**. Best chain: play to the Naim via
UPnP — neither the gateway nor CoreAudio is in the audio path.

## Library housekeeping tools

Full documentation for every tool — flags, safety rules, live-run results and
the incidents behind them — is in **[docs/TOOLS.md](docs/TOOLS.md)**. Common
contract: **DRY-RUN by default, `--apply` writes**; nothing moves, renames or
deletes media unless a flag says so; each tool has `tools/test_<name>.py`
(the risky logic is pure and tested directly). Run order for enrichment:
beets → `artist_mbid` → `artist_meta` → `track_credits`.

| Tool | Does | Must-know |
|---|---|---|
| `prune_empty_music_dirs.py` | Trashes dirs whose subtree has no music | Rule B: anything inside an album root is kept; `.mp4` is not music; hitting `--limit` executes no deletions |
| `find_corrupt_audio.py` | Flags files whose magic bytes don't match the extension | Scan-only by default; writes `corrupt-audio.txt`; unreadable ≠ corrupt |
| `artist_from_folder.py` | Writes a missing artist tag from folder evidence (`dlna_artist_infer`) | Re-reads each file before writing; **NO fallback when a container refuses a tag** — the ID3-prepend fallback destroyed 15 WAVs |
| `tag_from_filename.py` | Artist tag from the filename for untagged strays | `NN - Title` yields NO artist; never overwrites an existing artist |
| `audit_playlist_orphans.py` | Relinks playlist rows whose track id went stale (renamed folder) | Unmatched rows are KEPT unless `--remove-unmatched`; backs up first |
| `compilation_playlists.py` | Playlists for album TAGS scattered across folders | ≥5 tracks, ≥3 artists, no folder with ≥5 of them |
| `folder_compilation_playlists.py` | Playlists for one-folder compilations | A folder whose tracks declare no album never qualifies; tracks in `file_path` order |
| `artist_mbid.py` | Artist → MusicBrainz id (the keystone) | `dlna_mbid.accept_match` refuses rather than guesses; `notfound` sticky |
| `artist_meta.py` | MBID → facts, bio, best-known, line-up | **Set `LASTFM_API_KEY` before the first run** — `meta_fetched_at` marks done |
| `track_credits.py` | Composer/lyricist via an artist's MB works | Writes `track_credits`, never `tracks`; ~2 h |
| `correct_year_drift.py` | Year from the earliest copy in the library | Live-recording filter; ≥1950 floor; writes `source='manual'` |
| `improve_song_years.py` | Earliest MB recording year per song | `song_year_cache`; ~10 h full sweep; never touches user `manual` rows |
| `beets_enrich.py` | beets tag-in-place batch | Hard gate `write=yes copy=no move=no`; beets via **brew, not pip**; needs the `musicbrainz` plugin + `musicbrainzngs` |
| `post_beets_reindex.py` | Makes beets' tags visible | Deletes ONLY `source='acoustid'` overrides, then rescans |
| `openlibrary_books.py` | Audiobook series/author → `book_meta` | English/Dutch only — non-ASCII series names hard-rejected |
| `book_language.py` | Asserted audiobook languages | `--set` refused without `--match` |
| `immich_import.py` | Copies new Immich videos into GWMovies | Content-hash dedup is sticky; `--reimport-deleted` to undo that |
| `infer_video_locations.py` | Location for GPS-less clips from neighbours | Real-GPS evidence only; needed `--no-device-check` |
| `immich_people_sync.py` | Immich persons → `video_people` | Checksum match, then (name, size) — Immich checksums go stale |
| `upgrade.py` | Whole toolchain to newest: brew (python, sqlite, openssl, ffmpeg, node…), pip, dev pins + Playwright, vendored hls.js; reports osv.dev vulns | Fresh venv, old one MOVED aside; gates decide; failure → auto-rollback, `--rollback` undoes a passed run. Brew downgrades are HELD (pinned). Brew itself is not rolled back |

**Steady-state workflow for new music:** drop files →
`beets_enrich.py --quiet` → `post_beets_reindex.py --apply`.

## Audiobooks

A second LocalFs root (`AUDIOBOOKS_ROOT`, own UDN
`dlna_localfs_wiring.AUDIOBOOKS_UDN`, same `:8200` server), so books stay out
of music browse/search/radio by construction. Full design, phases P1–P6 and
the 🌐 language rules: **[docs/AUDIOBOOKS.md](docs/AUDIOBOOKS.md)**.

- **Multi-root safety for ANY additional root:** `_load_cache` is scoped to
  the provider's own root, and secondary-root track ids are salted
  (`_track_id_for(rel, namespace)`; music keeps `namespace=''`).
- **`kind`** (`audiobooks` | `music`) on `/api/servers` gates ALL resume
  behaviour in the PWA — never the track shape.
- **Resume** = `playback_positions`, ONE row per book (`album_key`; root-level
  single files key by URL), shared by the PWA, the Naim
  (`start_at_sec` → async Seek ~1.5 s after Play; the monitor saves every
  ~15 s) and CarPlay (Subsonic bookmarks). Continue is the default on every
  entry point; a finished book behaves like an unread one. Playback started
  from the Naim's own controller can't resume. `tests/test_positions.py`,
  `tests/test_renderer_resume.py`, `tests/frontend/test_audiobooks.py`.
- **`book_meta`** (OpenLibrary series, `series_seq` REAL) is a display
  overlay; `manual` wins.
- **🌐 Language** is DETECTED on read by `dlna_book_language` (English and
  Dutch only; Unknown when the words don't decide; capitalised names dropped
  but their lowercase connectors kept; each distinct string counts once;
  shared words in neither list). Only ASSERTED languages (`tag` / `manual`)
  are stored in `book_language`. Set by hand in the ✏️ form, which posts the
  chapter URL, never an `album_key`; `.edit-field[hidden]{display:none}` is
  load-bearing. `tests/test_book_language.py` (28),
  `tests/frontend/test_book_language.py` (10).
- Open question: does the Naim accept m4b/AAC over AVTransport?

## Subsonic API (`/rest/*` — Amperfy / CarPlay)

Shipped; the reason it exists is CarPlay. Endpoint table, auth flows, id
encoding and connection steps: **[docs/SUBSONIC.md](docs/SUBSONIC.md)**.

- XML by default (the spec), `f=json` for JSON. Single user from
  `SUBSONIC_USER` / `SUBSONIC_PASSWORD` in `.env`; **no password → 503 on
  every call** (deliberate). Token+salt and plaintext / `enc:` both accepted.
- Ids: `tr:<b64 url>`, `al:<b64 artist\0album[\0album_key]>` (third field
  ONLY when `album_key` is set — keeps non-LocalFs ids byte-identical),
  `ar:<b64>`, `pl:<id>`, `rs:<station_uuid>`. search3 album entries must
  carry `album_key`; match by id, not by name.
- `getAlbumList2` pages alphabetical types in SQL; `random` is day-seeded;
  a bad `size`/`offset` degrades to the default.
- Starring = `album_favourites`; scrobble bumps `play_counts`;
  `getCoverArt?size=` → `art_fetch_scaled`; bookmarks = `playback_positions`;
  radio methods = `radio_favourites`. Never transcodes (`maxBitRate` ignored).
- One INFO line per request — `grep Subsonic gateway.log`; empty during a
  flaky window means the request never reached the gateway. A `100.x` ip is
  the tailnet. Verify live with `tests/subsonic_verify.py`.
  `tests/test_subsonic.py`.

## Internet radio ("📡 Stations")

Complete, all phases. Spec: **[docs/INTERNET_RADIO.md](docs/INTERNET_RADIO.md)**.
Not the "📻 Radio" button (the play-count shuffle). Commercial streaming
services are out of scope (DRM; the Naim does those natively).

- `radio_favourites` is capped at 25 **server-side** (`radio_fav_add` →
  `'full'` → HTTP 409); re-adding is idempotent; survives `clear(udn)`.
- Catalogue = radio-browser.info, proxied and never persisted; `hls=1`
  filtered out; identifying User-Agent.
- A station is a single `is_stream` "track" through `/api/render_queue`;
  `_monitor_decision` never auto-advances a stream.
- Browser path `/radio_stream` → `dlna_radio_proxy` de-interleaves ICY
  metadata (deliberately separate from the byte-perfect `/stream`);
  `/api/radio/nowplaying` reads it (`?stream=`) or the renderer snapshot
  (`?udn=`).
- ⏮/⏭ cycle the favourites like presets. Subsonic's `*InternetRadioStation`
  methods map onto the same table. `tests/test_radio.py`,
  `tests/frontend/test_radio.py`.
