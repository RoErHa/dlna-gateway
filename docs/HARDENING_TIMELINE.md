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
| 2 | Off-disk copy of the library backups | 2026-11-11 | ☐ open — destination decided 2026-11-07/08 |
| 3 | Alerts when something goes wrong | 2026-11-18 | ☐ open — decided: push to phone, config in `.env` |
| 4 | Security headers on the web app | 2026-11-25 | ☐ open |
| 5 | Optional login for the web app | 2026-12-09 | ☐ open — **go/no-go not decided** |
| 6 | Rate limiting on Subsonic logins | 2026-12-16 | ☐ open — **go/no-go not decided** |

**Decisions still open (2026-11-04).** #2's destination is due on 7 or 8
November. Whether to build #5 and #6 at all is not decided yet. Until it is,
every working session starts with a reminder, and an item is not started
before its decision is recorded here.

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

## 2. Off-disk copy of the library backups — target 2026-11-11

- [ ] Not started.

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

**Decision due 2026-11-07/08.** Where to: `/Volumes/SAMDATA` (always
attached, but in the same room and on the same power), another machine over
the tailnet, or a cloud bucket. Setting up Time Machine would also cover
this, and the rest of the Mac. The 2026-11-11 target assumes the decision
lands on time.

---

## 3. Alerts when something goes wrong — target 2026-11-18

- [ ] Not started.

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

**Decided 2026-11-04: a push notification to the owner's phone.** Every
detail of the channel lives in `.env` (and is documented, empty, in
`.env.example`), never in code or the plist:
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

- [ ] Not started.

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

## 5. Optional login for the web app — target 2026-12-09

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

**Decision pending: build it at all?** Not decided as of 2026-11-04.

**Open question, if built.** Reuse the Subsonic credentials or a separate
password. Reuse is simpler, but means one leaked secret opens both.

---

## 6. Rate limiting on Subsonic logins — target 2026-12-16

- [ ] Not started.

**Why.** `/rest/*` is the one surface protected by a password, and nothing
slows down guessing it. It matters mainly if the gateway is ever reachable
beyond the tailnet, but it is cheap.

**Decision pending: build it at all?** Not decided as of 2026-11-04.

**Done when:**
- failed authentications are counted per client IP and back off (for
  example, after 10 failures in a minute every further attempt from that IP
  answers the Subsonic auth error without checking, for a growing interval);
- a successful login resets the counter, and Amperfy's normal traffic never
  trips it (measured against a real CarPlay session);
- the counter's memory is bounded (posture rule 10: nothing unbounded);
- a WARN is logged when an IP is throttled, which also feeds #3;
- `tests/test_subsonic.py` covers the throttle, the reset and the memory
  bound.
