# Fixes

A rolling log of non-obvious bugs: what was actually wrong, how it was
proven, and what would re-introduce it. Newest first, each entry headed by
the commit that carries the fix and the date it landed (`<sha> — YYYYMMDD`).

This is the "why", not the "what" — `git log` already has the what. An entry
earns its place here when the diagnosis took longer than the patch, or when
the symptom looked like something it wasn't.

> **Only the three most recent entries are kept.** Add a new one at the top,
> then run `python3 tools/rotate_fixes.py --apply` to drop whatever fell off
> the end. Nothing is lost — every rotated entry stays reachable in git
> history at the commit it names, which is why the sha is part of the
> heading and not decoration. Rotating by hand is fine too; the tool exists
> so the window is enforced rather than remembered.

---

## 568404d — 20260921 — 849 covers found, 67 albums gained one

**Symptom.** "Why do so many Pink Floyd albums have no covers?" 23% of
albums were bare, and a full fetcher pass over 1,497 of them returned
**32**. A 2% hit rate against a service that demonstrably has the art.

**What it wasn't.** Not MusicBrainz being thin, and not rate-limiting.
Sampling the 3,998 sticky `notfound` rows showed the sources answering
perfectly well — to a *different* question:

```
Pink Floyd | Ummagumma                 -> musicbrainz   FOUND
Pink Floyd | Ummagumma - Studio Album  -> notfound      never will be
```

One album, two rows, opposite fates, decided by a suffix the ripper
added. That reframed the whole thing: **the query was the defect, not
the lookup.**

**Four separate causes, all of them "a question with no possible
answer".** Each was found by reading what the fetcher actually sent, not
by reasoning about what it should send:

1. **169 of the 1,497 were AUDIOBOOKS.** `bare_albums()` had no udn
   filter, so the second LocalFs root was swept along with the music.
   The literal first query of the run asked a *music* database for
   `artist='Patrick Rothfuss' album='The Kingkiller Chronicle Book 2'`,
   then cached the miss as a `notfound` against a book.
2. **90 were one- or two-track strays** — a loose file in its own
   folder. It has no cover because it is not an album.
3. **`multi_artist` never reached `art_queries`.** The title-only query
   — the only form that can find a compilation, and one
   `tests/test_art_query.py` already covered — had never run in
   production. The tests passed; the wiring did not exist.
4. **The ENDPOINT was wrong for compilations.** Cover art attaches to a
   **release**; a *release-group* reports art only when one of its
   releases is flagged as the group cover:

   ```
   Harry Nilsson / Voices of the 70s
     release-group : 0 groups   -> no art
     release       : 1 release  -> art FOUND   (verified live)
   ```

**Proof.** After the four fixes the same fetcher ran at **78%** (849
found / 239 notfound). Separately, reading the cover file sitting
*beside* the music — which the indexer had never looked at, only
embedded pictures — found art in 95 of 506 bare folders (19%).

**The trap inside the fix.** Picking which image in a folder is the
front cover is the whole problem, and both obvious rules are wrong:

```
front.jpg          2927px  2032 KB
albumartsmall.jpg    75px     3 KB   <- "first wins" picks this
back.jpg           2900px  3015 KB   <- "biggest wins" picks this
```

Only the NAME says which one is the front. Selection is name-first with
a size floor as a last resort, and rejections match **whole words** —
rejecting anything containing `cd` throws away `ACDC - cover.jpg`. Same
substring trap `_is_junk_name` already documents, one domain over.

**What the numbers did NOT do.** 849 covers found produced **+67
folder-albums**. That is structural, not a regression: `album_art` is
keyed `(artist, album)` while a LocalFs album is a **folder**. Expect
the mismatch whenever those two identities meet — it is worth stating
out loud before quoting any art statistic.

**The honest stopping point.** Coverage went 77% → **85.1%** (1,913 of
2,248). Of the 402 art-less albums at audit time, ~60% are
compilation-shaped names no database holds (`Billboard Top 100 of
1970`), ~22% are the strays, and only ~17% look like real releases.
`beet fetchart` was evaluated and declined — beets knows 709 of 2,248
albums and just **three** of the 402 without art, because the albums
beets could not import are the same albums Cover Art Archive cannot
find, for the same reason. A Discogs integration was scoped and also
declined: the release-vs-release-group fix already recovers the one
category Discogs would have served.

**A measurement that was reported wrong, and corrected.** The dry run
was quoted as "17% of notfound rows get a better query". That was 664
rows whose query *shape* changed — not hits. Actual conversion was ~2%
on that biased sample. Query shapes are not outcomes.

## 339eed0 — 20260921 — the full UI, no content, and "works on iOS but not Mac"

**Symptom.** "Not seeing any content in music or audio books." Both
libraries. The app painted its complete chrome — header, letter bar,
panels — and never loaded a single row. Nothing in `gateway.log`.

**What it wasn't.** Everything server-side was healthy, and checking that
first is what made the real cause findable rather than guessable:

```
/api/servers   → both online: 26,548 music, 11,629 audiobook tracks
/api/libraries → all three roots active
/api/browse_letter, /api/albums → returning data
volumes        → mounted
```

A fresh Chromium pointed at the live gateway rendered 100 rows. So the
gateway was fine and the *client* was not.

**The tell was the platform split.** iOS worked; the Mac didn't. That is
not a WebKit-versus-Blink story — it means the two clients were holding
**different files**.

**Cause.** The ℹ️ Artist button was added to `index.html`, and `app.js`
bound it at the top level:

```js
$("np-btn-artist").addEventListener("click", …)
```

`app.js` is ONE long script. A client whose cached `index.html` predated
the button got `null` from `$()`, threw
`Cannot read properties of null (reading 'addEventListener')`, and
**every line below that point never ran** — including the initialisation
that loads content. iOS happened to hold a matching pair of files and
was unaffected.

**Proved before fixing**, by serving the previous `index.html` beside the
current `app.js` on a throwaway static server: the null throw appears,
and disappears once bound through the guard.

**Fix.** `bindClick(id, handler)` warns and skips when the element is
absent — a missing button is cosmetic, a dead `app.js` is the whole
application — and `APP_CACHE` v16 → v17 evicts the stale shell so clients
pull the matching HTML. The guard makes the mismatch survivable; the
bump is what ends it.

**What would re-introduce it.** Any new `$("id").addEventListener(...)`
at the top level of `app.js`. Bind through `bindClick`, and bump
`APP_CACHE` whenever `index.html` gains an element `app.js` reaches for.
Guarded by
`tests/frontend/test_artist_panel.py::test_a_missing_control_cannot_kill_app_js`,
which asserts both halves: an absent control returns false rather than
throwing, and the app is still alive afterwards.

**Worth remembering.** This failure mode is invisible from the server. No
log line, no failed request, no 5xx — the gateway answered every call
correctly throughout. When the UI is blank and the API is healthy, the
next question is which *files* the client is holding, not what the server
is doing.

## 80fe588 — 20260912 — the same drive, seventeen hours, and nobody was told

The fix below made the three LocalFs roots independent, so an unmounted
music volume stops taking the audiobooks down with it. It resolves each
root **at boot**. That turns out to be the wrong *number of times* to
ask, and the very next reboot proved it.

### What happened

```
ps:  gateway started            11 Sep 20:06:36
log: 20:06:50 WARNING  Music root not found: /Volumes/SAMDATA/Music — disabled
     20:06:50 WARNING  Video root not found: /Volumes/SAMDATA/GWMovies — disabled
     20:06:50 INFO     Audiobooks enabled: root=/Volumes/SAMDATA-1TB/Audio_Books
```

launchd beat macOS to the punch: fourteen seconds after the machine came
up, the external drive was not mounted yet. The new wiring did exactly
what it promises — two libraries disabled themselves, the third came up
— and then the drive mounted a few seconds later and **nothing
re-asked**.

The gateway then ran for **seventeen hours** with a file server whose
`allowed_roots` held only the books. `tracks` outlives its files, so the
music rows were all still there: every album browsed perfectly, and
every play 403'd.

```
WARNING dlna.localfs.server: path-traversal blocked:
        /Volumes/SAMDATA/Music/…/Burning Down The House (Live).flac
        not under ('/Volumes/SAMDATA-1TB/Audio_Books',)
WARNING dlna.asgi: stream ✗ upstream 403 … — refusing to relay a non-media body
INFO    dlna.client: client_log[audio_error] code=4 codeName=unsupported
```

**The tell is different from the outage below, which is worth knowing
before diagnosing the next one.** There the file server never started,
so `dlna_ssrf.guard` refused `:8200` as an unknown device and the log
read `SSRF guard: refused stream of …`. Here the server was up and
registered, so the request got all the way in and the **containment
check** (security posture §8) turned it away instead — a
`path-traversal blocked` line naming a music path and the audiobooks
root. Same sofa symptom, two different log lines, and the second one
looks alarming in a way it does not deserve: nothing was attacking
anything, a root was simply missing from the list.

### The fix

A missing root is now a **waiting** root. `dlna_localfs_watch.py` holds
each configured root's state, and `maybe_start_localfs` REGISTERS all
three with it rather than resolving them itself. Present ones activate
at once; the rest are re-checked every 30 s
(`$LOCALFS_ROOT_RECHECK_SEC`) by one daemon thread that **retires when
nothing is waiting**, so a fully-mounted machine carries no idle thread.

Four things there are load-bearing:

* **The file server starts on the FIRST root to arrive**, whenever that
  is, and every later one widens it via
  `dlna_localfs_server.add_allowed_root`. That **rebinds** the tuple
  rather than mutating it: the serving threads read `allowed_roots` off
  the handler class, and a single attribute store is atomic under the
  GIL, so a request sees the old tuple or the new one and never a
  half-built one. Canonicalisation lives in that function beside
  `make_handler_class`, because `resolve_within` compares RESOLVED
  paths — a root added raw would silently never match, which is the
  same bug wearing a different hat.
* **Activation happens exactly once.** The state flips to ACTIVE
  *before* the callback runs, since that callback starts a server, binds
  a provider and registers a device.
* **A failed activation is NOT retried.** The root is there; what failed
  is our handling of it. Retrying every 30 s would repeat the failure
  forever and re-run whichever half had succeeded.
* **An unconfigured root is not tracked at all.** Off is not waiting —
  warning someone about a Video library they never configured is exactly
  the noise that buried this.

### The half that is not code

Seventeen hours is not a re-check problem, it is a *telling* problem.
The only evidence was one WARNING at boot, in a file nobody reads from
the sofa. So `WATCH.snapshot()` backs **`GET /api/libraries`** (a
waiting row carries a message naming the volume to mount), every state
change publishes a `libraries` SSE event, and the PWA renders
`#library-bar` from it — built to the same shape as the index bar above
it, so it reads as part of the app rather than an error page.

It refreshes at **boot** (before the first poll tick — a missing drive
is the first thing to say, not the last), on the event, and on the
`servers` poll as the dropped-event fallback. A **failed fetch leaves
the bar exactly as it was**: a dropped tailnet must never read as "the
drive came back".

Activation also publishes `devices`, because the source picker has just
gained an entry — `SERVERS.add` does not fire `_on_server_found`, that
being the discovery hook, so nothing else would say so.

### Proof

`tests/test_localfs_watch.py` (25), `tests/test_localfs_wiring.py` (18)
and `tests/frontend/test_library_bar.py` (8). Both halves were verified
by breaking them: neutering the re-check reddens 7 of the 8 late-mount
tests, and neutering `add_allowed_root` reddens the three that assert
what the one file server ends up allowed to serve. The widening path is
additionally exercised on every real boot — music starts the server,
books and video widen it — and the wait-then-activate path was run with
real threads against a directory created mid-flight: registered,
waited, picked up 3 s after it appeared, thread retired.

### What would re-introduce it

Resolving a root anywhere other than through `WATCH` — a `Path.exists()`
at boot whose False means "off". And "simplifying" `add_allowed_root`
into an in-place append, or dropping its `Path.resolve()`, both of which
leave the code looking right and the volume unserved.
