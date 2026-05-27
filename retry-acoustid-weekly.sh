#!/usr/bin/env bash
# retry-acoustid-weekly.sh — weekly AcoustID notfound retry.
#
# Runs weekly via the com.roha.dlna-acoustid-retry LaunchAgent (macOS).
# Two-step pass:
#   1. Drop every `source='notfound'` row from metadata_overrides via
#      tools/retry_notfound_metadata.py --all -y.
#   2. Kickstart the gateway — its 120s-post-startup
#      ACOUSTID_FETCHER.start_initial_scan() then picks up all the
#      newly-bare tracks and re-asks AcoustID. Legit misses get
#      re-cached as notfound automatically; tracks newly identifiable
#      (MB database keeps growing) get proper metadata.
#
# macOS-specific (uses launchctl). On Linux replace the kickstart
# with `systemctl --user restart dlna-gateway.service` and ensure
# the gateway runs the AcoustID worker at startup the same way.
#
# All output appended to acoustid-retry.log next to gateway.log.
#
# Manual run:
#   ./retry-acoustid-weekly.sh           # do it now
#   ./retry-acoustid-weekly.sh --dry-run # just show what would happen

set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env so ACOUSTID_API_KEY is available — the retry is wasted
# work if the worker can't actually look anything up.
if [ -f "${REPO_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${REPO_DIR}/.env"
    set +a
fi

LOG="${REPO_DIR}/acoustid-retry.log"
PY="${REPO_DIR}/.venv/bin/python3"
if [ ! -x "$PY" ]; then
    PY="$(command -v python3)"
fi

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
    esac
done

ts() { date '+%Y-%m-%d %H:%M:%S'; }
say() { echo "$(ts) $*" | tee -a "$LOG"; }

say "═══ AcoustID weekly retry starting ═══"

if [ -z "${ACOUSTID_API_KEY:-}" ]; then
    say "ACOUSTID_API_KEY not set — nothing to do, exiting."
    exit 0
fi

if [ "$DRY_RUN" -eq 1 ]; then
    say "DRY-RUN: would run retry_notfound_metadata.py --all --dry-run"
    "$PY" "${REPO_DIR}/tools/retry_notfound_metadata.py" --all --dry-run 2>&1 | tee -a "$LOG"
    say "DRY-RUN: would kickstart com.roha.dlna-gateway"
    exit 0
fi

say "Step 1: deleting all notfound rows from metadata_overrides"
"$PY" "${REPO_DIR}/tools/retry_notfound_metadata.py" --all -y 2>&1 | tee -a "$LOG"
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ]; then
    say "retry_notfound_metadata.py exited rc=$rc — aborting before kickstart"
    exit "$rc"
fi

say "Step 2: kickstarting gateway so ACOUSTID_FETCHER picks up newly-bare tracks"
launchctl kickstart -k "gui/$(id -u)/com.roha.dlna-gateway"
say "═══ AcoustID weekly retry complete ═══"
