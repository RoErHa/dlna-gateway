# Library data rules — the full write-ups

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

### One unmounted volume must not take the others down (2026-09-10)

The three LocalFs roots — music, video, audiobooks — live on different
disks and are configured independently, but `maybe_start_localfs` used
to `return` when the **music** root was absent, before the file server
was started. So an unmounted music drive also took down the audiobooks
sitting on a healthy one.

**It did not present as "music is missing."** With no file server there
was no `MediaServer` in `SERVERS`, and `dlna_ssrf.guard` only lets a
private destination through when its host is a known device — so every
`/stream` and `/art` against `:8200` was refused, the relay correctly
declined to hand a non-200 to `<audio>` (§Two things the relay must
keep doing), and the PWA skipped **every track in the queue at about
one per second**. From the sofa that is "nothing plays, every song
skips"; in `gateway.log` it is one WARNING at boot and then thousands
of `SSRF guard: refused stream of …` lines. The trigger here was a
locked APFS volume, which macOS declines to mount at all.

`_resolve_root` now answers "configured, and actually there?" for each
root on its own, and the server starts on whatever resolved:

- **A missing root disables only itself**, whichever one it is. The
  music root has no special status any more.
- **The server starts if at least ONE root resolves** — video has no
  `LibraryProvider` but its bytes come from the same `:8200`, so a
  video-only setup still needs it.
- **No root resolving starts NOTHING.** An empty `allowed_roots` is a
  server that refuses every path while looking alive. The server is
  started by the FIRST root to arrive instead — which, since the
  boot-race fix below, may be minutes after boot.
- **Each initial scan is guarded on its own**, so one unreadable tree
  never costs the other library its index.

Guarded by `tests/test_localfs_wiring.py` (18). Four of them genuinely
fail against the old code — verified by reverting it and re-running,
including the audiobooks-survive-a-missing-music-root case.

**The Naim's music tree had the same bug from the other side.** The PWA
and Subsonic both degraded cleanly — they read the server registry, so
music simply stopped being offered — but `api_upnp` backed its
Artists/Albums/Genres tree with `DB.primary_udn()`, which is pure SQL
over `tracks`: *the udn with the most rows*. **`tracks` outlives its
files.** With the volume unmounted the music rows are still the majority
of the index, so the Naim was handed a full 26k-track tree in which
nothing could play — browsing worked, pressing play 404'd.

`api_upnp_ids.music_udn()` now answers *which music library is actually
serving*, and the root offers Artists/Albums/Genres only while one is:

- **A registered `MediaServer` is the evidence.** The wiring above adds
  one per root it actually brought up; books and videos are excluded
  because they own their own containers.
- **`primary_udn(among=…)`** narrows the same ranking to those udns, so
  "the biggest library" still decides — among the ones that can serve.
  An empty `among` means nothing is serving and returns `''`; that is
  the OPPOSITE of `None`, which means don't restrict.
- **An EMPTY registry is deliberately not read as "nothing is live".**
  The SSDP announcer starts BEFORE `maybe_start_localfs`, so a control
  point browsing in that window would be told the library is empty — and
  a client that caches an empty tree is a worse failure than the stale
  one this fixes. Only registered-but-no-music returns `''`.
- **A stale bookmark still opens an empty container, never a fault.**

Guarded by `tests/test_upnp_music_liveness.py` (16); three genuinely
fail against the old code. ⭐ Favourite Albums and Playlists are NOT
gated — they are user-curated and can hold books or radio, so a row
that no longer resolves stays visible for `tools/audit_playlist_orphans.py`
rather than vanishing.

### A root is asked for ONCE, and boot is the wrong moment (2026-09-12)

The fix above resolves each root at boot. That is the wrong **number
of times** to ask, and the next reboot proved it: launchd started the
gateway at **20:06:36**, and at **20:06:50** macOS had not finished
mounting the external music drive.

Everything then worked as designed — music and video disabled
themselves, audiobooks came up — and the drive mounted a few seconds
later with **nothing re-asking**. The gateway ran **seventeen hours**
with a file server whose `allowed_roots` held only the books, while
`tracks` still listed all 26k music rows. So every album browsed
perfectly and every play 403'd:

```
path-traversal blocked: /Volumes/SAMDATA/Music/…/Burning Down The House (Live).flac
                        not under ('/Volumes/SAMDATA-1TB/Audio_Books',)
stream ✗ upstream 403 … — refusing to relay a non-media body
client_log[audio_error] code=4 codeName=unsupported
```

Note the tell is DIFFERENT from the outage above: the containment
check (§Security posture 8) catches it before the SSRF guard does, so
it is a `path-traversal blocked` line naming a music path and the
audiobooks root, not a `refused stream of …`.

**A missing root is now a WAITING root** (`dlna_localfs_watch.py`).
`maybe_start_localfs` REGISTERS all three with `WATCH` instead of
resolving them itself; whichever are present activate at once, and the
rest are re-checked every `LOCALFS_ROOT_RECHECK_SEC` (default 30 s) by
one daemon thread that **retires when nothing is left waiting**, so a
fully-mounted machine carries no idle thread. Four things there are
load-bearing:

- **The file server starts on the FIRST root to arrive**, whenever
  that is, and every later one WIDENS it via
  `dlna_localfs_server.add_allowed_root` — which rebinds the tuple
  rather than mutating it, because the serving threads read
  `allowed_roots` off the handler class and must see the old tuple or
  the new one, never a half-built one. Canonicalisation lives there
  beside `make_handler_class`: `resolve_within` compares RESOLVED
  paths, so a raw root would silently never match.
- **Activation happens exactly once.** The state flips to ACTIVE
  BEFORE the callback runs — it starts a server, binds a provider and
  registers a device, and a re-check firing meanwhile would publish
  the library twice.
- **A failed activation is NOT retried.** The root IS there; what
  failed is our handling of it (a port in use, say). Retrying every
  30 s would repeat the failure forever and re-run whichever half
  succeeded — so it is logged once and recorded on the row.
- **An unconfigured root is not tracked at all.** Off is not waiting.
  Telling someone their Video library is unavailable when they never
  configured one is exactly the noise that buried the last outage.

**And the person is told.** The whole outage hid behind ONE boot-time
WARNING in `gateway.log`, which is not a place anyone looks from the
sofa. `WATCH.snapshot()` backs **`GET /api/libraries`** (one row per
configured root, and a waiting one carries a `message` naming the
volume to mount), and every state change publishes a `libraries` SSE
event. The PWA renders `#library-bar` — the same shape as the index
bar above it — from `refreshLibraries()`, called at **boot** (before
the first poll tick: a missing drive is the first thing to say, not
the last), on the `libraries` event, and on the `servers` poll as the
dropped-event fallback. A failed fetch leaves the bar **as it was** —
a dropped tailnet must not read as "the drive came back".

Activation also publishes `devices`, because the source picker has
just gained an entry — `SERVERS.add` does not fire `_on_server_found`,
which is the discovery hook, so nothing else would say so.

Guarded by `tests/test_localfs_watch.py` (25),
`tests/test_localfs_wiring.py` (18) and
`tests/frontend/test_library_bar.py` (8). Verified by breaking BOTH
halves: neutering the re-check reddens 7 of 8 late-mount tests, and
neutering `add_allowed_root` reddens the three that assert what the
one file server ends up allowed to serve.

### A rescan may only call a file gone when the walk looked (2026-10-10)

A LocalFs rescan decides "removed" by ABSENCE: a cached path the walk
did not visit is treated as deleted, and its `tracks` row goes with it.
The video scanner proved how badly that reads a failing drive — on
2026-10-03 a walk that saw 0 files pruned all 4,104 videos. The music
scanner had the same shape with three holes, all now closed through
`dlna_walk.trusted_removals`:

- **A folder that fails to list.** `os.walk` swallows directory errors
  by default, so an I/O error or a drive dropping mid-scan made
  everything under that folder read as deleted. The walk now records
  them (`onerror`), and removals UNDER an errored folder are held back.
  Only under it — a single permanently unreadable folder must not freeze
  removal across the whole library. Matched on path COMPONENTS, so an
  error on `Lost` never shields `Lost Tapes` (§Security posture 8's trap).
- **A walk that found no audio.** `probe()` refuses an EMPTY root, but a
  stale mount point holding a few leftover folders passes it. Zero audio
  seen against a non-empty index now removes nothing.
- **A file whose `stat` fails** was left out of `seen_paths`, so one
  unreadable file was deleted from the index. Unreadable is not gone.

Each case WARNs naming what it held back. Consequence worth knowing:
deleting the LAST audio file under a root no longer de-indexes it —
that is indistinguishable from the drive going wrong. Guarded by
`tests/test_walk_trust.py` (9); the three rescan cases fail against the
old code, and a control proves a real deletion is still removed.

### Folder-album identity — and the one folder it gets wrong (2026-08-25)

A LocalFs album is its FOLDER (`album_key`), which is what reunites a
Various-Artists compilation whose every track carries a different
performer. That inference holds only while the folder is *claimed* by
something: a real compilation names itself in the album tag.

**A folder where every album tag is blank has claimed nothing**, and
treating it as one album turns a junk drawer into a single enormous
record. Measured live: `<music-root>/Unknown Artist/Unknown Album/` held **247
tracks by 43 unrelated artists**, so playing one Marsh & Quinn song queued
Rio Verde Social Club behind it, and none of those files has art. It
read exactly like a corrupted database — it wasn't; playlists were 100%
intact and every id resolved to the right file.

`_localfs_album_group` (in `dlna_library_sql.py`, used by **all six**
localfs GROUP BY sites) therefore groups blank-album rows **per artist**
and tagged rows by folder as before. Three things there are load-bearing:

- **The artist branch is PREFIXED (`'a:'||artist`).** Unprefixed it keys
  on `''` — the same key the album branch uses — so untagged strays merge
  into whatever real album shares their folder. That rendered as one
  190-track "Various Artists" record.
- **`album_tracks` narrows on the ROW, not the folder.** A first attempt
  asked "does anything in this folder name an album?"; the real drawer
  held four stray tagged files, so the answer was yes and the bug
  survived. Tagged rows keep folder identity, which is what leaves
  genuine compilations and every normal album untouched.
- **Narrowing may never EMPTY an album.** A stale artist — a favourite
  saved before a retag, an id a Subsonic client cached — falls back to
  the whole folder. An album that resolves to nothing reads as data loss,
  which is worse than the over-broad queue this prevents.

`_localfs_album_name` also falls through to the folder leaf when the tag
is blank; it used to satisfy `COUNT(DISTINCT album)=1` and win, so such a
folder displayed as an album with no name at all.

The remaining lump is honest: ~186 files with no artist tag either, so
there is nothing to split them by. That is a **media** problem, and no
rebuild fixes any of this — re-reading the same files with the same blank
tags reproduces it exactly. Guarded by
`tests/test_album_grouping.py::TestUntaggedFolderIsNotOneAlbum` and
`::TestNarrowingLeavesRealAlbumsAlone`.

### Missing artist tags — infer, or hand it over (2026-08-25)

Some tracks carry no artist tag. Two questions follow, and they must be
answered by the SAME code or they contradict each other: *can this be
worked out?*, and *if not, who does it?*

**`dlna_artist_infer.infer_artist(album_key, sibling_artists)`** owns the
first. Pure, dependency-free, and shared by
`tools/artist_from_folder.py` (which WRITES the inferred tag into the
file) and `LibraryDB.sync_unknown_artist_playlist` (which sweeps up what
is left). If the sweep were more generous than the tool, fixable tracks
would sit in the worklist forever; stricter, and unfixable ones would
vanish from it. Evidence order, strongest first:

1. **Sibling unanimity** — every tagged track in the folder names the
   same performer. A folder is an album.
2. **An uncontradicted folder name** — nothing in the folder is tagged,
   so there is nothing to weigh against the name on the tin
   (`Mira Calvo (1996) Caminhos [FLAC]` → `Mira Calvo`).
3. **A folder name a sibling confirms** — several spellings, one of which
   IS the folder name (`Jean Vallier` beside `jean vallier`).

Everything else returns `""`. The asymmetry driving all of it: **a blank
artist asks a person; a wrong one files the track under a stranger and is
never questioned again.** Hence the refusals, each a test:
- a **compilation named after itself** — `Nights On Neptune` holds 20 artists
  and is not a band; `Unknown Artist/Unknown Album` holds 126;
- an **explicit `VA - …` folder outranks sibling unanimity**, because such
  a folder can easily have exactly ONE tagged track (this really did
  stamp a whole comp `Atlas & The Aviators`);
- a **dated spaceless slug** is a bootleg directory, not a name
  (`SVance2008-07-05-sbd`);
- junk names match on the WHOLE string, never as a substring, or
  "Various Comforts" and "The Unknown" get eaten.
- The artist portion of a folder ends at the EARLIEST of `" - "`, `(`,
  `[`, `{`. Stripping brackets in place instead yielded
  `Mira Calvo Caminhos ` — the album glued onto the artist.

**Everything unattributable carries ONE name: `Anon`** (`ANON_ARTIST`).
Never a per-track guess. A made-up name that LOOKS like a performer is
worse than an obvious placeholder — it can collide with a real act, and
it never invites correction. `is_unattributed()` treats blank and `Anon`
as the same state, so **every caller must ask that rather than testing
for an empty string**; `Anon` is how these tracks BROWSE, not a claim
about who played them, and they stay in the worklist either way.

`is_a_performer_name()` is the guard that keeps shelves out of the artist
field. Filename parsing lifts whatever sits left of the dash, which is
often a soundtrack, a genre shelf or a track number; the FOLDER rule had
the identical hole (a folder named `Guitar Lounge` was being recovered as
an artist), so both now face the same test. Three refusals inside it are
load-bearing, each learned the hard way on the real library:

- **The known-album check is filename-only.** "This name is also an album
  title" sounds right (an album name lifted into the artist field) and is
  catastrophic as a bulk audit: **self-titled albums are everywhere**, so
  it reclassified thousands of real artists as non-performers.
- **`allow_numeric` exists because 112, 911, 999 and 98° are bands.**
  Rejecting letter-less names catches `07` (a track number) when judging
  one parsed filename, and erases four real artists when auditing tags
  that already exist.
- **`Various Artists` is protected**, checked BEFORE the junk set that
  contains it. It is not a performer, but it is the sentinel
  `_localfs_album_artist` emits for a multi-performer folder — rewriting
  it would break album grouping, not just a tag.

**`- Unknown Artists -`** takes the remainder: swept automatically at the
end of each LocalFs scan, to be tagged by hand.

- **It syncs both ways.** Newly-untagged tracks are added; a row whose
  track has since gained an artist is pruned, because that work is done.
  Consequence: **removing a row by hand does not make it stay gone** —
  the next scan still sees a track with no artist. Give the file any
  artist tag to settle it.
- **It prunes a row when the work is done OR the file is gone.** In a
  playlist a PERSON curated, a row pointing at nothing is a repair for
  `tools/audit_playlist_orphans.py` and must never be deleted quietly —
  but this list is GENERATED, so the opposite holds: a dead row is not
  outstanding work, it is litter. Leaving them made the live worklist
  read **44 items when 15 were real**, right while it was being worked
  through. Another source's rows are still never touched, and a row
  pointing at a real track always survives.
- **Audiobooks opt out** (`collect_unknown_artists=False` in
  `dlna_localfs_wiring`) — a chapter with no artist tag is ordinary
  there, and ~550 would bury the music that needs the work.
- Created only when there is something to put in it; the leading `- `
  sorts it to the top. A sweep failure never fails a scan — the index is
  the product.

Live: of 141 artist-less tracks, **65 were recoverable from their folder**
and 76 are genuine hand-work (the junk drawer, a Sam Vance bootleg slug,
a handful of self-named compilations). Guarded by
`tests/test_artist_infer.py` (26) and
`tests/test_unknown_artists_playlist.py` (20).

### `metadata_overrides`: NULL and `''` are opposites (2026-08-25)

The table's contract is **NULL = no override for this field**, read back
as `COALESCE(override.col, tracks.col)`. An empty string is NOT NULL, so
it WINS that COALESCE and permanently blanks a field the file tags fill
correctly — on every re-index, forever.

That happened. `update_track_meta` seeded a new override row from the
track's CURRENT values ("fill blanks from current track record"), so
editing ONE field froze whatever the other three happened to be at that
moment, blanks included. **74 rows ended up masking real tags**, and 11
perfectly-tagged FLACs (MusicBrainz IDs, the lot) browsed with no artist and no album.

**Why it hid for so long:** those tracks were invisible to the
`- Unknown Artists -` sweep too, because their FOLDER can name them, so
they counted as tool-work rather than hand-work and appeared in neither
place. The tell was capitalisation — the DB's title was Title-Cased, the file's was sentence-cased, so the value had
never come from the file.

Fixed in three places, deliberately overlapping:
- **The writer** (`_blank_to_null` in `dlna_library_overrides.py`) stores
  `None` for any field that is blank, so an untouched field is *absent*
  rather than *empty*. Only `''` is treated as absent — a typed space is
  a choice, and guessing otherwise would be a different bug.
- **The reader** (`dlna_library_tracks.py`) wraps each subselect in
  `NULLIF(..., '')` before the COALESCE, so a stray `''` from ANY source
  — an old row, a tool, a hand-written UPDATE — can never mask a tag.
- **The data**: all `''` → NULL across the 11,620 rows (backup taken
  first; `integrity_check` ok).

Guarded by `tests/test_override_blank_masking.py` (10 tests). Three of
them genuinely fail against the old code — verified by reverting both
fixes and re-running, because a regression test that passes either way
is worse than none.

### "Show me this artist" means the same thing on every surface (2026-08-25)

A LocalFs album is its FOLDER, so an artist's album list is every folder
holding a track by them — compilations included. Opening one used to give
the WHOLE folder: browsing to a performer and tapping a comp returned
**356 tracks by other people** to reach the one you asked for. Search
never had the problem, because it matches tracks and filters by artist.

The fix is one line of intent in **`artist_albums`**: each row reports
**the queried artist and the count of THEIR tracks**, not the folder's
`Various Artists` aggregate. That is load-bearing far beyond the PWA —
the PWA's `onOpen`, the Naim's `galbum:` container id and CarPlay's `al:`
album id are ALL built from that row's `artist` field, so returning the
real performer makes `album_tracks` narrow on all three surfaces without
any of them knowing the rule exists. `HAVING track_count > 0` keeps a
folder out of an artist's list when none of the tracks are theirs.

**The artist page opens on THEIR records (2026-08-25).** `artist_albums`
also returns `own` per row — `is_own_album(mine, folder_tracks,
folder_artists)`: theirs when they hold at least half the folder, OR the
folder has at most two performers, the clause that protects a duo record
and a split album. Measured over all 6,390 artist/folder pairs in this
library, **97.3% are not close** (2,074 at 50%+, 4,142 under 10%); the
174 in between are mostly 3-to-9-performer folders.

The PWA renders the `own` rows, then folds the rest behind an
`Appears on N compilations` disclosure — **always folded, never
remembered**, so every artist opens the same way and the page never
depends on a state set days ago. An appearance's subtitle reads
`1 track of 67`, which says "compilation" before the title is read.
A source that sends no `own` field (anything non-localfs) treats every
row as theirs, so nothing can silently vanish into a fold.

**All three surfaces**, each given the honest equivalent of the same
idea rather than a copy of the PWA's:
- **The Naim** gets ONE `gappears:<b64(artist)>` container titled
  `Appears on (N)` under the artist. UPnP has containers but no
  dividers, so a sub-container is the fold. `gappears:` cannot be
  swallowed by `gartist:` (they differ at the 8th character) — pinned by
  a test, since the dispatch tables are disjoint by construction.
- **CarPlay** gets order plus a name: Subsonic's `getArtist` has neither
  dividers nor sub-containers, so their records come first and each
  appearance reads `Big Comp · 1 of 67`. The marker goes in the DISPLAY
  name only — changing the id would invalidate every album a client has
  cached.

**An artist with no records of their own shows their appearances
directly** on all three. Folding is what stops compilations burying real
albums; with none to bury it would leave a page holding a single
collapsed row, one tap from that artist's only music.

Guarded by `tests/frontend/test_appears_on.py` (11),
`tests/test_appears_on_devices.py` (10) and the classification tests in
`tests/test_album_grouping.py`.

`album_tracks` then narrows whenever it is given a **named** performer.
Two things keep that safe:
- **`Various Artists` is the sentinel for "the folder itself"** and never
  narrows, so the Albums view, favourites, genre and decade drill-downs
  are byte-identical to before. For an ordinary single-artist album the
  narrowing is a no-op, because `_localfs_album_artist` only yields a
  real name when `COUNT(DISTINCT artist)=1`.
- **Narrowing may never EMPTY an album.** A stale artist — a favourite
  saved before a retag, an id a client cached — falls back to the whole
  folder, because an album resolving to nothing reads as data loss.

Guarded by `tests/test_album_grouping.py` (44), including a
cross-surface class that round-trips the real UPnP container id and the
real Subsonic album id — the rule is easy to undo by "fixing"
`artist_albums` to report the aggregate again.

#### An album's date, on every surface (2026-09-21)

Reported as "I see the date of the tracks on an album but not the album
date". Neither missing metadata nor anything the credits sweep would fix
— **2,153 of 2,238 albums (96%) already had a year**; three queries
simply never selected one. `all_albums`, `browse_letter`'s album branch
and `album_tracks` now all carry it, alongside `artist_albums`.

> ⚠ **Join a NARROWED view of `metadata_overrides`, never the table.**
> It also has `artist` and `album` columns, so joining the whole thing
> made every bare `album`/`artist` in an `ORDER BY` ambiguous, and the
> UPnP branches that never got the join failed on `m.year` outright.
> `LEFT JOIN (SELECT url, year FROM metadata_overrides) m ON m.url = t.url`
> carries only what the expression needs and cannot collide with
> anything. The same trap bites a second time inside the query:
> **`year` is also a column of `tracks`**, so a bare `year` in `ORDER BY`
> resolves against the source row, not the aggregate alias — order by
> the expression. `tests/test_artist_chronology.py` (12).

#### An album has TWO dates, and reporting one was wrong (2026-09-21)

The first cut dated an album `MIN(_EFFECTIVE_YEAR)` — its oldest
recording, each track having already been pulled back to its
MusicBrainz original. The reasoning ("a 2011 remaster of a 1975 album
belongs at 1975") is sound for a *career view* and was wrong as an
album's label. Reported from the sofa, confirmed in the data:

```
Piper … 40th [Disc 1] 2007   tag 2007  no override   -> showed 2007
Piper … 40th [Disc 2] 2007   tag 2007  override 1967 -> showed 1967
Piper … 40th [Disc 3] 2007   tag 2007  override 1967 -> showed 1967
Piper at the Gates of Dawn   tag 1967  no override   -> showed 1967
```

Two defects, compounding:
- **The anniversary discs were indistinguishable from the original.**
- **And only sometimes** — the pull-back needs a `metadata_overrides`
  row, so whether a folder showed 2007 or 1967 depended on how far
  `improve_song_years` had got. Same record, same tags, three answers.
- **`MIN` is nonsense for a compilation**, which is most of the damage:
  `VA - 100 Greatest Jazz Icons (2020)` showed **1946**, `Now #1s - 70
  Years … (2022)` showed **1952**, `Kind Of Blue (2022 Remaster)` 1956.

Measured: **842 of 2,249 albums displayed a year that was not their own;
671 were out by 3+ years.**

An album now reports both facts and asserts neither
(`_ALBUM_EDITION_YEAR` / `_ALBUM_ORIGINAL_YEAR` in
`dlna_library_sql.py`):

| field | is | from |
|---|---|---|
| `year` | the EDITION — what this pressing says it is | `MAX` of the tracks' tag years |
| `year_original` | the oldest recording on it | `MIN(_EFFECTIVE_YEAR)`, as before |

- **`MAX` decides the edition, not `MIN`.** Every mixed-tag-year folder
  in this library (71 of 2,112) is a compilation whose tracks carry
  their own original years and whose compilation year is the newest of
  them — and matches the year in the folder name. For an ordinary album
  every track shares one year, so the choice never shows.
- **`album_tracks` carries `year_edition` per row** so the album header
  can still derive its date from the tracks it just fetched, which is
  what makes it work from favourites, a genre, a decade or a search
  result — none of which hand the view an album row.
- **The client owns the display rule**, in ONE place:
  `albumYearLabel(year, original)` renders `2007 · orig. 1967` at a gap
  of `REMASTER_GAP_YEARS` (3) or more, else just the year. It is shared
  by the album rows, the artist page and the album header — the artist
  page had its own copy of the subtitle and that is exactly how the
  three drifted apart. Same threshold the now-playing panel uses for
  "(remastered)".
- **The chronology sorts by what it displays.** A list whose order
  disagrees with its own labels reads as a bug.

> **The DECADE facet deliberately still uses `_EFFECTIVE_YEAR`.** "Which
> decade is this music from" is a different question from "which pressing
> is this": a 2007 reissue of a 1967 record belongs in the sixties.
> `tests/test_album_year.py` (12) pins that, and
> `tests/frontend/test_album_year.py` (7) pins the label.

### Songwriting credits: composer + lyricist (2026-09-20)

`tracks.composer` / `tracks.lyricist`, read straight from the tags
already on disk. Measured over 1,500 files with EXACT per-format keys:
**composer 32.3%, lyricist 18.4%**. (A first measurement said 46% — it
matched `composer` as a SUBSTRING and counted empty `composersort`
fields. Measure on whole keys.)

Three things are load-bearing:

- **mutagen's `easy=True` is NOT uniform.** FLAC and MP3 expose both
  fields natively; **EasyMP4 registers neither**, so an `.m4a` holding a
  real `©wrt` credit reads back blank — indistinguishable from an
  untagged file. `dlna_providers/localfs_tags.py` registers the key at
  import (guarded, so the module still imports without mutagen). One
  line, no second file open on a 26k-file scan.
- **A new column must join the change-guard, not just the SET.** Step 2a
  of `upsert_tracks` only fires when its WHERE clause sees a difference.
  A `beet parentwork` run writes ONLY the composer, so a guard that
  doesn't mention it leaves the row stale forever and reports 0
  refreshed. `tests/test_credits.py` (15) has that trap as a test; it
  genuinely reddens with the clause removed.
- **The three UNIQUE migrations rebuild `tracks` from their own
  hardcoded DDL** — a third copy of the schema. New columns must be
  added to `_TRACK_COLS` too, and backfill as `''` not NULL: they are
  `TEXT DEFAULT ''` and the refresh guard compares with `!= ''`, which
  a NULL never satisfies.

Blank-safe on refresh like genre/art: a file that lost its tag never
erases a stored credit.

**Display is filtered** (`dlna_credits.clean_credit`). Scene releases
advertise in the tag fields — 691 of 8,570 credited tracks (8%) carry
`www.t.me/pmedia_music` or `www.thenzbplace.com`, so the panel read
"Words www.t.me/pmedia_music". Both rules were measured against ALL
2,447 distinct credit values: each matches exactly 2, zero false
positives. Only a REPEATED punctuation run condemns a value, so
`J.S. Bach` survives.

> ⚠ **The rule that was tried and REJECTED**: "a credit with no A-Za-z
> letter is junk". It matched 55 rows, every one a real composer —
> Стравинский, Прокофьев, Чайковский, Рахманинов, Ջիվան Գասպարյան.
> Hiding the Russian composers from a classical library is the opposite
> of the feature. Same lesson as `is_a_performer_name`'s
> `allow_numeric` (112, 911, 98° are bands): a shape test that encodes
> "looks English" erases real data. **Never require a particular
> script.** `tests/test_credit_cleaning.py` (12) names that class after
> it.

Shown in the now-playing panel under the year, off the SAME
`/api/track_meta` request (`tests/frontend/test_credits.py` pins it at
one). `composer == lyricist` is common — MusicBrainz returns Freddie
Mercury as both for *Bohemian Rhapsody* — and renders once, as
"Written by"; a genuine split reads "Music X · Words Y".

#### Step 4 — the two thirds with no tag, via MusicBrainz works (2026-09-21)

Tags answered 32%. The other ~18,000 tracks were filled from MusicBrainz
into `track_credits`, taking live coverage to **73.9% (26,750 music
tracks; 11,025 matched, 6,067 sticky `notfound`, 0 errors, ~2 h)**.

**The ROUTE is the whole design.** The obvious one —
search-recording → recording → work — is three rate-limited requests per
song: 15,805 distinct songs, about **16 hours**, which does not fit a
night. Browsing each **artist's works** returns up to 100 at a time, each
already carrying its composer/lyricist relations: ~3,700 requests, about
**1.2 hours**. That is the direct payoff from having resolved artists to
MBIDs first — 2,307 artists with an mbid cover 95% of the composer-less
tracks. It is what the keystone was for.

- **Results go to `track_credits`, never `tracks.composer`.** A rescan is
  blank-safe, but `clear(udn)` DELETEs `tracks` and a rebuild-index would
  throw away the whole night.
- **The file tag wins on read** —
  `COALESCE(NULLIF(t.composer,''), c.composer)` in `track_meta_by_url`.
  MusicBrainz is filling gaps, not correcting the owner.
- **The work browse is deliberately BROAD** — it returns works the artist
  is related to in any way, so Brian May's "Driven by You" comes back
  under Pink Floyd. Harmless, because the caller only looks up titles of
  tracks it already holds BY THAT ARTIST, so a stray work never matches.
- **`writer` stands in for an absent composer.** MusicBrainz uses it when
  the music/words split was never recorded; dropping it loses a real
  credit. "reconstructed by" and friends are not authorship.
- **Unmatched tracks get a sticky `notfound`** — without it a second pass
  re-asks all 6,067 and costs another night.

`tests/test_track_credits.py` (10). Run it with
`python3 tools/track_credits.py --apply`; retry one by deleting its row.

### Artist metadata — the MBID keystone, and who was in the band

`artist_meta` + `artist_members`, filled by two sweeps. **Coverage on
this library: 2,889 of 3,289 artists resolved (87.8%)**, of which 443
cost no network at all.

**Everything keys off a MusicBrainz artist id**, which is why resolving
it is its own step: MusicBrainz, Wikidata, Wikipedia and ListenBrainz
all take the same key, so one wrong id is wrong in four places at once
— and a wrong id is worse than none, because a blank invites a fix while
a plausible biography for the *other* band called Nirvana never gets
questioned. `dlna_mbid.accept_match` therefore **refuses rather than
guesses**, exactly like `dlna_artist_infer`.

Rules that took evidence rather than reasoning (all in
`tests/test_mbid_match.py` (27)):

- **`&` is NEVER split, only fallen back to.** `Nick Cave & The Bad
  Seeds`, `Billy Larkin & The Delegates` and `Jr Walker & The All Stars`
  are single acts that match whole; eager splitting breaks all three.
  Trying the split form only AFTER the full name fails still rescues
  `Hans Zimmer & Benjamin Wallfisch` at no risk. **Commas are never
  split at all** — `Anderson, Bruford, Wakeman, Howe` is the band.
- **Every dash folds to a SPACE.** MusicBrainz spells names with
  TYPOGRAPHIC dashes while tags use ASCII hyphen-minus, or nothing:
  `Bachman-Turner Overdrive` (U+002D) vs MB's U+2013, `Jean Michel
  Jarre` (space) vs MB's U+2010. Both were refused **at score 100**.
  Folding to a space (not deleting) is what makes the second pair work.
- **Aliases are consulted only when nothing matched by name** —
  `Gonzales` → *Chilly Gonzales*, `AC-DC` → *AC/DC*. They ride on the
  same response, so it costs no request.
- **Two MB artists sharing a name are refused outright.** Widening the
  match without widening the ambiguity guard silently reintroduces the
  namesake problem it exists to prevent.

MB's `score` is TEXT relevance, not correctness — `Elbo` scores 100 for
`Elbow` — so the name comparison does the real work.

**The 400 that remain refused are media problems, not matcher
problems**: misspellings in the tags (`Harry Nilson`, `Edvart Greig`,
`Credence Clearwater Revival`), song titles sitting in the artist field,
comma-joined multi-artist credits, surname-only entries. A fuzzy rule
loose enough to catch `Edvart Greig` would start attaching the wrong
artist to things. They stay sticky; clear them selectively after a
retag.

#### The line-up, and why it is inferred

Measured against the live API: **per-RECORDING performer credits exist
for only ~17% of this library's tracks** (release-level ~22%) — Pink
Floyd's *Shine On You Crazy Diamond* has none. A credits-only feature
would be blank four times in five. Band MEMBERSHIP is near-universal for
groups AND carries instruments plus date ranges, so
`dlna_lineup.lineup_at` intersects those ranges with the recording's
year. `tests/test_lineup.py` (13):

- **Members rejoin.** Richard Wright is TWO rows on Pink Floyd
  (1965–1981, 1987–2008), which is why `begin_date` is in
  `artist_members`' PRIMARY KEY. A first-match lookup reports him absent
  from *The Division Bell*, which he played on.
- **No year means no answer.** The claim is "who was in the band THEN";
  with no year there is no claim, and returning the full roster would
  assert something different and wrong.
- **A member with no dates is always included** — MB often has the
  membership without the range, and dropping them makes a band look
  emptier than it was.

> ⚠ **`member of band` runs in BOTH directions.** On a Group,
> `direction='backward'` names the members; on a Person,
> `direction='forward'` names the bands that person joined. Read without
> checking, David Bowie's panel lists Tin Machine and The Konrads as his
> LINE-UP — which is what happened the first time real data was pulled.
> Filtering turns the bug into a second feature: **"Also played in"**.

**It is inference and the UI says so.** `/api/artist_info` returns
`lineup_tier` — `inferred` for a line-up derived from membership dates,
`credits` when MB names the players on that recording — and the PWA
renders amber vs green. The tier comes from the server and is never
derived client-side, so presenting a guess as a fact cannot be a
one-line mistake.

#### Sources, and what each is for

| Source | Gives | Key |
|---|---|---|
| MusicBrainz | identity, life-span, birthplace, genres, **members with instruments + dates** | none (UA + 1 req/s) |
| Wikipedia | the biography paragraph | none |
| Last.fm | "best known for", by actual listening | `LASTFM_API_KEY` |

**Last.fm needs a registered APPLICATION key**, not an account password
— the same trap AcoustID's two key types set. Register at
`https://www.last.fm/api/account/create`; absent, the block is omitted
and nothing else changes. Live coverage: **best-known 100%, birth/formed
92%, biography 74%, photo 67%**, 1,175 bands with line-ups over 12,101
member rows.

**A bio is DROPPED when its url is missing.** The text is CC BY-SA and
the link IS the attribution, so they travel together or not at all. A
disambiguation stub is refused too — a wrong answer dressed as a right
one. Photos route through the existing `/art?url=` proxy, so
`art_cache/` already handles fetch-once storage and is already
gitignored.

**ListenBrainz was evaluated and dropped**: it served top-recordings
without a key, then began answering `401 UNAUTHORIZED` mid-session. Its
data is thinner than Last.fm's anyway.
