# Installer proposal — getting the gateway onto someone else's Mac

> **Status: PROPOSAL, for review (2026-11-08).** Nothing here is built. It
> supersedes the parked feasibility plan in [PACKAGING.md](PACKAGING.md),
> which stays as the background: its four hard problems still hold, and this
> document turns them into a sequence with decisions attached. Work starts
> only after the owner has read this and answered the questions in
> [§7](#7-decisions-needed-before-any-code).

## 1. The problem in one paragraph

Today, installing the gateway means a terminal: `git clone`, `./setup.sh`,
hand-editing `.env` (about forty settings, a dozen of them relevant to a new
user), copying LaunchAgent templates whose paths read `/path/to/`, and
reloading them with `launchctl`. That is fine for the owner and for the kind
of person who reads `CLAUDE.md`. It locks out the person the project is
otherwise built for: someone with a music collection on a disk, a network
streamer (a Naim, any UPnP renderer, a TV) and maybe CarPlay, who has never
opened Terminal. Since the repo went public, that person can find the
project but cannot use it.

## 2. Who it is for, and what "installed" means

**The target user:** a Mac owner with music files on an internal or external
disk and something on the network that plays UPnP/DLNA. They can install an
app, answer a few questions in a browser, and click Allow on a system
prompt. They will not edit a text file, read a log or use a terminal.

**"Installed" means, without a terminal:**

1. the gateway starts at login and survives a reboot;
2. a first-run page asks for the music folder (and optionally audiobooks and
   video), a name, and a password if they want CarPlay;
3. the first scan runs and they can browse and play from a browser on the
   Mac, from their phone on the LAN, and on their renderer;
4. updates arrive without reinstalling;
5. uninstalling leaves nothing behind unless they ask to keep their library.

**Explicitly not in scope for the first version:** Windows, beets and the
enrichment tools, the video transcode (ffmpeg), Immich, the weekly
`upgrade.py`, and anything a power user configures by hand today. All of it
keeps working for a git install; the packaged app simply does not offer it
yet.

## 3. What stands in the way today (measured in the code, 2026-11-08)

| Today | Why it blocks a package |
|---|---|
| `library.db`, `gateway.log`, `config.json`, `.env` and `art_cache/` live **next to the source** (`dlna_config._BASE_DIR`, `dlna_art_cache`), as do the TLS cert and key (`hypercorn_conf`, `dlna_alerts`). | A signed app bundle is read-only and replaced on every update; data inside it would be lost or refused. |
| Configuration is **`.env`, edited by hand**; a missing listen address fails startup on purpose (security posture 7). | No one in the target group edits `.env`. The failure-on-purpose must become "the setup page decides", not "it doesn't start". |
| Python, the venv and the dependencies come from **Homebrew + `setup.sh`**. The runtime watch exists because a `brew upgrade` can delete the running Python. | The target user has no Homebrew. A bundled Python removes that whole class of problem, and the runtime watch with it. |
| **Scheduled jobs are shell scripts + LaunchAgent templates** (backup, cert renewal, upgrade check, Immich import), with paths filled in by hand. | Each would need an installer step and its own permission prompts. |
| **Choosing the music folder.** A web page cannot hand the server a filesystem path: browsers never reveal one. | The setup page needs a server-side folder browser (sensitive: it lists the disk) or a native dialog. |
| **HTTPS off the Mac** comes from `tailscale cert`. Without HTTPS, a phone gets no service worker: no home-screen install, no offline shell, no lock-screen art on iOS. | Most target users have no Tailscale. |
| **Gatekeeper.** An app without an Apple Developer ID signature and notarisation is blocked on double-click; since macOS 15 the only way past it is System Settings → Privacy & Security → Open Anyway. | That is exactly the step the target user will not find. |

## 4. The proposal: three stages, each useful on its own

The order is chosen so that every stage ships something real, and so the
expensive, irreversible decision (paying Apple and maintaining a signed app)
comes last, after the cheaper stages have shown whether anyone wants it.

### Stage 1 — Make the gateway configurable without a text file *(no packaging yet)*

This is most of the code work in every route, and it improves git installs
too, so it is never wasted.

- **A per-user data directory.** `~/Library/Application Support/DLNA Gateway/`
  holds `library.db`, the log, the art cache, the certificate and a
  `settings.json`. **Backwards compatible by rule:** a `library.db` already
  next to the code (the owner's install) keeps being used where it is, so
  nothing moves on this Mac unless asked. One function decides the path;
  every module asks it.
- **`settings.json` written by the app, `.env` still honoured.** Precedence:
  real environment > `.env` (power users, this install) > `settings.json`
  (the setup page) > defaults. `.env.example` and its gate stay as they are.
- **A first-run setup page at `/setup`.** It appears while no music folder is
  configured. Steps: music folder → optional audiobooks/video folders →
  name → optional CarPlay password and contact email → done, start the scan.
  The folder step is a **server-side folder browser** limited to `/Volumes`
  and the home folder.
- **Security rules for `/setup`** (written down now because they are the
  whole risk): reachable **only from loopback**, i.e. from a browser on the
  Mac itself (same pattern as `/api/renderers/forget`, posture 12); and
  **closed once setup is complete**, so re-configuring needs a deliberate
  "change settings" from loopback, never a LAN page. The folder browser
  lists directory names only, never file contents.
- **In-process scheduling** for the daily backup and the certificate check,
  so a packaged install needs one login item, not five LaunchAgents. The
  shell scripts and their LaunchAgents stay for the existing install.

*Done when* a fresh clone can be configured completely through `/setup`, the
owner's install is untouched, and the suite covers the loopback rule, the
precedence order and the legacy-path rule.
*Effort:* about 4–6 sessions.

### Stage 2 — A Homebrew tap *(the cheap distribution channel)*

`brew install roerha/tap/dlna-gateway` then `brew services start
dlna-gateway`; the browser opens to `/setup`. Homebrew provides the Python,
the dependencies, the login item (`brew services` writes the LaunchAgent)
and updates (`brew upgrade`). No Apple account and no signing, because brew
installs from source.

It is **not** click-through: it needs one Terminal line. It is offered
because it reaches the technical half of the audience at almost no cost, it
exercises Stage 1 on machines that are not the owner's, and it is how
people would report problems before a signed app exists.
*Effort:* 1–2 sessions, plus a separate `homebrew-tap` repository.

### Stage 3 — A signed Mac app *(the actual click-through)*

A menu-bar app ("DLNA Gateway" with Start, Stop, Open, Settings and Quit)
that contains a bundled Python and the server, distributed as a notarised
`.dmg`. First launch: drag to Applications → open → it registers itself as a
login item → opens `/setup` in the browser. The native shell also solves the
folder question properly (a real folder dialog) and asks the system for the
permissions the gateway needs at that moment (external volumes, Messages for
alerts) instead of failing silently later.

- **Packager:** PyInstaller for the server plus a small native menu-bar
  shell, or Briefcase for both. Decide with a one-day spike: build a bundle
  that serves the PWA and plays one track to the Naim, then compare size,
  start-up time and how each handles signing.
- **Updates:** Sparkle, the standard Mac self-updater, checking a signed
  appcast published with each GitHub release.
- **Signing:** Apple Developer Program (US$99 a year) + `notarytool`, run
  from a GitHub Actions workflow on every tagged release.

*Effort:* about 6–10 sessions after the spike, most of it signing, the
update path and testing on a clean Mac (a second user account or a VM).

## 5. HTTPS for phones without Tailscale

Recommendation: **plain HTTP on the LAN by default, Tailscale detected and
offered**, with the setup page saying honestly what HTTP costs.

| Option | Phone experience | Cost |
|---|---|---|
| **HTTP on the LAN** (recommended default) | Browse and play work. No home-screen install, no offline shell, no iOS lock-screen art. | None. The Mac itself still gets the full PWA, because `localhost` counts as secure. |
| **Tailscale detected → `tailscale cert`** (recommended upgrade) | The full PWA, at home and away; CarPlay over the tailnet. | The user installs Tailscale. The gateway already knows how. |
| Self-signed certificate | Each device must install and trust a profile; iOS buries the trust switch, and service workers stay unreliable. | Rejected: it trades one problem for a worse one. |

## 6. What changes for the owner's install

Nothing, unless asked. The legacy-path rule keeps `library.db`, `.env`, the
certificate and the LaunchAgents where they are; `setup.sh`, `upgrade.py`,
the runtime watch and every shell job keep working. A move into the data
directory would be a separate, deliberate step with a backup first, never a
side effect of an update.

## 7. Decisions needed before any code

1. **Audience and platform:** macOS only for the packaged app, with Linux
   staying a documented git install and Windows out of scope? *(Recommended:
   yes.)*
2. **Stage 3 at all, and when:** commit to the signed app now (US$99 a year
   plus release upkeep), or decide after Stage 2 has been used by someone
   other than the owner? *(Recommended: decide after Stage 2.)*
3. **HTTPS default:** HTTP on the LAN with Tailscale offered, as in §5?
   *(Recommended: yes.)*
4. **The scope cut in §2:** no beets, video transcoding, Immich or
   `upgrade.py` in the packaged app at first. *(Recommended: yes; each can
   return later as an advanced option.)*
5. **Name and identity:** the app's display name, a bundle identifier
   (e.g. `nl.roha.dlna-gateway`), and whether "RoHa" appears anywhere a
   stranger sees it.

## 8. Risks

- **Signing and notarisation** are the most common reason Mac app projects
  stall: certificates, entitlements, hardened runtime and a bundled Python's
  native libraries all have to agree. The Stage 3 spike exists to find this
  out cheaply.
- **Permissions on a clean Mac.** Reading `/Volumes` from a login item, local
  network access (macOS asks per app since 15) and Messages automation each
  bring a system prompt. Untested on a Mac that has never run the gateway;
  the owner's Mac granted them long ago.
- **Multicast on unfamiliar networks.** Some routers and mesh systems block
  SSDP or isolate clients. The setup page should test discovery and say so,
  rather than show an empty renderer list.
- **Support load.** A public installer invites issues from people who cannot
  read logs. Mitigation: a "copy diagnostics" button on the settings page
  (versions, bound addresses, discovered devices, the last errors, never
  library contents or secrets).

## 9. If approved, the first session

Stage 1, step 1: the data-directory function with the legacy-path rule,
every module moved onto it, tests for both layouts (a fresh install and this
one), and `gateway.log` showing which layout it picked. No visible change on
this Mac, which is the point.
