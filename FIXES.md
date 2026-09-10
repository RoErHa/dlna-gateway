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

## 97627ab — 20260825 — the skips, the mid-playlist stop, and the log that hid both

**Reported as:** "the app is skipping songs, and inexplicably stopping while
in a playlist — and why is the video being indexed over and over?"

Three independent bugs, plus a fourth that made the first three hard to see.
All four were diagnosed from `gateway.log` and then measured before being
touched.

### 1. The skipping — a dead playlist row served to `<audio>` as audio

```
10:17:46  stream ▶ START .../localfs/stream/a91fe25b78b8fd32 (404)
10:17:46  stream ■ END   ... sent=480 in 0.0s
10:17:46  client_log[audio_error] code=4 codeName=unsupported
                                  title=Harmonium - L'Heptade (disc 2)
```

That track id was not in `tracks` at all. It was in `playlist_tracks`.

`playlist_tracks` is **deliberately** independent of `tracks` — that
independence is what lets a playlist survive `clear(udn)` and a rebuild. The
cost is that nothing notices when a row goes stale, and a LocalFs track id is
`sha1(rel_path)`, so **renaming a folder, splitting a whole-album file into
per-track files, or changing the file-server port silently orphans every
playlist row that referenced it.** The files are still on disk under new ids;
the playlist still holds the old ones.

The relay then made it invisible. `_audio_relay_response` did
`status = resp.status` and streamed the body whatever it was, with the
content-type normalised into an audio MIME — so `<audio>` was handed a
**404 page labelled `audio/flac`**, reported `MediaError.code 4`
"unsupported format", and the PWA's error handler skipped the track. Nothing
in the log said why, because from the relay's point of view nothing had gone
wrong.

**Fixed** in `dlna_asgi_media._audio_relay_response`: only **200/206**
stream; **416** passes through bodyless (it is a real, bodyless Range
answer); everything else logs `stream ✗ upstream <status> …` at WARNING and
returns the opaque `502 {"error":"stream unavailable"}`.

> ⚠️ **Do not "improve" that by forwarding the upstream status.** Same rule
> and same reason as `/art` (CLAUDE.md § Security posture 2): a forwarded
> status is a clean open/closed/filtered probe oracle on an unauthenticated
> endpoint. The detail belongs in the log, not in the response.

> ⚠️ **The refusal path must release its `AUDIO_RELAYS` slot.** Leaking one
> per dead row would wedge `/stream` entirely after 64 of them — a much worse
> bug than the one being fixed. Guarded by
> `TestStreamUpstreamErrors::test_relay_slot_released_on_refusal`.

**The data:** `tools/audit_playlist_orphans.py` (new) found **14 orphans of
1091 rows**. 12 were the whole "latin Jazz" playlist, still pointing at the
pre-cutover **`:8201`** port — the 2026-05-31 port change healed
`metadata_overrides` but nobody ever healed playlists. Those relinked
cleanly (identical track ids, different port) and all 12 verified streaming
206. The other 2 were the whole-album Harmonium *L'Heptade* disc 1 & 2
files, since split into per-track files, so no single successor row exists;
they were removed by hand with `--remove-unmatched`. Final state: **0
orphans, 1089 rows.**

### 2. The stop — an `<audio>` state nothing was watching

The browser queue only ever advanced on `ended` or `error`. An `<audio>`
element has a **third resting state that fires neither**: buffer starved, no
more bytes arriving, and — from the element's own point of view — no error.
`app.js` listened for `ended`, `error`, `play`, `pause`, `playing`,
`seeked`, `timeupdate` and `loadedmetadata`. It did **not** listen for
`stalled`, `waiting`, `suspend` or `abort`.

So when that state was reached, the queue simply sat there showing
"⏸ Pause" with silence. The log is unambiguous: last byte of the track
delivered at **10:22:32**, then no further request, no `client_log` entry,
and nothing at all until **10:28:38** — six minutes later — when the next
track was started by hand.

**Fixed** with a watchdog in `static/app.js`. `_stallCheck` polls
`currentTime` every `_STALL_POLL_MS` (2 s) while the element believes it is
playing. No progress for `_STALL_GRACE_MS` (12 s — comfortably longer than a
tailnet rebuffer) → **one recovery nudge** (re-seek to where it died, which
re-issues the Range request; a dropped fetch usually resumes and the listener
never notices) → still nothing → report `kind:"audio_stall"` to
`/api/client_log` and **advance the queue**.

Cause (3) below is what starved the buffer here, and it is fixed. The
watchdog exists anyway, because **the player must not depend on the network
being well-behaved** — any stall that nothing reports still has to un-wedge
the queue.

Four things it must keep getting right, each one a test in
`tests/frontend/test_stall_watchdog.py`:

- **Progress resets everything.** A watchdog that fires on a slow-but-healthy
  stream is worse than no watchdog at all.
- **A user pause is not a stall** — `paused` stops `currentTime` too.
- **It never runs for UPnP output.** With a renderer selected, the gateway
  owns advancing the queue and the browser element is idle.
- **The `error` path and `_browserPlayIdx` both stop it**, so an error-driven
  skip and a watchdog-driven skip cannot both fire and jump two tracks.

### 3. Why the buffer starved — HTTP/2 has no backpressure here

This is the root cause of (2) and the reason the `stream ■ END` log line had
been lying for months.

**Hypercorn 0.18's `StreamBuffer.pop()` unpauses the producer whenever the
chunk it popped is under the low-water mark — including the empty chunk it
pops when the peer's flow-control window is shut.** A stalled reader
therefore does not stop us: the relay generator runs to EOF and the entire
remainder of the file lands in the worker's memory.

Measured on the live gateway, three 50 KB/s clients pulling one 70 MB FLAC:

| | RSS before | RSS during | `stream ■ END` logged |
|---|---|---|---|
| before | 195 MB | **383 MB** | within **0.1 s**, clients had minutes left |
| after  | 114 MB | **133 MB** | when the slice is actually done |

So `sent=` and `reason=` on the END line were describing a **buffer fill, not
a delivery**. HTTP/1.1 backpressures properly (the same test took 5.1 s);
**the PWA is on h2, so the PWA is exactly the case that breaks.** In the wild
Safari was seen opening **eleven concurrent Range requests for one track**
(10:38:24) — ~770 MB of buffer for one 70 MB file, with relays lingering
160–208 s after their track was over.

We cannot see how much the client consumed, so we bound what we hand it: a
request **that sent a Range** gets at most `_MAX_SLICE` (8 MB ≈ 70 s of 16/44
FLAC; env `STREAM_SLICE_BYTES`, `0` disables) with a **truthful
`Content-Range`/`Content-Length`**, and asks for the next slice when it wants
more. Serving less of a range than was asked for is ordinary HTTP, and it is
exactly how `<audio>` already drives this endpoint.

> ⚠️ **A request with no `Range` header is left alone, deliberately.** It has
> not shown itself Range-aware, and truncating it would corrupt a plain file
> download (curl, an Amperfy full-file sync).

> ⚠️ **Bit-perfect is unaffected — and this was verified, not assumed.**
> After the change, a no-Range pull and a 9-request walk of the whole file
> both `sha256`-match the source file on disk. `_clamp_content_range` is a
> pure function precisely because an off-by-one there is a corrupt audio
> stream, which the browser reports as an *unplayable file* rather than as a
> bad byte count — i.e. it would look exactly like bug (1).

### 4. The video "re-indexing" — it isn't, but it hid all of the above

`video scan …: 3977 files, +0, skip 3977, prune 0, overrides 0` every five
minutes is a **no-op incremental scan**, not a re-index. The walk was timed
at **0.05 s** for all 3977 files; nothing is being read or rewritten.

The real harm was the logging. That line plus a flat `FD usage 30/8192 (0%)`
heartbeat were **295 of `gateway.log`'s 400 lines — 74%**, which is precisely
why three playback bugs sat in the log undiagnosed. A heartbeat that never
changes is not information.

**Fixed:** `scan_videos` logs at INFO when the library actually moved
(`added`/`pruned`/`overrides` non-zero) and at DEBUG when it did not; the FD
heartbeat logs at INFO only when the count has moved by a meaningful amount.
The ALERT / rising / high-water branches that actually catch an FD leak are
untouched, and `GATEWAY_DEBUG=1` restores both in full. Since the restart:
**0 heartbeat lines**.

### Verification

218 backend tests, 238 Playwright tests, `chaos.py` 400 actions, ruff clean.
The new watchdog test genuinely fails with the give-up branch disabled
(checked, not assumed). One of the new tool tests caught a real bug in the
first draft of `apply_plan` — its `COALESCE(NULLIF(?,''), art)` overwrote
playlist art the owner may have chosen, instead of only filling a blank one.
