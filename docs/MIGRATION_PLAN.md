# Migration Plan — Replace AssetUPnP with an in-process library backend

> **Status (2026-05-29):** the operational roadmap for this work now
> lives in the top-level `CLAUDE.md` under
> [**Library backend migration (in flight)**](../CLAUDE.md#library-backend-migration-in-flight),
> which **supersedes this document** and adds the multi-backend
> `LibraryProvider` seam expansion (UpnpProvider, PlexProvider,
> JellyfinProvider, LocalFsProvider).
>
> This file is preserved as the historical artifact — the original
> 6-phase plan as captured before the modular-provider design was
> added.

**Purpose:** Remove the dependency on AssetUPnP by folding its two remaining
responsibilities — *indexing the music library* and *serving audio bytes* — into
`dlna-gateway`, which already owns presentation, playback control, the iOS UI and
media-session wiring.

This document is the working roadmap. It is written to be read by Claude Code as
project context; keep it in the repo (and reference it from `CLAUDE.md`).

---

## Why we're doing this

The current chain is:

```
dlna-gateway  --SOAP Browse-->  AssetUPnP  -->  files  -->  Naim (renderer)
```

There are **two sources of truth** — Asset's internal index and the gateway's view
of it — coupled over UPnP, which is a coarse, lossy channel. On rescan, Asset can
renumber object IDs, mishandle `UpdateID`/`SystemUpdateID`, and serve a half-built
tree. The gateway then has to defensively re-walk and retry SOAP. This is the source
of the recurring "hours fixing the gateway" pain.

Target chain:

```
dlna-gateway (owns index + serves files)  --AVTransport-->  Naim (renderer)
```

One index, owned by us, scanned on our terms. "Synchronising" becomes a local
database operation, not a protocol negotiation.

---

## Non-negotiable rules

1. **Bit-perfect.** Serve the original file bytes, unmodified. **Never transcode.**
   A checksum of served bytes must equal the source file.
2. **Additive & parallel.** The new backend runs *alongside* Asset against the same
   (read-only) music folder, on its own HTTP port. Asset is untouched until we
   choose to stop it.
3. **Reversible.** Backend selection is a config flag. We can flip back to Asset at
   any point until decommission.
4. **No big-bang cutover.** Real listening is not affected until Phase 4, and even
   then Asset remains as a one-flag fallback.

---

## Target architecture

### The seam

A thin provider interface decouples the gateway from the backend choice. Today one
implementation wraps SOAP-to-Asset; we add a second backed by our SQLite index.

```python
class LibraryProvider(Protocol):
    def list_artists(self) -> list[Artist]: ...
    def list_albums(self, artist_id: str) -> list[Album]: ...
    def list_tracks(self, album_id: str) -> list[Track]: ...
    def get_track(self, track_id: str) -> Track: ...     # metadata + embedded art
    def stream_url(self, track_id: str) -> str: ...        # URL the Naim fetches
```

### Key facts that shape the design

- The renderer fetches bytes **directly** from `stream_url` — the gateway does **not**
  proxy audio. So the serving endpoint must be reachable by the Naim on the LAN.
- The Naim issues HTTP **Range** requests. Correct `206 Partial Content` handling is
  mandatory (`Accept-Ranges`, `Content-Range`), or seeking and sometimes playback
  start will break.
- DLNA response headers (`contentFeatures.dlna.org`, `transferMode.dlna.org`) are
  required and will need iteration against the real Naim. Handle serving manually
  rather than via a framework static-file helper, so these can be set.
- SSDP is **not required**: the gateway pushes URIs to the Naim via `AVTransport`.
  The new server is just an HTTP file server on its own port; no UPnP device
  advertisement needed. (Running both servers is therefore safe — separate index
  DBs, separate HTTP ports, SSDP multicast coexists by design.)

### Suggested stack

- Scanner: `watchdog` (FSEvents on macOS) for incremental change detection.
- Tags / embedded art: `mutagen` (FLAC, MP3, DSF, etc.).
- Index: SQLite, with **stable internal IDs that never renumber across a rescan**.
- Serving: FastAPI/Starlette streaming endpoint with manual Range handling.
- Store `path + mtime + size` per file; rescan = diff against DB, touch only changed.
- Commit scans transactionally so the served view is never half-updated.

---

## Phases

### Phase 0 — Prep for Claude Code
- [ ] Write/refresh `CLAUDE.md`: architecture, the `LibraryProvider` seam, conventions,
      and the non-negotiable rules above (bit-perfect / never transcode at the top).
- [ ] Add this plan + a short schema doc to the repo (`docs/`).
- [ ] Ensure a test scaffold exists.
- **Done when:** Claude Code can read the repo and understand the seam without
  narration.

### Phase 1 — Index (no serving)
- [ ] Scanner: `watchdog` + `mutagen` + SQLite, pointed at the existing music folder.
- [ ] Stable IDs; per-file `mtime`/`size`; incremental diff; transactional commit.
- [ ] Extract and cache embedded art; flag (don't silently drop) malformed files.
- **Done when:** the new index matches Asset's view — same album/track counts, art
  present, oddities logged. Pure read, zero risk, runs alongside Asset.

### Phase 2 — Serve with Range
- [ ] `GET /stream/{id}` returns the **original** bytes (no transcode).
- [ ] Correct `206`/`Content-Range`/`Accept-Ranges`; DLNA headers.
- [ ] Own HTTP port (e.g. 8200), reachable by the Naim on the LAN.
- **Done when:** `curl -r 0-1023` returns a proper 206 with `Content-Range`; VLC
  plays the stream; **checksum of served bytes == source file** (bit-perfect proof).

### Phase 3 — The seam
- [ ] Implement `LibraryProvider` against the SQLite index.
- [ ] Keep the Asset implementation; select via config flag.
- **Done when:** flipping the flag makes the gateway browse from the new index.

### Phase 4 — Renderer + gapless
- [ ] Route playback through the new server's `stream_url`.
- [ ] Implement next-track queueing via `SetNextAVTransportURI`.
- **Done when:** a **segued album plays with no gap or click** (test with continuous
  prog suites — Ayreon, Focus, etc.) and seeking works on the Naim.

### Phase 5 — Parallel run
- [ ] Live on the new backend; Asset installed but idle as fallback.
- **Done when:** ~2 weeks of daily listening shows no regressions.

### Phase 6 — Decommission
- [ ] Stop Asset; remove references.
- **Done when:** nothing depends on AssetUPnP.

---

## Audiophile notes — what changes, what doesn't

**Sound quality: no change.** The Naim fetches the file over TCP, buffers it, and
clocks it to its own DAC with its own clock. TCP is error-corrected, so identical
bytes arrive regardless of which server sent them; the buffer absorbs network timing.
A server delivering unmodified files has no path to the analog output other than
"which bytes." Same bytes in → same sound out. The Phase 2 checksum makes this
certain. ("Server X sounds warmer than server Y" does not apply to bit-identical
local serving.)

**The only two ways to make it *worse* than Asset — both completeness, not capability:**

1. **Gapless.** Asset does it well; ours will only be as good as the
   `SetNextAVTransportURI` queueing. This is the one audible regression risk, and it
   bites hardest on segued/continuous material. Test ruthlessly in Phase 4.
2. **Format coverage.** Asset transparently handles DSD, high-res PCM, ReplayGain
   tags, embedded art. The scanner must read those tags and the server must serve
   DSD/high-res with correct MIME so the Naim accepts them. Verify the exact Uniti
   model's PCM/DSD ceiling so nothing is silently rejected.

**Open decision — ReplayGain.** Default: pass tags through, do **not** act on them
(bit-perfect, simplest, the choice most critical listeners make). Revisit only if
loudness normalization is actually wanted.

---

## Testing quick-reference

- Range / 206:  `curl -r 0-1023 -D - http://<host>:8200/stream/<id> -o /dev/null`
- Bit-perfect:  compare `sha256` of served bytes vs source file.
- Gapless:      a known segued album, listen for gaps/clicks at track boundaries.
- Format:       at least one each of FLAC 16/44, hi-res PCM, and DSD (if in library).
