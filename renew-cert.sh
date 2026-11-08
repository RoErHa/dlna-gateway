#!/usr/bin/env bash
# renew-cert.sh — Tailscale cert auto-renewal for dlna-gateway.
#
# Runs weekly via the com.roha.dlna-cert-renew LaunchAgent (macOS).
# Renews the Tailscale-issued Let's Encrypt cert when it has fewer
# than RENEW_DAYS left, then kickstarts the gateway so it picks up
# the new cert. macOS-specific (uses launchctl). On Linux you'd run
# this from a systemd timer and replace the kickstart with
# `systemctl --user restart dlna-gateway.service`.
#
# All output appended to cert-renewal.log next to gateway.log.
#
# Manual run:
#   ./renew-cert.sh           # check + renew if needed
#   ./renew-cert.sh --force   # always renew

set -u

# REPO_DIR derives from the script's own location — works regardless
# of where you cloned the repo, no hardcoded paths.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# .env (gitignored) holds TAILSCALE_CERT_HOST and friends, so they are there
# even under launchd / cron's sparse environment. It is READ, never sourced
# (2026-10-23): sourcing executes it as shell. `GW_NAME=DLNA Gateway (IINA)`
# — valid for the app's own loader — is a syntax error that made `.` abandon
# every key after it, and a line such as `KEY=x word` would RUN `word`. A
# config file must not be able to run anything. The real process
# environment still wins, as in dlna_config.
env_get() {
    local key="$1" val
    val="${!key:-}"   # the real environment first (bash 3.2 has ${!name})
    if [ -z "${val}" ] && [ -f "${REPO_DIR}/.env" ]; then
        val=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "${REPO_DIR}/.env" \
            | tail -1 | cut -d= -f2- \
            | sed -E "s/^[[:space:]]+|[[:space:]]+\$//g; s/^[\"'](.*)[\"']\$/\1/")
    fi
    printf '%s' "${val}"
}

ALERT_REPO="${REPO_DIR}"
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
        echo "[$(date '+%F %T')] ERROR alert '$1' was NOT sent (exit ${rc}): $(printf '%s' "${out}" | tr '\n' ' ' | cut -c1-300)" >> "${LOG:-/dev/null}"
    fi
    return 0
}

# A failed renewal must reach the owner: on 2026-07-20 one "succeeded" in the
# log only. The message is the last ERROR/FATAL line THIS run wrote.
LOG_START=0
on_exit() {
    local rc=$? last=""
    [ "${rc}" -eq 0 ] && return 0
    if [ -n "${LOG:-}" ] && [ -f "${LOG}" ]; then
        last=$(tail -n "+$((LOG_START + 1))" "${LOG}" | grep -E 'ERROR|FATAL' \
            | tail -1 | sed -E 's/^\[[^]]*\] //')
    fi
    push_alert cert_renew_failed "certificate renewal FAILED" \
        "${last:-renew-cert.sh exited ${rc}}. See cert-renewal.log." --priority high
}
trap on_exit EXIT

CERT_HOST="$(env_get TAILSCALE_CERT_HOST)"
if [ -z "${CERT_HOST}" ]; then
    echo "[$(date '+%F %T')] FATAL TAILSCALE_CERT_HOST not set (put it in .env)" >&2
    exit 1
fi

RENEW_DAYS="$(env_get RENEW_DAYS)"; RENEW_DAYS="${RENEW_DAYS:-30}"
LOG="${REPO_DIR}/cert-renewal.log"
[ -f "${LOG}" ] && LOG_START=$(wc -l < "${LOG}" | tr -d ' ')
TAILSCALE="$(env_get TAILSCALE_BIN)"; TAILSCALE="${TAILSCALE:-$(command -v tailscale)}"
OPENSSL="$(env_get OPENSSL_BIN)"; OPENSSL="${OPENSSL:-$(command -v openssl)}"

if [ -z "${TAILSCALE}" ] || [ -z "${OPENSSL}" ]; then
    echo "[$(date '+%F %T')] FATAL tailscale or openssl not in PATH" >> "${LOG}"
    exit 1
fi

cd "${REPO_DIR}" || { echo "[$(date '+%F %T')] FATAL cannot cd ${REPO_DIR}" >> "${LOG}"; exit 1; }

log() { echo "[$(date '+%F %T')] $*" >> "${LOG}"; }

force=0
[ "${1:-}" = "--force" ] && force=1

cert_file="${REPO_DIR}/${CERT_HOST}.crt"
key_file="${REPO_DIR}/${CERT_HOST}.key"

if [ ! -f "${cert_file}" ] || [ ! -f "${key_file}" ]; then
    log "WARN cert/key missing; running tailscale cert to seed (${cert_file})"
    force=1
fi

if [ "${force}" -eq 0 ]; then
    end_date=$("${OPENSSL}" x509 -in "${cert_file}" -noout -enddate 2>/dev/null | cut -d= -f2)
    if [ -z "${end_date}" ]; then
        log "ERROR could not read enddate from ${cert_file}; forcing renewal"
        force=1
    else
        # macOS date(1) needs -j -f to parse; GNU date uses -d. Try
        # macOS first, fall through to GNU.
        end_epoch=$(date -j -f "%b %e %T %Y %Z" "${end_date}" +%s 2>/dev/null) \
            || end_epoch=$(date -d "${end_date}" +%s 2>/dev/null)
        now_epoch=$(date +%s)
        if [ -z "${end_epoch}" ]; then
            log "ERROR could not parse '${end_date}'; forcing renewal"
            force=1
        else
            days_left=$(( (end_epoch - now_epoch) / 86400 ))
            log "INFO ${CERT_HOST} expires ${end_date} (${days_left} days left)"
            if [ "${days_left}" -gt "${RENEW_DAYS}" ]; then
                log "OK skipping renewal (>${RENEW_DAYS} days remain)"
                exit 0
            fi
        fi
    fi
fi

# The expiry BEFORE, so a renewal is judged by whether it MOVED. On
# 2026-07-20 tailscale answered "Public cert unchanged" and this script still
# logged "renewal complete" and restarted the gateway for nothing.
old_end=$("${OPENSSL}" x509 -in "${cert_file}" -noout -enddate 2>/dev/null | cut -d= -f2)

log "INFO renewing cert via tailscale cert ${CERT_HOST}"
if "${TAILSCALE}" cert "${CERT_HOST}" >> "${LOG}" 2>&1; then
    log "OK tailscale cert succeeded"
else
    rc=$?
    log "ERROR tailscale cert exited ${rc} — gateway NOT restarted; existing cert still in place"
    exit "${rc}"
fi

# Gateway restart — macOS launchd only. If you ported to systemd,
# replace this block with: systemctl --user restart dlna-gateway.service
new_end=$("${OPENSSL}" x509 -in "${cert_file}" -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "${old_end}" ] && [ "${new_end}" = "${old_end}" ]; then
    log "WARN tailscale returned the same certificate (expiry still ${new_end}) — nothing restarted; next week's run retries"
    exit 0
fi

# Every service that LOADS this certificate must be restarted, not just the
# gateway: epub-reader reads the same files, and after the 2026-09-21 renewal
# it served the old certificate until a restart happened to come along.
# CERT_RELOAD_LABELS (comma-separated, .env) lists them; without it, the
# gateway alone, as before.
gateway_label="$(env_get GATEWAY_LAUNCHD_LABEL)"
labels="$(env_get CERT_RELOAD_LABELS)"
labels="${labels:-${gateway_label:-com.roha.dlna-gateway}}"
failed=0
if command -v launchctl >/dev/null 2>&1; then
    uid=$(id -u)
    old_ifs="${IFS}"; IFS=','
    for label in ${labels}; do
        label=$(echo "${label}" | tr -d '[:space:]')
        [ -z "${label}" ] && continue
        log "INFO kickstarting ${label} to load new cert"
        if launchctl kickstart -k "gui/${uid}/${label}" >> "${LOG}" 2>&1; then
            log "OK ${label} kickstarted"
        else
            log "ERROR launchctl kickstart ${label} exited $? — it may still serve the OLD cert; restart it by hand"
            failed=1
        fi
    done
    IFS="${old_ifs}"
else
    log "WARN launchctl not found; restart ${labels} by hand to load the new cert"
fi

log "OK renewal complete; new expiry ${new_end}"
exit "${failed}"
