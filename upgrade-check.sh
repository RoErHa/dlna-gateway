#!/usr/bin/env bash
# =============================================================================
# upgrade-check.sh — weekly "is anything outdated or known-vulnerable?"
#
# Runs `tools/upgrade.py --check` (REPORT ONLY — never applies: an upgrade
# moves Homebrew and restarts the gateway, which stays a person's decision)
# and appends the report to upgrade-check.log. The report ENDS with an
# Attention list (DO / DECIDE / WAIT / NOTE). A notification is posted when
# there is a DO item, or a DECIDE/WAIT item that was not there last week:
# a log nobody opens is a finding nobody acts on, and a ping that repeats
# the same thing every week is one nobody reads. See docs/UPGRADE.md.
#
# Scheduled by com.roha.dlna-upgrade-check (Saturdays 05:00).
#
# USAGE
#   ./upgrade-check.sh            # run the check now
#
# Must stay bash 3.2-compatible: that is what launchd runs on macOS.
# =============================================================================
set -u
cd "$(dirname "$0")"

LOG="upgrade-check.log"
KEEP_LINES=3000

{
    echo
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') ====="
    python3 tools/upgrade.py --check 2>&1
} >> "$LOG"
status=$?

if [ "$status" -ne 0 ]; then
    # Read THIS run only — a crashed run must not borrow last week's summary.
    last=$(awk '/^===== /{buf=""} {buf=buf $0 "\n"} END{printf "%s", buf}' "$LOG")
    summary=$(printf '%s' "$last" | grep '^Attention:' | tail -1 | sed 's/^Attention: //')
    fresh=$(printf '%s' "$last" | grep -c '^NEW since last check:')
    msg="${summary:-check failed — see upgrade-check.log}"
    [ "$fresh" -gt 0 ] && msg="$fresh new item(s). $msg"
    osascript -e "display notification \"$msg — see the Attention list in upgrade-check.log\" with title \"DLNA Gateway — needs attention\"" \
        >/dev/null 2>&1 || true
fi

# Bounded log: keep the most recent runs only.
if [ "$(wc -l < "$LOG")" -gt "$KEEP_LINES" ]; then
    tail -n "$KEEP_LINES" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exit 0
