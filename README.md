# DLNA Gateway

A self-hosted UPnP/DLNA music library gateway. Discovers UPnP MediaServers
(AssetUPnP, MinimServer, Jellyfin, Plex DLNA) on your local network,
indexes their music into a local SQLite database, and serves a fast PWA
web UI for browsing and playback. Plays to UPnP MediaRenderers (Naim,
Sonos in UPnP mode, etc.) or directly in any modern browser.

Built because the manufacturer apps (Focal & Naim, etc.) are slow,
flaky, or platform-locked. This one runs anywhere Python runs and is
usable from any device with a browser.

---

## Features

- **One library, many sources.** Indexes anything that speaks UPnP
  ContentDirectory — *and* your own filesystem via the built-in
  **RoHaLocalFS** backend (point it at a music folder; no separate
  media server needed). Multiple sources coexist and the PWA has a
  source picker to switch between them.
- **Two playback paths.**
  - **UPnP renderers** — sends `SetURI` + `Play` SOAP to the device;
    the renderer streams audio directly from the source server
    (gateway is **not** in the audio path; bit-perfect).
  - **Browser audio** — `<audio>` element + a per-tab `/stream`
    Range-proxy. Works on any device with a browser.
- **PWA web UI.** Letter-indexed browse, FTS5 search, playlists,
  album-level favourites, lyrics (via lrclib), album art (sibling
  → MusicBrainz / Cover Art Archive fallback), per-track loudness
  normalisation (peak-based, ±2 dB clamp).
- **Metadata enrichment (in flight).** Background worker fingerprints
  tracks via Chromaprint and resolves them to MusicBrainz metadata
  through AcoustID, fixing mistagged / untagged tracks. SQLite-only
  by design — the worker fills `metadata_overrides`, never rewrites
  on-disk file tags.
- **Internet radio ("📡 Stations").** Search the radio-browser.info
  catalogue, favourite up to 25 stations, play with ICY now-playing
  metadata in a dedicated radio screen.
- **Subsonic-compatible API** (`/rest/*`). Read-only-ish surface that
  lets Subsonic clients (Amperfy, substreamer, …) browse and stream.
  Designed for **CarPlay**, which the PWA can't do.
- **Concurrent playback.** Per-renderer queue model; multiple users on
  multiple renderers can play simultaneously without stepping on each
  other (`409 Conflict` if you try to take over a busy renderer).
- **Self-healing.** Detects and auto-repairs FTS5 index corruption
  during rebuild-index instead of dying.
- **Observability.** Greppable per-track playback logs; client-side
  errors POST to `/api/client_log` and land in the same log.

## Cross-platform notes

**Server (runs anywhere Python runs).** Tested on macOS; the Python
code is platform-neutral except for two macOS-specific helpers
(`launchctl` for autostart, `osascript` for `tools/prune_empty_music_dirs.py`).
On Linux use a systemd unit instead of the LaunchAgent; on Windows
use WSL2 (easy) or run native with a service wrapper like NSSM.
Hard requirements:

- Python 3.9+
- (Optional) `ffmpeg` on `PATH` — only needed for the loudness scanner.
- (Optional) `fpcalc` from Chromaprint on `PATH` (`brew install chromaprint`
  on macOS) — only needed for the AcoustID metadata-enrichment worker.
- A music source: either network access to a UPnP MediaServer on your
  LAN, **or** a readable music folder via RoHaLocalFS (set
  `LOCALFS_MUSIC_ROOT` — see "Serving your own files" below).
- Inbound TCP 8765 (HTTP) and 8443 (HTTPS) to the gateway from your
  clients; UDP 1900 multicast for SSDP discovery.

**Clients (any browser on any platform).** The PWA uses standard
HTML5 `<audio>` + MediaSession APIs. Anything that runs a modern
browser — iOS Safari, Android Chrome, Firefox/Chrome/Edge on
Linux/Windows/macOS, Chrome OS — can browse the library and play
in-browser. iOS gives the best PWA polish (Add to Home Screen,
lock-screen artwork) but is **not** required.

**Tailscale (optional but recommended).** The gateway is LAN-only by
default. Run [Tailscale](https://tailscale.com/) on the server and
on each client device and you get end-to-end encrypted access from
anywhere with no port-forwarding. A Tailscale-issued Let's Encrypt
cert (`tailscale cert`) gives you a trusted HTTPS URL on the
`*.ts.net` MagicDNS hostname — no certificate warnings on mobile.
Auto-renewal: `renew-cert.sh` + the cert-renew LaunchAgent (macOS).

**Caveats.**
- **CarPlay** is iOS-only — that's the only reason the Subsonic API
  exists (via [Amperfy](https://github.com/BLeeEZ/amperfy)). For
  everything else CarPlay-compatible doesn't matter.
- **UPnP renderers** must be reachable from the gateway on the LAN
  (UPnP uses LAN multicast/HTTP). Tailscale doesn't help here —
  the renderer needs to be on the same LAN as the gateway.

## Quick start (macOS)

```bash
git clone <your-fork-url> dlna-gateway
cd dlna-gateway
cp .env.example .env       # then edit to set SUBSONIC_PASSWORD etc.
./setup.sh --run           # creates a venv, installs deps, starts the gateway
```

Open `http://localhost:8765/` in any browser.

For auto-start at login: see the comments at the top of
`com.roha.dlna-gateway.plist` (it's a LaunchAgent template — edit the
path placeholders, copy to `~/Library/LaunchAgents/`, `launchctl load`).

## Quick start (Linux)

```bash
git clone <your-fork-url> dlna-gateway
cd dlna-gateway
cp .env.example .env       # then edit
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python dlna_gateway.py
```

For autostart, drop something like this into
`~/.config/systemd/user/dlna-gateway.service`:

```ini
[Unit]
Description=DLNA Gateway
After=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/dlna-gateway
EnvironmentFile=%h/dlna-gateway/.env
ExecStart=%h/dlna-gateway/.venv/bin/python %h/dlna-gateway/dlna_gateway.py --no-browser
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

Then `systemctl --user daemon-reload && systemctl --user enable --now dlna-gateway`.

## Quick start (Windows)

**Recommended:** install [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install)
and follow the Linux instructions inside your WSL distro. Everything
just works.

**Native (no WSL):** open PowerShell:

```powershell
git clone <your-fork-url> dlna-gateway
cd dlna-gateway
copy .env.example .env       # then edit
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python dlna_gateway.py
```

For autostart use [NSSM](https://nssm.cc/) to wrap `dlna_gateway.py`
as a Windows Service. You may also need to allow inbound TCP
8765/8443 and UDP 1900 in Windows Firewall.

## Configuration

All configuration lives in `.env` (gitignored). Copy `.env.example`
to `.env` and edit. Variables:

- `SUBSONIC_USER` / `SUBSONIC_PASSWORD` — auth for the `/rest/*`
  Subsonic API. Leave password empty to refuse all Subsonic calls
  (the API then returns `503` on every request — safe default if
  you don't use CarPlay clients).
- `GATEWAY_CONTACT_EMAIL` — sent in the User-Agent of outbound calls
  to MusicBrainz, Cover Art Archive, and radio-browser.info. Their
  ToS requires an identifying email; anonymous-looking requests get
  throttled or blocked.
- `TAILSCALE_CERT_HOST` — only needed if you use `renew-cert.sh`
  for automated Tailscale cert renewal. Your tailnet hostname,
  e.g. `mymachine.tailXXXXX.ts.net`.
- `ACOUSTID_API_KEY` — only needed for the AcoustID metadata-
  enrichment worker. **Must be an application key**, not a user key:
  register a free application at
  [acoustid.org/applications](https://acoustid.org/applications). The
  user-account key from `/api-key` is for *submitting* fingerprints
  and will be rejected on lookup with `HTTP 400 invalid API key`.

Values set in the process environment (launchd plist, systemd
`EnvironmentFile`, shell `export`) override `.env`. **`.env` requires
`python-dotenv`** (in `requirements.txt`, installed automatically by
`setup.sh` / `pip install -r requirements.txt`) — without it, the file
is silently ignored and env vars must come from the process
environment.

## Serving your own files — RoHaLocalFS

Instead of (or alongside) a UPnP MediaServer, the gateway can index a
music folder directly and serve the original file bytes itself. This
backend shows up in the UI as the source **RoHaLocalFS**.

**What you need**
- A readable music folder (local disk, mounted NAS, external drive, …).
- [`mutagen`](https://mutagen.readthedocs.io/) — installed automatically
  by `setup.sh` / `pip install -r requirements.txt`; used to read tags
  and embedded cover art.
- A free TCP port for the file server (default **8200**) reachable by
  your UPnP renderers on the LAN — they fetch audio directly from it.

**Enable it** — set `LOCALFS_MUSIC_ROOT` to the folder and (re)start:

```bash
# macOS / Linux shell:
export LOCALFS_MUSIC_ROOT="/path/to/Music"
./setup.sh --run          # or: python dlna_gateway.py
```

To persist it, put the variable wherever your autostart reads env from
— the LaunchAgent plist `<EnvironmentVariables>` block on macOS, the
systemd `EnvironmentFile` (`.env`) on Linux, or `.env` generally.

On startup you'll see `LocalFs enabled: root=… port=8200 base_url=…` in
the log, and **RoHaLocalFS** appears in the PWA's source picker next to
any UPnP server. The first scan walks the tree (incremental afterwards
— only changed files are re-read).

**Optional settings**

| Variable | Purpose |
|---|---|
| `LOCALFS_MUSIC_ROOT` | **Required to enable.** Absolute path to the music folder. Unset = UPnP-only, backend dormant. |
| `LOCALFS_PORT` | File-server port (default `8200`). Change if 8200 is taken. |
| `LOCALFS_BASE_URL` | Override the auto-detected `http://<lan-ip>:<port>` the renderer fetches from — set this if the gateway's LAN IP isn't what renderers should use. |

**Folder layout — one album per folder.** RoHaLocalFS groups albums by
**folder**, not by tags. For the cleanest browse, keep **one album per
folder** (e.g. `Music/Artist - Album/…`). This is what makes
compilations work: a *Various Artists* collection lives in one folder,
so it shows as a single album even though every track has a different
performer — rather than fragmenting into one album per artist.
- **Subfolders are fine.** A multi-disc release split into `CD1` / `CD2`
  (or `Disc 1` / `Disc 2`, `Side A`, …) under the album folder still
  groups as **one** album — the disc subfolder is folded into its parent.
- Two different albums that merely share a name (e.g. two *Greatest Hits*)
  stay separate as long as they're in different folders.

**Notes**
- **Bit-perfect.** Files are served unmodified with HTTP Range support;
  no transcoding. Album art is the file's embedded cover, served at
  `/localfs/art/<id>` and proxied through the PWA's `/art` endpoint.
- **Re-scan on host/port change.** Track URLs embed the base URL; if the
  gateway's LAN IP or `LOCALFS_PORT` changes, a scan self-heals every
  URL on next startup.
- **`.mp4` is intentionally excluded** from the audio scan (music-video
  case). Supported: FLAC, MP3, AAC/M4A/ALAC, OGG/Opus, WAV, AIFF,
  DSF/DFF, APE, WMA.

## Database

The library index is a local SQLite file (`library.db`, gitignored).
It's created automatically on first run — there's nothing to import.
Persistent user data (playlists, favourites, play counts, lyrics,
loudness scans, radio favourites) survives a `clear()` /
rebuild-index. See `schema.sql` (committed) for the full schema.

## Architecture

For a deep dive on threading, the per-renderer playback model,
external services, and module layout, read [CLAUDE.md](CLAUDE.md).
It's written for a future engineer (or AI assistant) picking up
the code cold.

## Acknowledgments

This project would not exist without:

- [MusicBrainz](https://musicbrainz.org/) + [Cover Art Archive](https://coverartarchive.org/)
  — album art lookup.
- [AcoustID](https://acoustid.org/) + [Chromaprint](https://acoustid.org/chromaprint)
  — automatic metadata recognition via audio fingerprinting (the
  metadata-enrichment worker).
- [lrclib.net](https://lrclib.net/) — on-demand lyrics.
- [radio-browser.info](https://www.radio-browser.info/) — internet
  radio station directory.
- The [Subsonic API](http://www.subsonic.org/pages/api.jsp) — the
  CarPlay path runs on top of this protocol.
- [Tailscale](https://tailscale.com/) — secure remote access without
  port forwarding.

## License

[MIT](LICENSE). Use it, fork it, modify it — just keep the copyright
+ licence notice in copies.
