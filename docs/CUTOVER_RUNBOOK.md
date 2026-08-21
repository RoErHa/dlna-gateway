# 2.0 Cutover Runbook

> **✅ CUTOVER COMPLETE (2026-06-09) — historical record.** The 2.0 ASGI gateway
> is the live daily driver (`v2.0.0`); this is how it was done, kept for
> reference/rollback. **One decision below is now superseded:** the `/gw/*`
> device tier no longer runs on a separate `:8770` server — **Cleanup C
> (2026-06-12) folded `/gw/*` into the ASGI app on the plain `:8765` bind** and
> retired `dlna_server.py` + the `:8770`/`GATEWAY_PORT` plumbing. The current
> architecture lives in `CLAUDE.md` + `docs/ARCHITECTURE.PDF`.
>
> **The two one-shot tools this runbook drives were deleted on 2026-08-21**
> (`com.roha.dlna-gateway.cutover.plist`, `tools/cutover_copy_userdata.py`):
> the cutover finished in June 2026 and both hardcoded one machine's paths,
> which is not what a public repo should ship. They are still in git history
> — `git log --diff-filter=D -- tools/cutover_copy_userdata.py` finds the
> commit that removed them.

The ordered, reversible procedure to make the **2.0 ASGI gateway** the live
daily driver, taking over 1.x's identity so the Naim, CarPlay/Amperfy, Subsonic
and the PWA keep their existing connections. Companion: **`CUTOVER_LAUNCHD.md`**
(the LaunchAgent draft).

> **Gate before you start:** 2.x must have run a long session (incl. Naim
> playback) **without crashing** — the `Servers offline — subnet scan` lines
> gone and the FD sawtooth flat near ~25. Don't cut over a gateway that still
> exhausts FDs.

---

## Decisions (locked in)

| | choice |
|---|---|
| **Identity** | 2.x **adopts 1.x's** ports + UDN: gateway `:8765` (plain) + `:8443` (TLS), LocalFs `:8200`, `GW_UDN=uuid:dlna-gateway-iina-8765`, name "DLNA Gateway (IINA)". `/gw/*` device tier was on plain `:8770` at cutover; **Cleanup C folded it into `:8765`** (`:8770` retired). |
| **User data copied** | `album_art` · `radio_favourites` · `play_counts` · `lyrics` · `metadata_overrides` (via `tools/cutover_copy_userdata.py`). |
| **Started fresh on 2.x** | `playlists` / `playlist_tracks` (= ⭐ track favourites) · `album_favourites`. |

**Why the ports line up:** LocalFs URLs are `http://<lan-ip>:<port>/localfs/stream/<sha1(rel_path)>`.
Both 1.x and 2.x hash the same files; once 2.x serves LocalFs on `:8200` (1.x's
port) the URLs are byte-identical, so the URL-keyed copied data (`play_counts` /
`lyrics` / `metadata_overrides`) matches 2.x's tracks **after a force rebuild on
`:8200`** (step 5).

---

## Rollback (read first)

At any point before step 8 you can revert to 1.x in ~30 s:

```bash
# stop 2.x, restore 1.x
launchctl bootout gui/$(id -u)/com.roha.dlna-gateway 2>/dev/null
cp ~/Library/LaunchAgents/com.roha.dlna-gateway.plist.1x-bak \
   ~/Library/LaunchAgents/com.roha.dlna-gateway.plist
launchctl load ~/Library/LaunchAgents/com.roha.dlna-gateway.plist
```

2.x's own `library.db` is never written by 1.x, and the copy tool backs up the
2.x DB before `--apply`, so nothing is lost.

---

## Steps

### 0 — Backups + cert check
```bash
cp /Users/ronhamersma/dlna-gateway/library.db{,.precutover-bak}        # 1.x DB
cp /Users/ronhamersma/dlna-gateway-2.0/library.db{,.precutover-bak}    # 2.x DB
cp ~/Library/LaunchAgents/com.roha.dlna-gateway.plist{,.1x-bak}        # 1.x plist
# tailscale cert valid? (must not be near expiry)
openssl x509 -enddate -noout -in /Users/ronhamersma/dlna-gateway/ronsmacmini.tail5be6ad.ts.net.crt
```

### 1 — Code flips on the `2.0` branch (commit + gate-green BEFORE installing the plist)
- `dlna_asgi.py`: `FastAPI(..., docs_url=None)` (drop the Swagger CDN call — privacy).
- TLS on by default (the LaunchAgent passes `--certfile/--keyfile` directly; the
  `GATEWAY_TLS` opt-in in `run-2.0-asgi.sh` stays for manual runs).
- Run the full test gate, commit, push.

### 2 — Stop both gateways
```bash
launchctl bootout gui/$(id -u)/com.roha.dlna-gateway 2>/dev/null   # stop 1.x
pkill -f "hypercorn dlna_asgi" 2>/dev/null                          # stop any 2.x
pkill -f "dlna-gateway-2.0/dlna_gateway.py" 2>/dev/null
```

### 3 — Install the 2.x LaunchAgent (adopts 1.x identity) and load it
Use the plist from **`CUTOVER_LAUNCHD.md`** (it sets `GW_UDN`, `LOCALFS_PORT=8200`,
the dual bind `:8765`+`:8443`, `GATEWAY_PORT=8770`, and the 8192 FD
`SoftResourceLimits`). `SUBSONIC_USER`/`SUBSONIC_PASSWORD` and
`GATEWAY_CONTACT_EMAIL` are NOT in the plist — they come from `.env`
(gitignored), the single source for names/emails/passwords.
```bash
# NB: the 2.x cutover plist is com.roha.dlna-gateway.cutover.plist — NOT the
# public template com.roha.dlna-gateway.plist (placeholder paths, stdlib python).
# It is installed UNDER the 1.x label name. Put SUBSONIC_USER/PASSWORD +
# GATEWAY_CONTACT_EMAIL in <repo>/.env (the gateway loads it via dlna_config);
# the plist deliberately omits them so .env stays authoritative.
cp com.roha.dlna-gateway.cutover.plist ~/Library/LaunchAgents/com.roha.dlna-gateway.plist
launchctl load ~/Library/LaunchAgents/com.roha.dlna-gateway.plist
tail -f /Users/ronhamersma/dlna-gateway-2.0/gateway.log  # watch it boot
```
Confirm: `Open-file soft limit already 8192`, `FD monitor started`, LocalFs on
`:8200`, device server `/gw/*` on `:8770`, **no** `Servers offline — subnet scan`.

### 4 — Copy 1.x user-data → 2.x
```bash
cd /Users/ronhamersma/dlna-gateway-2.0
python3 tools/cutover_copy_userdata.py                 # DRY-RUN — review counts
python3 tools/cutover_copy_userdata.py --apply         # backs up 2.x DB, commits
```

### 5 — Force rebuild on `:8200` (aligns URLs + applies copied overrides/art)
A normal scan skips unchanged files (no COALESCE). Force a full rebuild so every
track is re-upserted on the adopted `:8200` base and the copied `metadata_overrides`
(your year corrections) + `album_art` covers are applied:
```bash
UDN=$(curl -sk https://127.0.0.1:8443/api/servers | python3 -c \
   'import sys,json;print(next(s["udn"] for s in json.load(sys.stdin) if s["udn"].startswith("uuid:localfs-")))')
# NB: /api/index/rebuild is a GET route (in GET_ROUTES) — a -X POST returns 405.
curl -sk "https://127.0.0.1:8443/api/index/rebuild?udn=$UDN"
# watch gateway.log for "rescan complete" / index done
```

### 6 — Verify
- **PWA**: open `https://ronsmacmini.tail5be6ad.ts.net:8443/` from the phone (trusted h2, no warning); browse shows ~2,110 folder-grouped albums; album **covers** present; a known **year correction** shows in now-playing; **radio stations** present; playlists/favourites **empty** (fresh, by design).
- **Naim**: queue an album → plays, gapless across tracks.
- **Amperfy/CarPlay**: connects to `https://…:8443/rest`, browses, plays.
- **Art port**: if you soaked on a *different* `LOCALFS_PORT` (e.g. `:8201` via
  `run-2.0-asgi.sh`) than the cutover (`:8200`), `tracks.art` may still carry the
  stale soak port — the force rebuild does NOT rewrite already-set art. Check +
  fix:
  ```bash
  sqlite3 library.db "SELECT COUNT(*) FROM tracks WHERE art LIKE '%:8201/%';"   # must be 0
  sqlite3 library.db "UPDATE tracks SET art=replace(art,':8201/localfs/art/',':8200/localfs/art/') WHERE art LIKE 'http://%:8201/localfs/art/%';"
  ```
- **FD monitor**: `grep "FD usage" gateway.log` — flat, low (no subnet-scan spikes).
- **No** `Servers offline — subnet scan` and **no** asyncio `client_connected_cb` tracebacks.

### 7 — Soak
Leave it running for a few hours / overnight under normal use. Confirm no FD
`ALERT`, no crash.

### 8 — Tag + merge (point of no easy return)
```bash
cd /Users/ronhamersma/dlna-gateway-2.0
git tag v2.0.0 && git push origin v2.0.0
git checkout main && git merge 2.0          # brings the heartbeat/subnet fix to 1.x too
# resolve any conflicts, run the gate on main, push
```

---

## Post-cutover (not blocking — track separately)
- **AcoustID full cleanup** — remove the dormant `dlna_acoustid` module/worker/tests.
- **Cleanup C** — fold `/gw/*` into the ASGI app on a Hypercorn `--insecure-bind`
  and retire `dlna_server` + `DeviceHandler` + `run-2.0.sh` → one framework.
- **R6** — Plex / Jellyfin providers on the `LibraryProvider` seam.
- Decommission the old `run-2.0.sh` (stdlib) path once C lands.
