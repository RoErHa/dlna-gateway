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

## 15f0922 — 20261023 — a TLS failure that wasn't renewal, and two renewal bugs that were

**Symptom.** epub-reader failed to start once, on 2026-08-15 13:37: hypercorn's
`load_cert_chain` raised `FileNotFoundError` on the Tailscale certificate it
shares with this gateway. The natural suspect was a renewal racing the start.

**What it actually was.** Not renewal. `cert-renewal.log` shows the last
renewal on 07-27 and the next on 09-21, and the certificate on disk on 08-15
was valid until October. epub-reader's own log shows 12:44 and 12:49 starting
on **http**, 13:37 its first-ever **https** start (failed), and 13:45 https
working ever since, with no commit in between. TLS was being switched on in
the untracked `.env`, and the first attempt named a file that was not there.
OpenSSL does not say which file, so the exact typo is not recoverable.

**What the log showed instead.** Two real defects:

- **2026-07-20: a "renewal" that renewed nothing.** `tailscale cert` printed
  *Public cert unchanged* with 26 days left, and the script logged
  `OK renewal complete; new expiry Aug 16` (the same expiry) and restarted
  the gateway for nothing. It renewed a week later. Harmless that time; the
  script would have said the same with 2 days left.
- **The second service was never restarted.** epub-reader loads these same
  `.crt`/`.key` files, but the script only kickstarted the gateway. After the
  09-21 renewal epub-reader kept serving the OLD certificate (expiring 10-18)
  until a restart happened along. Its last restart before expiry was
  10-17 18:04, about **7 hours** before iPhones and iPads would have refused
  it. Saved by development churn, which is not a mechanism.

**Fix.** Compare the expiry before and after `tailscale cert`: unchanged →
WARN, restart nothing. After a real renewal, restart every label in
`CERT_RELOAD_LABELS` (`.env`); one failure does not stop the rest and sets
the exit code.

**The trap the test found.** `renew-cert.sh` loads `.env` by *sourcing* it,
so `CERT_RELOAD_LABELS=a, b` is not an assignment of `a, b`: bash assigns
`a,` and runs `b` as a command, and the variable is silently unset. That key
is read from the file directly.

**Guard.** `tests/test_renew_cert.py` runs the real script in a sandbox: real
`openssl` on throwaway certs, fake `tailscale`/`launchctl` recording calls.
Three of its five tests fail on the old script.

**Don't re-introduce.** Any new service that serves this certificate goes
into `CERT_RELOAD_LABELS`. "The command exited 0" is not evidence of a
renewal; the expiry date is.

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
