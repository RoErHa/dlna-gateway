# Fixes

A rolling log of non-obvious bugs: what was actually wrong, how it was
proven, and what would re-introduce it. Newest first, each entry headed by
the commit that carries the fix and the date it landed (`<sha> — YYYYMMDD`).

This is the "why", not the "what" — `git log` already has the what. An entry
earns its place here when the diagnosis took longer than the patch, or when
the symptom looked like something it wasn't.

> **Only the three most recent entries are kept.** Add a new one at the top,
> then run `python3 tools/rotate_fixes.py --apply` to drop whatever fell off
> the end. Nothing is lost — every rotated entry stays reachable in git
> history at the commit it names, which is why the sha is part of the
> heading and not decoration. Rotating by hand is fine too; the tool exists
> so the window is enforced rather than remembered.

---

## 8d74d5e — 20261103 — a renderer switched off mid-send killed its queue's monitor thread

**Symptom.** Found by the new `chaos.py --fake-renderer`, not by a user: the
first run passed, yet `gateway.log` held three `RendererQueue.start crashed
for Chaos test (fake)` tracebacks ending in `ConnectionResetError: [Errno 54]`,
logged just after the fake renderer was shut down.

**What was actually wrong.** `/api/render_queue` answers 200 and starts the
queue on a background thread, so a few starts were still sending when the run
ended and the fake went away. A vanished renderer should have been a
`✗ SEND FAILED`. It wasn't, because `avtransport_send` carried its OWN copy of
the SOAP request — connect, read, status check — with no `except`. Every
other AVTransport call goes through `_av_soap`, which catches; this one was
written separately and never got that. One line proved it:
`avtransport_send('http://127.0.0.1:1/av', …)` → `RAISED ConnectionRefusedError`.

**Why it matters beyond the test.** The 2026-05-20 design for a renderer that
goes off mid-track (CLAUDE.md, "Why the monitor watchdog matters") is: the
watchdog advances, the send fails, five failures abort the queue cleanly.
In reality the first send to the dead renderer RAISED inside the monitor
thread, which has no catch-all — the thread died, its traceback went only to
`/tmp/dlna-gateway.err`, and the queue was never marked stopped.

**Why nothing caught it.** `TestRendererQueueSendFailure` patched
`avtransport_send` to `return_value=False`. The mock asserted the behaviour
the function was SUPPOSED to have, so the test could never see that it
didn't. The two new tests use the real function against a closed port, and
both fail on the old code.

**Fix.** `avtransport_send` now goes through `_av_soap` (never raises). To
keep the one thing the copy did better — logging a renderer's SOAP fault when
it refuses a track — `_av_soap` returns the fault body alongside
`HTTP <status>`; `avtransport_get_position` now checks `err` so a fault body
is never parsed as a position. Deleting the copy also took
`dlna_avtransport.py` from 410 back to 391 lines.

**Don't.**
- Don't hand-roll another SOAP request in `dlna_avtransport.py`; add a
  caller of `_av_soap`. A second copy is how this one lost its `except`.
- Don't test "a failing renderer" by mocking the transport function to
  return False — mock below it, or point the real function at a closed port.
- Don't forget the fake renderer before the last queue starts have settled
  (`chaos._cleanup_fake` waits for quiet first); forgetting mid-burst is what
  exposed this, and would otherwise leave orphan queues polling a dead port.

---

## 4846067 — 20261103 — music dead, radio fine: brew deleted the Python we were running

**Symptom.** From the evening of 2026-10-31 the PWA skipped every track
(`MediaError code=4`, a whole album in 14 s) while internet radio played
normally. Nothing changed until a manual restart on 11-03 fixed it at once.

**What the logs said.** `gateway.log` at 21:59:18: every `:8200` request
ended `RemoteDisconnected: Remote end closed connection without response`;
`/tmp/dlna-gateway.err` filled with `:8200` tracebacks (`Errno 57`) whose
frames point into `Cellar/python@3.15/3.15.0_1/…` — with no source lines,
because those files no longer existed. From 22:28, every video scan:
`[Errno 1] Operation not permitted: '/Volumes/SAMDATA/GWMovies'` — the disk
was mounted; macOS was refusing *this process*. That warning was the only
line in the whole 11-02 log.

**Cause.** `Cellar/python@3.15/3.15.2` has mtime **21:57:43** on 10-31: a
brew upgrade (glib and clamav moved at the same minute — not `upgrade.py`)
installed 3.15.2 and cleaned up the 3.15.0_1 keg the gateway had been
started from 26 minutes earlier. Radio survived because its relay lives in
the already-loaded hypercorn process and reads nothing from SAMDATA. The
EPERM mechanism (macOS judging file access against a binary no longer on
disk) is inference; the trigger and timing are not.

**Fix.** `dlna_runtime_watch` fingerprints interpreter, stdlib and venv
site-packages at boot and, on two consecutive mismatches with a launchable
venv `python3` to restart into, exits the worker **75**. Not 0: hypercorn's
master respawns a worker that exits 0 — from its own deleted interpreter.
Non-zero shuts the master down and launchd `KeepAlive` starts the new keg.

**Don't.** Don't make it exit 0, and don't drop the `replacement_ready`
check — exiting with nothing to restart into is a launchd respawn loop.
And don't drop the `.maintenance.pid` deferral: `upgrade.py --apply`
renames `.venv` aside and gates a new one for minutes — the watch would
otherwise restart the gateway onto it before the gates have spoken.

## 15f0922 — 20261023 — a TLS failure that wasn't renewal, and two renewal bugs that were

**Symptom.** epub-reader failed to start once, on 2026-08-15 13:37: hypercorn's
`load_cert_chain` raised `FileNotFoundError` on the Tailscale certificate it
shares with this gateway. The natural suspect was a renewal racing the start.

**What it actually was.** Not renewal. `cert-renewal.log` shows the last
renewal on 07-27 and the next on 09-21, and the certificate on disk on 08-15
was valid until October. epub-reader's own log shows 12:44 and 12:49 starting
on **http**, 13:37 its first-ever **https** start (failed), and 13:45 https
working ever since, with no commit in between. TLS was being switched on in
the untracked `.env`, and the first attempt named a file that was not there.
OpenSSL does not say which file, so the exact typo is not recoverable.

**What the log showed instead.** Two real defects:

- **2026-07-20: a "renewal" that renewed nothing.** `tailscale cert` printed
  *Public cert unchanged* with 26 days left, and the script logged
  `OK renewal complete; new expiry Aug 16` (the same expiry) and restarted
  the gateway for nothing. It renewed a week later. Harmless that time; the
  script would have said the same with 2 days left.
- **The second service was never restarted.** epub-reader loads these same
  `.crt`/`.key` files, but the script only kickstarted the gateway. After the
  09-21 renewal epub-reader kept serving the OLD certificate (expiring 10-18)
  until a restart happened along. Its last restart before expiry was
  10-17 18:04, about **7 hours** before iPhones and iPads would have refused
  it. Saved by development churn, which is not a mechanism.

**Fix.** Compare the expiry before and after `tailscale cert`: unchanged →
WARN, restart nothing. After a real renewal, restart every label in
`CERT_RELOAD_LABELS` (`.env`); one failure does not stop the rest and sets
the exit code.

**The trap the test found.** `renew-cert.sh` loads `.env` by *sourcing* it,
so `CERT_RELOAD_LABELS=a, b` is not an assignment of `a, b`: bash assigns
`a,` and runs `b` as a command, and the variable is silently unset. That key
is read from the file directly.

**Guard.** `tests/test_renew_cert.py` runs the real script in a sandbox: real
`openssl` on throwaway certs, fake `tailscale`/`launchctl` recording calls.
Three of its five tests fail on the old script.

**Don't re-introduce.** Any new service that serves this certificate goes
into `CERT_RELOAD_LABELS`. "The command exited 0" is not evidence of a
renewal; the expiry date is.

**Follow-up, the same day: `.env` must never be sourced.** The first live
`./renew-cert.sh --force` confirmed the WARN path (*same certificate*,
nothing restarted) and printed `.env: line 48: syntax error near unexpected
token '('`. `GW_NAME=DLNA Gateway (IINA)` is valid for the app's loader and
invalid shell, and `.` abandons the rest of the file on a syntax error: every
key after line 48 had been silently missing for this script, on every
scheduled Monday run, into a `/tmp` stderr nobody reads. It still worked only
because `TAILSCALE_CERT_HOST` sits on line 28. Worse than fragile: sourcing
*executes* the file, so `KEY=x word` runs `word`. The script now reads each
key with `env_get` (environment first, then the last matching `.env` line,
quotes stripped). A test asserts that a `KEY=x touch <file>` line creates no
file; it failed on the sourcing version.

---
