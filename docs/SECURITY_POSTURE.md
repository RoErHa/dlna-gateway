# Security posture — the audit write-ups

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

## Security posture (audit 2026-08-20)

The gateway is unauthenticated except for `/rest/*` (Subsonic) — CLAUDE.md's
long-standing position is that **Tailscale is the primary access control**.
That is still true, but it means every control here is what stands between a
LAN peer and the gateway. Four things were fixed after an audit; the notes
below exist so none is quietly undone.

**1. SSRF on the three `?url=` endpoints (`dlna_ssrf.py`).** `/art`,
`/stream` and `/radio_stream` take a caller-supplied URL. They used to fetch
ANY http/https address, which made the gateway an unauthenticated SSRF proxy:
`/art`'s error text was a clean open/closed/filtered **port oracle**, and
`/stream` — which relays bytes with no content-type gating — returned the
body of **any internal HTTP service** verbatim
(`?url=http://127.0.0.1:8765/api/servers` dumped the JSON).
`dlna_ssrf.guard(url)` now fronts all four fetch entry points **and every
redirect hop**: a destination resolving to a private/loopback/link-local/
reserved address is refused unless its host is a **device already in
`SERVERS`/`RENDERERS`** (which is how the LocalFs file server on `:8200`
stays reachable — it registers itself at boot). Public destinations stay
allowed because cover art, station logos and radio streams are public by
nature. Non-http(s) schemes (`file:`, `gopher:`, …) are refused outright.
Read `dlna_ssrf.py`'s docstring for the two limits it does NOT claim to fix
(host- not port-granularity; a DNS-rebinding window).

**2. The error text WAS the oracle.** `/art` forwarded the upstream status
and raw exception string. Every failure now returns an identical
`502 {"error":"art unavailable"}` (`"stream unavailable"` for the relays) and
the detail goes to the log only. `dlna_ssrf.guard` returns a bare bool for
the same reason. **Do not "improve" these by forwarding the upstream status** —
that is precisely the bug. Guarded by
`tests/test_asgi.py::TestArtProxy::test_every_failure_looks_identical_to_the_caller`.

**3. TLS verification is ON.** Five runtime call sites used
`ssl._create_unverified_context()`, so cover art and HTTPS radio were
MITM-able. Removed. Verified the real path still works end-to-end
(coverartarchive → 307 → archive.org, verified cert). If a radio station ever
fails to play with a certificate error, that is a real signal, not something
to switch back off.

**4. Untrusted device text is escaped and sanitised.** `esc()` in `app.js`
escaped only `& < >` — fine for a text node, unsafe in the quoted attribute
it actually lands in (`<option value="${esc(s.udn)}">`), and a UDN comes
straight from a discovered device's XML. `esc()` now escapes both quote
characters, and `dlna_discovery._clean_udn` / `_clean_name` strip
markup-significant and control characters server-side as a second layer.
Guarded by `tests/frontend/test_xss.py` (the UDN case genuinely fails on the
old `esc()`).

**5. Untrusted XML cannot amplify (`dlna_xml.py`).** `ET.fromstring` was
called on nine network-sourced documents, two of them the unauthenticated
`/gw/*` SOAP endpoints. ElementTree expands internal entities: a
billion-laughs body was measured expanding **10× per nesting level**, so a
few small requests could exhaust memory. External entities were already
refused (no file disclosure), making this DoS — but on a machine whose job is
to keep playing music. `safe_fromstring` now refuses **any DTD** (SOAP, DIDL
and device descriptors have no legitimate use for one) plus a 4 MB cap, and
every parse site routes through it. Guarded by `tests/test_xml_safety.py`,
which also asserts real SOAP still parses — a hardening that broke the Naim
would be worse than the bug.

**6. Auth comparison is constant-time.** Subsonic's MD5 token is mandated by
the protocol and can't be revisited, but the comparison is ours:
`hmac.compare_digest` replaces `==` on both the token and the plaintext path.

**7. The gateway no longer binds `0.0.0.0` (2026-08-20).** An unauthenticated
control API on *every* interface was the biggest thing left open. Listeners
are now named explicitly in `.env` and read by `hypercorn_conf.py`:

| Port | Addresses | Why |
|---|---|---|
| `8443` TLS | tailnet + LAN + loopback | Cert is for the tailnet hostname, so **only the tailnet URL validates** — see the caveat below. Loopback for the live suite |
| `8765` plain | LAN + tailnet + loopback | The Naim and LG TV can't do HTTPS, so the UPnP tier lives here |
| `8200` file server | LAN only | Renderers fetch bytes directly; a single socket, so one address |

⚠️ **The LAN address on `:8443` serves a certificate that does not match it.**
`192.168.1.125:8443` was added 2026-08-20 so the PWA is reachable over
HTTPS on the LAN, but the `tailscale cert` is issued for
`ronsmacmini.tail5be6ad.ts.net` — a browser warns on every visit
(`curl` without `-k` fails outright; with `-k` it is a normal 200/HTTP-2).
Consequence worth knowing before debugging a blank app: a cert-warning
origin is **not a trusted secure context**, so an installed PWA there can
fail to register its Service Worker and show the "full UI, no content"
symptom — the same appearance as the 2026-06-27 SW outage, different cause.
For a LAN-installable PWA use `http://192.168.1.125:8765` instead: plain
HTTP on a private address *is* treated as a secure context. The clean HTTPS
route is the tailnet hostname, which validates properly.

This closed two real exposures: a **second LAN interface** (`192.168.1.238`)
that nobody intended to serve, and TLS on the LAN address where the cert
never matched anyway. The LAN address is **DHCP-reserved** on the router
(Netgear @ `192.168.1.1`, against the Ethernet MAC `d0:11:e5:ed:76:35`,
2026-08-20) — which is what makes naming a literal IP in `.env` reasonable
rather than fragile. Note the Wi-Fi interface carries a macOS *randomized*
MAC, so it could never have been reserved reliably; it is no longer served. **Loopback must stay bound** — `tests/run_all.py` and
the beets tools reach the gateway at `127.0.0.1`. If a configured address
stops existing the gateway FAILS TO START; that is deliberate, because the
alternative is silently falling back to `0.0.0.0`. Changing an address is an
`.env` edit + `./setup.sh --restart` — no plist bootout, which is the whole
reason the binds live in `.env` rather than the LaunchAgent's argv.

**8. Containment is about path COMPONENTS, not string prefixes (2026-08-21).**
The LocalFs file server checked `str(resolved).startswith(root)`, so any
sibling directory whose name merely began with a root's name was inside it —
`/Volumes/SAMDATA/Music` admitted `/Volumes/SAMDATA/Music Videos`, and
`Audio_Books` admitted `Audio_Books_private`. Every byte of them, to any
unauthenticated peer on the LAN, because renderers fetch from this server
directly. All three byte routes were affected; the check had been
copy-pasted. It is now **one** helper, `dlna_localfs_http.resolve_within`,
and the caller opens the path it RETURNS — resolving follows symlinks, so
re-opening the original path would hand back a time-of-check/time-of-use
gap. `/localfs/poster/<id>` is the only route where a URL segment becomes a
filesystem path; it keeps `basename()` **and** gets containment. Guarded by
`tests/test_localfs_traversal.py`, which asks the adversarial questions the
older `TestPathTraversalDefence` did not.

**9. An SSDP packet may not cost unbounded work — or name someone else
(2026-08-21).** SSDP is the one place a stranger hands the gateway bytes with
no authentication and no proven return address. Three things followed from
that and are now enforced in `dlna_discovery_ssdp.parse_location` (pure, so
the hostile cases are directly testable) plus `_spawn_registration`:
*(a)* every LOCATION-bearing packet used to spawn a thread that slept 1.5 s
then made an HTTP request, so a flood exhausted threads and FDs — appearing
as SQLite's `unable to open database file`, which looks nothing like an
attack. Capped at **8 registrations in flight**, excess **dropped** (safe:
devices re-announce on a timer). *(b)* the URL was fetched unvalidated and
read **uncapped** — `file://` worked, and an endless stream was buffered
until death, since `safe_fromstring`'s 4 MB cap only sees bytes already in
memory. Now http/https only, body capped at `_MAX_DESC_BYTES` (512 KB;
real descriptions are ~4 KB). *(c)* nothing required the URL to name the
sender, so any peer could aim the gateway's HTTP client at a third party —
now the URL's host must BE the datagram source, compared via
`urlsplit().hostname` so `http://192.168.1.5@elsewhere/` reads as
*elsewhere* and is refused. `_seen_locations` is bounded for the same
reason.
> ⚠️ **A too-strict SSDP rule shows up as "the Naim stopped browsing", not
> as a red test.** Verified against 25 s of real LAN traffic before shipping
> — router, gateway, a Google device and both Naim descriptors all accepted,
> zero false drops — and again after restart. `tests/test_ssdp_parsing.py`
> puts the *legitimate* packet shapes first for this reason; they caught a
> first draft that anchored the regex on `$` and rejected every real device
> (SSDP lines end `\r\n`). If a device ever vanishes, run with
> `GATEWAY_DEBUG=1` and `grep "announced a URL for" gateway.log`.

**10. Nothing a peer can hold is unbounded (2026-08-21).** Three resources
had no ceiling, all measured on the running gateway before being fixed.
*(a)* **Reads from devices** — anything that answers an SSDP packet becomes a
renderer/server the gateway talks SOAP to, and every response was a bare
`resp.read()`. `dlna_xml.read_capped` bounds the read itself; it lives beside
`safe_fromstring` because that cap could only ever judge bytes **already in
memory**. Every device-facing site now uses it, plus the four outbound
service calls, and `tests/test_resource_limits.py` asserts no bare read is
left — so the next one added is caught rather than discovered. *(b)*
**Audio relays** — 40 stalled `/stream` requests cost **120 file
descriptors** (client socket + upstream socket + the LocalFs server's own
accepted socket), and 80 survived the client disconnecting, draining over
about a minute. `/stream`, `/radio_stream` and Subsonic `/rest/stream` share
a **64-slot cap** (`dlna_asgi_state.AUDIO_RELAYS`) and answer 503 +
`Retry-After` beyond it. *(c)* **SSE subscribers** — same shape, capped at 64
(`SSE_STREAMS`). Verified live: 80 simultaneous requests to each → exactly 64
× 200 and 16 × 503, with full recovery and no slot leak afterwards.
> **What is NOT a problem — do not "fix" these again.** Half-open requests
> (classic slowloris) are reaped by hypercorn's `keep_alive_timeout` in ~5 s
> (60 of them never moved the FD count). The shared 256-token threadpool
> absorbed **150 concurrent full `/stream` pulls with zero failures**. And
> `/art` already capped its read at 12 MB *during* the read — that is the
> pattern everything else now copies.

**11. A switched-off renderer used to cost six seconds per poll
(2026-08-21).** `tests/chaos.py` failed on `snapshot (no udn) > 5s`, and the
cause was real: `/api/renderer_state` with no udn took **6.011 s** while the
same call for the reachable Naim took **2 ms**. Two compounding bugs.
`QueueRegistry.snapshot_all` walked every queue **sequentially** — and queues
are *never evicted*, so it visited the LG TV, switched off, and paid its full
SOAP timeout; `RendererQueue.snapshot` already fires its own two SOAP calls
in parallel precisely so a dead renderer costs ~6 s not 12 s, and doing the
queues sequentially threw that away one level up, making the total the **SUM**
over dead renderers. It now fans out in parallel (4 unresponsive renderers:
1.62 s → 0.41 s in a direct A/B), and a queue that misses the deadline is
served from its own cached snapshot. On top of that, `snapshot()` now leaves
a renderer that answered `UNREACHABLE` alone for `_UNREACHABLE_BACKOFF_SEC`
(30 s) instead of re-dialling it every time the 500 ms cache expires —
**the PWA polls this endpoint every second while audio plays**. Recovery is
unaffected: the monitor thread probes independently while a queue is actually
playing. After both fixes chaos passes and runs 400 actions in 9.5 s instead
of 25.1 s.

### Can a media file phone home? No — and here is what keeps it that way

The tag reader (`dlna_providers/localfs_tags._read_tags`) opens files with
mutagen `easy=True` and reads a **fixed allowlist of scalar fields**. It
never touches the ID3 URL frames (`WOAR`/`WXXX`/`WCOM`), so nothing from a
tag can become a URL the gateway fetches. Album art is a `localfs-art:<hash>`
sentinel derived from the **hash of the embedded bytes**, never a tag string.

The subtle part is `_sniff_image_mime`, which **ignores the declared MIME and
sniffs magic bytes**. That is load-bearing, not cosmetic:

  * ID3 `APIC` with MIME `-->` means the payload is a **URL**, not image
    bytes — a documented phone-home form. Because we sniff, that payload is
    served as opaque bytes and **never dereferenced**.
  * An SVG "cover" is script-capable markup. Falling back to `image/jpeg`
    means the browser refuses to parse it as SVG, so a cover cannot become an
    XSS or a beacon.

The allowlist is raster-only for exactly that reason. **Do not add
`image/svg+xml` and do not trust the container's declared MIME** — either
change reopens both vectors in one commit. Guarded by
`tests/test_art_safety.py`.

**What legitimately leaves the machine because of your files** (all
documented in the External services table below): artist+album tags →
MusicBrainz/Cover Art Archive during indexing; title/artist/album/duration →
lrclib on the 📜 button; and — the privacy-relevant one — **video GPS
coordinates → Nominatim automatically on every video scan**, at ~1 m
precision with the contact email in the User-Agent. That is inherent to
reverse-geocoding (cached per coordinate, ~1 req/s); the opt-out is leaving
`LOCALFS_VIDEO_ROOT` unset.
