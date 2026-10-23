# Library housekeeping tools

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

## Library housekeeping tools

### `tools/prune_empty_music_dirs.py`

Walks the music root and **moves to Trash** any directory whose entire
subtree contains zero music files. The user's library lives at
`/Volumes/SAMDATA/Music` (external drive — see project memory).

Music extensions (default): `.mp3 .flac .ogg .opus .m4a .aac .wav .wma
.ape .aiff .aif .dff .dsf .alac`. **`.mp4` is deliberately excluded**
so a music-video MP4 in a folder doesn't mark that folder as "kept".

#### Protection rule (Rule B — subtree-protect)

As soon as the walker descends into a directory whose **subtree** (any
depth) contains a music file, that directory is "an album root" and
*all* of its descendants — including non-music siblings of music — are
preserved. This protects:
- `Album/scans/`, `Album/coverart/`, `Album/booklet/` next to music.
- Multi-disc albums: `Album/CD1/track.flac` AND `Album/scans/cover.jpg`
  both survive even though the cover-art subdir is a sibling of (not
  under) the music subdir.

The root music folder itself is **never** treated as an album root —
loose music files at root don't grant blanket protection to root's
other subdirs.

**Documented trade-off:** the rule cannot tell apart a multi-disc
album's `scans/` (you want kept) from a junk subdir next to a music
subdir (you might want deleted). If you have a folder that mixes
music subfolders and pure-junk subfolders at the same level, the
junk is **preserved**. The rule errs on the side of preservation;
clean such mixed dirs manually.

#### Defaults & safety

- **Trash, not delete.** Default behaviour moves directories to the
  macOS Trash via `osascript` — recoverable from Finder for ~30 days.
- **Confirmation prompt** — shows the first 20 dirs that would be
  deleted + total count, then `Y/n`. Pass `-y` to skip.
- **Limit acts as a safety belt** — if `--limit N` halts the walk
  early, the script does NOT execute deletions for that run (you're
  not seeing the full picture, so we never act on a partial list).

#### Usage

```bash
# Required first step — preview without acting:
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --dry-run

# Verbose preview (logs every kept directory too):
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --dry-run -v

# Stop after 200 dirs (safety/sanity check; no deletions when limit hit):
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --dry-run -v --limit 200

# Real run — Trash, with confirmation prompt:
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music

# Real run — non-interactive (e.g. cron / a script):
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music -y

# Override extension list:
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --exts mp3,flac,ogg

# Permanent rm -rf (NOT recoverable — use only if you're sure):
python3 tools/prune_empty_music_dirs.py /Volumes/SAMDATA/Music --hard-delete -y
```

#### Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Print decisions without acting |
| `-v` / `--verbose` | Log every kept directory too (default: only deletions print) |
| `--limit N` | Stop after evaluating N directories (no deletions executed when limit is hit) |
| `--exts a,b,c` | Override the music extension list (commas, with or without leading dot) |
| `--hard-delete` | Permanent `rm -rf` instead of Trash. NOT recoverable. |
| `-y` / `--yes` | Skip the confirmation prompt |

#### Why this is safe for the gateway

The gateway frontend never references files in the music root
directly — every `tracks.art` and `album_art.art_url` value in
`library.db` is `http://` or `https://` (the LocalFs file server or
Cover Art Archive). Album art is fetched through `/art` proxying an
HTTP URL, never a filesystem path. Music-less directories hold no
audio, so the indexer has no entry for them at all. Pruning
them is invisible to the gateway.

#### Tests

`tools/test_prune_empty_music_dirs.py` — 14 unit tests over
throw-away tempdirs. Cover: album-with-music kept, multi-disc
support dirs (`scans/`, `booklet/`) kept, root-level music doesn't
protect siblings, branch with deep-only music kept, split-branch
trade-off (junk-next-to-music preserved), symlinks not followed,
case-insensitive extension match, `mp4` treated as non-music,
`--limit` halts cleanly. Run standalone:

```bash
python3 -m unittest tools.test_prune_empty_music_dirs -v
```

### `tools/artist_from_folder.py`

Restores a missing artist tag from what the FOLDER already knows —
sibling tags, or the name on the tin. Stronger evidence than a filename,
so run it BEFORE `tag_from_filename.py`. The decision is
`dlna_artist_infer.infer_artist` (see "Missing artist tags" above);
this module is just the walk plus the write.

Same contract as its sibling tool: never moves/renames/deletes, never
overwrites an existing artist, and **re-reads each file before writing**
so a stale index cannot cause a bad write (that check earned its keep —
11 files the DB called blank already had tags). DRY-RUN by default.

> ⚠️ **There is deliberately NO fallback when a container refuses a tag,
> and it must stay that way.** WAV rejects a bare string through mutagen's
> easy interface ("not a Frame instance"). The first version caught that
> and fell back to `ID3(path).save(path)` — which, for a RIFF container,
> **PREPENDS a standalone ID3v2 tag**, so the file starts with `ID3`
> instead of `RIFF` and stops being a WAV. It destroyed 15 files of a
> Mira Calvo album; the next scan reported them as `malformed`, which is
> the only reason it was caught. They were recoverable *only* because
> prepending leaves the original bytes untouched further in: strip exactly
> the ID3 length and `RIFF` is sitting there. Repaired and verified —
> declared RIFF size == filesize-8 on all 15, zero decode errors.
> **A tag is a convenience; the audio is the product.** A container that
> will not take a tag gets reported as `unsupported` and left alone.
> Guarded by `tools/test_artist_from_folder.py`, whose WAV test genuinely
> fails against the old code (checked — `RIFF` becomes `ID3\x04`).

```bash
python3 tools/artist_from_folder.py            # preview
python3 tools/artist_from_folder.py --apply    # then rescan
python3 tools/artist_from_folder.py -v         # also list the hand-work
```

Live run: **39 files tagged** across 8 folders (Ray & Nadia Orbit, Mira Calvo,
Jean Vallier, RVM, Stormwind, Vex …); the rest went to the worklist.

### `tools/tag_from_filename.py`

Gives untagged strays an **artist tag recovered from their own filename**
— the companion to the folder-album fix above. The browse layer now groups
blank-album rows per artist, which rescues every file that HAS an artist;
this rescues the ones that have nothing. Written for
`<music-root>/Unknown Artist/Unknown Album/` (251 loose 2009 MP3s), where it
tagged **149**, left **64** already-tagged files untouched, and left **38**
alone because their names carry no artist to read.

`parse_name` is pure and is the whole risk in the tool — a bad split writes
someone else's name into a real file — so the hostile shapes are tests, not
discoveries made mid-batch: `NN - Title` yields NO artist (it is a track
number and a title), `NN - Artist - Title` yields one, a bare `-` is never
a separator (it would halve "Jean-Marc Aubert"), and a literal
`Unknown`/`Various` in the artist slot is refused.

Three things it will not do: never move, rename or delete; **never
overwrite an artist that already exists** (a real tag is evidence, a
filename is a guess, and a guess must not beat evidence); and never write
what it had to invent — no separator means the file is reported and left
exactly as it was. Because it only writes fields that are EMPTY, there is
nothing to overwrite and nothing to lose, which is what makes it safe to
run on the real library. Titles arrive clipped (these filenames are
truncated at ~36 chars) and that is accepted: the artist decides which
album a file lands in, and the artist is at the front, which is the half
that survived. DRY-RUN by default.

```bash
python3 tools/tag_from_filename.py "<music-root>/Unknown Artist/Unknown Album"
python3 tools/tag_from_filename.py "<folder>" --apply    # then rescan
python3 -m unittest tools.test_tag_from_filename -v      # 19 tests
```

### `tools/find_corrupt_audio.py`

Walks the music root and flags audio files whose first 16 bytes are
wrong for their extension — catches the failure mode the AcoustID
worker surfaced on 2026-05-25 (13 MB FLAC files starting with `\x00`
instead of `fLaC` magic). Per-format magic-byte validators cover the
full default extension set: `.flac .mp3 .ogg .opus .m4a .alac .aac
.wav .aiff .aif .wma .ape .dsf .dff`.

#### Three reasons a file gets flagged

| reason | What it means |
|---|---|
| `zero-size` | File is 0 bytes on disk |
| `zero-header` | First 16 bytes are all `\x00` — the actual failure mode found in the user's library |
| `magic-mismatch:.<ext>` | Header isn't valid for the file's extension (e.g. a `.flac` not starting with `fLaC` or `ID3`) |

#### Defaults & safety

- **Scan-only by default.** No file is touched unless `--trash` or
  `--hard-delete` is explicitly passed. Default run only PRINTS the
  findings and WRITES them to `./corrupt-audio.txt` (one
  `<reason>\t<path>` per line) — usable as input to any downstream
  cleanup script the user wants to write.
- **`--trash` and `--hard-delete` are mutually exclusive** and both
  prompt for confirmation unless `-y` is passed.
- **`--limit N` is a safety belt**: when the limit is hit, the run
  is reported as PARTIAL and any delete flags are ignored — you only
  act on a complete picture.
- **Symlinks are not followed** (`os.walk(followlinks=False)`) —
  avoids loops and accidental scans of foreign volumes.
- **Unreadable files** (permission errors) go into a separate
  `read_errors` bucket, never `files_corrupt` — we don't delete what
  we couldn't even open.

#### Usage

```bash
# Default — scan only, write list to ./corrupt-audio.txt:
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music

# Verbose preview (logs every OK file too):
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music -v

# Stop after scanning 1000 files (no deletions even if --trash passed):
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --limit 1000

# Move flagged files to Trash, with confirmation prompt:
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --trash

# Non-interactive permanent delete (NOT recoverable):
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --hard-delete -y

# Restrict to one or two formats:
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --exts flac,mp3

# Different output path for the list:
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --out /tmp/broken.txt

# Suppress the list file (just print):
python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --out /dev/null
```

#### Flags

| Flag | Effect |
|---|---|
| `-v` / `--verbose` | Also log every OK file (default: only corrupt) |
| `--limit N` | Stop after scanning N files (0 = no limit); halts the delete step |
| `--exts a,b,c` | Override the audio extension list (commas, with or without leading dot) |
| `--out path` | Write the corrupt-paths list here (default: `./corrupt-audio.txt`; pass `/dev/null` to suppress) |
| `--trash` | Move flagged files to macOS Trash via `osascript` |
| `--hard-delete` | Permanent `unlink()` instead of Trash. NOT recoverable. Mutually exclusive with `--trash` |
| `-y` / `--yes` | Skip the confirmation prompt before deleting |

#### Tests

`tools/test_find_corrupt_audio.py` — 23 unit tests over throw-away
tempdirs. Cover: every format's valid-magic happy path, raw-MP3-sync
without ID3 prefix, AIFC variant of AIFF, zero-size flagged,
zero-header flagged across multiple extensions, per-format magic-
mismatch, non-audio extensions skipped by the walker, case-insensitive
extension match, `--limit` halts cleanly, symlinks not followed,
unreadable files recorded separately (not auto-deleted). Run standalone:

```bash
python3 -m unittest tools.test_find_corrupt_audio -v
```

### `tools/immich_import.py`

Copies **new videos from Immich's originals** (`library/` + `upload/` on
the Immich data volume; `encoded-video/` transcodes never touched) into
the gateway video root (`/Volumes/SAMDATA/GWMovies`). The gateway does
the rest automatically: the 5-minute periodic scan picks new files up,
reverse-geocodes their GPS and titles them `<location>_<YYYYMMDD>_<HHMM>`
(see `docs/VIDEO_SUPPORT.md`) — filenames are preserved because titles
come from metadata, not names.

Dedup is by **content hash** (BLAKE2b), remembered in
`<dest>/.immich-import.db`: files already in GWMovies (any name, any
origin) are registered on first run and never re-copied; processed
sources are skipped by (path, size, mtime) without re-reading. Re-running
after new phone uploads only copies what's new — safe as a habit. A name
collision with different content gets a ` (2)` suffix. DRY-RUN by
default (`--apply` copies; the hash cache is warmed even in dry-run so
the apply is fast). `--min-seconds 10` skips Live-Photo motion clips;
`--limit N` stops early.

**The dedup cache is deliberately STICKY** — `dest_files` keeps every
content hash EVER seen in dest, so a clip you deleted from GWMovies never
comes back on a re-run. That stickiness once silently withheld 1,783
PHOTOS-ALL videos whose content had been in GWMovies before (2026-07-07);
**`--reimport-deleted`** narrows the dedup set to files PRESENT now (and
re-evaluates stale 'duplicate' source verdicts) when you DO want deleted
content back. **`--subdirs ""`** walks the source root as a plain folder
tree (e.g. Immich's external library, PHOTOS-ALL) instead of the default
`library,upload` Immich storage layout.

```bash
python3 tools/immich_import.py               # preview
python3 tools/immich_import.py --apply       # copy new videos
python3 tools/immich_import.py --source /Volumes/SAMDATA-1TB/PHOTOS-ALL \
    --subdirs "" --reimport-deleted --apply  # external library, full
python3 -m unittest tools.test_immich_import -v   # 18 tests
```

### `tools/infer_video_locations.py`

Infers locations for **GPS-less videos from their temporal neighbors**
(2026-07-07): a clip shot the same day (or within ±`--window` days,
default 3) as real-GPS-located clips gets their location. Tiers, strongest
first: `same_day` unanimous → `±1d` → `±window` → **country-only** (cities
differ but country agrees — browsable via the "(no city)" bucket).
Evidence = real-GPS rows ONLY (no chaining off previous inferences).
Results go to `video_location_overrides` (source=`inferred_*`), which the
end-of-scan hook re-applies after every rescan; `manual` rows always win.
`--retry-geocode` also re-asks Nominatim for GPS-but-cached-empty rows
(evicts the sticky '' cache entry). DRY-RUN by default.

**Device check caveat:** `--no-device-check` was needed on the real
library — the check (quicktime make/model of video + neighbors must
match) blocked 92/113 targets because GPS-less videos are almost all
re-encoded (WhatsApp/exports) with NO device tags at all. First real run
(user-approved, check off): 64 inferred + 8 geocode retries → located
videos 1,050 → 1,111. Undo one:
`DELETE FROM video_location_overrides WHERE video_id='…'`.

```bash
python3 tools/infer_video_locations.py                     # preview
python3 tools/infer_video_locations.py --no-device-check --retry-geocode --apply
python3 -m unittest tools.test_infer_video_locations -v    # 16 tests
```

### `tools/immich_people_sync.py`

Syncs **Immich's person recognition** onto gateway videos (2026-07-07):
Immich keeps face/person data in ITS Postgres (never in the files), so
this pulls named persons over the REST API (`GET /api/people`, then
`POST /api/search/metadata {personIds, type: VIDEO}`; auth header
`x-api-key`; `IMMICH_URL` + `IMMICH_API_KEY` from `.env`) into the
`video_people` table → the "👤 By person" DLNA container + PWA grouping.

Matching, in order: (1) **content checksum** — Immich returns base64
SHA1; GWMovies files are SHA1'd once (cached in `.immich-import.db`
table `video_sha1`); (2) **(normalized filename, exact byte size)**
fallback via a per-asset GET — needed because **Immich's stored checksums
go stale** when files are metadata-edited in place after indexing (same
size, different bytes; proven live — checksum-only matched 8/349, the
fallback took it to 337/349, 20 persons). The importer's ` (N)` collision
suffix is normalized; ambiguous (name, size) keys are dropped, never
guessed. Per-person writes are a SYNC (replace, not merge) — re-run after
new Immich face-tagging, safe as a habit. DRY-RUN by default.

```bash
.venv/bin/python tools/immich_people_sync.py            # preview
.venv/bin/python tools/immich_people_sync.py --apply    # sync
python3 -m unittest tools.test_immich_people_sync -v    # 14 tests
```

### `tools/openlibrary_books.py`

Enriches **audiobook metadata from the OpenLibrary API** (2026-07-13):
canonical author/title + **series name & number-in-series** per book
(a book = its folder = `album_key` under the audiobooks UDN) into the
`book_meta` display overlay. Never touches files or `tracks`.

Per book: (author, title) guessed from majority tags, falling back to
folder-name parsing ("57 - The Reality Dysfunction - Peter F Hamilton -
1996"); `search.json` behind a fuzzy title floor (0.60) + author-token
overlap guard; series majority-voted from the work's editions'
`series` strings ("Discworld (13)", "Name #3", "Name, Book 3", "Book 3
of Name"; number is REAL so novellas are #1.5). Hard-won guards from
the first live batches: **catalog numbers > 100 reject the whole entry**
(a "Frye annotated #1249" publisher series is bookkeeping, not a story
series); **short titles (<3 words) never retry title-only** (the
narrator-in-artist fallback matched "Alpha" to Lisi Harrison's
"Alphas"); **publisher/imprint series rejected** (curated blocklist +
the "Series (Publisher)" wordy-paren form — "SF Masterworks",
"Historia (DeBolsillo)"); **multi-series edition strings split** into
separate candidates; and **≥2 non-ASCII chars hard-reject a series
name** — the USER RULE that books are English/Dutch ONLY means a
translated edition's series ("Madaʻ bidyoni", Armenian Narnia, pinyin)
is never valid data. Transport failures are transient (NOT cached — retried next
run); a confident miss is a sticky `notfound`; `manual` rows are never
overwritten. ~1 req/s, identifying UA (contact email from `.env`).
Root-level single-file books (album_key='') are skipped — give them a
folder. Deliberately does NOT import dlna_library (module import runs
migrations on the live DB); creates `book_meta` itself if missing.

```bash
python3 tools/openlibrary_books.py                # dry-run preview
python3 tools/openlibrary_books.py --apply -v     # fetch + write
python3 tools/openlibrary_books.py --refetch --apply   # redo non-manual
python3 -m unittest tools.test_openlibrary_books -v    # 36 tests
# retry one book:
sqlite3 library.db "DELETE FROM book_meta WHERE source='notfound' AND album_key='…'"
```

### `tools/book_language.py`

Reports the language of every audiobook, and settles the Unknowns (see
**Audiobooks by language**). Writes only `book_language`; never a file,
never `tracks`. DRY-RUN by default.

- `--tags` reads each book's first file for a language tag (ID3 `TLAN`,
  an MP4/Vorbis `language` field) and stores it as `source='tag'`.
- `--set XX --match TEXT` stores `XX` as `manual` for every book whose
  folder contains TEXT (case-insensitive). `--set` without `--match` is
  refused — it would touch every book.
- `--unknown-only` narrows either to the books currently Unknown.
- `--clear --match TEXT` hands books back to detection.

```bash
python3 tools/book_language.py                    # tally + the Unknown list
python3 tools/book_language.py --tags --apply
python3 tools/book_language.py --set en --match "Top 100 Sci-Fi" --unknown-only --apply
python3 -m unittest tools.test_book_language -v   # 6 tests
```

### `tools/compilation_playlists.py`

Creates playlists for **scattered compilation albums** — an album TAG
("2 meter sessies Volume 1", "Billboard Top 100 of 1970") shared by
tracks that live in many different folders (one per contributing
artist). Folder-based album grouping can't reunite them, so they're
invisible as albums; this exposes each as a playlist named after the
tag, tracks ordered artist → title.

Selection (defaults tuned on the real library, 2026-07-03): ≥`--min-tracks`
(5) tracks share the exact album tag, by ≥`--min-artists` (3) distinct
artists, and **no single folder holds ≥`--max-per-folder` (5) of them**.
The artist floor excludes single-artist albums (Supertramp "Paris"); the
per-folder ceiling excludes generic-title collisions ("Greatest Hits" =
20 different artists' separate albums). Existing playlists are skipped by
case-insensitive name, so re-running after new rips only adds what's new
— safe as a post-rip habit. DRY-RUN by default; `--apply` mutates.

```bash
python3 tools/compilation_playlists.py               # preview
python3 tools/compilation_playlists.py --apply       # create
python3 tools/compilation_playlists.py --min-tracks 8
python3 -m unittest tools.test_compilation_playlists -v   # 10 tests
```

First real run (2026-07-03) created 13 playlists (the 2 meter sessies
family, Essential Classical Chillout, Toen Was Het Stil Op Straat,
Cohen Covered, …); the 3 Billboard candidates already existed. Side
fix: `pl_get` now orders by `added_at, id` — `added_at` alone has
second resolution, so bulk adds tied and returned in arbitrary order.

### `tools/folder_compilation_playlists.py`

The other half of the compilation problem. `compilation_playlists.py`
(above) reunites an album TAG *scattered* across many folders; this one
takes the opposite shape — **one folder that IS a compilation**, holding
a track each by dozens of performers:

    Various Artists - 70s HITS 100 Greatest Songs of the 1970s (2023) …
    VA - 100 Greatest Jazz Icons (2020) …
    Blue Note Trip - Somethin' Old

These already browse correctly — folder-album grouping renders them as
one Various-Artists album, and they resolve fine through `album_tracks`
(checked: the `0` letter bucket is the digits catch-all and holds all
204 of them). What they lack is a NAME anywhere the eye lands: they sort
under a leading digit among 200-odd other digit-albums, and they appear
on **no** performer's artist page, because every track carries its own
artist and nothing files the folder under "Various Artists" (that artist
page holds 7 albums, not 137). A playlist puts each one in the list
that every surface shows first — the PWA panel, the Naim's `Playlists`
container, Amperfy/CarPlay.

**The risk runs one way**, which is what shapes the rule: a missed
compilation stays exactly as browsable as it is today, while a false
positive drops litter into a list the owner curates by hand. So "lots of
artists" is never sufficient on its own — measured live, `Unknown
Artist/Unknown Album` holds **126 performers** and is a junk drawer,
while `Santana - Supernatural` holds 9 because the record is full of
guests. A folder qualifies when it makes an album claim and then either

* **says so** — a `Various Artists` / `VA` / `V.A.` head or a collection
  phrase ("greatest", "the best", "hits", "top 40", "collection",
  "anthology", "essentials", Dutch "beste"/"voltreffers"), with
  ≥`--min-artists` (4) performers at ≥45% performers-per-track; or
* **is shaped like one** — no such phrase anywhere, but ≥8 performers at
  ≥65%, or a near-perfect ≥95% spread across ≥5. This narrower tier is
  what catches `Blue Note Trip - Sunset`, `Life On Mars` and
  `De Pre Historie 1964`, which announce nothing.

Four things are load-bearing, each learned from the data rather than
guessed:

- **The album TAG is searched, not just the folder name.** Three folders
  here are called `Wembley`, `Grrl` and `Speed Ticket` and carry the tag
  `Best Of JMFH's Choice 2004-2007 - <name>`. The tin is blank; the
  tracks are not. (The reverse also bites: the folder named `Captain
  Beefheart … Trout Mask Replica` actually contains `28 Vlaamse
  voltreffers` — misfiled, and only the tag says so.)
- **A folder whose tracks declare NO album never qualifies**, whatever
  its performer count. Same junk-drawer rule `_localfs_album_group`
  already enforces for browsing, and the one check that keeps the
  126-artist drawer out. A real compilation names itself.
- **Only the ABBREVIATED VA markers are read as a bare leading token**
  (`VA The Very Best Of Smooth Jazz`, no separator). "Various" is an
  ordinary English word and would claim a band called `Various
  Comforts` — the whole-head match that `dlna_artist_infer` uses is
  what normally prevents that.
- **Names are tidied and de-collided.** A 40-CD box here nests every
  disc in a directory literally called `CD` and tags all forty with the
  same string, so tag-first naming would yield forty identical
  playlists and leaf-first naming forty called "CD"; the distinguishing
  segment wins instead (`Greatest Hits Collection - 50's Cd1…Cd40`).
  `tidy_name` then strips release-group cruft so a name fits a Naim
  remote — `VA - Hi-Res Masters 50 Britpop Tracks … [24Bit-FLAC]
  [PMEDIA] ⭐️` → `Hi-Res Masters 50 Britpop Tracks To Test Your
  Speakers` — while **keeping the year and the disc number**: `(2023)`
  says which edition, and `(Disc 1)` is the only thing separating two
  discs.

Tracks are added in **`file_path` order** — the running order the
compiler chose, which also keeps `CD 1/` ahead of `CD 2/`. Artist→title
would destroy the sequencing that is the whole point of a compilation.

Nothing here writes to `tracks`, touches a file, or edits an existing
playlist; a candidate whose name already matches one is skipped, so
re-running after new rips only adds what is new. DRY-RUN by default.

```bash
python3 tools/folder_compilation_playlists.py            # preview
python3 tools/folder_compilation_playlists.py -v         # + why rejects were rejected
python3 tools/folder_compilation_playlists.py --named-only
python3 tools/folder_compilation_playlists.py --apply
python3 -m unittest tools.test_folder_compilation_playlists -v   # 36 tests
```

First real run (2026-09-09): **137 playlists created** from 2,223
folders — 113 named, 24 structural, 5,528 track rows; playlists went
39 → 176. Rejected as intended: the 126-artist junk drawer, the
guest-heavy Eminem/Santana/Rod Stewart records, and a 53-track
Tchaikovsky box at 11% performers-per-track.

### `tools/artist_mbid.py`

Resolves every artist to a MusicBrainz id — the keystone the rest of the
artist metadata hangs off. Two phases, cheapest first: read the
`musicbrainz_artistid` the file already carries (free, exact, immune to
the namesake problem), then search by name with
`dlna_mbid.accept_match` deciding what to believe.

Live result: **2,889 artists, 2,841→2,889 resolved (443 from tags, 2,446
by search), 400 refused, 85.9% search acceptance.** Resumable — a row of
ANY source including the sticky `notfound` means "already asked", so a
second run costs minutes rather than hours.

Skips names that aren't artists before spending a request, via the UPnP
tree's own `_is_junk_name`: the first live pass sent 12 of 23 searches to
things like `13. My Girl - The Temptations` and `07`. Note
`is_a_performer_name` is NOT the right guard here — it needs
`allow_numeric=True` (112 and 10CC are real bands) and with that flag it
accepts everything, junk included.

```bash
python3 tools/artist_mbid.py                     # preview
python3 tools/artist_mbid.py --tags-only --apply # the free phase only
python3 tools/artist_mbid.py --apply             # the full sweep (~2h)
python3 -m unittest tests.test_mbid_match -v     # 27 tests
```

To retry refusals after retagging files:
`sqlite3 library.db "DELETE FROM artist_meta WHERE source='notfound'"`.

### `tools/artist_meta.py`

Turns each MBID into the facts the ℹ️ panel shows: identity, life-span,
birthplace, genres, biography, best-known tracks, and — for a band — the
members with their instruments and date ranges. Runs after
`artist_mbid.py`.

Live: **2,889 artists in 3h37m, zero errors** — best-known 100%,
birth/formed 92%, biography 74%, photo 67%, 1,175 line-ups over 12,101
member rows. Resumable via `meta_fetched_at`.

> ⚠ **Set `LASTFM_API_KEY` BEFORE the first run.** `meta_fetched_at`
> marks an artist done, so a pass without the key leaves every artist
> with an empty "best known for" and skips them on any re-run.

KNOWN LIMIT: Wikipedia is looked up by ARTIST NAME. MusicBrainz no
longer carries direct wikipedia links (it points at Wikidata), and
resolving through Wikidata would add two requests per artist to an
already hour-long pass. A wrong title usually lands on a disambiguation
stub, which `parse_wikipedia` refuses — so the shortcut costs a missing
biography, never a wrong one.

Known inefficiency: it fetched 2,889 and wrote 2,858 rows — spelling
variants that normalise to one key were each fetched. 31 duplicate
requests, harmless.

```bash
python3 tools/artist_meta.py                  # preview
python3 tools/artist_meta.py --apply --limit 50
python3 tools/artist_meta.py --apply          # the full pass (~1h)
python3 -m unittest tests.test_artist_fetch -v   # 32 tests
```

### `tools/track_credits.py`

Step 4 of the enrichment chain: composer/lyricist from MusicBrainz for
the tracks whose files carry none. Runs after `artist_mbid.py`, because
it browses each **artist's works** (up to 100 per request, each already
carrying its writer relations) rather than resolving track → recording →
work — the difference between ~1.2 h and ~16 h. See
§"Step 4 — the two thirds with no tag" for the rules.

Live: **11,025 matched, 6,067 sticky `notfound`, 0 errors, ~2 h** —
composer coverage 32% → **73.9%**. Resumable: a row of any source,
including the negative, means "already asked". DRY-RUN by default.

```bash
python3 tools/track_credits.py                     # preview
python3 tools/track_credits.py --apply --limit 20  # a bounded pass
python3 tools/track_credits.py --apply             # the full sweep
python3 -m unittest tests.test_track_credits -v    # 10 tests
# retry one track:
sqlite3 library.db "DELETE FROM track_credits WHERE source='notfound' AND url='…'"
```

### `tools/audit_playlist_orphans.py`

Finds — and relinks — **playlist rows pointing at a track the index no longer
has.** `playlist_tracks` is deliberately independent of `tracks` (that is what
lets a playlist survive `clear(udn)` and a rebuild); the cost is that nothing
notices when a row goes stale. A LocalFs track id is `sha1(rel_path)`, so
**renaming a folder** — or splitting a whole-album file into per-track files,
or changing the file-server port — silently orphans every playlist row that
referenced it. The files are still on disk under new ids; the playlist still
holds the old ones.

From the sofa that is "the app skips songs at random": the dead id resolves
to a 404 and the browser reports an unplayable file. (The relay no longer
hands that 404 to `<audio>` as audio — see **Two things the relay must keep
doing** — but the row stays dead until it is repaired, which is what this is
for.)

Matching ladder, both sides normalised
(diacritics, smart quotes, case, whitespace) so a retag doesn't break it:
**(artist, album, title)** strong → **(artist, title)** song-level (the album
name legitimately differs between a compilation and the original release).

**A row that matches nothing is KEPT by default** (`--remove-unmatched` to
delete). It is usually music still on disk under a name the tool cannot
guess, and a playlist entry the owner can see and fix beats one that
vanished. Relinking onto a track already in that playlist would violate
`UNIQUE(pl_id, url)`, so the stale row is dropped as a duplicate instead. A
blank artist/title is never a match *target* — it would collide every orphan
onto one row. Existing `art` is never overwritten; a blank one is filled.
Idempotent; DRY-RUN by default; `--apply` backs up `library.db` first.

```bash
python3 tools/audit_playlist_orphans.py                    # report
python3 tools/audit_playlist_orphans.py --apply            # relink
python3 tools/audit_playlist_orphans.py --apply --remove-unmatched
python3 -m unittest tools.test_audit_playlist_orphans -v   # 15 tests
```

First real run (2026-08-25, 14 orphans of 1091): **12 relinked** — the whole
"latin Jazz" playlist, still on the pre-cutover `:8201` port (the 2026-05-31
port change healed `metadata_overrides` but nobody healed playlists) — and
**2 kept**: two whole-album Harmonium files since split into per-track files,
which have no single successor row and are the owner's call to re-add.

### One-shot migration tools (historical)

`tools/relink_orphan_overrides.py`, `tools/audit_override_mismatches.py`,
`tools/find_duplicate_audio.py`, `tools/relink_playlists_to_localfs.py` and
`tools/relink_overrides_to_localfs.py` worked on the previous backend's
URLs during the move to LocalFs. They have done their job; each carries its own
docstring and tests if ever needed again.

### `tools/correct_year_drift.py`

Fixes the case where AcoustID resolves a fingerprint to a later
re-release recording on MusicBrainz (e.g. a 2001 compilation's
recording entry instead of the 1979 studio original), and BOTH
`tracks.year` and `metadata_overrides.year` agree on the reissue date
so the in-display MIN logic can't recover. The tool walks
`(artist, title)` groups and rewrites `metadata_overrides.year` on
the later instances to the earliest plausible year evidenced by
another instance of the same song in the user's library.

#### Identification (v3)

- **Effective year** per row: `MIN(file_year, mb_year)` when both
  present, else whichever is set.
- **earliest_plausible** per `(lower(artist), lower(title))` group:
  `MIN(eff)` among non-live rows with `eff >= 1950`. The floor
  excludes file-tag errors like the Bowie `1905` / Trammps `1927`
  cases observed 2026-05-28.
- **Live filter**: row excluded from being a candidate (and from
  being someone else's earliest_plausible) if its album OR title
  matches any `LIVE_MARKERS` substring: `live`, `in concert`,
  `on tour`, `at the …`, `pulse`, `earls court`, `wembley`,
  `madison square`, `fillmore`, `royal albert`, `wall live`,
  `unplugged`, `(live …)`, `[live …)`, `- live`, `session`,
  `bootleg`. Conservative on the "live" side — a studio album
  named "Live At Carnegie Hall" by mistake would be excluded;
  worth tuning per-corpus if it bites.
- **Candidate** when `eff - earliest_plausible >= 3` AND the row
  is non-live.

#### Persistence

Writes `metadata_overrides.year = earliest_plausible,
source='manual'`. Same column the PWA's edit modal writes to.
Survives re-index and AcoustID re-runs (`manual` beats `acoustid`
in `metadata_override_set`). Idempotent — a second run finds zero
candidates because the freshly-applied year now IS the earliest.

#### Library-only correction limit

The tool can only correct to the earliest year *available in the
library*. If a song only exists in your library on a 1997
compilation (no 1972 original copy), the tool will surface a
candidate but its target year is 1997 — better than 2001, but not
the MB-true 1972. There's no MusicBrainz fallback in this tool by
design; it's the AcoustID worker's job to ask MB.

#### Usage

```bash
# Dry-run preview (default top=30):
python3 tools/correct_year_drift.py

# Full preview, all candidates:
python3 tools/correct_year_drift.py --top 0

# Apply with confirmation prompt:
python3 tools/correct_year_drift.py --apply

# Non-interactive (cron / scripts):
python3 tools/correct_year_drift.py --apply -y

# Custom DB:
python3 tools/correct_year_drift.py --db /path/to/library.db
```

#### Tests

`tools/test_correct_year_drift.py` — 13 tests over a throw-away
SQLite. Covers: studio-vs-live exclusion (Pulse, MTV Unplugged),
drift threshold boundary, pre-1950 floor excludes bogus tags,
case-insensitive `(artist, title)` grouping, MIN logic on file +
mb_year, no candidate when only one album, apply creates a new
override / merges with an existing one and forces source='manual',
apply is idempotent.

```bash
python3 -m unittest tools.test_correct_year_drift -v
```

### `tools/improve_song_years.py`

External-lookup counterpart to `correct_year_drift.py`. Where the
drift tool can only correct songs the user owns multiple copies of
(~2.4k songs in this library), this tool queries MusicBrainz for
the **earliest known recording year** of every song in the library
(~25k unique `(artist, title)` groups). Targets the case where the
user only owns one copy of a song and that copy is a later
compilation/remaster — e.g. "Louis Armstrong / What a Wonderful
World" on a 2017 compilation, true original 1967.

#### Algorithm

Per `(artist, title)` group:

1. MB `recording?query=artist:"X" AND recording:"Y"&fmt=json&limit=100`
2. Paginate up to 5 pages (max 500 recordings) at 1.1s/req rate limit
3. Collect every recording's `first-release-date` year
4. Cache `MIN(years)` in `song_year_cache` with `source='mb_recording'`
5. If no usable date → cache as `source='notfound'` (sticky)

The pagination matters: the top-15 result set often surfaces
later-edition recordings before the 1967 original. Full pagination
on "What a Wonderful World" → 234 matches across 3 pages → MIN
year = 1967.

#### Cache table

```sql
CREATE TABLE song_year_cache (
  artist_key TEXT,           -- _norm_title(artist)
  title_key  TEXT,           -- _norm_title(title)
  year       INTEGER,        -- MIN MB year, NULL on no-match
  source     TEXT,           -- 'mb_recording' | 'notfound'
  n_matches  INTEGER,
  fetched_at INTEGER,
  PRIMARY KEY (artist_key, title_key)
)
```

Same persistence invariant as `album_art` / `play_counts` /
`lyrics` / `metadata_overrides`: survives
`clear(udn)` and rebuild-index. Sticky-negative cache prevents
re-hammering MB for songs with no MB entry.

#### Apply step

After lookups complete, walks cached hits. For each track of the
matching `(artist, title)` whose current effective year is later
than the cached year, writes `metadata_overrides.year = cached_year`
with `source='manual'`. The display rules and AcoustID worker
already treat `manual` as the highest-trust source, so subsequent
worker passes won't overwrite. User-edited manual rows are NEVER
touched.

#### Phantom-row skip

(Unknown Artist) and track-number-prefixed titles ("01 - …") are
skipped before MB queries — they're filename-derived metadata that
will never match. Saves ~700 queries on this library.

#### Cost

~25k uncached groups × ~1.5s avg (rate limit + most songs need
only 1 page) ≈ **10 hours** for a full library sweep. Most songs
need 1 page; long-tail ("Yesterday", "Stairway to Heaven") may
hit the 5-page cap. Subsequent incremental runs only hit newly-
indexed songs.

#### Usage

```bash
# Dry-run preview, no MB calls, no DB writes
python3 tools/improve_song_years.py

# Query MB for uncached groups (cap at N for testing)
python3 tools/improve_song_years.py --lookup --limit 100 -v

# Apply cached hits to tracks
python3 tools/improve_song_years.py --apply

# Combined: lookup then apply in one invocation
python3 tools/improve_song_years.py --lookup --apply

# Force re-query a single song:
sqlite3 library.db "DELETE FROM song_year_cache WHERE artist_key='louis armstrong' AND title_key='what a wonderful world'"
```

Validated 2026-05-28 — first 10 live queries hit 9/10, recovered
correct original years for 10cc "Donna" (1972, was 1997 in library),
"Art for Art's Sake" (1975), "Dreadlock Holiday" (1978).

#### Tests

`tools/test_improve_song_years.py` — 26 tests covering: normalisation
(curly apostrophe, diacritics, case, whitespace, empty); schema
creation (idempotent); candidate selection (distinct groups, dedup
via norm, cache exclusion, empty fields, **(Unknown Artist) phantom
exclusion, track-number-prefix exclusion**); lookup (cache hit /
notfound / transient error leaves uncached / `--limit` honored);
apply (write when later, skip when at/before cached, skip manual
overrides, overwrite acoustid override, skip notfound entries,
apostrophe normalisation); MB query string escape.

```bash
python3 -m unittest tools.test_improve_song_years -v
```

### `tools/beets_enrich.py`

Runs the **beets tag-in-place enrichment batch** described in
`docs/enrichment.md`. beets reads MusicBrainz + AcoustID and writes clean
tags + MBIDs **into the files**, in place; the existing mutagen indexer
then picks them up on the next re-index. beets is an *upstream batch
stage*, never a live metadata authority (routing the serve path at a
beets/Jellyfin DB would create a second source of truth). This tool is a thin, safe wrapper around the external `beet`
CLI — it does not reimplement beets.

#### The one non-negotiable invariant — tag IN PLACE

```
import.write = yes    import.copy = no    import.move = no
```

`verify_inplace()` is a hard gate: any write run **aborts** unless the
config is verified in-place safe. A *missing* key counts as unsafe (beets'
`import.copy` default is `yes`, which would duplicate files), so the user
is pushed to `--write-config` rather than relying on beets defaults. beets'
own `library.db` (`~/.config/beets/library.db`) is kept deliberately
separate from the gateway's `library.db`. The `scrub` plugin is
intentionally NOT enabled (it strips existing tags — `docs/enrichment.md`
§3).

The generated config sets `timid: no` (not §3's `yes`): beets refuses to
run `-q`/`--quiet` while timid is on (*"can't be both quiet and timid"*),
so timidity is a **CLI choice** here — `--quiet` for the auto-accept bulk
pass, `--timid` for a cautious per-match review pass. If a hand-edited
config still has `timid: yes`, `--quiet` aborts early with a clear message
pointing at `--write-config`.

**beets 2.x pluginized MusicBrainz.** MB is no longer built into the
autotagger — it is the `musicbrainz` metadata-source plugin, which the
generated `plugins:` line now enables and which needs the `musicbrainzngs`
package. Without **both**, beets has no metadata source and silently
matches nothing ("No matching release found" for every album → 0 imports,
exit 0 — the failure mode that wasted two multi-hour runs on 2026-06-03).
The tool guards this: it aborts before running if the config's `plugins:`
line lacks `musicbrainz`, or if `musicbrainzngs` isn't importable in
beets' own environment (probed via the `beet` console-script shebang),
each with the exact fix. One-time deps (2026-07-02 — **install beets via
Homebrew, NOT pip**; a `pip3 install beets` gets wiped by every Homebrew
python upgrade, which is how the 2026-06 install died):

```bash
brew install chromaprint beets
# the formula ships WITHOUT these two plugin deps; put them in the keg
# venv — and RE-RUN this after any `brew upgrade beets`:
BEETS_KEG=$(brew --prefix beets)/libexec
$BEETS_KEG/bin/python -m pip install --prefix $BEETS_KEG musicbrainzngs pyacoustid
```

(Same block lives in `requirements.txt` → "beets enrichment toolchain".
The tool's start-up guards print these exact fixes when either is missing.)

**Quiet mode says just "Skipping." on an untagged album?** Two known
causes, diagnosable with `beet -v import -q <dir>`: (1) `chroma: acoustid
album candidates: 0` — beets' fingerprint lookups use a SHARED bundled
AcoustID key that gets rate-limited (error 14), every lookup fails
silently, and text-matching bare tags yields garbage; (2) the release
isn't on MusicBrainz at all. Proven fix for bare files (Nena, 2026-07-02):
pre-tag minimal TEXT tags with mutagen (artist, exact MB album title,
title = filename stem), then re-run `--quiet --revisit` — the MB text
search matches without fingerprints and beets writes full canonical tags.

#### Flags

| Flag | Effect |
|---|---|
| `--write-config` | Write the prog-tuned, tag-in-place `~/.config/beets/config.yaml` (backs up any existing) and exit |
| `--music-root PATH` | Library root (default `/Volumes/SAMDATA/Music`) |
| `--album PATH` | Import a single album dir instead of the whole root |
| `--quiet` | Auto-accept strong matches, no prompts — the bulk pass (`beet import -q`) |
| `--timid` | Prompt per match (more granular than the default per-album prompt) |
| `--revisit` | Re-import a dir already recorded done (`-I` / noincremental) |
| `--reindex` | After import, discover the LocalFs server UDN via `/api/servers` and POST `/api/index/rebuild` |
| `--gateway URL` | Gateway base for `--reindex` (default `http://127.0.0.1:8765`) |
| `--udn UDN` | Server to reindex (default: auto-pick the `uuid:localfs-*` server) |
| `-n` / `--dry-run` | Print the resolved command + safety report; do not invoke beets |
| `-y` / `--yes` | Skip the §7 backup-warning confirmation before in-place writes |

#### Safety

- Hard-fails fast if `beet` isn't installed (`brew install chromaprint
  beets` + the keg plugin deps — see the one-time-deps block above) or the
  config is missing/not in-place.
- Refuses to run if the music drive isn't mounted (external SAMDATA).
- Confirms before any in-place write (with the backup warning) unless `-y`.
- Warns when DSD (`.dsf`/`.dff`) files are under the target or when fpcalc
  is absent — Chromaprint can't decode DSD (`docs/enrichment.md` §6); those
  tag by existing metadata or fall to Picard.

#### Usage

```bash
# one-time deps (brew, NOT pip — see the block above for the keg plugin deps)
brew install chromaprint beets
# write the tag-in-place config, then interactive review, then bulk
python3 tools/beets_enrich.py --write-config
python3 tools/beets_enrich.py                 # interactive (prompts per album)
python3 tools/beets_enrich.py --quiet         # auto-accept strong matches
python3 tools/beets_enrich.py --quiet --reindex   # then re-index LocalFs
python3 tools/beets_enrich.py --dry-run       # show command + safety report
```

#### Tests

`tools/test_beets_enrich.py` — 23 unit tests over the pure helpers (never
invokes `beet` or the network): config carries the in-place invariant +
separate beets library + no scrub + original-year bias and passes its own
gate; `verify_inplace` flags copy/move/write violations and missing keys;
`build_import_cmd` argv for interactive/quiet/timid/revisit; LocalFs UDN
selection (override / prefer-localfs / sole / ambiguous / none);
`find_binary` fallback.

```bash
python3 -m unittest tools.test_beets_enrich -v
```

### `tools/post_beets_reindex.py`

The "make beets' work visible" step that runs AFTER `beets_enrich.py` has
tagged files in place. Two things must happen for the gateway to show the
new tags, and this tool does both in the right order:

1. **Clear the AcoustID `metadata_overrides`.** LocalFs track URLs are
   PATH-based (`sha1(rel_path)`, see `_track_id_for` in
   `dlna_providers/localfs_tags.py`), so a
   beets-tagged file keeps the SAME url. The COALESCE pass in
   `LibraryDB.upsert_tracks` (`dlna_library_tracks.py`) therefore re-lays
   the old `source='acoustid'` override straight back on top of beets'
   fresh tags and masks them. Deleting the acoustid rows lets the file
   tags show through.
2. **Reindex LocalFs** (`POST /api/index/rebuild?udn=…` → for LocalFs this
   dispatches to `provider.rescan(force=True)`, which re-reads every file's
   tags but does **NOT clear** the tracks table first).

   > **✅ FIXED 2026-07-12 (was the 2026-07-03 caveat): a rescan now
   > updates the metadata of EXISTING-URL rows in place.** `upsert_tracks`
   > gained a URL-keyed metadata-refresh pass (title/artist/album/year/
   > duration/etc.; change-guarded so untouched rows are no-ops;
   > incoming-empty genre/art never blank an existing value; UNIQUE
   > collisions skipped via `OR IGNORE`) plus the previously-missing
   > `tracks_au` AFTER-UPDATE FTS trigger — the old schema only synced FTS
   > on INSERT/DELETE, so ANY in-place metadata UPDATE (including the
   > overrides COALESCE pass) desynced search until the next full rebuild;
   > the trigger's migration does a one-time FTS rebuild on existing DBs.
   > In-place retagging (beets) is therefore visible to a plain rescan —
   > the old `DELETE FROM tracks` + rebuild workaround is obsolete.
   > Regression-guarded by `tests/test_library.py::TestUpsertMetadataRefresh`
   > and the retag/force tests in `tests/test_provider_localfs.py`.

**Manual-override safety (the core invariant, regression-guarded):** ONLY
`source='acoustid'` rows are deleted. `source='manual'` (user edits + the
year-drift / `improve_song_years` corrections) is NEVER touched — those
legitimately win over beets. `notfound` / `video_skip` rows carry NULL
metadata so they mask nothing and are left alone.

DRY-RUN by default (prints the override breakdown + planned actions);
`--apply` deletes + reindexes, auto-backing-up `library.db` first.

```bash
python3 tools/post_beets_reindex.py            # preview
python3 tools/post_beets_reindex.py --apply    # backup + clean + reindex
python3 tools/post_beets_reindex.py --apply --no-reindex   # clean only
python3 tools/post_beets_reindex.py --apply --no-clean     # reindex only
python3 -m unittest tools.test_post_beets_reindex -v
```

Flags: `--apply` / `-n`/`--dry-run` · `--no-clean` · `--no-reindex` ·
`--no-backup` · `--udn` · `--gateway` · `--db` · `-y`/`--yes`.
Tests: `tools/test_post_beets_reindex.py` — 9 tests (delete-only-acoustid,
dry-run no-op, default-is-dry-run, backup-on-apply, clean-only under
`--no-reindex`, `--no-clean`+`--no-reindex` rejected, missing DB fails
cleanly).

### Steady-state workflow for new music

Drop files → `beets_enrich.py --quiet` (incremental — only new folders;
already-seen dirs need `--revisit`) → `post_beets_reindex.py --apply`.
