# Building 2.0 alongside 1.0 — approach notes

Read the full `REQUIREMENTS_2.0.md` — that anchors this. The headline 2.0 move is *operational* (front the gateway with `tailscale serve` for HTTP/2 + free TLS, delete the cert machinery), plus library-completeness (R4/R5), SSE push (R2), and Plex/Jellyfin (R6). That shape matters because it answers your "can they coexist?" question well.

## Short answer to the feasibility question

**Yes — two instances on the same Mac mini, same music folders, is not only possible, it's the pattern this project already proved** (the AssetUPnP→LocalFs migration ran "additive & parallel: alongside, same read-only music folder, on its own HTTP port, separate index DBs" — CLAUDE.md's non-negotiable rule #2). Three hard rules and one behavioral caveat:

| Shared resource | Coexist? | What to do |
|---|---|---|
| **Music files** | ✅ free | Both read-only. No conflict. |
| **`library.db`** | ❌ must split | **Separate DB file per instance.** Two processes doing `clear(udn)` + re-crawl + writing playlists to one file = clobbering + `SQLITE_BUSY` + schema-drift hazard (2.0 may migrate the schema and break 1.x reading it). Give 2.x its own `library.db`. |
| **Ports** | ❌ must split | 1.x = 8765/8443/8200 (LocalFs). 2.x gets its own, e.g. 8766 / `tailscale serve` on 443 / LocalFs 8201. |
| **Working dir** | ❌ must split | Run 2.x from a **separate checkout** so `library.db`, `config.json`, `gateway.log`, `*.crt` are naturally distinct (they're relative paths). This is the cleanest isolation — solves DB + config + log in one move. |
| **SSDP MediaServer announce** | ⚠️ distinct UDN | Both announce as a gateway-MediaServer; give 2.x a **distinct UDN + friendly name** ("DLNA Gateway 2.0") so the Naim shows two and you can tell them apart. SSDP multicast listeners coexist fine. |
| **TLS / cert machinery** | ✅ via separation | 1.x keeps its `:8443` + cert renewal; 2.x uses `tailscale serve` on `:443`. Different ports → no clash. **Don't** delete the cert machinery from the box until 1.x is retired — but since 2.x is a separate checkout, its *code* can drop TLS while 1.x's checkout keeps it. |
| **Driving the same Naim** | ⚠️ behavioral | The per-UDN queue is per-process; cross-process has no coordination. Don't actively stream to the *same* physical renderer from both at once — they'd fight. Fine in practice (you test 2.x playback deliberately). |

The one real cost: **user data diverges** while both run — playlists / favourites / radio favs / play_counts created in 1.x won't show in 2.x (separate DBs). At cutover you do a one-time copy of those tables (`playlists`, `playlist_tracks`, `album_favourites`, `radio_favourites`, `play_counts`, `lyrics`, `metadata_overrides`) from 1.x's DB into 2.x's. Everything else (the `tracks` index) just re-derives from the music folder.

## How to structure the code: branch strategy (the palette)

| Option | Model | Pros | Cons |
|---|---|---|---|
| **A. `main` stays 1.x; `2.0` long-lived branch** | Bugfixes → main; 2.0 work on branch; merge main→2.0 periodically | Your daily-driver (main) never moves under you; 2.0 is abandonable with zero risk to 1.x | Long-lived branch drift; periodic merges |
| **B. Cut `1.x` maintenance branch; `main` becomes 2.0 trunk** | 1.x frozen-but-patchable; new work is 2.0 on main | Standard "current release branch" model; 2.0 is trunk | main stops being "the stable thing"; must remember to backport fixes to `1.x` |
| **C. git *worktree* for the second line** | `git worktree add ../dlna-gateway-2.0 2.0` — two working dirs, **one** shared history | Physically separate dirs (⇒ separate db/config/log for free) **and** one repo; ideal for "both running on the box" | minor worktree learning curve |
| **D. Separate `git clone`** | A second independent clone for 2.x | Dead simple mental model | Two `.git`s; sync via origin; worktree is the lighter version of this |
| **E. Feature flags, one codebase** | Gate 2.0 behind config | No branch divergence | 2.0 is structural (delete TLS code, schema change, tailscale serve) — flags would litter everything. **Bad fit here.** |

**My recommendation: A + C.** Keep `main` = stable 1.x (the thing you use daily), develop 2.0 on a `2.0` branch checked out in a **worktree** at `../dlna-gateway-2.0`. You get: 1.x untouched, 2.x in its own directory (so its `library.db`/`config.json`/`gateway.log` are automatically separate — solving the coexistence isolation), and one git history. Periodically `git merge main` into `2.0` to absorb 1.x fixes. When 2.0 proves out, merge `2.0 → main` and tag `v2.0.0`.

## Versioning: tags + branches, not commit subjects

Commit-message prefixes (`1.xx`/`2.xx`) are weak — they don't gate anything and get noisy. Use the real mechanisms:

- **Branch carries the line** — `main` (1.x) vs `2.0`.
- **Tags carry the version** — SemVer: `git tag v1.4.0` on main, `git tag v2.0.0-alpha.1` on the 2.0 branch. This is what "the version" actually is.
- **Surface the version in the running app** — add a `VERSION` constant (or read the nearest git tag) exposed at `/api/version` and shown in the PWA footer. With two instances live, you *want* the UI to say "2.0.0-alpha" vs "1.4.0" so you know which one you're looking at. Small, high-value, do it early.
- Keep your existing `type(scope):` commit style (e.g. `feat(transport): …`) — the branch already tells you the line, so no `[2.x]` clutter needed.

## Status (2026-06-04)

- **Phase 0 — parallel rig: ✅ DONE.** `2.0` branch + worktree at `../dlna-gateway-2.0`; 2.x runs on `:8766` / LocalFs `:8201` / distinct UPnP UDN+name via `./run-2.0.sh`; `/api/version` + header badge; its own `library.db`/`config.json`/`gateway.log` (separate working dir, derived from `__file__`). 1.x and 2.x verified running side by side. (tag `v2.0.0-alpha.1`.)
- **Phase 1 — drop the gateway's own TLS: ✅ CODE DONE** (`baa2c0c`). Removed `TLSThreadedHTTPServer`, the HTTP→HTTPS redirect, the cert auto-detect / `_warn_if_cert_expiring_soon`, and the `--tls-*` args. The stdlib gateway now serves **plain HTTP** on `0.0.0.0:8766`; device endpoints (`/stream`, `/gw/`, LocalFs `:8201`) stay un-proxied on the LAN.
- **Phase 2 — Hypercorn + FastAPI ASGI rewrite: 🔄 UNDERWAY.** `dlna_asgi.py` (FastAPI) served by **Hypercorn**, with `dlna_asgi_bridge.py` running the legacy `(h, params)` handlers unchanged via a shim. Most of the **read** API is now native FastAPI routes; the rest runs through the bridge. Run it: `.venv/bin/hypercorn dlna_asgi:app --bind 127.0.0.1:8768` (docs at `/api/docs`).

## Transport / TLS decision — REVISED 2026-06-04: `tailscale serve` DROPPED

The original plan was to front the gateway with **`tailscale serve`** (h2 + free TLS, delete the cert machinery). It's **abandoned** after a full day of debugging — `tailscale serve` on `:443` is **broken on this Mac mini's Tailscale install**. The clincher: from a phone with healthy DNS + data path, 1.x's *self-served* HTTPS on `:8443` loads fine, but `serve` on `:443` does not. Gateway, tailnet data path, macOS firewall, and `ShieldsUp` were all ruled out — it's the `serve` subsystem itself (likely update damage: stale system-extensions + a MagicDNS regression). The MacBook's Tailscale is *separately* broken (dead DNS + data path) — a standalone "reinstall Tailscale" chore, unrelated to 2.0.

**New plan — TLS is APP-OWNED via Hypercorn.** Once the ASGI app is the gateway, **Hypercorn terminates TLS + HTTP/2/3 natively** with a `tailscale cert`-issued cert. No `tailscale serve`. This is *proven viable* (self-served TLS demonstrably works over the tailnet), gives h2/h3 without a proxy, and the cert-renewal machinery **stays** (Hypercorn-owned, not deleted). **B-later (chosen):** change nothing about 2.x TLS now — 2.x stays plain-HTTP / LAN-only for testing; it gains TLS when Hypercorn becomes the server. Mobile-anywhere + privacy still come from Tailscale (the tunnel), independent of who terminates TLS.

## Runthrough — remaining order

1. ✅ Parallel rig (Phase 0).
2. ✅ Drop the stdlib gateway's own TLS (Phase 1) — plain-HTTP backend.
3. 🔄 **Hypercorn + FastAPI ASGI rewrite (Phase 2)** — incremental: native route batches + bridge for the rest; then POST routes, the byte-streamers as `StreamingResponse`, static serving; finally make `dlna_asgi` the entrypoint (start the gateway's daemon threads alongside Hypercorn) and retire `dlna_server`.
4. **TLS via Hypercorn** — point Hypercorn at the `tailscale cert`; HTTPS + h2 (h3 when wanted). Set `docs_url=None` here (drops the Swagger CDN call — privacy).
5. **R4 + R5** — LocalFs completeness + folder/`album_key` grouping.
6. **R2 — SSE push** for now-playing + index status (unlocked by ASGI).
7. **R6 — Plex/Jellyfin providers**.
8. **Cutover** — copy user-data tables 1.x→2.x **except playlists** (start those fresh — beets gives good genres); point launchd at the 2.x checkout; tag `v2.0.0`; merge `2.0 → main`.

Throughout: the **run-all-tests-before-git gate** applies on the 2.0 branch; pull 1.x fixes in via periodic `git merge main`.

## Task list

**Phase 0 — rig**
- [x] `2.0` branch + worktree, separate ports/DB/UDN, `/api/version` + badge
- [x] Merge `main` beets fixes into `2.0`

**Phase 1 — drop gateway TLS**
- [x] Remove TLSThreadedHTTPServer / redirect / cert-detect / cert-expiry-warn / `--tls-*` (`baa2c0c`)
- [x] Plain HTTP on `0.0.0.0`; device endpoints un-proxied on LAN
- [~] ~~`tailscale serve` front~~ — **dropped** (broken on this tailnet); TLS → Hypercorn instead

**Phase 2 — Hypercorn + FastAPI (underway)**
- [x] ASGI skeleton + Hypercorn (`dlna_asgi.py`, `/api/version` native)
- [x] Legacy-handler bridge (`dlna_asgi_bridge.py`) — whole read API under Hypercorn
- [x] Native read routes: servers/renderers; artists/albums/genres + drill-downs; decades/decade_*; search; browse_letter; index/status; track_meta; playlists/playlist; album_favourites(+check); radio/favourites
- [ ] AcoustID **minimal removal** (endpoints + PWA 🔎 Enrich button) — scheduled next
- [ ] Remaining bridged reads native (lyrics, radio/search, radio/nowplaying, radio, browse) — optional
- [ ] POST routes (bridge, then native)
- [ ] Byte-streamers as `StreamingResponse` (`/stream`, `/art`, `/radio_stream`)
- [ ] Static serving (the PWA) under ASGI
- [ ] **R2 — SSE push**
- [ ] Make `dlna_asgi` the entrypoint (start daemon threads alongside Hypercorn); retire `dlna_server`
- [ ] **TLS via Hypercorn** (`tailscale cert`) + `docs_url=None`

**Phase 3 — library + providers**
- [ ] R4 LocalFs completeness · R5 folder/`album_key` grouping · R6 Plex/Jellyfin

**Cutover**
- [ ] Copy user-data tables 1.x→2.x **except playlists** (fresh start); launchd→2.x; tag `v2.0.0`; merge `2.0 → main`
- [ ] AcoustID **full cleanup** (module + worker + tests) once 2.0 is stable
