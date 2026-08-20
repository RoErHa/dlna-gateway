#!/usr/bin/env python3
"""
hypercorn_conf.py — the server's listen addresses, read from `.env`.

    hypercorn -c file:/path/to/hypercorn_conf.py dlna_asgi:app

WHY THIS EXISTS (audit follow-up, 2026-08-20). The gateway used to bind
`0.0.0.0`, which puts an UNAUTHENTICATED control API on every interface the
machine has — including ones nobody thought about. This mini has three
addresses (two LAN, one tailnet), so `0.0.0.0` was three exposures where one
or two were wanted.

Binds now name specific addresses, and live in `.env` rather than in the
LaunchAgent's argument list for one practical reason: a plist change needs a
full `launchctl bootout` + `bootstrap`, while `.env` needs only
`./setup.sh --restart`. When the LAN address changes — DHCP, a new router,
moving the machine — that difference is a one-line fix instead of a
debugging session. It also keeps the project rule intact: `.env` is THE
configuration file; the plist carries only PATH and the command.

FAILURE MODE, ON PURPOSE: if a configured address no longer exists on the
machine, hypercorn fails to bind and the gateway does not start. That is
loud, and loud beats silently falling back to `0.0.0.0` and re-exposing the
API — the exact thing this file exists to stop.

TWO MECHANICS THIS FILE MUST RESPECT, both learned by watching it fail:

  * hypercorn EXECUTES this file rather than importing it as part of the
    app, so the repo is not on `sys.path` and a bare `import dlna_config`
    raises at boot. Hence the explicit path insert below.
  * hypercorn PICKLES the resulting namespace to hand to workers. A `def`,
    a class, or an imported module left at module scope is unpicklable and
    the server dies with a confusing `Can't pickle …: No module named
    'module'`. So everything here is computed inline and every helper name
    is deleted at the end — what survives must be plain data.

Defaults are `0.0.0.0` so a fresh clone on another machine works with no
configuration; the deployment narrows it.
"""
import os as _os
import sys as _sys

_repo = _os.path.dirname(_os.path.abspath(__file__))
if _repo not in _sys.path:
    _sys.path.insert(0, _repo)

# Importing dlna_config is what loads `.env` (at module import, before any
# value is read). It pulls in no database or network code.
import dlna_config as _cfg  # noqa: E402,F401  — imported for the .env load

# ── Listeners ────────────────────────────────────────────────────────
# TLS + HTTP/2 (ALPN). The certificate is issued for the tailnet hostname,
# so this is the tailnet-facing listener; loopback is included because the
# live test suite and several tools reach https://127.0.0.1:8443.
_tls = _os.environ.get("GATEWAY_BIND_TLS", "") or "0.0.0.0:8443"
bind = [a.strip() for a in _tls.split(",") if a.strip()]

# Plain HTTP — the tier UPnP devices use, since the Naim and the LG TV
# cannot do HTTPS. Must include the LAN address they reach the gateway at.
_plain = _os.environ.get("GATEWAY_BIND_PLAIN", "") or "0.0.0.0:8765"
insecure_bind = [a.strip() for a in _plain.split(",") if a.strip()]

# ── TLS material ─────────────────────────────────────────────────────
certfile = _os.environ.get("GATEWAY_CERTFILE") or None
keyfile = _os.environ.get("GATEWAY_KEYFILE") or None
if not certfile:
    _host = _os.environ.get("TAILSCALE_CERT_HOST", "").strip()
    if _host:
        _c = _os.path.join(_repo, f"{_host}.crt")
        _k = _os.path.join(_repo, f"{_host}.key")
        if _os.path.isfile(_c) and _os.path.isfile(_k):
            certfile, keyfile = _c, _k
        del _c, _k
    del _host

# Without a certificate there is nothing to terminate TLS with, so drop the
# TLS listener rather than let hypercorn fail on a half-configured bind.
if not (certfile and keyfile):
    bind = []

accesslog = None      # access logging off; the app logs what matters
errorlog = "-"        # stderr, which launchd captures

# Leave ONLY picklable data behind — see the docstring.
del _os, _sys, _cfg, _repo, _tls, _plain
