#!/bin/bash
# renew-cert.sh — Tailscale cert auto-renewal for dlna-gateway.
#
# Runs weekly via the com.roha.dlna-cert-renew LaunchAgent. Renews the
# Tailscale-issued Let's Encrypt cert when it has fewer than RENEW_DAYS
# left, then kicks the gateway so it picks up the new cert.
#
# All output appended to cert-renewal.log next to gateway.log.
#
# Manual run:
#   ./renew-cert.sh           # check + renew if needed
#   ./renew-cert.sh --force   # always renew

set -u

REPO_DIR="/Users/ronhamersma/dlna-gateway"
CERT_HOST="ronsmacmini.tail5be6ad.ts.net"
RENEW_DAYS=30
LOG="${REPO_DIR}/cert-renewal.log"
TAILSCALE="/usr/local/bin/tailscale"
OPENSSL="/opt/homebrew/bin/openssl"

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
        end_epoch=$(date -j -f "%b %e %T %Y %Z" "${end_date}" +%s 2>/dev/null)
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

uid=$(id -u)
log "INFO kickstarting com.roha.dlna-gateway to load new cert"
if launchctl kickstart -k "gui/${uid}/com.roha.dlna-gateway" >> "${LOG}" 2>&1; then
    log "OK gateway kickstarted"
else
    rc=$?
    log "ERROR launchctl kickstart exited ${rc} — new cert on disk but gateway may still be using old one; restart manually"
    exit "${rc}"
fi

new_end=$("${OPENSSL}" x509 -in "${cert_file}" -noout -enddate 2>/dev/null | cut -d= -f2)
log "OK renewal complete; new expiry ${new_end}"
exit 0
