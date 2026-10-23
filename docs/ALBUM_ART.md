# Album art — design, fetcher and measurements

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

## Album art persistence (Phase A + Phase B)

Album covers are cached in a dedicated `album_art` table, keyed by `(artist, album)`, independent of `tracks`. This means a rebuild-index is non-destructive to art:

```
album_art(artist, album, art_url, source, updated_at)
  PRIMARY KEY (artist, album)
  source ∈ {'sibling', 'musicbrainz', 'notfound', 'manual'}
```

### How it fills up

- **Phase A — sibling harvest** (`dlna_library.LibraryDB._backfill_album_art`): instantaneous SQL pass. Runs at end of every `upsert_tracks(udn, rows)` call (i.e. after each indexer run) and once at startup as a migration. Harvests per-album art from tracks that brought their own art (source = `'sibling'`) and applies it onto sibling tracks of the same `(artist, album)` that were missing one.
- **Phase B — external lookup** (`dlna_library.AlbumArtFetcher`, singleton `ART_FETCHER`): event-driven background worker. Fires on two hooks only — (1) a one-shot startup scan 120s after boot (`ART_FETCHER.start_initial_scan()` in `dlna_gateway.main`) to catch albums left bare by a previous interrupted run, and (2) `ART_FETCHER.trigger()` at the tail of every successful `Indexer._run()` so new bare albums from a fresh crawl get looked up immediately. No periodic poll. Walks `bare_albums(*, skip_udns, min_tracks, with_performers)` (tracks with no art AND no `album_art` row of any source — see §"Four things the fetcher was asking" for what those three opt-in filters exclude and why), asks each query `dlna_art_query.art_queries()` hands it, and for each resolves MusicBrainz **release-group first, then release** → HEADs `coverartarchive.org/{entity}/{mbid}/front-500`, writing hits as `source='musicbrainz'` and misses as `source='notfound'`. Rate-limited to ~1 req/sec (`_MB_RATE_LIMIT_SEC = 1.1`) per MusicBrainz ToS. If a trigger arrives while a scan is in flight, it's a no-op — the ongoing `run_once()` re-queries `bare_albums()` between batches and absorbs the new work into the current pass.

### What survives a rebuild-index

The `album_art` cache persists across rebuild-index operations — only the `tracks` table gets wiped and repopulated. After an `upsert_tracks()` call:

1. The fresh `tracks` rows may bring new per-album art from the server → harvested into `album_art` (source=`'sibling'`).
2. `_backfill_album_art(conn, udn=udn)` applies existing `album_art` URLs onto the new tracks that lack one.
3. Brand-new albums not yet in the cache become bare. At the end of `Indexer._run()` (success path), `ART_FETCHER.trigger()` fires and the MB/CAA lookup runs in a background thread. Hits are written into `album_art` AND straight onto `tracks.art` via an UPDATE within `run_once()`, so the now-playing window and any future playlist queries pick them up without another indexer pass. After a gateway restart, the 120s-post-startup scan picks up anything that an earlier run couldn't finish.

### Sticky "notfound" cache

The negative cache is sticky by design — once MusicBrainz failed to match an album, `AlbumArtFetcher` skips it forever so re-indexing doesn't re-hammer MB for the same misses. If you've since fixed artist/album metadata in the source server and want to retry, delete the notfound row(s):

```sql
-- Retry a single album
DELETE FROM album_art WHERE source='notfound' AND artist='…' AND album='…';

-- Retry everything MB missed previously
DELETE FROM album_art WHERE source='notfound';
```

Those albums become bare again and get looked up on the next `ART_FETCHER.trigger()` — which means either a rebuild-index of any server, or a gateway restart (the 120s startup scan).

### Phase A2 — the cover that sits BESIDE the music (2026-09-21)

The indexer only ever read **embedded** pictures, so an album whose cover
is a separate file in its folder showed none at all. Measured live: of
506 art-less album-folders, 99 had an image sitting right there that was
never looked at. `dlna_providers/localfs_art.py` finds a usable cover in
**95 of the 506 (19%)**.

**Choosing WHICH image is the whole problem.** From those same folders:

```
front.jpg          2927px  2032 KB
folder.jpg         1123px   425 KB
cover.jpg           300px    36 KB
albumartsmall.jpg    75px     3 KB   <- Windows Media Player
back.jpg           2900px  3015 KB   <- the BACK of the sleeve
label.jpg          2840px  1801 KB   <- the disc label
```

"First image wins" picks the 75px thumbnail. **"Biggest wins" picks the
BACK COVER** — it is the largest file in that list. Only the NAME says
which one is the front, so selection is **name-first**, with a size floor
only as a last resort.

- **Rejections match whole WORDS, never substrings.** Rejecting anything
  containing `cd` throws away `ACDC - cover.jpg`. Same trap
  `_is_junk_name` documents, one domain over.
- **A `CD1`/`Disc 2` subfolder also looks in its PARENT** — a box set's
  single cover sits beside the disc folders, not inside each. An ordinary
  folder never climbs, or every album under a genre folder would inherit
  a stray image from it.
- **WMP's `_large` variant is recognised BY NAME**, not left to the byte
  floor: measured at 6–14 KB (~200px), most sat just under it. A floor is
  the wrong instrument for a file that declares its own variant; `_small`
  is still rejected. Worth 23 of the 95.
- **12 MB cap, MIME sniffed from magic bytes** — same rules as embedded
  art (§"Can a media file phone home?"). An embedded picture is bounded
  by its audio file; a folder of 3 MB sleeve scans is not.

The fallback lives inside **`_extract_art_bytes`**, the ONE function both
the scanner and `/localfs/art/<id>` call — so one change indexes the
cover AND serves it, with no new route, no schema change, no URL change.
Its two early returns (mutagen cannot open the file / does not recognise
it) now fall THROUGH to the fallback: a file with no readable tags is
exactly when the folder cover is the only one there.
`tests/test_folder_art.py` (23).

### A marker is not a URL — `localfs-art:` in `album_art` (2026-09-22)

A LocalFs scan writes **`localfs-art:<sha1>`** into `tracks.art` as a
placeholder: the embedded picture's bytes are not read until serve time,
so at scan time all there is is a hash. `LocalFsProvider._rescan` heals
those into real `<base_url>/localfs/art/<obj_id>` URLs once the base URL
is known.

**That heal only ever covered `tracks`.** The Phase-A sibling harvest
(`_backfill_album_art`) copies `MIN(art)` straight out of `tracks`, so a
harvest running before the heal stored the marker in `album_art` — and
`INSERT OR IGNORE` guaranteed it was never replaced. **3,982 rows** on
this library held an unfetchable string.

It hid because `tracks` was clean (0 markers), so the PWA reads fine; the
exposure is everything that reads `album_art` DIRECTLY — Subsonic /
CarPlay cover art and the favourites list. It surfaced as five Queen
albums whose covers 404'd (`/art refused or failed (404) … Upstream 404`
in `gateway.log`), and those five were FLACs with **no embedded picture
at all** — `tracks.art` pointed at art that had never existed.

Both halves of the harvest are now marker-aware:
- **It heals first**, keyed on the marker, so a MusicBrainz or hand-set
  cover is never touched.
- **Then it refuses to harvest another**, so the row cannot come back.
- **A marker whose album has no healed track is LEFT, not deleted.**
  Deleting makes the album bare, which sends it to MusicBrainz — an hour
  of rate-limited lookups to repair a cache. The next scan gets it.
- **The harvest's second half** pushes `album_art.art_url` back onto
  art-less tracks, so a stored marker could travel INTO `tracks`; that
  direction is guarded too.

Live repair: **3,982 → 120**. All 120
remaining are ORPHANS — their album no longer exists in `tracks` — so
they are inert. `tests/test_album_art_sentinel.py` (9).

> **Correction (2026-10-04): the "1,099 art-less tracks filled" reported
> with this repair was never real.** Those tracks belong to `notfound`
> albums, whose `art_url` is `''`; the harvest's second half copied that
> empty string onto them, counted the no-op as a fill, and logged
> `filled=1099` on EVERY boot. The fill now requires a non-empty
> `art_url`. A boot line that reports the same non-zero number every
> time is a no-op being counted, not a repair that keeps undoing itself.

### Ask a question the cover sources can ANSWER (2026-09-21)

3,998 albums sat in `album_art` as a sticky `notfound`. Sampling showed
the **sources were never the bottleneck — the QUESTION was**:

```
Pink Floyd | Ummagumma                 -> musicbrainz   FOUND
Pink Floyd | Ummagumma - Studio Album  -> notfound      never will be
```

One album, two rows, opposite fates, decided by a suffix the ripper
added. And for compilations the question is unanswerable in principle:
*"does Harry Nilsson have an album called Voices of the 70s?"* No — it
isn't his; he is one contributing performer.

**`dlna_art_query.art_queries()` returns ORDERED attempts.** The exact
`(artist, album)` is **always first**, so nothing that resolves today can
regress; looser forms follow in increasing order of risk — a tidied title
is still that album, while title-only could in principle return a
different record of the same name, so it is offered **only for
multi-artist folders**, where including the artist is guaranteed to fail.

- **`tidy_album` is for LOOKUP ONLY** — never identity, never display. A
  too-loose query costs one wasted request; a too-loose identity merges
  two albums.
- Edition words are stripped only **after a dash at the END**: "Sign o'
  the Times - Live in Paris" is a title, "Ummagumma - Studio Album" is a
  marker. A name made entirely of cruft is handed back **unchanged**
  rather than emptied — an empty query searches for everything and caches
  the miss.
- **No new external source was added.** Dropping the artist and searching
  Cover Art Archive by title alone found covers for 3 of 4 such albums
  using the service we already have; iTunes and Deezer both scored 1/4 on
  the same failures, and iTunes' single "hit" was the wrong release.

`tests/test_art_query.py` (14).

### Four things the fetcher was asking that could not have an answer (2026-09-21)

A live pass over 1,497 "bare" albums returned **32 covers**. The low
yield was not bad luck — a rate-limited budget was being spent on
impossible questions. All four filters are **opt-in**, so no existing
caller silently narrows.

- **169 were AUDIOBOOKS.** `bare_albums()` had no udn filter, so the
  second LocalFs root was swept along with the music. The very first
  query of the run asked MusicBrainz for
  `artist='Patrick Rothfuss' album='The Kingkiller Chronicle Book 2'` —
  of a *music* database — and then cached the miss as a `notfound`
  against a book. `skip_udns` / `_audiobooks_udn()`.
- **90 were one- or two-track STRAYS** — a loose file in its own folder.
  It has no cover because it is not an album; every one is a guaranteed
  miss. `min_tracks`.
- **`multi_artist` never reached `art_queries`**, so the title-only query
  — the only form that can find a compilation, and the one
  `tests/test_art_query.py` already covered — never ran in production.
  `bare_albums` now reports the folder's distinct-performer count, which
  is what makes the decision possible at all.
- **The ENDPOINT was wrong for compilations.** Cover art attaches to a
  **release** — a specific pressing — while a *release-group* reports art
  only when one of its releases is flagged as the group cover:

  ```
  Harry Nilsson / Voices of the 70s
    release-group : 0 groups   -> no art
    release       : 1 release  -> art FOUND   (verified live)
  ```

  Measured 10% vs 4% over ten current failures. **Release-group stays
  FIRST** — it is the better answer when it works, one abstract album
  rather than forty pressings — and release is the fallback that rescues
  compilations.

After the fix the same fetcher ran at a **78% hit rate** (849 found / 239
notfound) against ~2% before.

### Where album art actually stands — and why the ceiling is low

Measured on the live library after all of the above:

```
album_art     sibling 11,424 · musicbrainz 2,217 · notfound 239
albums w/art  1,933 / 2,264 (85.4%)     was 77%
folders       fully covered 1,875 · PARTLY 58 · NONE 331
tracks w/art  25,245 / 26,905 (93.8%)
```

(Re-measured 2026-09-22, after the `localfs-art:` marker repair below.
The figures move with the library; treat
them as a scale, not a contract.)

> **849 covers found yielded only +67 folder-albums**, and the mismatch
> is structural, not a bug: `album_art` is keyed `(artist, album)` while
> a LocalFs album is a **FOLDER**. The same shape shows up every time
> these two identities meet; expect it when reading any art statistic.

**The remaining 335 are a media problem.** Of 402 art-less albums at the
time of the audit, **~60% are compilation-shaped names no database holds**
(`Billboard Top 100 of 1970`, `Greatest Hits Collection 60s`) and ~22%
are the one-or-two-track strays. Only ~17% look like real releases. Two
options were evaluated and **both declined**:

- **`beet fetchart`** — beets knows 709 of 2,248 albums and just **THREE**
  of the 402 without art. The albums beets could not import are the same
  albums Cover Art Archive cannot find, for the same reason.
- **Discogs** — has a free API and would have served the compilation
  category, but the release-vs-release-group fix above already recovers
  it from a source we are already using and already rate-limit correctly.
  Not worth a fifth external dependency for what is left.

Stop here unless the tags change. The lever that remains is retagging,
not another lookup service.
