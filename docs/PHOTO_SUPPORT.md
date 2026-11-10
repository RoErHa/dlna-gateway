# 📷 Photos — the Immich library on the LG TV

## Why it exists

The LG TV (OLED55C9PLA, 2019, webOS 4.x, `192.168.1.35`) cannot run Immich's
web app: its browser is too old and shows a blank page. Its **DLNA media
player** works and already browses the gateway's 📹 Videos, so the gateway
also offers Immich photos there, as a 📷 Photos folder.

What that player shows: **JPEG**. Not HEIC, not NEF, not WebP. A third of the
library is HEIC, and Immich's small thumbnails are WebP, so the gateway never
hands the TV an original. It hands it Immich's **preview**: a 1440 px JPEG,
`GET /api/assets/{id}/thumbnail?size=preview`. That preview is also the
item's art.

## Storage: nothing is added

Immich makes that preview for every photo when it is added, for its own web
page and app. It lives in Immich's `thumbs/` folder and existed before this
feature. The gateway fetches it, passes it on, and keeps only the last 48
in RAM (the TV asks for each photo twice: art, then the photo). That is
~30 MB at most. Originals are never opened, converted or copied.

## History

- 2026-11-09: one album (`PHOTOS_ALBUMS`), verified on the TV.
- 2026-11-10: the whole library. `PHOTOS_ENABLED=1` replaced `PHOTOS_ALBUMS`.

## Shape

```
0 (root)
└── 📷 Photos                 photos            (absent while the index is empty)
    ├── 📅 By date            phdates → phyear:<YYYY> → phmonth:<YYYY-MM> → photos
    ├── 📚 Albums             phalbums → phalbum:<album id> → photos
    ├── 👤 People             phpeople → phperson:<b64 name> → photos
    ├── 📍 Places             phplaces → phcountry:<b64> → phcity:<b64 country\0city> → photos
    └── ⭐ Favourites         phfavs → photos
    photo: ph:<asset id>, object.item.imageItem.photo
           res = http://<lan-ip>:8765/gw/photo/<asset id>.jpg
```

An empty branch is left out. Order:
- Years: newest first.
- Months and the photos in any folder: chronological.
- Albums: newest first.
- People: A–Z.
- Places: the country with the most photos first. Inside a country, the
  biggest city first, then "(no city)" for photos with a country only.

A photo title is `YYYY-MM-DD HH:MM · city`.

`<res>` advertises `image/jpeg` with `DLNA.ORG_PN=JPEG_LRG` (1440 px fits
JPEG_LRG's 4096² limit) and `DLNA.ORG_OP=00`: a still image needs no byte
seeking. `image/jpeg` is in the ConnectionManager's `SourceProtocolInfo`.

## What is in it

- **Timeline:** every image Immich shows on its timeline. Videos are in
  📹 Videos instead.
- **Never shown:** trashed, locked and hidden photos.
- **Archived photos:** only inside an album that holds them.
- **Albums:** every album either user owns or was shared.
- **Favourites and people are per Immich user**, so every
  `IMMICH_API_KEY*` is scanned and the answers merged.
  - A favourite of either user is a favourite here.
  - A person is one NAME: if Ron and Marja both name a face "Benjamin",
    that is one 👤 Benjamin.
  - Only named, non-hidden people appear.

Measured 2026-11-10:
- 48,958 images. Each key sees all of them; Immich returns both users'
  photos to either key.
- 31 + 83 albums. 38 named people for Ron, 1 for Marja.
- About 27,000 photos carry a place.
- One full scan takes ~28 s per key. The index is ~20 MB of RAM.

## The modules

| File | Job |
|---|---|
| `dlna_photos.py` | The index. `build()` is pure: Immich items in, one `Library` snapshot out. `PhotoIndex.refresh()` collects the items on a daemon thread every `PHOTOS_REFRESH_SEC` (900 s, min 300), builds off to the side and swaps the snapshot in whole. Browsing never waits on Immich and never sees a half-built library. If no key can read Immich, the previous snapshot stays. `start()` configures the shared `INDEX` **in place**: rebinding it would strand every reader on an empty index. |
| `dlna_photos_immich.py` | The Immich 3 client: paged search, albums, preview. |
| `api_upnp_browse_photo.py` | The browse handlers and their own `EXACT`/`PREFIX` tables, merged into `api_upnp_browse`'s. Each handler reads the snapshot once. |
| `dlna_asgi_photos.py` | `GET`/`HEAD /gw/photo/<asset id>.jpg`, on the plain `:8765` bind with the rest of `/gw/*` (the TV does no TLS there). |

## Immich 3 quirks this depends on

- **`GET /api/albums/{id}` no longer lists the assets.** Every list comes
  from `POST /api/search/metadata`, 1,000 per page, following `nextPage`:
  - the timeline: `{"type":"IMAGE","withExif":true,"withPeople":true}`;
  - an album: the same, plus `"albumIds":[id]`.
- **`isFavorite` and `people` on an item are the caller's.** They differ per
  key; that is why every key is scanned.
- **Albums** are `GET /api/albums` plus `GET /api/albums?shared=true`,
  deduplicated by id.

## Safety rules — do not loosen

- **Immich must be on this Mac.** `IMMICH_URL` has to be a loopback address,
  or the folder stays off with a warning. The keys never leave the process.
- **Only an asset in the index is fetched**, with the key that listed it
  first. A LAN peer cannot walk Immich by id through the gateway.
- **Only JPEG bytes go out** (magic bytes `FF D8 FF`, at most 25 MB).
- **Every failure is the same opaque `502 {"error":"photo unavailable"}`.**
  The reason goes to the log only. Same rule as `/art` (Security posture 2).

Guarded by `tests/test_photos.py`.

## Diagnosing

| Log line | Means |
|---|---|
| `photos: N photos, N months, N albums, N people, N countries, N favourites (Ns)` (INFO) | A refresh finished. |
| `photos: off (needs PHOTOS_ENABLED=1, IMMICH_URL and an IMMICH_API_KEY*)` | Photos is not configured. |
| `photos: an IMMICH_API_KEY* could not read Immich` | One key failed; the others still count. |
| `photos: no key could read Immich; keeping the previous index` | Every key failed; the last good snapshot is still served. |
| `photos: preview of … failed` / `… is not a JPEG` | One photo could not be served; the TV got the 502. |
