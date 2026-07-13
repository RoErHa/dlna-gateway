# 2.0 Cutover — the LaunchAgent (plist draft)

> **⚠ HISTORICAL (superseded 2026-07-13).** The plist below shows config
> keys (`GW_UDN`, `LOCALFS_*`, …) inside `EnvironmentVariables` — that
> was the cutover-era layout. **All configuration now lives in `.env`**
> (see `.env.example`); the live plist carries only PATH + the launch
> command. Never add config keys back to the plist: plist env OVERRIDES
> `.env`.

Companion to **`CUTOVER_RUNBOOK.md`**. This is the 2.x `com.roha.dlna-gateway`
LaunchAgent that **adopts 1.x's identity** so the Naim, CarPlay/Amperfy,
Subsonic and the PWA keep their existing connections after cutover.

It reuses the **same `Label`** (`com.roha.dlna-gateway`) and the **same log
paths** (`/tmp/dlna-gateway.{out,err}`) as 1.x, so every `launchctl …
com.roha.dlna-gateway` command in the runbook works unchanged. Installing it
overwrites the 1.x plist — which is why **step 0 of the runbook backs the old
one up to `…plist.1x-bak`** (that backup is what Rollback restores).

## What's different from 1.x

| | 1.x | 2.x (this plist) |
|---|---|---|
| Process | `.venv/bin/python dlna_gateway.py` (stdlib server) | `.venv/bin/hypercorn dlna_asgi:app` (ASGI) |
| WorkingDirectory | `…/dlna-gateway` | `…/dlna-gateway-2.0` |
| TLS | gateway self-wraps the socket | **Hypercorn owns TLS** via `--certfile/--keyfile` (negotiates HTTP/2 over ALPN) |
| Binds | `:8765` plain + `:8443` TLS in-process | `--insecure-bind :8765` (plain) + `--bind :8443` (TLS) |
| Device `/gw/*` + LocalFs | in-process | in-process, driven by `GATEWAY_PORT` / `LOCALFS_PORT` env |

**Ports adopted from 1.x:** main `:8765` (plain) + `:8443` (TLS), device `/gw/*`
`:8770`, LocalFs `:8200`. Identity: `GW_UDN=uuid:dlna-gateway-iina-8765`, name
`DLNA Gateway (IINA)`. (Those are already the app defaults in `api_upnp.py` and
`dlna_localfs_wiring.py`; they're pinned here explicitly so the identity can
never drift from the runbook's "Decisions (locked in)" table.)

**Cert:** points directly at the 1.x tailscale cert
(`…/dlna-gateway/ronsmacmini.tail5be6ad.ts.net.{crt,key}`), which keeps its
existing weekly auto-renewal (`renew-cert.sh` + `com.roha.dlna-cert-renew`).
2.x picks up a renewed cert on its next restart — no second cert owner.

---

## The plist

```xml
<!--
  com.roha.dlna-gateway.plist  (2.x cutover — adopts 1.x identity)
  ───────────────────────────────────────────────────────────────
  Runs the 2.0 ASGI gateway (Hypercorn + dlna_asgi:app) under 1.x's label,
  ports, UDN and log paths. Installed at cutover step 3 (see CUTOVER_RUNBOOK.md).

  INSTALL (runbook step 3):
    launchctl bootout gui/$(id -u)/com.roha.dlna-gateway 2>/dev/null   # stop 1.x
    cp com.roha.dlna-gateway.plist ~/Library/LaunchAgents/             # this 2.x one
    launchctl load   ~/Library/LaunchAgents/com.roha.dlna-gateway.plist

  ROLLBACK (runbook): restore ~/Library/LaunchAgents/com.roha.dlna-gateway.plist.1x-bak
-->
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.roha.dlna-gateway</string>

    <key>WorkingDirectory</key>
    <string>/Users/ronhamersma/dlna-gateway-2.0</string>

    <!-- Hypercorn owns TLS. --bind = TLS (8443, HTTP/2 via ALPN);
         --insecure-bind = plain HTTP (8765). The device /gw/* server (:8770)
         and the LocalFs file server (:8200) are started IN-PROCESS by the app
         (driven by GATEWAY_PORT / LOCALFS_PORT below) and ALWAYS stay plain
         HTTP — the Naim can't do HTTPS. -->
    <key>ProgramArguments</key>
    <array>
        <string>/Users/ronhamersma/dlna-gateway-2.0/.venv/bin/hypercorn</string>
        <string>dlna_asgi:app</string>
        <string>--bind</string>
        <string>0.0.0.0:8443</string>
        <string>--insecure-bind</string>
        <string>0.0.0.0:8765</string>
        <string>--certfile</string>
        <string>/Users/ronhamersma/dlna-gateway/ronsmacmini.tail5be6ad.ts.net.crt</string>
        <string>--keyfile</string>
        <string>/Users/ronhamersma/dlna-gateway/ronsmacmini.tail5be6ad.ts.net.key</string>
    </array>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <!-- MUST be inside SoftResourceLimits or launchd silently ignores it.
         The Hypercorn threadpool + LocalFs scan otherwise hit the default 256
         FD limit → EMFILE → sqlite "unable to open database file".
         The app also raises this in-process (raise_fd_limit) as a backstop;
         the gateway logs "Open-file soft limit already 8192" when both agree. -->
    <key>SoftResourceLimits</key>
    <dict>
        <key>NumberOfFiles</key>
        <integer>8192</integer>
        <key>NumberOfProcesses</key>
        <integer>512</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/tmp/dlna-gateway.out</string>
    <key>StandardErrorPath</key>
    <string>/tmp/dlna-gateway.err</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>

        <!-- Identity adopted from 1.x (locked-in decisions) -->
        <key>GW_UDN</key>
        <string>uuid:dlna-gateway-iina-8765</string>
        <key>GW_NAME</key>
        <string>DLNA Gateway (IINA)</string>
        <key>APP_VERSION</key>
        <string>2.0.0</string>

        <!-- LocalFs: same music root + 1.x's :8200 so stream URLs are
             byte-identical to 1.x's (sha1(rel_path) over the same files) →
             copied play_counts/lyrics/metadata_overrides match after the
             step-5 force rebuild. -->
        <key>LOCALFS_MUSIC_ROOT</key>
        <string>/Volumes/SAMDATA/Music</string>
        <key>LOCALFS_PORT</key>
        <string>8200</string>

        <!-- Device tier: plain-HTTP /gw/* for the Naim + the SSDP advert port -->
        <key>GATEWAY_PORT</key>
        <string>8770</string>

        <!-- Subsonic (CarPlay/Amperfy): SUBSONIC_USER / SUBSONIC_PASSWORD come
             from .env (gitignored), NOT from this plist. The gateway loads
             .env via dlna_config; plist EnvironmentVariables OVERRIDE .env, so
             these keys are deliberately absent — .env is the single source for
             all names / emails / passwords. -->
    </dict>
</dict>
</plist>
```

---

## Pre-install sanity (before runbook step 3)

```bash
# hypercorn present in the 2.x venv?
ls -l /Users/ronhamersma/dlna-gateway-2.0/.venv/bin/hypercorn
# cert + key both exist and not near expiry? (runbook step 0 also checks)
openssl x509 -enddate -noout \
  -in /Users/ronhamersma/dlna-gateway/ronsmacmini.tail5be6ad.ts.net.crt
# plist parses?
plutil -lint ~/Library/LaunchAgents/com.roha.dlna-gateway.plist   # after copy
```

## Verify after load (runbook step 3 confirmations)

```bash
launchctl list | grep dlna                 # loaded + a PID, no rapid restarts
tail -f /Users/ronhamersma/dlna-gateway-2.0/gateway.log
```

Look for: `Open-file soft limit already 8192`, `FD monitor started`, LocalFs on
`:8200`, device server `/gw/*` on `:8770`, and **no** `Servers offline — subnet
scan`. Then:

```bash
curl -sk https://127.0.0.1:8443/api/servers | python3 -m json.tool   # TLS + h2 alive
curl -s  http://127.0.0.1:8765/api/servers  >/dev/null && echo "plain :8765 ok"
```

> **One gateway at a time.** Don't run `run-2.0.sh` / `run-2.0-asgi.sh` while
> this LaunchAgent is loaded — they'd double-announce on SSDP and clash on the
> shared ports. Use `launchctl kickstart -k gui/$(id -u)/com.roha.dlna-gateway`
> to restart the agent instead.
