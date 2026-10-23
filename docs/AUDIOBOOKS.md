# Audiobooks

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

## Audiobooks (P1+P2 shipped 2026-07-13; P3–P5 planned)

Full plan/design lived in the local-only `REPORTS.html` Part II (not in the
repo — see above). What's live:

- **Second LocalFs library** — `AUDIOBOOKS_ROOT` in `.env`
  (`/Volumes/SAMDATA-1TB/Audio_Books`; or `localfs.audiobooks_root` in
  config.json). `maybe_start_localfs` starts a second `LocalFsProvider`
  (name "RoHaAudioBooks", own UDN, `dlna_localfs_wiring.AUDIOBOOKS_UDN`),
  served by the same `:8200` file server (root joins `allowed_roots`).
  Per-UDN isolation keeps books out of music browse/radio/search for
  free. First scan: 11,548 chapters / 509 books (163 malformed files
  skipped — worth a `find_corrupt_audio.py` pass someday). `.m4b` added
  to `AUDIO_EXTENSIONS` (MIME `audio/mp4`).
- **Two multi-root safety fixes that matter for ANY future third root**:
  `_load_cache` is scoped to the provider's own root (the shared
  `localfs_files` cache + removed-detection would otherwise purge the
  OTHER provider's rows every rescan), and secondary-root track ids are
  salted (`_track_id_for(rel, namespace)` — the file server resolves
  obj_id across ALL localfs UDNs, so identical rel_paths under two roots
  would collide; music keeps namespace='' so its ids/URLs are unchanged).
- **`kind` field** on `/api/servers` entries (`audiobooks` | `music`) —
  the PWA gates all resume behaviour on it, never on track shape, so a
  music playlist can't trigger position writes.
- **Resume positions** — `playback_positions` (see schema), LibraryDB
  `position_set/get/clear/positions_list`, native `POST /api/position` +
  `GET /api/position?album_key=` + `GET /api/positions` (NOT on the SW
  CACHEABLE_API allowlist — live state, network-only by default; POST
  parses raw body so sendBeacon works).
- **PWA** (`static/app.js`): a book queue (`opts.book` through
  `playTracklist` → `browserQueueIsBook`) never shuffles; positions save
  every ~20s + on pause/chapter-end + `sendBeacon` on visibilitychange;
  playback-rate select `#ab-rate` (0.8–2×, browser output only,
  localStorage-persisted, re-asserted per chapter). Root-level
  single-file books (album_key='') key their position by file URL.
  **CONTINUE IS THE DEFAULT (2026-07-14, after the first real Naim/iPad
  test):** the bookmark is SERVER-side per book, so EVERY play entry
  point resumes on every device — `playAlbumFromDB` auto-resumes for
  the audiobooks source (covers the header button AND the ▶ on book
  rows), the header PRIMARY button relabels to "▶ Resume · Ch N —
  H:MM:SS" with `#browse-resume` as the explicit "↻ Start over", and
  tapping a chapter row routes through `_playBookFromChapter` (a real
  book queue from that chapter — the old single-track path silently
  never saved positions). A finished book behaves like an unread one.
  Caveat: playback started from the Naim's OWN controller (browsing the
  📖 UPnP tree) can't resume — the renderer fetches files directly and
  the gateway isn't in that loop; resume applies to playback initiated
  from the PWA/CarPlay.
- **Tests**: `tests/test_positions.py` (20 — DB contract incl.
  clear(udn) survival, payload validation, wiring config),
  multi-root provider tests in `test_provider_localfs.py`,
  `tests/frontend/test_audiobooks.py` (9 Playwright — button states,
  source gating, unshuffled resume queue, pause-POST assertions,
  URL-fallback, rate control).

**OpenLibrary metadata overlay (P6, shipped 2026-07-13).** Canonical
author/title + **series name & number-in-series** per book, fetched by
`tools/openlibrary_books.py` (see its section under Library housekeeping
tools) into the `book_meta` table — a DISPLAY-layer overlay: files and
`tracks` are never written; sticky `notfound`; `manual` wins
(`LibraryDB.book_meta_set/get/all`). The PWA fetches
`GET /api/book_meta_all` once per switch onto the audiobooks source
(`abMeta` map in app.js) and renders 📚 series chips on album rows
(`renderBrowseItems`) plus a `#browse-series` header line
("📚 Night's Dawn #1 · ✒️ Peter F. Hamilton"). `series_seq` is REAL —
novellas are #1.5.

**P3 — Naim resume (shipped 2026-07-14).** `avtransport_seek()`
(AVTransport#Seek, `Unit=REL_TIME`) + `RendererQueue.start(...,
start_at_sec=, is_book=)`: the resume offset rides the
`/api/render_queue` POST (`book` + `start_at_sec` keys, sent by
`_resumeBook`), and the Seek fires async ~1.5s after Play with one
retry (a Seek during TRANSITIONING faults on the Naim; failure =
non-fatal, chapter plays from 0:00). While an `is_book` queue is
PLAYING, the existing 2s monitor persists the position every ~15s
(`playback_positions`, lazy `dlna_library.DB` import) — stop on the
Naim, resume in the PWA/CarPlay. `tests/test_renderer_resume.py`.

**P4 — CarPlay bookmarks (shipped 2026-07-14).** Subsonic
`getBookmarks` / `createBookmark` / `deleteBookmark` in `api_subsonic`
map onto the SAME `playback_positions` table (track id → its
`album_key` = the book; root-level single files key by URL;
`createBookmark` position is ms). `getBookmarks` lists unfinished books
whose chapter still resolves (`LibraryDB.track_by_url`); finished +
orphan rows are skipped. Cross-path consistency is regression-tested
(a CarPlay bookmark resumes in the PWA). `TestBookmarks` in
`tests/test_subsonic.py`.

**P5 — polish (shipped 2026-07-14).**
- **m4b chapter atoms**: `dlna_ffmpeg.probe_chapters/parse_chapters`
  (ffprobe `-show_chapters`; [] when no ffprobe/no chapters) behind
  `GET /api/chapters?url=` (in-memory cache keyed (url, mtime)). PWA:
  `#ab-chapters` picker in book mode (browser output) — choosing a
  chapter seeks.
- **Continue-listening shelf**: `GET /api/positions` rows are enriched
  with the chapter's track fields (book/author/art/chapter_title;
  orphans still listed bare). PWA: a **📖 letter-bar entry** (front,
  audiobooks source only) rendering in-progress books with a chapter
  progress bar; click opens the book.
- **Naim tree**: root gains "📖 Audiobooks" (only when the source has
  authors) → `abauthor:*` → `abbook:*` (3-field codec like `galbum:*`,
  resolved against `dlna_localfs_wiring.AUDIOBOOKS_UDN`) → chapter
  items. Book titles carry the OpenLibrary series ("… 📚 Culture #2").
  Garbled ids → empty container. `tests/test_upnp_audiobooks.py`.
- **Sleep timer**: `#ab-sleep` select (off/15/30/45/60 min, book mode)
  → pause + position save when it fires.

### Audiobooks by language — the 🌐 entry (2026-10-12)

The audiobooks source's letter bar reads `📖 🌐 ⭐ # 0 A…Z`. 🌐 lists
the languages, each named in itself via the browser's
`Intl.DisplayNames` ("Nederlands", "English"), and opens to that
language's books. PWA only — not on the Naim tree or Subsonic.

**The evidence is thin, and that shaped everything.** Measured over 491
books: **7 carry a language tag** (all "English"). OpenLibrary is no
help — a work has editions in many languages, which is how translated
series names got into `book_meta` before the English/Dutch rule. So the
language is DETECTED from the words the book already carries (folder,
album tag, chapter titles) by `dlna_book_language.detect_language`, and
answers **Unknown when they do not decide it**. Live first pass: 431
English, **9 Dutch** (exactly the Dutch LibriVox books), 51 Unknown —
nearly all of them titles that are just a name (*Neuromancer*, *Dune*).

Rules each learned from that pass — all tests in
`tests/test_book_language.py` (28):
- **A capitalised name is not evidence.** "Dan" is Dutch for "then",
  and Dan Simmons' *Hyperion* came out Dutch because his name is in
  every album tag. The capitalised words of the author/artist names are
  dropped wherever they appear.
- **…but a name's lowercase connector IS.** Discarding every name token
  lost the only Dutch word on *Sara Burgerhart*: "Betje Wolff **en**
  Aagje Deken".
- **Each distinct string counts once** — an album tag is repeated on
  every chapter, and must not outvote fifty distinct chapter titles.
- **A word both languages use is in neither list** (`in`, `is`, `was`,
  `over`, `door`, `die`, `man`, `we`) — asserted at import.
- **Only English and Dutch are detected.** Every extra word list is
  another source of false positives from names. Other languages arrive
  via a tag or a manual row; the facet shows whatever codes exist.

**A detected language is never stored.** It is recomputed on every
read (~0.05 s over 11k chapters), so a retag or rescan shows at once and
there is nothing to go stale. Only ASSERTIONS are stored, in
`book_language`: `tag` (from `tools/book_language.py --tags`) and
`manual` — which always wins, and which a `tag` never replaces.
"Unknown" is a real bucket, requested as `language=` (empty), and is the
to-do list for the tool, not a failure to hide.

The books view hides **Play all** (nobody plays a whole language);
`_drillShowChrome` restores it for every other view.

**Setting one by hand is in the ✏️ edit form** — a *Book language*
select, shown only when the chapter belongs to a folder book (the server
decides, from the URL: `GET /api/book_language?url=` answers
`{book:false}` otherwise). Its first option is **Auto — detected: X**
(or *from tag*, or *not detected*), which clears the stored row; any
other choice is stored as `manual`. The POST names the chapter URL,
never an `album_key`, so a client cannot write a row for a book it did
not open. A language change alone posts nothing to `/api/edit_track`.
`.edit-field[hidden]{display:none}` is load-bearing: `.edit-field` is
`display:flex`, which beats the `hidden` attribute, and every music
track would otherwise show an empty language select. Guarded by
`tests/frontend/test_book_language.py` (10).

Open question still standing: whether the Naim accepts m4b/AAC over
AVTransport — the tree exposes the books either way; test with one
file on the real device.
