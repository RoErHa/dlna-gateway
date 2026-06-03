#!/usr/bin/env bash
# =============================================================================
# run-2.0.sh — start the 2.0 instance SIDE-BY-SIDE with the 1.x gateway.
#
# This worktree is the `2.0` branch (see docs/BUILDING_2.0_SIDE_BY_SIDE.md).
# 1.x keeps its defaults: gateway :8765 / HTTPS :8443 / LocalFs :8200, the
# launchd LaunchAgent, and its own library.db/config.json/gateway.log in the
# main checkout. This script gives 2.x a fully separate identity so both can
# run on the same Mac mini against the same (read-only) music folders:
#
#   • gateway HTTP port        8766   (--port below; 1.x = 8765)
#   • LocalFs file-server port 8201   ($LOCALFS_PORT; 1.x = 8200)
#   • distinct UPnP identity          ($GW_UDN / $GW_NAME → the Naim shows
#                                      "DLNA Gateway 2.0" as a second server)
#   • own library.db/config.json/gateway.log — automatic: dlna_config derives
#     these from __file__, and this is a separate working directory.
#
# The one behavioural caveat: don't actively stream to the SAME physical
# renderer from both 1.x and 2.x at once (per-UDN queues don't coordinate
# across processes).
#
# Usage:
#   ./run-2.0.sh                 # start 2.x on :8766 (foreground)
#   ./run-2.0.sh --debug         # verbose logging (forwarded to dlna_gateway)
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

export APP_VERSION="${APP_VERSION:-2.0.0-alpha.1}"
export GW_UDN="${GW_UDN:-uuid:dlna-gateway-iina-2-8766}"
export GW_NAME="${GW_NAME:-DLNA Gateway 2.0}"
export LOCALFS_MUSIC_ROOT="${LOCALFS_MUSIC_ROOT:-/Volumes/SAMDATA/Music}"
export LOCALFS_PORT="${LOCALFS_PORT:-8201}"

exec ./setup.sh --run --no-browser --port 8766 "$@"
