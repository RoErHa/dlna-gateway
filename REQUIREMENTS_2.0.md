# DLNA Gateway 2.0 — Requirements & Roadmap

> **✅ 2.0 SHIPPED — this is the historical pre-build PROPOSAL (kept for
> rationale), not the as-built doc.** The 2.0 transport refresh is done and live
> (cutover 2026-06-08/09; tag `v2.0.0`). **What shipped differs from the headline
> below:** TLS + HTTP/2 are **app-owned via Hypercorn + FastAPI
> (`dlna_asgi.py`)**, NOT `tailscale serve` (tried and dropped — broken on this
> mini's Tailscale `:443`). For the as-built picture see **`CLAUDE.md`** (living
> reference), **`docs/BUILDING_2.0.md`** (build log + checklist), and
> **`docs/CUTOVER_RUNBOOK.md`**. Everything below is the original proposal.

Status: **proposal / backlog (HISTORICAL).** Nothing here was committed to a
release date. This was the running list of "big improvements to come" for a 2.0
transport + architecture refresh, gathered while shipping the 1.x responsiveness
work (SW caching, HTTP/1.1 keep-alive, the album-key browse index). The
**headline item was fronting the gateway with `tailscale serve`** to gain
HTTP/2 + free TLS — superseded by app-owned Hypercorn TLS; everything else was
bundled so a 2.0 was worth the churn.

### Status ledger — what became of each item (checked 2026-08-20)

The proposal below is preserved verbatim for its rationale. This table is
the only part kept current; when an item ships, annotate it here rather
than editing the argument that produced it.

| Item | Status | Where it landed |
|---|---|---|
| §1 headline — h2 + TLS | ✅ **shipped, differently** | App-owned Hypercorn TLS/ALPN, not `tailscale serve` (tried, dropped). The cert machinery B5/B6 proposed deleting was therefore **kept**. |
| R1 — ASGI rewrite | ✅ shipped | `dlna_asgi.py` + `dlna_asgi_bridge.py`; "only if pushed" turned out to be the anchor change. |
| R2 — SSE push | ✅ shipped | `dlna_events.py`, `GET /api/events`; the PWA's polls became a two-tier fallback rather than the update path. |
| R3 — outbound SOAP pool | ❌ open | Still one TCP connection per SOAP call (`dlna_avtransport`). ~1 ms on LAN, so it has never hurt. |
| R4 — library completeness | ✅ shipped | 2026-07-12 audit + the `album_key` UNIQUE widening; 26,051 rows. 78 same-folder same-tag collisions remain (genuine tag ambiguities). |
| R5 — album grouping | ✅ shipped | Folder/`album_key` identity is consistent across browse, UPnP and Subsonic. |
| R6 — Plex / Jellyfin | ❌ open | Seam is proven; neither provider written. |
| R7 — merged library view | ❌ open | Still separate switchable trees via the source picker. |
| R8 — file-tag write-back | ✅ superseded | beets writes canonical tags **into the files** in place; the gateway never write-backs itself. |
| R9 — LUFS normalization | ⛔ closed | Deliberately not tracked; would be a fresh decision. |
| R10 — sweeps as managed jobs | ❌ open | Still CLI tools. |
| R11 — maintenance panel | ❌ open | — |
| R12 — metrics endpoint | ❌ open | No `/api/metrics`. |
| R13 — config consolidation | ✅ shipped | `.env` is the single source (2026-07-13). |
| R14 — native iOS app | ❌ open (low priority) | Amperfy still covers CarPlay. |

**Not in the proposal but shipped in the 2.0/2.1 line:** audiobooks as a
second library with cross-device resume, home-video indexing + DLNA browse,
the Subsonic surface's growth (bookmarks, radio, size-bucketed art), the PWA
navy/responsive redesign, the code-quality gates (ruff, the module-size
ratchet, lock-sync, no-silent-swallows), and — in **2.1** — the security
hardening from the 2026-08-20 audit: the `dlna_ssrf` outbound-fetch guard on
the three `?url=` endpoints, uniform error responses (the old ones were a
port oracle), TLS certificate verification on outbound fetches, and escaping
of untrusted device text. See CLAUDE.md → "Security posture".

**A note on §2.4/R12 in light of that audit:** the proposal framed
observability as a performance concern. The audit added a second reason —
the SSRF guard's refusals are logged precisely so a probe is visible to
whoever runs the gateway, which is the only signal there is on an
unauthenticated LAN surface.

See `CLAUDE.md → "HTTP/2 · HTTP/3 · TLS — DONE in 2.0 (roadmap retained
for history)"` for the protocol-level detail this document summarises, and
`docs/ARCHITECTURE.PDF` for the current end-to-end picture this proposes to
evolve.

---

## 1. Headline — HTTP/2 + free TLS via `tailscale serve`

### Where we are in 1.x

The gateway serves **HTTP/1.1 over TLS with keep-alive** from Python's
stdlib `http.server` (`BaseHTTPRequestHandler` + `ThreadingMixIn` +
`ssl`-wrapped socket). It **owns its own TLS**: it auto-detects the
Tailscale-issued Let's Encrypt cert (`*.crt`/`*.key`), warns at startup if
the cert has < 14 days left, and renews it weekly via `renew-cert.sh` + the
`com.roha.dlna-cert-renew` LaunchAgent. Stdlib `http.server` is
**HTTP/1.1-only** — it cannot speak HTTP/2 (needs ALPN negotiation + the h2
binary framing/HPACK layer) or HTTP/3 (needs QUIC over UDP). There is no
`protocol_version = "HTTP/2"`.

### The move

Bind the gateway to **localhost HTTP/1.1** and put **`tailscale serve`** in
front of it on 443. Tailscale terminates TLS with the tailnet cert and
reverse-proxies to `http://127.0.0.1:<port>`. Go's `net/http` does **HTTP/2
over TLS by default**, so tailnet clients get h2 for free.

### Benefits (the full list)

| # | Benefit | Why it matters here |
|---|---|---|
| B1 | **HTTP/2 multiplexing** | Many concurrent requests over **one** connection instead of ~6 parallel HTTP/1.1 sockets. The one place it shows is the **cold, thumbnail-heavy browse page** (~20 `/art` images at once) — they stream concurrently without head-of-line blocking across separate sockets. |
| B2 | **HPACK header compression** | Repeated headers (cookies, UA, accept) compressed per-connection. Marginal at our request volume, but free. |
| B3 | **Single connection, server-managed** | One long-lived h2 connection replaces keep-alive's per-connection daemon-thread bookkeeping; the proxy owns connection lifecycle, not our `ThreadingMixIn`. Fewer threads held on the gateway. |
| B4 | **Free, auto-renewed TLS** | `tailscale serve` provisions and **auto-renews** the tailnet cert. This is the big simplification — see B5. |
| B5 | **Deletes the entire cert-renewal machinery** | `renew-cert.sh`, the `com.roha.dlna-cert-renew` LaunchAgent, `cert-renewal.log`, the `*.crt`/`*.key` auto-detection, and `_warn_if_cert_expiring_soon` in `dlna_gateway.py` all become **dead code we can remove**. One fewer scheduled job, one fewer failure mode (a silently-dead renew LaunchAgent), one fewer secret on disk. |
| B6 | **Gateway drops its TLS code path** | No more `TLSThreadedHTTPServer`, no HTTP→HTTPS redirect server, no dual HTTP/HTTPS bind. The gateway becomes a plain localhost HTTP server; the proxy is the only TLS owner. Smaller, simpler `dlna_gateway.main()`. |
| B7 | **Modern TLS for free** | Tailscale keeps TLS 1.3 / cipher suites current without us tracking OpenSSL. |
| B8 | **No new infrastructure** | The deployment is *already* on Tailscale. `tailscale serve` is one command, no extra process to supervise (unlike Caddy/nginx). Lowest-effort path to h2. |
| B9 | **Path to HTTP/3** | If h3 ever matters, swap `tailscale serve` for Caddy/nginx (both do h2 **and** h3/QUIC) with the same "front a localhost HTTP gateway" shape — no gateway rewrite. |

### Non-negotiable caveats (carry forward from 1.x)

- **Device endpoints stay HTTP-only and un-proxied.** The Naim (and any
  UPnP renderer) fetches audio bytes and browses gateway playlists over
  **plain HTTP on the LAN** — UPnP renderers can't do HTTPS. The
  `/stream`, `/radio_stream`, the LocalFs file server (`:8200`), and the
  `/gw/`-style UPnP device endpoints must **not** be routed through the
  proxy. The renderer talks to the gateway **directly on the LAN**, never
  through Tailscale. Keep that path byte-for-byte untouched.
- **`tailscale serve` is tailnet-only.** Fine — the gateway is LAN/tailnet
  only by design and never publicly exposed. If non-tailnet access is ever
  wanted, that's the Caddy/nginx branch (B9).
- **Two cert owners is a bug.** When TLS moves to the proxy, the gateway's
  own TLS + renewal machinery must be **removed**, not left running
  alongside.

### What HTTP/2 will *not* fix

Honest scoping: keep-alive already removed the per-request handshake for
sequential traffic (the polling loop, drilling through browse), and the
Service-Worker `ART_CACHE` + `API_CACHE` already make repeat loads instant.
So h2's marginal benefit for this **single-user** workload is **real but
secondary**. The dominant remaining remote-latency cost is raw Tailscale
RTT, which no protocol upgrade removes. h2 is worth doing **as part of the
cert-machinery deletion (B5/B6)**, not for raw speed alone.

---

## 2. Other candidate 2.0 improvements

Bundled so a 2.0 cutover earns its keep. Roughly ordered by value.

### 2.1 Transport / serving

- **R1 — ASGI rewrite (only if pushed).** The app is stdlib
  `BaseHTTPRequestHandler`, not WSGI/ASGI. Moving to an ASGI server
  (Hypercorn/Uvicorn) would give native h2/h3, async I/O, and WebSockets,
  but it's a **big rewrite** of every handler and the routing maps. Only
  justified if WebSockets (R2) or async fan-out genuinely become needed —
  otherwise `tailscale serve` (§1) gets h2 with **zero** app changes.
- **R2 — WebSocket / SSE push for now-playing + index status.** Today the
  PWA polls `/api/renderer_state` (~1 s), `/api/index/status`,
  `/api/servers`, `/api/renderers` on timers. A single server-push channel
  (SSE is enough; WebSocket if bidirectional) would **eliminate the poll
  storm** — fewer requests, instant now-playing updates, and it kills the
  exact 4-worker snapshot-contention pattern chaos surfaced against the
  Naim's SOAP endpoint. High value for responsiveness; needs R1 or a
  dedicated thread + a streaming response path.
- **R3 — Connection pool / keep-alive for outbound SOAP to the Naim.**
  Renderer control (`AVTransport`/`RenderingControl`) opens a fresh TCP
  connection per SOAP call. Over the LAN it's ~1 ms so it hasn't hurt; a
  small pool would smooth bursts (and is the *real* fix for the synthetic
  snapshot contention, distinct from inbound keep-alive).

### 2.2 Library backend (finish the LocalFs story)

- **R4 — Library completeness.** RoHaLocalFS currently indexes a **subset**
  of what AssetUPnP served (the playlist relink lost ~38%). 2.0 should make
  the LocalFs library the **complete** source of truth: a verified one-pass
  import of the full music root, a coverage report (files on disk vs rows
  indexed), and an idempotent re-run of `tools/relink_playlists_to_localfs.py`
  once the gap is closed.
- **R5 — LocalFs album grouping.** RoHaLocalFS splits multi-part
  collections / multi-disc sets into many browse "albums" (count inflated
  ~2k → 8k). 2.0 should group by folder/`album_key` consistently so a
  boxed set or "Top 100 Hits of the 70s" is **one** album everywhere
  (browse, UPnP, Subsonic). (`album_key` plumbing already landed in 1.x —
  this is the grouping policy on top of it.)
- **R6 — Plex / Jellyfin providers.** The `LibraryProvider` seam exists and
  is proven by LocalFs + the kept `UpnpProvider`. Plex (`plex.py`) and
  Jellyfin (`jellyfin.py`) become weekend projects: richer native metadata
  (ratings, play counts, smart playlists), token auth, config-driven (no
  SSDP). Good 2.0 "now the seam pays off" story.
- **R7 — Mixed-provider merge view.** Today multiple sources show as
  **separate** switchable browse trees (the source picker). 2.0 could offer
  an optional **merged** library view across providers.

### 2.3 Metadata / quality

- **R8 — File-tag write-back (mutagen).** AcoustID enrichment currently
  writes only to the gateway's `metadata_overrides` cache, never the
  on-disk tags (deliberate in 1.x). A 2.0 opt-in could write corrected
  tags back to files (with backups), so the corrections survive outside the
  gateway.
- **R9 — Perceptual / LUFS loudness normalization (opt-in).** Peak-mode
  loudness was built then removed in 1.x (negligible benefit, broke
  browser bit-perfect). If ever genuinely wanted, a 2.0 LUFS-based,
  renderer-side-only (never browser-PCM) implementation that **preserves
  bit-perfect on the UPnP path** would be the way — strictly opt-in,
  default off.
- **R10 — Background year/metadata sweeps as managed jobs.** The
  `tools/improve_song_years.py` (~10 h full sweep), `correct_year_drift.py`,
  and the AcoustID passes are manual/cron today. 2.0 could expose them as
  first-class, observable background jobs in the PWA (progress, pause,
  cancel) rather than CLI tools + LaunchAgents.

### 2.4 Operability / observability

- **R11 — Unified job/observability surface.** One PWA "Maintenance" panel
  showing index status, AcoustID enrichment, art backfill, and the
  housekeeping tools — with run/last-run/progress — instead of scattered
  CLI tools, LaunchAgents, and log greps.
- **R12 — Structured metrics endpoint.** A `/api/metrics` (or Prometheus
  exposition) for request latency, SOAP latency to the renderer, cache hit
  rates, indexer throughput — so responsiveness work is measured, not
  guessed.
- **R13 — Config consolidation.** Backend selection, provider config,
  ports, and secrets are spread across `config.json`, `.env` (at the time
  of writing inert — dotenv wasn't installed, env came from
  `launchctl setenv`), and LaunchAgent plists. 2.0 should make `.env`
  actually load or pick one config source of truth.
  *(**Shipped 2026-07-13**: `.env` is now the single source; `dlna_config`
  loads it with a built-in fallback parser so it works even without
  python-dotenv, and the plist carries only `PATH` + the launch command.)*

### 2.5 Client reach

- **R14 — Native iOS companion (stretch).** The PWA can't do CarPlay or
  proper `AVAudioSession` interruption recovery; that's why Subsonic +
  Amperfy exist as the CarPlay path. A thin native shell is a large effort
  and probably **not worth it** while Amperfy covers CarPlay — listed for
  completeness, low priority.

---

## 3. Suggested 2.0 cut order

1. **`tailscale serve` front + delete cert machinery** (§1, B5/B6) — the
   anchor change; biggest simplification, lowest effort, enables h2.
2. **R4 + R5** — make LocalFs the complete, correctly-grouped source of
   truth. (Library correctness should land before client-facing polish.)
3. **R2 (SSE push)** — kills the poll storm; the single biggest *felt*
   responsiveness win after the network layer.
4. **R6 (Plex/Jellyfin)** — opportunistic, once the seam is exercised.
5. Everything else as capacity allows.

---

## 4. Explicitly out of scope (still, for 2.0)

- Commercial streaming (Spotify/Tidal/Apple Music/Qobuz) — DRM + closed
  protocols + licensing. The Naim already speaks these natively; they
  belong on the renderer, not the gateway.
- Public-internet exposure — the gateway stays LAN/tailnet-only.
- Transcoding / server-side resampling — **bit-perfect is non-negotiable**;
  always serve original bytes.
- Multi-user / roles / per-user libraries — single-user by design.
