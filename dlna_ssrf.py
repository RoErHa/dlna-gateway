#!/usr/bin/env python3
"""
dlna_ssrf.py — the outbound-fetch guard for the three endpoints that take a
caller-supplied URL: `/art`, `/stream` and `/radio_stream`.

WHY THIS EXISTS (audit, 2026-08-20). Those three routes are unauthenticated
and used to fetch ANY http/https URL with no restriction, which made the
gateway a server-side request forgery (SSRF) proxy for anything it could
reach. Two things were demonstrably possible from an unauthenticated caller:

  * A port/host oracle. `/art` returned the upstream status or the raw
    exception text, so "Upstream 404" / "Connection refused" / "timed out"
    cleanly distinguished open / closed / filtered on any address.
  * Full-body read. `/stream` relays bytes with no content-type gating, so
    `?url=http://127.0.0.1:8765/api/servers` returned the JSON verbatim.
    Any internal HTTP service the gateway could reach was readable.

THE RULE. A destination that resolves to a PRIVATE address (loopback,
RFC1918, link-local, CGNAT, ULA, multicast, reserved) is refused unless its
host is a device the gateway already knows — the LocalFs file server or a
discovered UPnP server/renderer, i.e. somewhere it legitimately fetches from.
Public destinations stay allowed, because cover art (coverartarchive.org →
archive.org), station logos and internet-radio streams are all public by
nature and an allowlist there would be endless.

The known-host set is taken from the live registries, so it follows discovery
with no extra configuration and shrinks when a device goes away.

DELIBERATE SCOPE — two limits worth stating rather than pretending away:

  * Granularity is HOST, not host:port. A UPnP server may legitimately serve
    art from a different port than its device descriptor, and pinning the
    port breaks that for a class of servers we still support. The guard
    therefore stops LAN scanning (only a handful of known devices are
    reachable at all) without claiming to stop port enumeration OF those
    devices — which the caller can already reach directly anyway.
  * There is a DNS-rebinding window: we resolve to validate, then
    http.client resolves again to connect, and a hostile resolver could
    answer differently. Closing it means connecting to the pinned IP with a
    manual Host header, which breaks TLS SNI/verification for public hosts —
    a worse trade for a LAN/tailnet gateway than the window it removes.

Callers get `(allowed, reason)`; `reason` is for the LOG, never for the HTTP
response — the whole point is not to hand the caller an oracle.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import urllib.parse

log = logging.getLogger("dlna.ssrf")

# Schemes we will ever fetch. Anything else (file:, gopher:, data:, ftp:)
# is refused outright — none is a legitimate cover-art or audio source and
# several are classic SSRF escalation vectors.
_ALLOWED_SCHEMES = ("http", "https")

_DEFAULT_PORT = {"http": 80, "https": 443}


def _is_private(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address a caller should not be able to reach THROUGH us.

    Broader than `.is_private` alone on purpose: link-local carries the cloud
    metadata endpoint (169.254.169.254), and loopback is how the gateway's own
    admin API and the LocalFs server are addressable from the box itself.
    """
    return bool(
        ip.is_private          # RFC1918 / ULA
        or ip.is_loopback      # 127.0.0.0/8, ::1
        or ip.is_link_local    # 169.254.0.0/16 (cloud metadata), fe80::/10
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _known_internal_hosts() -> set[str]:
    """Hostnames/IPs the gateway legitimately fetches from on the LAN.

    Read from the live registries (the LocalFs file server registers itself
    as a MediaServer at boot, so it is included automatically), which means
    this follows discovery rather than needing its own config. Imported
    lazily: dlna_registry pulls in the library stack, and this module is
    imported by the proxies.
    """
    hosts: set[str] = set()
    try:
        from dlna_registry import RENDERERS, SERVERS
        for dev in list(SERVERS.all()) + list(RENDERERS.all()):
            loc = getattr(dev, "location", "") or ""
            host = urllib.parse.urlparse(loc).hostname
            if host:
                hosts.add(host.lower())
    except Exception as e:                                    # noqa: BLE001
        # Never fail OPEN on a registry problem — an empty set simply means
        # "no private destination is allowed", which is the safe direction.
        log.warning(f"SSRF guard: could not read device registries ({e}); "
                    "refusing all private destinations this call")
    return hosts


def check_url(url: str) -> tuple[bool, str]:
    """Decide whether `url` may be fetched on a caller's behalf.

    Returns `(allowed, reason)`. `reason` explains a refusal for the LOG —
    callers must NOT echo it to the client, or the oracle this closes is
    simply reopened in the response body.
    """
    if not url:
        return False, "empty url"
    try:
        parsed = urllib.parse.urlparse(url)
    except (ValueError, AttributeError) as e:
        return False, f"unparseable url ({e})"

    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False, f"scheme {parsed.scheme!r} not allowed"

    host = parsed.hostname
    if not host:
        return False, "no host in url"
    try:
        port = parsed.port or _DEFAULT_PORT[parsed.scheme]
    except ValueError as e:                       # out-of-range port in the url
        return False, f"bad port ({e})"
    if not (0 < port < 65536):
        return False, f"bad port {port}"

    # Resolve to every address the name answers with — a name that returns a
    # mix of public and private records must NOT pass on the public one.
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (TimeoutError, socket.gaierror, UnicodeError, ValueError) as e:
        # UnicodeError: an over-long / malformed IDNA label. Every one of
        # these means "we could not establish where this points", which must
        # refuse rather than fall through to a fetch.
        return False, f"cannot resolve {host!r} ({e})"

    addrs = []
    for info in infos:
        try:
            addrs.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not addrs:
        return False, f"no usable address for {host!r}"

    private = [a for a in addrs if _is_private(a)]
    if not private:
        return True, "public destination"

    # Private destination: only a device we already talk to is acceptable.
    known = _known_internal_hosts()
    if host.lower() in known:
        return True, f"known internal host {host}"
    # The URL may address a known device by IP where the registry holds a
    # name (or vice versa) — compare resolved addresses too.
    for kh in known:
        try:
            k_infos = socket.getaddrinfo(kh, None, proto=socket.IPPROTO_TCP)
        except (TimeoutError, socket.gaierror, UnicodeError, ValueError) as e:
            # A known device that no longer resolves simply cannot vouch for
            # this destination; skip it and keep checking the others.
            log.debug(f"SSRF guard: known host {kh!r} did not resolve ({e})")
            continue
        k_addrs = set()
        for ki in k_infos:
            try:
                k_addrs.add(ipaddress.ip_address(ki[4][0]))
            except ValueError:
                continue
        if any(a in k_addrs for a in addrs):
            return True, f"known internal host {kh} (by address)"

    return False, (f"private destination {addrs[0]} for host {host!r} "
                   f"is not a known device")


def guard(url: str, what: str = "fetch") -> bool:
    """`check_url` + a WARN naming the refusal. Returns True when allowed.

    The log line is the only place the reason appears — deliberately. It is
    what makes a blocked probe visible to whoever runs the gateway while
    telling the caller nothing.
    """
    ok, reason = check_url(url)
    if not ok:
        log.warning(f"SSRF guard: refused {what} of {url[:120]!r} — {reason}")
    return ok
