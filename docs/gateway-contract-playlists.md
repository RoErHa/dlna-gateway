# Gateway contract: playlist editing (D-49)

For the **dlna-gateway** project. rAudio (Sprint 11) needs these four
endpoints; the PWA may use them too. Written against gateway commit
`cc1dbc1`; the line references below are to that version. Nothing here
changes an existing endpoint's request or reply.

| Story | Endpoint |
|---|---|
| RA-90 rename | `POST /api/playlist/rename` |
| RA-92 add several | `POST /api/playlist/add_many` |
| RA-92 remove several | `POST /api/playlist/remove_many` |
| RA-91 reorder | `POST /api/playlist/reorder` |

RA-90 and RA-92 can ship without RA-91, which is the only one that changes
the database.

**Implemented 2026-11-23** (gateway 2.3.0, `tests/test_playlist_editing.py`).
Settled while building it:

- The code lives in `dlna_library_playlists.py` (`PlaylistsMixin`), because
  `dlna_library_collections.py` was at its 400-line limit. `DB.pl_*` is
  unchanged.
- Rows without a `position` get their number when the next track is added.
  With only "highest + 1", that track would have come before them.
- The worklist's name is protected in both directions: renaming another
  playlist TO it would hand that playlist to a sync that deletes rows. So
  the worklist answers `protected` even for the name it already has.
- A name with control characters is a `bad_request` (not legal XML for the
  Naim and Subsonic lists).
- `remove_many` counts distinct urls: one given twice is removed once and is
  not `missing`.
- `null` is a wrong type, also for an optional track field: leave it out.
- A renamed compilation playlist is offered again by the tools. Accepted:
  they are run by hand and show it in the dry run first.
- Subsonic `createPlaylist` with a `playlistId` now replaces the order too.
- Subsonic `songIndexToRemove` took the wrong tracks when given several
  indices (the list was re-read between removals). Fixed: all indices mean
  the list as the client saw it, and removals come before additions.
- Since a call takes at most 2,000 urls, a longer playlist can't be
  reordered (the longest today has 468).

## Rules for all four

1. **POST only** (a GET is a 405), behind the existing request guard.
2. **One transaction each**: everything happens or nothing does.
3. Every reply is JSON with `ok`. Every refusal also has a fixed `code`, so a
   client never reads the sentence in `error`:
   `{"ok": false, "code": "playlist_not_found", "error": "Playlist not found"}`

   | Status | `code` | When |
   |---|---|---|
   | 400 | `bad_request` | body isn't valid JSON, a field is missing, empty or of the wrong type, or over a limit |
   | 400 | `protected` | renaming Favourites (or the generated worklist, below) |
   | 404 | `playlist_not_found` | no such playlist |
   | 409 | `playlist_changed` | reorder only: the list is not what the client thought |

   A 404 **without** `code` tells rAudio that the gateway is too old.
4. Limits: name 200 characters after trimming; 2,000 tracks or urls per call.
5. Never a 500 for bad input.
6. `/api/version` goes up (2.3.0).

## 1. Rename

`POST /api/playlist/rename?id=<id>&name=<name>` (query values, like create)

- `name` is trimmed; empty → 400 `bad_request`.
- `__favourites__` → 400 `protected`.
- The same name as now is fine (200). Two playlists may share a name, as today.
- Reply: `{"ok": true, "id": "ab12cd34", "name": "Road trip"}`

**Watch out**

- `api_subsonic_playlists._update_playlist` renames with its own SQL, with no
  Favourites check. Put the rule in one `pl_rename` in
  `dlna_library_collections.py` and call it from there too.
- `dlna_library_worklist.py` and `tools/compilation_playlists.py` recognise
  *their* playlists by name. Renaming the worklist would make the next run
  create a second one, so refuse it (`protected`). A renamed compilation
  playlist would be created again by the tool: decide there.

## 2. Add several

`POST /api/playlist/add_many`

```json
{"pl": "ab12cd34",
 "tracks": [{"url": "…", "title": "…", "artist": "…", "album": "…",
             "duration": "0:04:12.227", "art": "…"}]}
```

- Added at the end, **in the order given**. `pl` may be `__favourites__`.
- Every track needs a `url`; the other fields may be missing (`""`). One
  track without a `url` → 400, nothing added.
- A url already in the playlist, or twice in the request, is skipped and
  counted; the present row stays where it is.
- An empty `tracks` list → 400.
- Reply: `{"ok": true, "added": 11, "duplicates": 1, "count": 57}`
  (`count` = tracks in the playlist afterwards)

## 3. Remove several

`POST /api/playlist/remove_many`

```json
{"pl": "ab12cd34", "urls": ["…", "…"]}
```

- A url that isn't in the playlist is counted, not an error: the result is
  what was asked for, and a repeated request stays harmless.
- An empty `urls` list → 400. It must never mean "all".
- Reply: `{"ok": true, "removed": 2, "missing": 0, "count": 55}`

## 4. Reorder

`POST /api/playlist/reorder`

```json
{"id": "ab12cd34", "urls": ["…all of them, in the new order…"]}
```

- `urls` must be exactly the playlist's present urls, each once. A repeated
  url → 400 `bad_request`. One missing or extra → **409 `playlist_changed`**,
  nothing written: another device edited the list meanwhile, and the client
  reloads and asks again.
- Compare and write in the same write transaction.
- Favourites may be reordered.
- Reply: `{"ok": true, "count": 57}`

### Database

Today `pl_get` orders by `added_at, id`. Proposal:

- `ALTER TABLE playlist_tracks ADD COLUMN position INTEGER` (nullable), in the
  DDL for a fresh database and as an idempotent `_migrate_playlist_position`
  (like `_migrate_device_roles`: check `PRAGMA table_info` first).
- Fill it once, so **no playlist changes order by upgrading**:
  `ROW_NUMBER() OVER (PARTITION BY pl_id ORDER BY added_at, id)`.
- `pl_get`: `ORDER BY position IS NULL, position, added_at, id`. Rows inserted
  by code that doesn't know the column (the tools) then simply come last.
- `pl_add_track` and `add_many`: position = highest in the playlist + 1.
- `reorder`: positions 1…n. Removing leaves gaps; that's fine.
- Going back to an older gateway stays possible: it ignores the column.
- Run `./backup-library.sh` before the first start with it.

### Who follows the new order by itself

`/api/playlist`, the Naim tree (`api_upnp_browse.py`), Subsonic
`getPlaylist` and `pl_to_m3u` all read `pl_get`. So does Subsonic's
`songIndexToRemove`, whose indices therefore stay right.

Subsonic `createPlaylist` with a `playlistId` replaces the tracks but not
their order. It could call the same reorder code. Optional.

## Tests the gateway should have

Use a temporary database, and bind `DB` where the handlers read it (see the
comment in `tests/test_positions.py`: rebinding the re-export left the
handlers on the real library).

1. Rename: trims; empty → 400; Favourites → 400 and unchanged, **also
   through Subsonic**; unknown → 404; a name with `&`, `"` and emoji survives.
2. add_many: order kept; duplicates in the request and in the list counted;
   one bad track → nothing added; unknown playlist → 404; over the limit → 400.
3. remove_many: missing ones counted; empty list → 400 and **nothing
   removed**; the same request twice.
4. reorder: new order comes back from `/api/playlist`, Subsonic and the Naim
   tree; a missing, extra or repeated url changes nothing; a track added
   afterwards comes last.
5. Migration: an old database keeps every playlist's order; running it twice
   changes nothing; a row without a position comes last.
6. GET on each → 405; a cross-site POST → 403; a broken body → 400.

## How rAudio will use it

- Writes are sent once: never retried, never queued.
- Remove and delete always ask first; rename and reorder don't.
- 409 → "Changed on another device": reload, nothing lost.
- 404 without `code` → "This needs a newer gateway", and the rest keeps working.
