#!/usr/bin/env bash
# =============================================================================
# run-2.0-asgi.sh — start THE 2.0 gateway (Hypercorn + FastAPI dlna_asgi:app)
# in the foreground, with the same config the launchd job uses. For manual /
# debug runs where you want to watch the logs live.
#
# Post-cutover, the gateway normally runs under launchd
# (com.roha.dlna-gateway). This script is the SAME app, run by hand — so it
# uses the SAME ports + identity and would clash with the launchd copy. It
# therefore REFUSES to start while the launchd gateway is loaded; stop that
# first if you really want a foreground run (instructions printed below).
#
# Live config (matches CUTOVER_LAUNCHD.md):
#   • Hypercorn main app   :8443 TLS+HTTP/2 (ALPN)  +  :8765 plain
#     Listen addresses come from .env via hypercorn_conf.py (NOT 0.0.0.0).
#   • /gw/* (UPnP, Naim)   served by the app on the :8765 plain bind (no HTTPS)
#   • LocalFs file server  :8200  ($LOCALFS_PORT, plain — the Naim fetches bytes)
#   identity: GW_UDN / GW_NAME (adopts 1.x's "DLNA Gateway (IINA)").
#   Secrets (SUBSONIC_*, GATEWAY_CONTACT_EMAIL) come from .env (loaded in-process
#   by dlna_config) — never set here.
#
# Usage:
#   ./run-2.0-asgi.sh                 # TLS :8443 + plain :8765 (foreground)
#   GATEWAY_TLS=0 ./run-2.0-asgi.sh   # plain :8765 only (LAN testing, no cert)
#   ./run-2.0-asgi.sh --access-log -  # extra hypercorn args forwarded
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

LABEL="com.roha.dlna-gateway"

# ── Refuse to clash with the launchd-managed gateway ─────────────────────────
# Running this while launchd has the gateway loaded would double-bind :8443 /
# :8765 / :8200 and double-announce on SSDP (it's what wedged the gateway
# before). Use the launchd copy, or stop it first to run manually.
if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
  echo "✗  The gateway is already running under launchd (${LABEL}) on" >&2
  echo "   https://127.0.0.1:8443 — this script starts the SAME Hypercorn app," >&2
  echo "   so it would clash on :8443 / :8765 / :8200." >&2
  echo >&2
  echo "   • Just restart it:   launchctl kickstart -k gui/\$(id -u)/${LABEL}" >&2
  echo "   • Run manually here: launchctl bootout gui/\$(id -u)/${LABEL}  &&  ./run-2.0-asgi.sh" >&2
  echo "     (then restore launchd: launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/${LABEL}.plist)" >&2
  exit 1
fi

# macOS Terminal defaults to a 256 open-file soft limit; the gateway needs more
# headroom (Hypercorn threadpool + LocalFs scan → EMFILE → sqlite 'unable to
# open database file'). The launchd plist sets 8192; match it here.
# (dlna_config.raise_fd_limit() also raises it in-process as a backstop.)
ulimit -n 8192 2>/dev/null || true

# ── Live identity + ports (match the launchd plist / CUTOVER_LAUNCHD.md) ──────
export APP_VERSION="${APP_VERSION:-2.0.0}"
export GW_UDN="${GW_UDN:-uuid:dlna-gateway-iina-8765}"
export GW_NAME="${GW_NAME:-DLNA Gateway (IINA)}"
export LOCALFS_MUSIC_ROOT="${LOCALFS_MUSIC_ROOT:-/Volumes/SAMDATA/Music}"
export LOCALFS_PORT="${LOCALFS_PORT:-8200}"
# /gw/* (UPnP for the Naim) + the SSDP advert are served by the app on the
# plain :8765 bind (GATEWAY_PLAIN_PORT, default 8765) — no separate device port.

if [ ! -x ".venv/bin/hypercorn" ]; then
  echo "✗  .venv/bin/hypercorn not found." >&2
  echo "   Run ./setup.sh once (creates the venv), then:" >&2
  echo "     .venv/bin/pip install hypercorn" >&2
  exit 1
fi

# ── Transport: TLS+HTTP/2 on :8443 (default) + plain on :8765 ─────────────────
# Hypercorn terminates TLS and negotiates HTTP/2 via ALPN on the --bind port;
# --insecure-bind serves plain HTTP on :8765. Set GATEWAY_TLS=0 for plain-only
# (LAN testing, no cert). /gw/* (on the :8765 plain bind) and LocalFs
# (:${LOCALFS_PORT}) ALWAYS stay plain HTTP — the Naim can't do HTTPS.
HYP_ARGS=(dlna_asgi:app)
SCHEME="http"
case "${GATEWAY_TLS:-1}" in
  0|false|no)
    # Plain-only: reuse the same .env-driven addresses, with the TLS
    # listener suppressed by handing it no certificate.
    export GATEWAY_CERTFILE="" GATEWAY_KEYFILE=""
    HYP_ARGS+=(-c "file:${PWD}/hypercorn_conf.py")
    echo "▶  2.0 ASGI gateway → plain only, on \$GATEWAY_BIND_PLAIN from .env"
    ;;
  *)
    # Auto-discover the tailscale cert (+ matching .key): this worktree first,
    # then the sibling 1.x checkout (which owns the cert + its renewal).
    # Override with GATEWAY_CERTFILE / GATEWAY_KEYFILE.
    CERTFILE="${GATEWAY_CERTFILE:-}"
    KEYFILE="${GATEWAY_KEYFILE:-}"
    if [ -z "${CERTFILE}" ]; then
      for d in . ../dlna-gateway; do
        for c in "$d"/*.crt; do
          [ -e "$c" ] || continue
          k="${c%.crt}.key"
          [ -f "$k" ] && { CERTFILE="$c"; KEYFILE="$k"; break 2; }
        done
      done
    fi
    if [ ! -f "${CERTFILE:-/nonexistent}" ] || [ ! -f "${KEYFILE:-/nonexistent}" ]; then
      echo "✗  No TLS cert/key found (looked in . and ../dlna-gateway)." >&2
      echo "   Seed one:  tailscale cert \"\$TAILSCALE_CERT_HOST\"   # writes <host>.crt/.key" >&2
      echo "   …or point explicitly: GATEWAY_CERTFILE=/path/<host>.crt GATEWAY_KEYFILE=/path/<host>.key ./run-2.0-asgi.sh" >&2
      echo "   …or run plain HTTP only: GATEWAY_TLS=0 ./run-2.0-asgi.sh" >&2
      exit 1
    fi
    # Binds + cert now come from hypercorn_conf.py, which reads .env — so a
    # manual run listens on exactly what the launchd job does (audit
    # 2026-08-20: no more 0.0.0.0). GATEWAY_CERTFILE/KEYFILE still win if
    # the discovery above found something specific.
    export GATEWAY_CERTFILE="${CERTFILE}" GATEWAY_KEYFILE="${KEYFILE}"
    HYP_ARGS+=(-c "file:${PWD}/hypercorn_conf.py")
    SCHEME="https"
    echo "🔒  2.0 ASGI gateway → TLS+HTTP/2 on \$GATEWAY_BIND_TLS, plain on \$GATEWAY_BIND_PLAIN (.env)"
    echo "    cert ${CERTFILE}"
    ;;
esac

echo "    /gw/* + SSDP on the plain :8765 bind, LocalFs :${LOCALFS_PORT} (plain HTTP for the Naim)"
exec .venv/bin/hypercorn "${HYP_ARGS[@]}" "$@"
