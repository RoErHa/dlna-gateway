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

1. **Set up the parallel rig** (no code yet): `git checkout -b 2.0 && git worktree add ../dlna-gateway-2.0 2.0`. In the worktree, point 2.x at its own ports + DB (env: `LOCALFS_PORT=8201`, gateway port 8766, a `library_2.db`), distinct gateway UDN/name. Add `/api/version` + footer badge. Prove both run at once and the Naim sees two distinct MediaServers.
2. **Anchor change — `tailscale serve` front + drop the gateway's TLS** (§1, B5/B6): bind 2.x to localhost HTTP, `tailscale serve` it on 443, delete `TLSThreadedHTTPServer` / the HTTP→HTTPS redirect / `_warn_if_cert_expiring_soon` *in the 2.x checkout only*. Keep `/stream`, `/radio_stream`, `:8201` LocalFs, and `/gw/` device endpoints on plain LAN HTTP, un-proxied (the Naim caveat). This is the biggest simplification for the least code.
3. **R4 + R5 — make LocalFs complete + folder-grouped** before client polish: full one-pass import of the music root, a coverage report (files-on-disk vs rows), consistent `album_key` grouping so boxed sets are one album everywhere.
4. **R2 — SSE push** for now-playing + index status: kills the polling storm; biggest *felt* responsiveness win after the network layer.
5. **R6 — Plex/Jellyfin providers** opportunistically once the seam's exercised; then the rest as capacity allows.
6. **Cutover**: copy the user-data tables 1.x→2.x, point launchd at the 2.x checkout, retire 1.x's cert LaunchAgent, tag `v2.0.0`, merge `2.0 → main`.

Throughout: your **run-all-tests-before-git gate** still applies on the 2.0 branch, and 2.0's docs (`REQUIREMENTS_2.0.md`, CLAUDE.md, ARCHITECTURE.PDF) stay where they are.

---

Want me to do step 1 now — create the `2.0` branch + worktree at `../dlna-gateway-2.0`, wire its separate ports/DB/UDN, and add the `/api/version` + footer badge? That's a self-contained, low-risk first action that leaves `main`/1.x completely untouched and gives you the side-by-side rig to build on. (I'd confirm the exact ports with you first.)
