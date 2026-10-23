# Play counts, lyrics, album favourites and the UPnP Media Server

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

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
`Albums`, `Genres` (the **full library**),
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

All four `/gw/*` route groups are native in `dlna_asgi.py` on the
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
