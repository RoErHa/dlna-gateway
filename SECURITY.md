# Security policy

## The threat model, stated plainly

**This gateway is built for a home LAN or a private tailnet. It is not
hardened for the public internet, and it should never be port-forwarded.**

Everything below follows from that. The control API — browse, play, playlists,
the whole `/api/*` surface — is **deliberately unauthenticated**. The access
control is the network: if you can reach the gateway, you can control it, in
the same way anyone in your living room can press play on your hi-fi. The one
exception is the Subsonic `/rest/*` surface, which is authenticated because
third-party clients (Amperfy and friends) expect credentials and because it is
the surface most likely to be reached from a phone on a hostile network.

So **"the API has no authentication" is not a vulnerability report** — it is
the documented design. What follows is what *is*.

## In scope

Anything that lets a peer on the network do more than browse and play:

- **Reading files the gateway was never meant to serve** — escaping the
  configured music/video/audiobook roots, whether by path, symlink, or the
  file server's containment check being wrong.
- **Making the gateway attack something else** — SSRF through a
  caller-supplied URL, using it as an HTTP reflector, or getting it to
  amplify traffic toward a third party.
- **Resource exhaustion that a single peer can trigger** — unbounded reads,
  unbounded connection or thread growth, or anything that makes the gateway
  stop playing music. On a box whose job is to keep playing music, denial of
  service is a real bug, not a lesser one.
- **Injection into a client** — untrusted device text (a UDN, a friendly
  name, a tag) reaching the PWA in a way that executes.
- **Anything a media file can do to the host.** Tags and embedded art are
  attacker-controlled data if you ever download music. A file that makes the
  gateway fetch a URL, run code, or read something it shouldn't is in scope.
- **Leaking data off the machine** beyond what the README documents.
- **A web page driving the gateway.** "Whoever can reach it can control it"
  means a peer on the network, not a site you happen to open in a browser on
  it. A page that can change state here (CSRF) or read replies (DNS
  rebinding) is in scope.

## Out of scope

- The unauthenticated control API (above).
- Consequences of exposing the gateway to the internet yourself.
- The plain-HTTP tiers. The UPnP device surface (`/gw/*`) and the file server
  are unencrypted **because the renderers cannot do HTTPS** — a Naim streamer
  or a TV will not negotiate TLS. That is a constraint of the hardware, not an
  oversight, and it is why those tiers are LAN-bound.
- Serving a certificate that does not match a LAN IP on `:8443`. Known,
  deliberate, and documented — use the tailnet hostname for a valid one.
- Findings that require an attacker who already has a shell on the host.

## Reporting

Please report privately, via **GitHub's private vulnerability reporting**:
the repository's **Security** tab → **Report a vulnerability**. That opens a
draft advisory only you and the maintainer can see.

Please do not open a public issue for something exploitable.

Useful in a report: what an attacker can reach (LAN peer? tailnet? a media
file?), the request or input that does it, and what you got back. A `curl`
that shows the behaviour is worth more than a scanner's category name.

**This is a one-person hobby project.** Expect a reply in days rather than
hours, and no bounty. Fixes land with a regression test that is verified to
fail on the unfixed code, and the reasoning goes into `CLAUDE.md` →
"Security posture" so it does not get quietly undone later.

## What has already been looked at

An audit in August 2026 and two follow-up passes found and fixed eleven
issues, and a review in November 2026 a twelfth. Each is written up in
`CLAUDE.md` → "Security posture", with the measurement that justified it and
a note on what must not be reverted. The headlines:

| Area | What was wrong |
|---|---|
| SSRF | The three `?url=` endpoints fetched any address, and `/art`'s error text was a working port oracle. |
| Path containment | The file server compared string prefixes, so a sibling directory whose name merely started with a root's name was inside it. |
| XML | Untrusted XML was parsed with DTDs allowed — entity expansion measured at 10× per nesting level, on an unauthenticated endpoint. |
| SSDP | Unauthenticated multicast could name any URL for the gateway to fetch, and cost it unbounded threads. |
| Resource limits | Reads from devices were unbounded; audio relays and event streams had no ceiling. |
| TLS | Outbound fetches did not verify certificates. |
| Injection | The PWA's escaping did not cover quoted attributes, where discovered-device text lands. |
| Browser (Nov 2026) | Any web page could act on the API — writes answered GETs, `text/plain` POSTs needed no preflight, and no `Host` check stopped DNS rebinding. |
| Headers (Nov 2026) | No Content-Security-Policy, so an escaping slip would have been script execution; the PWA could be framed; no `nosniff`. Now `script-src 'self'` with no inline handlers (report-only until verified on real devices), `frame-ancestors 'none'`, `nosniff`, same-origin referrer. |
| Subsonic (Nov 2026) | Nothing limited how fast the `/rest/*` password could be guessed. Now 10 failures a minute block the address, doubling up to an hour, and a blocked address is refused without its credentials being checked. |

**One deliberate exception to path containment (2026-11-07).** With `LOCALFS_VIDEO_LINK_ROOTS` set, the
video route may follow a symlink out of the video root, so `GWMovies` can link to Immich originals instead
of copying them. It requires a real symlink inside the video root, a target inside a listed folder, and a
video extension. Audio, art and direct database paths get no exception. The server still answers any
unauthenticated LAN peer, and still serves only videos that are in the database.

The regression tests for these live in `tests/test_ssrf.py`,
`tests/test_localfs_traversal.py`, `tests/test_xml_safety.py`,
`tests/test_ssdp_parsing.py`, `tests/test_resource_limits.py`,
`tests/test_art_safety.py`, `tests/test_request_guard.py`,
`tests/test_security_headers.py`, `tests/test_subsonic.py` (the login
throttle), `tests/frontend/test_csp.py` and `tests/frontend/test_xss.py`.

## Running it safely

- Keep it on a LAN or a tailnet. Do not port-forward it.
- Name your listen addresses explicitly in `.env` rather than leaving the
  `0.0.0.0` default, so the API appears only on interfaces you intended.
- Set `SUBSONIC_PASSWORD`, or leave it empty — empty makes the Subsonic API
  refuse every call, which is the safe default if you do not use it.
  After changing it, update your Subsonic apps promptly: a client still
  sending the old password is throttled like a guesser.
- If you enable video, know that **GPS coordinates from your clips are sent
  to Nominatim** to turn them into place names. Leave `LOCALFS_VIDEO_ROOT`
  unset to opt out.
