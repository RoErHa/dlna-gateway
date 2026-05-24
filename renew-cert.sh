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

# Load .env (gitignored) so TAILSCALE_CERT_HOST and friends are
# available even when run from launchd / cron with a sparse env.
if [ -f "${REPO_DIR}/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "${REPO_DIR}/.env"
    set +a
fi

CERT_HOST="${TAILSCALE_CERT_HOST:-}"
if [ -z "${CERT_HOST}" ]; then
    echo "[$(date '+%F %T')] FATAL TAILSCALE_CERT_HOST not set (put it in .env)" >&2
    exit 1
fi

RENEW_DAYS="${RENEW_DAYS:-30}"
LOG="${REPO_DIR}/cert-renewal.log"
TAILSCALE="${TAILSCALE_BIN:-$(command -v tailscale)}"
OPENSSL="${OPENSSL_BIN:-$(command -v openssl)}"

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
if command -v launchctl >/dev/null 2>&1; then
    uid=$(id -u)
    label="${GATEWAY_LAUNCHD_LABEL:-com.roha.dlna-gateway}"
    log "INFO kickstarting ${label} to load new cert"
    if launchctl kickstart -k "gui/${uid}/${label}" >> "${LOG}" 2>&1; then
        log "OK gateway kickstarted"
    else
        rc=$?
        log "ERROR launchctl kickstart exited ${rc} — new cert on disk but gateway may still be using old one; restart manually"
        exit "${rc}"
    fi
else
    log "WARN launchctl not found; restart the gateway manually to load the new cert"
fi

new_end=$("${OPENSSL}" x509 -in "${cert_file}" -noout -enddate 2>/dev/null | cut -d= -f2)
log "OK renewal complete; new expiry ${new_end}"
exit 0
