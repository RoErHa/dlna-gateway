# Hardening timeline

The recommendations from the November 2026 review: things the gateway should
do that nobody had asked for yet, mostly security. Each has a target date,
what "done" means, and a box to tick.

**How to use this file.** Tick an item (`- [x]`) **in the same commit that
ships it**, add the date and the commit to its line, and record anything
learned in the item's own notes. When the item is a security control, its
full write-up goes into
[SECURITY_POSTURE.md](SECURITY_POSTURE.md) as a numbered entry, as #1 did.
Dates are targets, not promises. Move one if something more important
comes up, and note why.

## At a glance

| # | Item | Target | Status |
|---|---|---|---|
| 1 | Browser request guard (CSRF + DNS rebinding) | 2026-11-04 | ✅ done 2026-11-04 |
| 2 | Off-disk copy of the library backups | 2026-11-07/08 | ✅ done 2026-11-08 (Time Machine to the new WD-SERVER disk) |
| 3 | Alerts when something goes wrong | 2026-11-18 | ✅ done 2026-11-04 (text message via Messages; live once `ALERT_SMS_TO` is set) |
| 4 | Security headers on the web app | 2026-11-25 | ◐ built 2026-11-04, CSP report-only; enforce ~2026-11-11 if the log stays clean |
| 5 | Optional login for the web app | — | ⏸ deferred 2026-11-04 (see §5) |
| 6 | Rate limiting on Subsonic logins | 2026-12-16 | ✅ done 2026-11-04 (Amperfy + CarPlay verified live) |

**Decisions (2026-11-04).** #2 is done by the owner over the weekend of 7–8
November, depending on when the new disk arrives. #6: build — built the same
day. #5: deferred — see its section. No decision is open.

Order: #1 first because it was the only one where a stranger could act on
the system. #2 next because it is cheap and its failure mode, losing every
playlist and favourite, cannot be undone. #5 is the largest item.

---

## 1. Browser request guard — CSRF and DNS rebinding

- [x] **Done 2026-11-04.** `dlna_asgi_guard.py`, SECURITY_POSTURE item 13.

**Why.** The access control was "whoever can reach it on the LAN or the
tailnet". That missed the owner's own browser: any web page opened on a LAN
device could delete playlists via `<img src=".../api/playlist/delete?id=1">`,
start playback or change volume with a `text/plain` POST, and, with DNS
rebinding, read the whole API.

**What was built.**
- A Host allowlist: IP literals always pass, plus known names and
  `GATEWAY_ALLOWED_HOSTS`.
- `Sec-Fetch-Site: cross-site` is refused unless it is a GET navigation.
- An `Origin` check on writes when there is no Sec-Fetch-Site.
- Every write moved from GET to POST.

**Learned.** Browsers send `Sec-Fetch-*` only to HTTPS. On plain `:8765` the
`Origin` rule and the GET→POST change do the work, which was measured in
real Chromium and WebKit, not assumed. Side effect: `beets_enrich.py
--reindex`'s POST to `/api/index/rebuild` had been failing silently and now
works.

---

## 2. Off-disk copy of the library backups — target 2026-11-07/08

- [x] **Done 2026-11-08 by the owner: Time Machine, to the new WD-SERVER
  disk.** Checked the same day: `tmutil destinationinfo` names WD-SERVER,
  there are two backups that day (08:17 and 10:13), and `tmutil isexcluded`
  reports both `library.db` and `~/dlna-gateway-backups` as included. The
  local weekly snapshot ran at 03:30 the same morning (`integrity ok`,
  38,881 tracks), so every verified snapshot now also reaches a second
  device. Time Machine also covers the rest of the Mac. The script-based
  criteria below were the spec for doing it inside `backup-library.sh`;
  that route was not needed. **Limit:** Time Machine does not alert when
  backups stop (a disk that is unplugged or full only shows up in
  System Settings), and the disk is in the same room as the Mac.

**Why.** `backup-library.sh` writes its weekly snapshots to
`~/dlna-gateway-backups`, on the same internal SSD as `library.db`. That
protects against corruption and mistakes, not against a dead SSD, theft or
ransomware. Checked 2026-11-04: **Time Machine has no destination
configured**, so nothing else copies them either. Playlists, favourites,
overrides, resume positions and a night's worth of MusicBrainz lookups live
only in that file.

**Done when:**
- every verified snapshot is also copied to a second device;
- the copy is checked (`PRAGMA integrity_check` and a non-zero `tracks`
  count) before it counts, and the second location has its own retention;
- a missing or unmounted destination is logged loudly and makes the job exit
  non-zero. It must never pass silently, and it must never stop the local
  snapshot;
- the script stays bash 3.2-compatible and `tests/test_backup*.py` covers the
  copy, the verification and the missing-destination path.

**The owner does this over the weekend of 7–8 November** and
decides the destination then. The options: `/Volumes/SAMDATA` (always
attached, but in the same room and on the same power), another machine over
the tailnet, or a cloud bucket. Setting up Time Machine would also cover
this, and the rest of the Mac.

---

## 3. Alerts when something goes wrong — target 2026-11-18

- [x] **Done 2026-11-04**, ahead of target. `dlna_alerts.py`,
  `dlna_alerts_sms.py`, `tests/test_alerts.py` (39). Live as soon as
  `ALERT_SMS_TO` is set in `.env` and macOS has allowed the gateway to
  control Messages. **Verified live 2026-11-04:** the CLI test text and an
  alert raised by the running gateway itself (a deliberately refused
  request, sent from the launchd process ~6 s later) both arrived on the
  owner's phone. Not yet seen live: a text from one of the weekly launchd
  jobs, which may need its own one-time Automation grant. The log shows
  `could not text … (-1743)` if so.

**What was built.**
- `dlna_alerts.alert(kind, …)` queues (bounded, one daemon sender), is
  rate-limited per kind (1 h), and never raises, so it can be called from
  any path, including the request guard.
- Raised by: the runtime watch when no replacement Python is installed
  (half-alive, now); a self-restart, reported by the NEXT process at boot from
  a marker, because a process whose Python was deleted cannot be trusted to
  make a TLS call; a restart loop (5 boots in 10 minutes); a library root
  still missing after an hour; a refused browser request (low priority); the
  certificate under 14 days; and a failed weekly backup, certificate renewal
  or upgrade check, through the CLI `python3 dlna_alerts.py <kind> <title>
  <message>` from each script's failure path.
- The test suite cannot reach the phone: `tests/__init__.py` and
  `run_all.py` set an empty `ALERT_PUSH_URL`, which `.env` never overrides.

**Learned.** CLAUDE.md said the gateway WARNs when the certificate has under
14 days left. That check (`_warn_if_cert_expiring_soon`) left with the stdlib
TLS server in `baa2c0c`; the paragraph had described nothing for a year. It
is back, as `dlna_alerts.check_cert`, at boot and every 6 h.

**Watch out for.** Give the number with a `+` and the country code. It was
first entered as `0316…`, which Messages would read as a Dutch landline
(area code 0316) instead of the intended `+316…` mobile number.

**Limit.** A gateway that dies before any app code runs (the 2026-09-20
`RLIMIT_NPROC` respawn loop failed inside hypercorn's own startup) never
reaches `on_boot`, so that loop is not reported. The weekly jobs are
unaffected.

**Why.** The gateway now detects its own failures (a stale Python, an
unmounted volume, a dead renderer, a refused browser request) but only writes
them to `gateway.log`, which nobody reads. The 2026-10-31 incident ran half
alive for 2.5 days for exactly that reason.

**Done when** a notification reaches the owner for:
- the runtime watch firing, or a restart loop;
- a library root still waiting after an hour;
- a failed weekly backup, certificate renewal or upgrade check;
- (optionally) a burst of `guard ✗ refused` lines, which means someone is
  probing.

And:
- each event kind is rate-limited, so one problem gives one notification,
  not one a minute;
- the channel is configured in `.env` and absent means off;
- a failing notifier never affects playback, and its own failure is logged;
- `tests/test_alerts.py` covers the rate limit and the off-by-default path.

**Decided 2026-11-04: a text message, sent by this Mac's Messages app.**
First built as an ntfy push, which was a misunderstanding: the owner wanted
SMS. Messages was chosen over an SMS provider such as Twilio because it stays
local, with no account and no third party. The one thing it cannot report is
the Mac itself being down, which was accepted: that shows itself the moment
you try to use the gateway. Settings: `ALERT_SMS_TO` (the number) and
`ALERT_SMS_SERVICE` (SMS first, iMessage as fallback).

The ntfy push was kept as a second, optional channel for installs without a
Mac signed in to Messages. Every detail lives in `.env` (and is documented,
empty, in `.env.example`), never in code or the plist:
- `ALERT_PUSH_URL`: the endpoint to POST to, e.g. an ntfy topic URL
  (`https://ntfy.sh/<long-random-topic>` or a self-hosted server). A public
  ntfy topic is readable by anyone who guesses its name, so the topic must be
  unguessable, or the server private;
- `ALERT_PUSH_TOKEN` (optional): a bearer token for a protected topic or
  server;
- unset `ALERT_PUSH_URL` means alerts are off.

The message carries what happened and when, never library content or
secrets.

---

## 4. Security headers on the web app — target 2026-11-25

- [ ] **Built 2026-11-04; CSP in report-only.** `dlna_asgi_headers.py`,
  SECURITY_POSTURE item 14. Tick when `CSP_MODE` defaults to `enforce`.

Progress against "done when":
- [x] the shell and the API send `Content-Security-Policy` (report-only for
  now), `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`
  and `frame-ancestors 'none'` (+ `X-Frame-Options: DENY`);
- [x] report-only first: live since 2026-11-04, violations logged as
  `csp ✗ violation …` via `/api/csp_report`;
- [ ] enforced, once a week of real use (iPhone PWA, Mac, iPad) has logged no
  violations. **Check due 2026-11-11**: `grep "csp ✗" gateway.log`, then flip
  `DEFAULT_MODE` to `enforce`;
- [x] both engines pass the browser suite with the CSP ENFORCED, and any
  violation fails the test;
- [x] real-Safari smoke and its poison-recovery check pass with the policy
  ENFORCED (the smoke runs against the stub), 2026-11-04;
- [x] `/gw/*` is untouched (tested); byte relays get only the headers, which
  renderers never see.

**What it took.** 46 inline handlers (`onclick=`, image `onerror=`) became
`data-act` / `data-onerr` attributes handled by delegated listeners through
an allowlist; `style-src` keeps `'unsafe-inline'` (about 100 `style=""` in
generated HTML; CSS cannot run script). Playwright's `wait_for_function`
uses `eval` inside the page, so the stub's policy adds `'unsafe-eval'`, and a
unit test keeps the app itself eval-free.

**Why.** Expected in a public project, and cheap defence in depth.
- A Content-Security-Policy limits what an injected script could do.
- `frame-ancestors 'none'` stops the PWA being framed for clickjacking.
- `nosniff` stops content-type guessing.

**Done when:**
- the PWA shell and the API send `Content-Security-Policy`,
  `X-Content-Type-Options: nosniff`, `Referrer-Policy` and
  `frame-ancestors 'none'`;
- the CSP shipped as **report-only first**, and was then enforced once no
  violations showed up;
- the browser suite passes with the CSP enforced on both engines;
- the real-Safari smoke still passes (`safari_smoke.py`), and the
  service-worker poison-recovery check passes with the policy in place;
- `/gw/*` and the byte relays are unaffected (renderers ignore these headers,
  but test it).

**Watch out for.**
- `app.js` sets inline `style=` attributes, so `style-src` will need
  `'unsafe-inline'` or a refactor.
- `hls.js` is vendored, so `script-src 'self'` should suffice.
- Cover art comes through same-origin `/art`.

---

## 5. Optional login for the web app — deferred

- [ ] Not started.

**Why.** For people who run the public release on a LAN they trust less than
a home network. The design stays "the network is the access control" by
default; this is an opt-in for those who want more.

**Done when:**
- setting a password in `.env` makes the PWA and `/api/*` require a login,
  and leaving it unset changes nothing;
- the session is an `HttpOnly`, `Secure` (on HTTPS), `SameSite=Strict`
  cookie, and the comparison uses `hmac.compare_digest`;
- **devices are never asked to log in**: `/gw/*` (the Naim and the TV), the
  `:8200` file server and Subsonic's own auth are untouched. That
  requirement is the whole design problem;
- the service worker and offline mode still work behind it;
- the browser suite runs both with and without a password set.

**Decision (2026-11-04): deferred.** For the owner's own install it adds
nothing — Tailscale and the home LAN are the access control, and the guard
(#1) already stops a web page from driving the gateway. The cost is the
device carve-out (the Naim, the TV and `:8200` must never be asked) and the
service worker behind a login. Revisit when someone running the public
release asks for it; the criteria above stay as the spec.

**Open question, if built.** Reuse the Subsonic credentials or a separate
password. Reuse is simpler, but means one leaked secret opens both.

---

## 6. Rate limiting on Subsonic logins — target 2026-12-16

- [x] Done 2026-11-04 — `dlna_auth_throttle.py`, consulted first by the
  `/rest/*` route (7efa582). Verified with Amperfy on the owner's iPhone
  after deployment: 25 requests in two minutes (15:29–15:31, up to 12 a
  minute: album lists, playlists, radio stations, cover art, scrobbles),
  every one authenticated, no failed login, no throttling. Then a CarPlay
  session (15:59–16:00): 62 requests, up to 38 a minute — the heaviest
  Subsonic burst on record, mostly cover art and albums, plus 9 streams,
  all ending `reason=eof` — again no failed login and no throttling. The
  only throttle entries in the log are the deliberate loopback test at 15:09.

**Why.** `/rest/*` is the one surface protected by a password, and nothing
slows down guessing it. It matters mainly if the gateway is ever reachable
beyond the tailnet, but it is cheap.

**Decision (2026-11-04): build.**

**Done when:**
- failed authentications are counted per client IP and back off (for
  example, after 10 failures in a minute every further attempt from that IP
  answers the Subsonic auth error without checking, for a growing interval);
- a successful login resets the counter, and Amperfy's normal traffic never
  trips it (measured against a real CarPlay session); ✅ reset; ✅ Amperfy and CarPlay, live.
  Evidence so far: two weeks of `gateway.log` hold 161 Amperfy requests,
  at most 20 a minute, and **not one failed login** — and only failures are
  counted;
- the counter's memory is bounded (posture rule 10: nothing unbounded);
  ✅ 1,024 addresses, evicting stale records first and a block last;
- a WARN is logged when an IP is throttled, which also feeds #3; ✅
  `Subsonic ✗ throttled <ip>` + a `subsonic_throttled` text;
- `tests/test_subsonic.py` covers the throttle, the reset and the memory
  bound. ✅ `TestLoginThrottle` (8) + `TestLoginThrottleRoute` (5); the
  route tests fail with the gate removed.

**As built.** 10 failures within 60 s block the address for 60 s, doubling
per further trip up to 1 h; a success, or a day without failures, forgets
it. While blocked, a request gets the ordinary wrong-password answer
without its credentials being checked — a guess made then cannot succeed,
and the answer gives nothing away. IPv6 is keyed by /64, since one host
owns a whole /64. A request with no peer address, or with no
`SUBSONIC_PASSWORD` configured, is not counted. A client whose stored
password went stale WILL be blocked — it needed updating anyway, and it is
let back in a minute after it stops failing.
