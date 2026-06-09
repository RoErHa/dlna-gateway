#!/usr/bin/env bash
# =============================================================================
# run-2.0-asgi.sh — start the 2.0 gateway on the NEW Hypercorn/ASGI stack.
#
# This is the Phase-2 transport: FastAPI app (dlna_asgi.py) served by Hypercorn
# instead of the stdlib BaseHTTPRequestHandler (run-2.0.sh). Same library.db /
# config / LocalFs library as run-2.0.sh (same worktree, same env identity) —
# it's the SAME gateway, different web server.
#
# Port layout (2.x ASGI):
#   • Hypercorn main app (PWA + /api + /rest + byte relays)   8768
#   • device server (/gw/* UPnP, plain HTTP for the Naim)     8770  ($GATEWAY_PORT)
#   • LocalFs file-server (Naim fetches audio bytes)          8201  ($LOCALFS_PORT)
#   The gateway-as-MediaServer SSDP record advertises :8770 (the device port),
#   so the Naim's "DLNA Gateway 2.0" browse lands on the device server.
#
# IMPORTANT: run EITHER this OR run-2.0.sh, never both at once — they'd start
# duplicate discovery threads, double-announce on SSDP, and clash on :8201.
# (And don't actively stream to the SAME renderer from 1.x and 2.x together.)
#
# Usage:
#   ./run-2.0-asgi.sh                       # serve on 0.0.0.0:8768 (foreground)
#   HYPERCORN_BIND=0.0.0.0:9000 ./run-2.0-asgi.sh   # override the bind
#   ./run-2.0-asgi.sh --access-log -        # extra hypercorn args forwarded
# TLS (later): add --certfile/--keyfile here once Hypercorn owns the cert.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

# macOS Terminal defaults to a 256 open-file soft limit; the gateway needs more
# headroom (Hypercorn threadpool + LocalFs scan → EMFILE → sqlite 'unable to
# open database file'). 1.x gets 8192 from its launchd plist; match it here.
# (dlna_config.raise_fd_limit() also raises it in-process as a backstop.)
ulimit -n 8192 2>/dev/null || true

export APP_VERSION="${APP_VERSION:-2.0.0-alpha.1}"
export GW_UDN="${GW_UDN:-uuid:dlna-gateway-iina-2-8766}"
export GW_NAME="${GW_NAME:-DLNA Gateway 2.0}"
export LOCALFS_MUSIC_ROOT="${LOCALFS_MUSIC_ROOT:-/Volumes/SAMDATA/Music}"
export LOCALFS_PORT="${LOCALFS_PORT:-8201}"
# Device-tier server port (plain-HTTP /gw/* for the Naim) + SSDP advert port.
export GATEWAY_PORT="${GATEWAY_PORT:-8770}"

BIND="${HYPERCORN_BIND:-0.0.0.0:8768}"

if [ ! -x ".venv/bin/hypercorn" ]; then
  echo "✗  .venv/bin/hypercorn not found." >&2
  echo "   Run ./setup.sh once (creates the venv), then:" >&2
  echo "     .venv/bin/pip install hypercorn" >&2
  exit 1
fi

# ── TLS (opt-in) ─────────────────────────────────────────────────────────────
# Default = plain HTTP (current behaviour — for LAN testing). Set GATEWAY_TLS=1
# to have HYPERCORN terminate TLS on the MAIN app port (it negotiates HTTP/2 via
# ALPN automatically). The device server (/gw/* :${GATEWAY_PORT}) and LocalFs
# (:${LOCALFS_PORT}) ALWAYS stay plain HTTP — the Naim can't do HTTPS.
#   Cert resolution order: $GATEWAY_CERTFILE/$GATEWAY_KEYFILE → a single *.crt
#   (+ matching .key) in this worktree → error with how to seed/point one.
#   Seed a real one:  tailscale cert "$TAILSCALE_CERT_HOST"   (writes <host>.crt/.key here)
#   Or point at the 1.x cert:  GATEWAY_CERTFILE=../dlna-gateway/<host>.crt \
#                              GATEWAY_KEYFILE=../dlna-gateway/<host>.key
# HTTP/3 (QUIC) is a later add: needs `pip install aioquic` + `--quic-bind`.
HYP_ARGS=(dlna_asgi:app --bind "${BIND}")
SCHEME="http"
case "${GATEWAY_TLS:-}" in
  1|true|yes)
    CERTFILE="${GATEWAY_CERTFILE:-}"
    KEYFILE="${GATEWAY_KEYFILE:-}"
    # Auto-discover a cert (+ matching .key): this worktree first, then the
    # sibling 1.x checkout (which owns the tailscale cert + its auto-renewal —
    # 2.x reuses it, picking up renewals on restart). Override with
    # GATEWAY_CERTFILE/GATEWAY_KEYFILE.
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
      echo "✗  GATEWAY_TLS=1 but no cert/key found (looked in . and ../dlna-gateway)." >&2
      echo "   Seed one:  tailscale cert \"\$TAILSCALE_CERT_HOST\"   # writes <host>.crt/.key" >&2
      echo "   …or point explicitly:" >&2
      echo "       GATEWAY_CERTFILE=/path/<host>.crt GATEWAY_KEYFILE=/path/<host>.key \\" >&2
      echo "       GATEWAY_TLS=1 ./run-2.0-asgi.sh" >&2
      exit 1
    fi
    HYP_ARGS+=(--certfile "${CERTFILE}" --keyfile "${KEYFILE}")
    SCHEME="https"
    echo "🔒  TLS on — cert ${CERTFILE} (Hypercorn negotiates HTTP/2 via ALPN)"
    echo "    device /gw/* :${GATEWAY_PORT} + LocalFs :${LOCALFS_PORT} stay PLAIN HTTP (the Naim needs it)"
    ;;
esac

echo "▶  2.0 ASGI gateway → ${SCHEME}://${BIND}   (device /gw/* + SSDP on :${GATEWAY_PORT}, LocalFs :${LOCALFS_PORT})"
exec .venv/bin/hypercorn "${HYP_ARGS[@]}" "$@"
