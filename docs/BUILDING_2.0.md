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

## Concrete runthrough — the order to actually build it

Following `REQUIREMENTS_2.0.md §3`'s cut order, adapted for side-by-side:

1. **✅ DONE — parallel rig.** `2.0` branch + worktree at `../dlna-gateway-2.0`; 2.x on `:8766` / LocalFs `:8201` / distinct UPnP UDN+name; `/api/version` + header badge; own `library.db`/`config.json`/`gateway.log` (separate working dir). Both run side-by-side, verified. Run with `./run-2.0.sh`. (commit `755601a`, tag `v2.0.0-alpha.1`.)
2. **`tailscale serve` front + drop 2.x's TLS** (§1, B5/B6): bind 2.x to localhost HTTP, `tailscale serve` it on 443, delete `TLSThreadedHTTPServer` / the HTTP→HTTPS redirect / `_warn_if_cert_expiring_soon` *in the 2.x checkout only*. Keep `/stream`, `/radio_stream`, `:8201` LocalFs, and `/gw/` device endpoints on plain LAN HTTP, un-proxied (the Naim caveat). Biggest simplification for the least code.
3. **Hypercorn + FastAPI/Starlette ASGI rewrite — the "modern app."** Move the handlers off stdlib `BaseHTTPRequestHandler` onto an ASGI app served by Hypercorn (behind the `tailscale serve` front, so h2 + cert-deletion + privacy are retained). Do it **incrementally** — port endpoints in batches, keep the test gate green each step — not big-bang. Mind the hard parts: the `/stream` + `/radio_stream` byte relays (streaming responses), the UPnP SOAP/XML + `/gw/` device endpoints (stay plain-HTTP, un-proxied), static serving, and the SSDP/threading model.
4. **R2 — SSE push** for now-playing + index status (unlocked by the ASGI move): kills the polling storm; biggest *felt* responsiveness win.
5. **R4 + R5 — make LocalFs complete + folder-grouped**: full one-pass import of the music root, a coverage report (files-on-disk vs rows), consistent `album_key` grouping so boxed sets are one album everywhere. Can run in parallel with the above.
6. **R6 — Plex/Jellyfin providers** opportunistically once the seam's exercised.
7. **HTTP/3 — deferred, low priority.** Tipping point: **when `tailscale serve` itself supports h3** → h3 comes for free while keeping the front (no cert re-ownership). *Not* planning to drop `tailscale serve` to get h3 (that re-introduces the cert machinery and h3's tailnet RTT win is likely marginal — measure first if ever pursued).
8. **Cutover**: copy the user-data tables 1.x→2.x, point launchd at the 2.x checkout, retire 1.x's cert LaunchAgent, tag `v2.0.0`, merge `2.0 → main`.

Throughout: the **run-all-tests-before-git gate** applies on the 2.0 branch, and main fixes are pulled in via periodic `git merge main` into the worktree.

---

## Decisions locked (2026-06-03)

- **Branch model: A + C.** `main` = stable 1.x daily-driver (untouched); 2.0 on a long-lived `2.0` branch in a worktree at `../dlna-gateway-2.0`. Merge `main → 2.0` periodically.
- **Transport: `tailscale serve` first** (h2 + free TLS, deletes the cert machinery, zero app rewrite) — *then* the Hypercorn/ASGI rewrite for a modern app (async + WebSocket/SSE). These compose: Hypercorn runs behind the `tailscale serve` front.
- **HTTP/3: low priority, gated on `tailscale serve` gaining h3 support.** Keep the Tailscale front; don't drop it to chase h3.
- **Mobile-anywhere + privacy come from Tailscale (tailnet overlay)** and hold through every phase, independent of which server terminates TLS.
- **Versioning:** SemVer tags + branch name (no commit-subject version prefixes).

## 2.0 task list

**Phase 0 — rig**
- [x] `2.0` branch + worktree, separate ports/DB/UDN, `/api/version` + badge, side-by-side verified (`755601a`)
- [x] Merge `main` beets fixes into `2.0` (`aaf9a35`)

**Phase 1 — `tailscale serve` transport (next)**
- [ ] Bind 2.x gateway to `127.0.0.1` HTTP only (no TLS bind)
- [ ] `tailscale serve` 443 → `http://127.0.0.1:<port>`; verify h2 + trusted cert on mobile over tailnet
- [ ] Delete 2.x TLS code: `TLSThreadedHTTPServer`, HTTP→HTTPS redirect, `_warn_if_cert_expiring_soon`, `*.crt/.key` auto-detect
- [ ] Confirm device path untouched: Naim still reaches `/stream`, `/gw/`, LocalFs `:8201` on plain LAN HTTP (un-proxied)
- [ ] Update CLAUDE.md / ARCHITECTURE.PDF for the new transport (on request)

**Phase 2 — Hypercorn + FastAPI/Starlette (the modern app)**
- [ ] Stand up an ASGI app skeleton served by Hypercorn behind `tailscale serve`
- [ ] Port `/api/*` JSON handlers off `BaseHTTPRequestHandler` (incremental, gate-green each batch)
- [ ] Port the byte relays (`/stream`, `/radio_stream`) as ASGI streaming responses
- [ ] Keep UPnP SOAP/XML + `/gw/` device endpoints plain-HTTP, un-proxied
- [ ] Port static serving + retire the threading/`ThreadingMixIn` model
- [ ] **R2 — SSE push** for now-playing + index status (replace the PWA poll loop)

**Phase 3 — library + providers**
- [ ] R4 LocalFs completeness (full import + files-on-disk vs rows coverage report)
- [ ] R5 folder/`album_key` grouping consistent across browse / UPnP / Subsonic
- [ ] R6 Plex / Jellyfin providers on the seam

**Phase 4 — deferred**
- [ ] HTTP/3 — *only when `tailscale serve` supports it* (low priority)

**Cutover**
- [ ] Copy user-data tables 1.x → 2.x (playlists, playlist_tracks, album_favourites, radio_favourites, play_counts, lyrics, metadata_overrides)
- [ ] Point launchd at the 2.x checkout, retire 1.x cert LaunchAgent
- [ ] Tag `v2.0.0`, merge `2.0 → main`
