# Packaging the gateway into a click-through installer (macOS / Linux / Windows)

> **Status: PARKED / freezer.** A feasibility plan + runbook for shipping the
> gateway as a native, double-click installer that a non-technical user can run
> — a wizard asks a few questions, then it installs + autostarts with no
> software knowledge required. Not started; pick up if/when wanted.

**Verdict: feasible, but a real project (weeks, not days).** Freezing a Python
server into a native installer is well-trodden; the effort is in four
cross-cutting problems, not the packaging itself.

---

## The 4 hard problems (these dominate the work)

| Problem | Why it matters for a no-knowledge user | Approach |
|---|---|---|
| **Code signing / notarization** | Unsigned apps hit Gatekeeper (macOS) + SmartScreen (Windows) blocks a layperson won't bypass. | Apple Developer ID ($99/yr) + `notarytool`; Windows Authenticode (paid cert, or Azure Trusted Signing). Unavoidable for polish. |
| **TLS for phones/TVs** | Service Workers, PWA-install, and iOS lock-screen art **require HTTPS** off-`localhost`. A phone on `http://<lan-ip>:8765` loses the PWA. | **DECISION DEFERRED** — three paths kept open below. |
| **Autostart as a background service** | Must run headless + survive reboot, per-OS. | macOS LaunchAgent (have it), Linux systemd-user unit, Windows Service (NSSM/pywin32) or logon Scheduled Task. |
| **Firewall + multicast (SSDP/mDNS)** | First run triggers a firewall prompt; SSDP discovery needs LAN multicast (some routers block it / AP isolation). | Installer pre-adds firewall rules (`netsh advfirewall` / `socketfilterfw`); document the multicast caveat. |

### TLS-for-mobile — decision deferred, all three documented
- **Tailscale (recommended if chosen):** auto-detect/recommend Tailscale → trusted HTTPS, zero cert work (the app already owns a `tailscale cert`). User installs Tailscale once. Plain HTTP LAN fallback.
- **Bundled self-signed:** app serves HTTPS on the LAN with a shipped self-signed cert; user taps "trust" once per device (some PWA features stay flaky).
- **Plain HTTP only:** simplest install; desktop + basic phone/TV playback works, but PWA-install / offline / lock-screen art are lost on remote devices.

Pick at Phase 0; the rest of the runbook is TLS-choice-agnostic.

---

## Packaging options (the easy part)

| Option | What | Pros | Cons | Verdict |
|---|---|---|---|---|
| **A. PyInstaller + per-OS installer** (Inno/WiX→MSI, pkgbuild→.pkg/.dmg, AppImage/.deb) | Freeze Python+app to one binary, wrap per OS | Mature, full control | Per-OS tooling, big bundles, wire signing yourself | ✅ Solid default |
| **B. Briefcase (BeeWare)** | Purpose-built Python→native packager | One tool (dmg/msi/AppImage/deb), signing hooks | Tuned for GUI apps; this is a server | ✅ Strong contender |
| **C. Docker + "install Docker" wrapper** | Ship a container | Clean deps | **Breaks SSDP/mDNS multicast** (no host networking on mac/Win Docker Desktop); Docker is a heavy ask | ❌ Rejected |
| **D. Native tray/menubar shell** (Tauri/menubar) running the bundled server | GUI wrapper = control surface + form | Best UX (start/stop/status/Open) | Most work (GUI + server) | ⭐ Best end-state (phase 2) |

**The "form":** prefer an **app-served `/setup` web page** over a native installer wizard — it reuses the existing web stack, is cross-platform for free, and is friendlier. Installer stays near-silent; first launch opens the browser to `/setup`.

**Setup questions** (map to today's `.env`/config): music folder (required) · friendly name (auto-generate UDN) · TLS choice · Subsonic password (optional, CarPlay) · contact email (optional, MusicBrainz) · start-at-login? Ports stay defaulted (advanced-only).

---

## Runbook (phased)

- **Phase 0 — Decide:** target OSes · signed-or-not (budget) · TLS default (above).
- **Phase 1 — Make it packageable (most of the code work):**
  - Move config off `.env`/launchd into a per-OS **user-data dir** (`~/Library/Application Support/DLNAGateway`, `%APPDATA%\DLNAGateway`, `~/.config/dlna-gateway`); `library.db` / `config.json` / logs / `art_cache` live there.
  - Build the `/setup` first-run web wizard (writes config, kicks the first scan).
  - Make the **no-Tailscale TLS** path work (per Phase-0 choice).
  - Single-instance guard + clean shutdown; don't assume the repo layout.
- **Phase 2 — Freeze:** PyInstaller/Briefcase per OS; bundle `static/`; treat `fpcalc` as optional (leave beets out — it's an advanced manual workflow).
- **Phase 3 — Installer + autostart:** .pkg/.dmg (LaunchAgent), MSI/Inno (Windows Service via NSSM/pywin32), AppImage + .deb (systemd-user unit).
- **Phase 4 — Sign & notarize** (mac + Windows) + pre-add firewall rules.
- **Phase 5 — First-run UX:** install → autostart → browser opens to `/setup` → then the app.
- **Phase 6 — Updates & uninstall:** "new version available" check (or Sparkle/Squirrel/winget); clean uninstaller that also removes the user-data dir on request.

### Effort / risk notes
- Biggest non-code risks: **signing certs** (cost + Apple/MS account setup) and **TLS-for-mobile** UX. Everything else is mechanical.
- Cross-OS **testing** matrix (3 OSes × signed installer × autostart × firewall × multicast) is the long tail.
- Keep the **server code unchanged** as much as possible — the packaging lives in a `packaging/` dir (spec files, installer scripts, service units) + the Phase-1 config refactor.
