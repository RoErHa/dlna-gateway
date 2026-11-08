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
ALERT_REPO="$(pwd)"
# Alert the owner's phone via dlna_alerts.py (hardening #3). Off unless
# ALERT_SMS_TO or ALERT_PUSH_URL is set in .env; it can never fail the job
# itself. A send that FAILED is logged here (exit 1 = no channel took it,
# e.g. macOS refusing this job control of Messages, -1743): before
# 2026-11-08 its output went to /dev/null, so an alert that never left the
# Mac looked exactly like one that did. Exit 3 (alerts off) stays quiet.
# dlna_alerts_sms scrubs the number from any error text it reports.
push_alert() {
    local py="${ALERT_REPO}/.venv/bin/python" out rc=0
    [ -x "${py}" ] || py=python3
    [ -f "${ALERT_REPO}/dlna_alerts.py" ] || return 0
    out="$("${py}" "${ALERT_REPO}/dlna_alerts.py" "$@" 2>&1)" || rc=$?
    if [ "${rc}" -ne 0 ] && [ "${rc}" -ne 3 ]; then
        echo "  ✗ alert '$1' was NOT sent (exit ${rc}): $(printf '%s' "${out}" | tr '\n' ' ' | cut -c1-300)" >> "$LOG"
    fi
    return 0
}
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
    push_alert upgrade_check "toolchain needs attention" \
        "$msg. See the Attention list in upgrade-check.log."
fi

# Bounded log: keep the most recent runs only.
if [ "$(wc -l < "$LOG")" -gt "$KEEP_LINES" ]; then
    tail -n "$KEEP_LINES" "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
exit 0
