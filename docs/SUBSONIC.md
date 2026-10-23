# Subsonic API

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

## Subsonic API (shipped — browse/playlists/starring/stream/radio all live)

A read-only-ish Subsonic-compatible HTTP API that lets any third-party
Subsonic iOS client (Amperfy, substreamer, play:Sub, …) browse the
gateway's library and stream from it. **The primary motivator is
CarPlay**: those clients have polished CarPlay implementations, the
gateway PWA fundamentally can't (CarPlay is a closed iOS-native
framework). Subsonic API is also pure HTTP, so it traverses Tailscale
cleanly — unlike UPnP's SSDP multicast discovery which doesn't.

### What the iOS client sees

The Subsonic client connects to **the gateway**. The flow:

```
iPhone Subsonic client (CarPlay)
    ↓ HTTPS over Tailscale
dlna-gateway  /rest/*
    ↓ library.db reads for browse/playlists/favourites/search
    ↓ relays audio bytes from the LocalFs file server for /rest/stream
RoHaLocalFS :8200 (invisible to the client; just a byte source)
```

Every piece of gateway state is exposed:
- Tracks, artists, albums (as indexed into library.db).
- User playlists + the existing `__favourites__` track-level playlist.
- `album_favourites` is exposed as Subsonic's "starred albums".
- `play_counts` increments via `/rest/scrobble` so radio bias keeps
  working from cars too.
- Cover art (gateway's `album_art` cache + embedded art via
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
| `/rest/getAlbumList2?type=&size=&offset=` | `DB.all_albums(order=,limit=,offset=)` | type ∈ newest/alphabeticalByName/recent/random/frequent/starred. The alphabetical types page in **SQL** (`LIMIT/OFFSET`) so Amperfy's full-library sync doesn't reload every album per page; `alphabeticalByArtist` orders by artist. `random` is **day-seeded** so paging is coherent (no dup/missing albums across pages). `size`/`offset` parse tolerantly (a bad value degrades to the default, no 500). |
| `/rest/search3?query=` | `DB.search` | Existing FTS5 search |
| `/rest/getPlaylists` | `DB.pl_list()` | All playlists |
| `/rest/getPlaylist?id=` | `DB.pl_get(pl_id)` | One playlist's tracks |
| `/rest/createPlaylist` `/updatePlaylist` `/deletePlaylist` | `DB.pl_create / pl_add_track / pl_remove_track / pl_delete` | Bidirectional sync |
| `/rest/star` `/unstar` | `DB.album_fav_add / album_fav_remove` | Album-level starring → reuses Album Favourites |
| `/rest/getStarred2` | `DB.album_fav_list()` | Starred albums |
| `/rest/stream?id=` | track ID → URL → `dlna_player.proxy_stream` | Audio (Range supported via existing proxy) |
| `/rest/getCoverArt?id=&size=` | album/track ID → art URL → `api_playback.art_fetch_scaled` | Cover image, downscaled to the `size` box (bucketed 96/256/512/1024, Pillow) so thumbnails aren't full-res originals |
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
