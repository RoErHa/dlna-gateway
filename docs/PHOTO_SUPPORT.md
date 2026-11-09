# 📷 Photos — Immich albums on the LG TV (2026-11-09)

## Why it exists

The LG TV (OLED55C9PLA, 2019, webOS 4.x, `192.168.1.35`) cannot run Immich's
web app: its browser is too old and shows a blank page. Its **DLNA media
player** works and already browses the gateway's 📹 Videos, so the gateway
also offers Immich photos there, as a 📷 Photos folder.

What that player shows: **JPEG**. Not HEIC, not NEF, not WebP. A third of the
test album is HEIC, and Immich's small thumbnails are WebP, so the gateway
never hands the TV an original. It hands it Immich's **preview**: a 1440 px
JPEG, `GET /api/assets/{id}/thumbnail?size=preview`. That preview is also the
item's art.

## Status: a single-album test

`PHOTOS_ALBUMS` (in `.env`) names the albums. The first test is Marja's
*Verjaardag Benjamin 6 jaar* (`c6fe38ee-8332-4f49-bb1f-ff278cc8cd39`): 73
photos (45 JPG, 28 HEIC) and 10 videos. The videos are left out: images only.

Next, once the TV test passes: the full tree (by date, albums, people,
places, favourites).

## Shape

```
0 (root)
└── 📷 Photos                        id photos            (absent while the index is empty)
    └── <album name>                 id phalbum:<album id>
        └── <date · city>            id ph:<asset id>     object.item.imageItem.photo
                                     res = http://<lan-ip>:8765/gw/photo/<asset id>.jpg
```

Photos are sorted by their local capture time. The title is `YYYY-MM-DD HH:MM
· city`, or the file name when Immich knows neither.

`<res>` advertises `image/jpeg` with `DLNA.ORG_PN=JPEG_LRG` (1440 px fits
JPEG_LRG's 4096² limit) and `DLNA.ORG_OP=00`: a still image needs no byte
seeking. `image/jpeg` was added to the ConnectionManager's
`SourceProtocolInfo`.

## The modules

| File | Job |
|---|---|
| `dlna_photos.py` | The index and the Immich client. `PhotoIndex.refresh()` reads each album with the first `IMMICH_API_KEY*` that may see it, on a daemon thread every `PHOTOS_REFRESH_SEC` (300 s, min 60), so a Browse never waits on Immich. `start()` configures the shared `INDEX` **in place**: rebinding it would strand every reader on an empty index. |
| `api_upnp_browse_photo.py` | The three browse handlers (`_br_photos`, `_br_phalbum`, `_br_ph`) and the DIDL item. |
| `dlna_asgi_photos.py` | `GET`/`HEAD /gw/photo/<asset id>.jpg`, on the plain `:8765` bind with the rest of `/gw/*` (the TV does no TLS there). |

## Immich 3 quirks this depends on

- `GET /api/albums/{id}` no longer lists the assets. It still gives the
  name. The assets come from `POST /api/search/metadata`
  `{"albumIds":[id],"type":"IMAGE","withExif":true,"size":1000,"page":n}`,
  following `nextPage`.
- An album belongs to one Immich user. Owner's album is read with
  `IMMICH_API_KEY`, Marja's with `IMMICH_API_KEY_MARJA`. Every key whose
  name starts with `IMMICH_API_KEY` is tried in name order
  (`tests/test_env_example.py` knows that family).

## Safety rules — do not loosen

- **Immich must be on this Mac.** `IMMICH_URL` has to be a loopback address,
  or the folder stays off with a warning. The keys never leave the process.
- **Only an asset in the index is fetched**, and only with the key that
  listed it. A LAN peer cannot walk Immich by id through the gateway.
- **Only JPEG bytes go out** (magic bytes `FF D8 FF`, at most 25 MB).
- **Every failure is the same opaque `502 {"error":"photo unavailable"}`**.
  The reason goes to the log only. Same rule as `/art` (Security posture 2).
- Trashed, locked and hidden assets are never listed (`visibility` must be
  `timeline` or `archive`).
- Up to 48 previews are kept in memory: the TV asks for each one twice (art
  and `res`).

Guarded by `tests/test_photos.py`.

## Diagnosing

- `photos: N album(s), M photos` (INFO) after every refresh.
- `photos: off (needs IMMICH_URL, an IMMICH_API_KEY* and PHOTOS_ALBUMS)`.
- `photos: album … could not be read with any IMMICH_API_KEY*`.
- `photos: preview of … failed` / `… is not a JPEG`.
