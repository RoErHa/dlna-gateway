#!/usr/bin/env bash
# =============================================================================
# immich-import.sh — nightly link of new Immich videos into the video root
#
# WHY: tools/immich_import.py is safe to re-run but only happens when someone
# starts it, so new phone videos stayed off the TV, and a file Immich moved
# stayed out of the index until the next manual run. Each run adds links and
# re-points moved ones; it never deletes a video or changes an Immich file.
#
# Any non-zero exit sends an `immich_import_failed` alert: exit 2 is a library or
# the video root not being mounted, exit 3 is links leading nowhere.
#
# Output appends to immich-import.log.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")"

LOG="immich-import.log"
PY=".venv/bin/python"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

push_alert() {
    [ -f dlna_alerts.py ] || return 0
    "$PY" dlna_alerts.py "$@" >/dev/null 2>&1 || true
}

log "start"
"$PY" tools/immich_import.py --apply >> "$LOG" 2>&1
rc=$?
log "exit $rc"

if [ "$rc" -ne 0 ]; then
    push_alert immich_import_failed "Immich video import FAILED" \
        "tools/immich_import.py exited $rc: $(tail -n 3 "$LOG" | head -n 2 | cut -c1-200). See immich-import.log." \
        --priority high
fi
exit "$rc"
