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
- 2026-11-10: big folders split up; screenshots moved into their own albums
  (owner, after checking the tree on the TV).
- 2026-11-10: hidden from the Naim, kept on the TV (owner).

## Shape

```
0 (root)
└── 📷 Photos                 photos            (absent while the index is empty)
    ├── 📅 By date            phdates → phyear:<YYYY> → phmonth:<YYYY-MM> → photos
    ├── 📚 Albums             phalbums → phalbum:<album id> → photos
    ├── 👤 People             phpeople → phperson:<b64 name> → photos
    ├── 📍 Places             phplaces → phcountry:<b64> → phcity:<b64 country\0city> → photos
    └── ⭐ Favourites         phfavs → photos
    any folder of photos over 200 → phsplit:<b64 base id \x1f level=key …>
    photo: ph:<asset id>, object.item.imageItem.photo
           res = http://<lan-ip>:8765/gw/photo/<asset id>.jpg
```

**Not on the Naim.** The Naim is audio-only, so the root does not offer
📷 Photos to it (`api_upnp_browse_photo.shown_to`). A Browse carries the
caller's address; when that address is a discovered renderer whose
device.xml `<manufacturer>` starts with an entry of `HIDDEN_FROM`
("Naim Audio"), the folder is left out of the root and its count. Any
other device, and an address the gateway does not know, sees it. The
Naim browses from its own address, the same one it is discovered at.

An empty branch is left out. Order:
- Years: newest first.
- Months and the photos in any folder: chronological.
- Albums: newest first; the screenshot albums last.
- People: A–Z.
- Places: the country with the most photos first. Inside a country, the
  biggest city first, then "(no city)" for photos with a country only.

A photo title is `YYYY-MM-DD HH:MM · city`.

### Big folders split up

Some folders were thousands of photos long: the album "Recent" 11,840,
"Vastgelegd door mij" 9,121, December 2023 6,645 (an import date). Paging
through those with a remote is hopeless, so **a folder holding more than 200
photos shows sub-folders instead**, in the order the owner asked for:

    country → city → year → month → week

- A level that would not divide the photos is skipped: December 2023 splits
  by country, then city, then (one year, one month) straight into weeks.
- A group of 200 or fewer lists its photos; a bigger one splits again from
  the next level down. Past the week, the photos are listed as they are:
  Trondheim in December 2023 is 215 photos from one day.
- Order: places biggest first, dates old to new; "(no place)", "(no city)"
  and "(no date)" always last.
- Names follow the rest of the tree: "The Netherlands", "Hoorn", "2023",
  "December 2023", "Week 49 · 2023" (ISO week numbers).
- It applies to every folder of photos: albums, months, people, cities and
  favourites. Years, countries and the album list are lists of folders and
  stay as they are.
- `phsplit:` carries the folder it came from plus one `level=key` step per
  level (`c` country, `t` city, `y`, `m`, `w`). A Browse finds the base
  folder's photos the usual way, then narrows them one step at a time. Steps
  must go down the levels, or the id is empty.

Measured 2026-11-10: "Recent" opens in 10 ms (8 countries), its Netherlands
group in 7 ms (122 cities).

### Screenshots get their own albums

At the owner's request, screenshots leave every album and gather in:

- **Mac-screencaptures** — both users';
- **iOS-screencaptures <first name>** — one per Immich user who has any.

Photos inside are old to new, like every folder. They stay on the timeline
(📅 By date, 📍 Places, 👤 People); only the albums change. An album left
empty disappears, which is what happens to Apple's own "Screenshots"/
"Schermafbeeldingen" album. **Nothing in Immich is changed**: this is the
TV's view only.

A photo is a screenshot only on a marker the device or Apple Photos wrote
(`dlna_photos.shot_kind`):

| Marker | Kind |
|---|---|
| File name `Screenshot …`, `Screen Shot …`, `Schermafbeelding …`, `Schermopname …` (macOS) | Mac |
| EXIF description "Screenshot" (iOS writes it) | iOS |
| PNG with make "Apple Computer Inc." (iOS screenshots, also cropped ones) | iOS |
| PNG with no camera at an exact iPhone/iPad screen size | iOS |
| In Apple Photos' own Screenshots album, as synced into Immich | iOS (unless Mac by name) |

A screen-sized **JPEG** without one of those markers is left alone. Checked
by eye on 2026-11-10: of 68 such images, about half were photos (an old
iPhone saved pictures at its screen size, and the metadata of both is
equally empty).

Measured 2026-11-10:
- 363 screenshots, all Marja's. 326 of them agree with Apple's own
  Screenshots album (329); the album's other 3 count through the last rule.
  The 34 found outside that album were all checked by eye: all screenshots.
- **Mac-screencaptures holds one photo.** The Macs' screenshots are not in
  Immich: one "Screenshot 2022-06-14 at 21.47.09" is.
- **Ron has no iOS-screencaptures album.** His screenshots in Immich are
  JPEGs without a marker (converted on import), alike in metadata to his
  real photos, so none qualifies. The album appears by itself once his
  iPhone uploads screenshots as PNGs.

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
| `dlna_photos_immich.py` | The Immich 3 client: paged search, albums, preview, and `users/me` (whose key is whose, to name the iOS screenshot albums). |
| `dlna_photos_split.py` | How a big folder splits: levels, order, names, the step check. Pure. |
| `api_upnp_browse_photo.py` | The browse handlers and their own `EXACT`/`PREFIX` tables, merged into `api_upnp_browse`'s. Each handler reads the snapshot once. `_find()` answers every folder of photos, so a `phsplit:` group finds its base the same way. |
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
