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

## 254a54b — 20260910 — "nothing plays, every song skips" was an unmounted drive

**Reported as:** "check the log — nothing is playing, every song is skipping."

The log said it in one line, at boot, 61 minutes before the first skip:

```
15:22:43 [WARNING] dlna.localfs.wiring: LocalFs root not found:
         /Volumes/SAMDATA/Music — skipping (is the volume mounted / unlocked?)
```

and then said something else about four thousand times:

```
16:23:52 [WARNING] dlna.ssrf: SSRF guard: refused stream of
         'http://192.168.1.125:8200/localfs/stream/a67a204f6bb383ad'
         — private destination ... is not a known device
16:23:52 [INFO ] dlna.client: client_log[audio_error] code=4
         codeName=unsupported title=Feel Like Makin' Love network_state=3
```

`/Volumes/SAMDATA` was not mounted. The disk was attached and healthy
(`disk10s1`, APFS, 3.5 TB) but **encrypted and locked**, which macOS does
not mount at all — so the path simply did not exist.

### Why one missing volume broke three libraries

`maybe_start_localfs` returned when the MUSIC root was absent, and it did so
**before starting the file server**. Everything downstream hangs off that
server, so:

* **the audiobooks died too** — on `SAMDATA-1TB`, mounted and fine. The video
  and audiobook roots each degraded gracefully with a warning; the music root
  was a hard gate on the whole function.
* **`/api/servers` returned `[]`**, because the synthetic `MediaServer` entries
  are registered by that same function.
* **so every fetch was refused.** `dlna_ssrf.guard` lets a private destination
  through only when its host is a device in `SERVERS`/`RENDERERS` — that is
  precisely how `:8200` is normally allowed. With nothing registered, the file
  server was a stranger on the LAN.

The relay then did its job correctly (§*Two things the relay must keep doing*):
a non-200 is not media, so it refused rather than handing `<audio>` a 403 body
labelled `audio/flac`. The PWA saw `MediaError.code 4` and skipped. At one
track per second, that reads as "every song skips" — not as "a disk is
missing", which is what it was.

**The fix is not the mount.** Unlocking the volume restored playback in one
restart (26,477 files rescanned, 0 new, 0 changed, 0 removed — nothing had
drifted). The fix is that a missing root must disable only itself:
`_resolve_root` resolves the three independently and the server starts on
whatever is present.

### The same bug from the other side, on the Naim

The PWA and Subsonic both degraded cleanly — they read the server registry, so
music stopped being offered. The UPnP tree did not, and would have shown a
library that could not play a byte:

```python
"SELECT udn FROM tracks GROUP BY udn ORDER BY COUNT(*) DESC LIMIT 1"
```

**`tracks` outlives its files.** With the volume gone, the 26,362 music rows
were still the majority of the index, so `DB.primary_udn()` handed the Naim a
full Artists/Albums/Genres tree in which every play 404s. Browsing works,
which is what makes it convincing. `api_upnp_ids.music_udn()` now asks which
music library is *serving* — a registered `MediaServer` being the evidence —
and the root offers those three containers only while one is.

### What would re-introduce it

* **Gating the whole wiring on any single root again.** Each root is optional
  and independent; the server needs only one of them to be worth starting.
* **Starting the file server with an empty `allowed_roots`.** That is a server
  that refuses every path while looking alive — worse than no server, because
  the SSRF guard would then allow URLs that can never resolve.
* **Reading an EMPTY server registry as "nothing is live."** The SSDP announcer
  starts *before* `maybe_start_localfs`, so a control point browsing in that
  window would be told the library is empty, and a client that caches an empty
  tree is a worse failure than the stale one this fixed. Only
  registered-but-no-music returns `''`.
* **Answering "which library?" from row counts alone**, anywhere. The index
  knows what was once there, never what is reachable now.

Guarded by `tests/test_localfs_wiring.py` (19) and
`tests/test_upnp_music_liveness.py` (16). Seven of those fail against the old
code — verified by reverting each half and re-running, because a regression
test that passes either way is worse than none.
