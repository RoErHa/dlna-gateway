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

echo "▶  2.0 ASGI gateway → http://${BIND}   (device /gw/* + SSDP on :${GATEWAY_PORT}, LocalFs :${LOCALFS_PORT})"
exec .venv/bin/hypercorn dlna_asgi:app --bind "${BIND}" "$@"
