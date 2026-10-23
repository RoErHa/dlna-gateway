# Keeping the toolchain current — `tools/upgrade.py`

> Added 2026-10-23. Tool reference and flags: [TOOLS.md → `tools/upgrade.py`](TOOLS.md#toolsupgradepy--keep-the-toolchain-current-2026-10-23).

## The principle

Security fixes land upstream all the time, in Node, SQLite, Python, the pip
packages and every vendored JS library. The fixes rarely reach a running
app, because upgrading is manual, spans five package managers, and is scary.
An app should be able to answer three questions on demand:

1. **What am I actually built from?** Not what the README says: what is
   installed, and where each piece really lives.
2. **What of that is known to be vulnerable right now?** Checked against a
   public advisory database rather than remembered.
3. **Is the newest version safe for *me*?** Proven by the app's own test
   suite, with a one-step way back if it isn't.

`tools/upgrade.py` answers all three. The **"Found"** section below shows why
the second question matters: none of those findings came from a news story.

## What "the toolchain" is — and where each piece really lives

| Layer | Pieces | How it is upgraded |
|---|---|---|
| Homebrew | `python@3.x`, `sqlite`, `openssl@3`, `ca-certificates`, `ffmpeg`, `chromaprint`, `beets`, `node` | `brew upgrade` of the installed, outdated ones |
| Python packages | the `requirements.txt` closure, *including* indirect deps (h2, idna, certifi…) | a FRESH venv, resolved eagerly, inside the deliberate major-version caps → `requirements.lock` |
| Test tooling | `requirements-dev.txt` + the Playwright browsers | latest, re-pinned in place; the whole closure → `requirements-dev.lock` |
| Vendored JS | `static/vendor/hls.min.js` | npm registry tarball, **sha512-verified** against the registry's published checksum before a byte is written; stays on its major unless `--allow-major` |
| npm global | `appium` | `npm install -g appium@latest` |

Two facts that are not obvious and change what "upgrade X" means:

- **SQLite and OpenSSL are not part of Python here.** Homebrew's Python loads
  them from `/opt/homebrew/opt/sqlite` and `/opt/homebrew/opt/openssl@N` when
  it starts. *Which* OpenSSL it uses can change in a patch release (see
  below), so the tool asks `brew deps python@3.x` instead of hardcoding it. `brew upgrade sqlite` plus a gateway restart therefore patches
  the database engine, with no venv rebuild.
- **This app does not run Node.** The gateway and the PWA never execute it.
  The two Node copies on the machine are test tooling: Homebrew `node`
  (which hosts Appium), and a **private Node bundled inside the Playwright
  pip package**. That second copy is invisible to `brew` and updates only
  when `playwright` does.

## How it stays safe

- **Dry run by default.** `python3 tools/upgrade.py` reports what is
  outdated and what is known-vulnerable, then changes nothing.
- **`--apply`** runs in this order:
  1. back up `library.db`
  2. Homebrew upgrades
  3. move `.venv` aside (not deleted) and build a fresh one
  4. regenerate the lock file and test-tool pins
  5. update `hls.js`
  6. run the offline gates, then the Playwright UI suite
  7. restart the gateway and check that it answers
- **The test suite decides.** Any failure from step 3 onwards restores the
  previous venv and files and restarts the gateway on them.
- **`--rollback`** undoes a run that passed every gate but misbehaves live.
  The previous set stays in `~/dlna-gateway-backups/upgrade-prev/` until the
  next `--apply`.
- **Downgrades are never silent.** See the OpenSSL finding below.
- **The Python minor is opt-in** (`--python 3.15`). The compiled packages
  for a new Python release can lag behind it.
- **Homebrew is not rolled back.** It has no downgrade command, so it is the
  one layer the tool can move forward but not back. Old kegs are kept
  (`HOMEBREW_NO_INSTALL_CLEANUP`), partly so the running gateway does not
  lose the stdlib files it imports lazily.

## Found on 2026-10-23 — the first run

This is what the very first report said about a gateway that passed all of
its 1,529 unit tests and 293 browser tests. Green tests say the code is
right; they say nothing about the code you didn't write.

**Known vulnerabilities in installed packages (osv.dev):**

| Package | Installed | Advisories | Why it matters here |
|---|---|---|---|
| **pillow** | 12.2.0 | **~60** (GHSA + PYSEC) | Pillow decodes **cover art taken from the music files themselves** (`art_fetch_scaled`). A media file is untrusted input, so a decoder bug is reachable just by indexing a bad file. |
| **starlette** | 1.2.1 | 4 | The web framework under every request |
| **h2** / **hpack** | 4.3.0 / 4.1.0 | 2 / 2 | HTTP/2 on `:8443`, which is what the PWA uses |
| **anyio** | 4.13.0 | 4 | The async runtime under Hypercorn |
| **urllib3** | 2.7.0 | 6 | Outbound HTTP |
| **idna** | 3.11 | 2 | Hostname parsing |
| **certifi** | 2026.7.22 | 2 | The trusted root certificates |
| **pygments** | 2.19.2 | 2 | Log formatting (via rich) |

**Homebrew moving under you:**

- **`openssl@3` would have been DOWNGRADED**, from 3.6.5 to 3.5.9.
  Homebrew had repointed the formula to the 3.5 long-term-support line
  (alias `openssl@3.5`). A plain `brew upgrade` does this without asking.
  The tool marks it HELD and `brew pin`s it for the duration of the
  upgrade, because upgrading `python@3.14` would otherwise drag the
  downgrade in as a dependency. Deciding between 3.5 LTS and 3.6 is left to
  a person.
- **…and then Python left `openssl@3` altogether.** The same run's
  `python@3.14` 3.14.8 → 3.14.9 bottle re-linked `_ssl` against the new
  **`openssl@4`** (4.0.3). A *patch* release of Python moved the gateway's
  TLS onto a new *major* OpenSSL, and the guarded `openssl@3` stopped
  mattering to the gateway (it is still used by beets, certbot, krb5 and
  samba). The tool hardcoded `openssl@3` that morning; it now derives the
  list from `brew deps`. TLS was verified live afterwards (below).
- `sqlite` 3.54.0 → 3.54.1, `python@3.14` 3.14.8 → 3.14.9, `node`
  26.10.0 → 26.12.1, plus `ffmpeg`, `chromaprint` and `ca-certificates`.

**Also behind:**

- 35 pip packages, e.g. fastapi 0.136 → 0.161 and pillow 12 → 13.
- `hls.js` 1.5.17 → 1.7.3.
- `appium` 3.5.2 → 3.8.0.
- Python 3.15.0 had been released, while the venv was on 3.14.

## What `--apply` did with it — three runs, two refusals

The refusals are the point. Each one stopped a change that would otherwise
have shipped silently.

| Run | Outcome | Cause |
|---|---|---|
| 1 | **Refused**, rolled back | A bug in the tool: its sanity check read the version out of `hls.min.js` with a pattern that only fit 1.5.x. 1.7.x returns a minified variable (`var rs="1.7.3"`). The download had passed its sha512 check, but the tool would not install a file whose version it could not confirm. Fixed, with a test built from the real 1.7.3 bytes instead of a guessed format. |
| 2 | **Refused**, rolled back | **16 unit tests failed** on FastAPI 0.161. It no longer copies an included router's routes into `app.routes` and appends an opaque `_IncludedRouter` instead. The app itself served correctly (`/api/version` → 200 on both versions); only the tests' introspection broke. Fixed in `tests/test_asgi.py` with a `_flatten` helper that answers for both versions. |
| 3 | **Passed** every gate | 192/192 offline checks, the full Playwright UI suite, restart, health probe. |

Each rollback restored the previous venv and files and restarted the
gateway, which answered within seconds both times.

**Verified live after run 3**, beyond what the gates cover:

- Inbound HTTPS on `:8443` negotiates **HTTP/2** with a valid certificate
  on OpenSSL 4.0.3.
- Outbound TLS (MusicBrainz) answers 200.
- **Bit-perfect still holds:** the sha256 of the bytes served by `:8200`
  equals the sha256 of the file on disk.
- No `ERROR` or traceback in `gateway.log` since the restart, with real
  playback traffic running through it.

**After:** Python 3.14.9 · SQLite 3.54.1 · OpenSSL 4.0.3. osv.dev reports
**no known vulnerabilities** in any installed package. The one open item is
the opt-in move to Python 3.15.

## The Attention list — what to do, decide, wait for, or just know

Every report ends with one list that sorts each finding by **what a person
should do about it**:

| Kind | Meaning | Examples |
|---|---|---|
| **DO** | Safe to apply: `--apply` upgrades it and the gates prove it | upgrades available; vulnerable packages that have a fixed release |
| **DECIDE** | A person's call; nothing is wrong with doing it | a held Homebrew downgrade; Python 3.N once every package is ready |
| **WAIT** | Should **not** be done yet, with the reason | Python 3.N while a package has no wheel; a major blocked by a deliberate cap in `requirements.txt`; an hls.js major that needs an app.js review first |
| **NOTE** | Information only | a vulnerability with no fixed release yet; the rollback point being kept |

What it said on 2026-10-23, after the upgrade:

```
Attention
  DO      pip: 1 package(s)
  WAIT    Python 3.15: not yet — no 3.15 wheel for 1 package(s): watchdog
  NOTE    rollback point from 2026-10-23 13:24:10 kept in ~/dlna-gateway-backups/upgrade-prev
Attention: 1 do · 0 decide · 1 wait · 1 note
```

The DO was certifi, which published new root certificates that same
afternoon, a few hours after the upgrade. That is the argument for a weekly
check in one line.

## Dry-running a Python minor move

`python3 tools/upgrade.py --python 3.15`, without `--apply`, is the dry run.
It installs nothing. For the **latest release of every installed package**,
it asks PyPI whether a wheel exists that installs on CPython 3.15 on
Apple-silicon macOS (`wheel_fits`). Each package qualifies in one of three
ways:

- a pure wheel (`py3-none-any`)
- a wheel built for exactly cp315
- a stable-ABI wheel (`abi3`) from an older minor

The free-threaded `cp315t` builds don't count, because they're not our
interpreter. The answer is a list of blockers:

```
Python 3.15 readiness (venv is on 3.14):
  ✗ watchdog 6.0.0 (source only — pip would have to COMPILE it)
```

**"No wheel" is a risk, not a verdict — so the dry run now TESTS it.** The
first version stopped at the line above and called 3.15 a WAIT. The user
then pointed out that watchdog runs fine on 3.15, and both were true at
once: PyPI's newest watchdog is still 6.0.0 (2024-11-01), with macOS wheels
only up to cp313, yet the 3.14 venv had run it all day. pip had been
**compiling it from source on every `--apply`** (its `WHEEL` file says
`Generator: setuptools`, tag `cp314-cp314-macosx_26_0_arm64`, a local
build). Built by hand on 3.15.0, its native `FSEventsObserver` saw a live
file event.

So when the target Python is installed, `rehearse_builds` builds each
source-only package in a throwaway venv. One that compiles stops being a
blocker and is named in the verdict instead:

```
  … building watchdog 6.0.0 from source on 3.15 (throwaway venv)
  ✓ watchdog 6.0.0 — no wheel, but VERIFIED to compile here
Attention
  DECIDE  Python 3.15: READY (watchdog compiles from source) — move with `--apply --python 3.15`
```

Lesson for any port: a dry run that can cheaply *try* the risky thing
should, rather than report the risk.
(`pip install --dry-run --python-version 3.15` was tried first. It stops at
the first failure, so it cannot produce the list.) The regular report runs
the same check whenever a newer minor exists. While it is blocked, it is a
**WAIT**; the week every package is ready, it becomes **DECIDE: READY**, and
that change sends a notification.

## The weekly check

`upgrade-check.sh`, run by the `com.roha.dlna-upgrade-check` LaunchAgent
(Saturdays 05:00, a slot that collides with neither the backup on Sunday
03:30 nor the cert renewal on Monday 04:30), runs `tools/upgrade.py
--check`. It appends the full report, Attention list included, to
`upgrade-check.log`.

It only reports and never applies. An upgrade restarts the gateway and
moves Homebrew, so applying one stays a decision a person makes.

**When it notifies.** A macOS notification is posted when there is any
**DO** item, or a **DECIDE/WAIT item that was not on last week's list**.
Last week's list is kept in `~/dlna-gateway-backups/upgrade-attention.json`.
This rule was earned on day one. The first version notified on *any*
finding, and its very first scheduled run fired on "Python 3.15 is
available": true, opt-in, and going to stay true every Saturday until acted
on. A notification that repeats the same thing every week trains you to
ignore it, and then it fails on the week that matters. So an unchanged WAIT
or DECIDE stays in the log and the notification stays quiet. A change of
verdict, such as Python going from WAIT to READY, counts as new.

The notification reads e.g. *"1 new item(s). 1 do · 0 decide · 1 wait ·
1 note — see the Attention list in upgrade-check.log"*.

## The fourth run, and the lock it exposed

A same-day `--apply` picked up the certifi release the Attention list had
flagged. It passed every gate, and **`git status` came back empty.**
certifi is used only by `requests` and `selenium`, which are test tooling,
so it was in neither `requirements.lock` (runtime only) nor
`requirements-dev.txt` (direct dev tools only). The venv had moved and
nothing tracked had recorded it, so a fresh clone would test with whatever
was newest that day.

The fix is **`requirements-dev.lock`**: `-r requirements.lock`, then the
dev-only part of the closure (26 + 26 pins), generated by
`tools/regen_lock.py` and gated by `tests/test_lock_sync.py`. Before it was
committed, it was proven by installing it into an empty venv: `pip check`
was clean, and `pip freeze` matched the working `.venv` package for
package. The gate was then shown to FAIL on a hand-drifted pin.

(That certifi update never reached the running gateway: the gateway's
outbound TLS trusts the roots from OpenSSL and Homebrew's
`ca-certificates`, not certifi.)

## A bug found by the port, fixed here before it bit

The port to epub-reader found a bug in this tool. **Its first real
`--apply` was refused, for a reason that was the tool's own:** the README
version strip was rewritten only *after* the gates, so its pinned-version
test compared the old strip with the new locks and failed. Every upgrade
that moved a pin would have been refused. This gateway had the same
ordering; its two runs since the strip was added moved no pin, so it never
showed. Inside the snapshot, `--apply` now runs `GUARDED_STEPS = venv,
hls, versions, gates` in that order (held by a test), and the snapshot
restores `README.md` too.

The same epub-reader run found a second bug, which this gateway already
avoided: a rollback must REINSTALL the restored Playwright's browsers,
because the upgrade's install let Playwright garbage-collect them.
`restore_previous` has done that since day one. The browser cache
(`~/Library/Caches/ms-playwright`) is shared by both apps.

## Open items after the first run (2026-10-23)

- **Python 3.15**: **DONE** (same day, `--apply --python 3.15`). It was
  briefly WAIT, until the watchdog rehearsal above showed that the one
  "blocker" compiles. The move passed every gate first time, and the
  package set came out **identical** to 3.14's: only the interpreter line
  in the two lock headers changed. Verified live afterwards:
  - HTTP/2 + TLS on `:8443`, and outbound TLS (MusicBrainz → 200)
  - bit-perfect sha256 on two random FLACs
  - watchdog running the native `FSEventsObserver`, with clean rescans of
    music (27,045) and audiobooks (11,753)
  - Pillow 13 scaling `/art` (256 → 256×256; 1024 → the 300×300 original,
    never upscaled)
  - zero errors in `gateway.log` since the restart

  `python3` on this machine is now 3.15. Tools that import the app's test
  stack must be run with `.venv/bin/python`.
- **`openssl@3` 3.6.5 → 3.5.9** is still held, but no longer the gateway's
  concern. Python now links `openssl@4`, so the report (which follows
  `brew deps python@3.x`) no longer lists it. Only beets, certbot, krb5 and
  samba use `openssl@3`. Whether to take Homebrew's 3.5 LTS for those is a
  machine-wide decision, not this app's.
- **The rollback point** (now the 3.14 venv, ~276 MB) is kept in
  `~/dlna-gateway-backups/upgrade-prev/` until the next `--apply` replaces it.

## Bringing this to another app

The design is portable; only the layer table is per app. To port it:

1. **Inventory where each piece really lives.** Check what the runtime
   links (`otool -L`), what packages bundle internally (Playwright's Node),
   and what is vendored by hand into `static/`.
2. **Ask osv.dev** (`POST https://api.osv.dev/v1/querybatch`) for every
   installed package. It covers PyPI, npm, crates.io, Go and more, though
   not Homebrew.
3. **Upgrade into something you can swap back**: move the old environment
   aside rather than upgrading it in place.
4. **Let the app's own test suite decide**, then restart and probe health.
5. **Never take a silent downgrade.**
6. **End with an Attention list** (DO / DECIDE / WAIT / NOTE). A finding
   is only useful once it says what to do about it, including "not yet".
7. **Schedule the report, not the upgrade**, and notify only on what is
   actionable or *new*.
