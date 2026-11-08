#!/usr/bin/env bash
# =============================================================================
# backup-library.sh — daily snapshot of library.db
#
# WHY: library.db is the gateway's whole index — 26k music tracks, 11k
# audiobook chapters, 4k videos — plus everything a person CREATED that
# deliberately survives a rebuild: playlists, album favourites, audiobook
# resume positions, play counts, lyrics, radio stations, metadata overrides.
# Re-indexing can rebuild `tracks`; it cannot rebuild those. The file is
# gitignored (and, since 2026-08-20, purged from git history), so git is not
# a safety net for it. This is.
#
# THE ONE THING THAT MAKES THIS CORRECT: `sqlite3 .backup`, not `cp`.
# The gateway is live and runs in WAL mode, so a plain copy can capture a
# torn page set or miss a checkpointed WAL and yield a file that opens fine
# and is subtly wrong. `.backup` uses SQLite's online-backup API, which is
# safe against concurrent writers and produces a consistent snapshot without
# stopping the gateway. Every snapshot is then integrity-checked before it
# is allowed to count as a backup.
#
# Retention (daily since 2026-11-08; it ran weekly with "newest 8" before):
# the newest BACKUP_KEEP_DAILY snapshots (default 7) PLUS the newest one of
# each of the last BACKUP_KEEP_WEEKLY ISO weeks (default 8), so a daily
# schedule keeps the old two months of reach in ~15 files instead of 8 days.
# A snapshot's date comes from its NAME, never its mtime (a copy or restore
# touches mtime), and a name that does not parse is never pruned. An
# unbounded backup directory is its own failure, hence any pruning at all.
#
# OFF-DISK (hardening #2, 2026-11-08): the snapshots live on the same SSD as
# library.db, so the second copy is Time Machine's job. Time Machine says
# nothing when it stops (an unplugged or full disk shows only in System
# Settings), so after the local snapshot this job checks that Time Machine's
# newest backup is at most BACKUP_TM_MAX_AGE_DAYS old (.env, default 3, 0 =
# off), read from Time Machine's own preferences file. Too old, or none on
# record, makes the job exit 1, and the EXIT trap sends the alert. The local
# snapshot is already done by then, so this check can never cost one.
# Skipped where that file is absent (not macOS) or names no destination, so
# an install that never set Time Machine up isn't nagged every day. Daily
# since 2026-11-08: weekly, a stopped Time Machine went ~10 days unnoticed.
#
# USAGE
#   ./backup-library.sh              # take a snapshot now
#   ./backup-library.sh --list       # show what exists
#   ./backup-library.sh --keep 12    # keep 12 daily snapshots this run
#   BACKUP_KEEP_DAILY / BACKUP_KEEP_WEEKLY in .env set the defaults
#
# RESTORE (deliberately manual — clobbering a live index should be a choice)
#   launchctl bootout gui/$(id -u)/com.roha.dlna-gateway
#   cp ~/dlna-gateway-backups/library-YYYYMMDD-HHMMSS.db library.db
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.roha.dlna-gateway.plist
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

# .env is READ, never sourced: sourcing runs it as shell (the 2026-10-23
# renew-cert.sh lesson). The real environment wins over .env.
env_get() {
  local key="$1" val
  val="${!key:-}"
  if [ -z "${val}" ] && [ -f .env ]; then
    val=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" .env \
        | tail -1 | cut -d= -f2- \
        | sed -E "s/^[[:space:]]+|[[:space:]]+\$//g; s/^[\"'](.*)[\"']\$/\1/")
  fi
  printf '%s' "${val}"
}

DB="${LIBRARY_DB:-library.db}"
DEST="${BACKUP_DIR:-$HOME/dlna-gateway-backups}"
KEEP_DAILY="$(env_get BACKUP_KEEP_DAILY)"; KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="$(env_get BACKUP_KEEP_WEEKLY)"; KEEP_WEEKLY="${KEEP_WEEKLY:-8}"
TM_MAX_AGE_DAYS="$(env_get BACKUP_TM_MAX_AGE_DAYS)"; TM_MAX_AGE_DAYS="${TM_MAX_AGE_DAYS:-3}"
LOG="library-backup.log"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }
FAIL_MSG=""
fail() { FAIL_MSG="$*"; log "✗ FAILED: $*"; }

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
        log "  ✗ alert '$1' was NOT sent (exit ${rc}): $(printf '%s' "${out}" | tr '\n' ' ' | cut -c1-300)"
    fi
    return 0
}

# ── flags ────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --list)
      if [ -d "$DEST" ]; then
        echo "Snapshots in $DEST:"
        ls -lh "$DEST"/library-*.db 2>/dev/null | awk '{printf "  %s  %6s  %s %s %s\n", $9, $5, $6, $7, $8}' \
          || echo "  (none yet)"
      else
        echo "No backup directory yet: $DEST"
      fi
      exit 0 ;;
    --keep) KEEP_DAILY="$2"; shift 2 ;;
    -h|--help) sed -n '2,50p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "${KEEP_DAILY}" in ''|*[!0-9]*|0) echo "BACKUP_KEEP_DAILY / --keep must be a whole number ≥ 1, got '${KEEP_DAILY}'" >&2; exit 2 ;; esac
case "${KEEP_WEEKLY}" in ''|*[!0-9]*) echo "BACKUP_KEEP_WEEKLY must be a whole number, got '${KEEP_WEEKLY}'" >&2; exit 2 ;; esac

# Any non-zero exit from here on is a failed backup, including one `set -e`
# raised on a line nobody anticipated: tell the owner, not just the log.
on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    push_alert backup_failed "library backup FAILED" \
      "${FAIL_MSG:-backup-library.sh exited $rc}. Existing snapshots are untouched; see library-backup.log." \
      --priority high
  fi
}
trap on_exit EXIT

# ── preconditions ────────────────────────────────────────────────────────
if [ ! -f "$DB" ]; then
  fail "$DB not found (cwd $(pwd))"
  exit 1
fi
command -v sqlite3 >/dev/null 2>&1 || { fail "sqlite3 not on PATH"; exit 1; }
mkdir -p "$DEST"

STAMP="$(date '+%Y%m%d-%H%M%S')"
OUT="$DEST/library-$STAMP.db"

# The source runs in WAL mode, so the .backup DESTINATION opens in WAL too and
# gets `-shm` / `-wal` sidecars beside it. Renaming the .part away orphans
# them, and the retention glob (library-*.db) can never match them — so before
# this they accumulated 2–3 files per run, for ever. Every path that disposes
# of a database file goes through here.
drop_db() { rm -f "$1" "$1-shm" "$1-wal"; }

# ── snapshot ─────────────────────────────────────────────────────────────
# .backup is safe while the gateway is writing; cp is not. Snapshot to a
# .part file so an interrupted run can never leave a half-written file
# looking like a valid backup.
if ! sqlite3 "$DB" ".backup '$OUT.part'" 2>>"$LOG"; then
  fail "sqlite3 .backup errored"
  drop_db "$OUT.part"
  exit 1
fi

# ── verify before trusting it ────────────────────────────────────────────
CHECK="$(sqlite3 "$OUT.part" 'PRAGMA integrity_check;' 2>&1 | head -1)"
if [ "$CHECK" != "ok" ]; then
  fail "integrity_check on the snapshot said: $CHECK"
  drop_db "$OUT.part"
  exit 1
fi

# Sanity-check the CONTENT too: an empty-but-valid database passes
# integrity_check happily, and would be a silent disaster to keep as the
# newest backup while pruning a good one.
TRACKS="$(sqlite3 "$OUT.part" 'SELECT count(*) FROM tracks;' 2>/dev/null || echo 0)"
if [ "${TRACKS:-0}" -lt 1 ]; then
  fail "snapshot holds $TRACKS tracks — refusing to keep it"
  drop_db "$OUT.part"
  exit 1
fi

mv "$OUT.part" "$OUT"
# Whatever WAL sidecars the snapshot left under the .part name are now
# orphaned by the rename; the snapshot itself is complete and verified.
rm -f "$OUT.part-shm" "$OUT.part-wal"
SIZE="$(du -h "$OUT" | awk '{print $1}')"
log "✓ snapshot $(basename "$OUT")  ${SIZE}  ${TRACKS} tracks  (integrity ok)"

# ── retention ────────────────────────────────────────────────────────────
# Prune only AFTER a good snapshot exists, so a failing run never reduces
# the number of backups you have.
#
# NB: plain `while read`, not `mapfile` — macOS ships bash 3.2, where
# mapfile does not exist, and this script's whole job is to run unattended
# under launchd on exactly that bash.
#
# Newest first BY NAME (library-YYYYMMDD-HHMMSS.db sorts by time). Keep the
# first KEEP_DAILY; beyond those, keep the first (= newest) snapshot of each
# ISO week until KEEP_WEEKLY distinct weeks are covered. The current week
# counts as one of them, so "8 weekly" reaches back eight weeks from today.
ls -1 "$DEST"/library-*.db 2>/dev/null | sort -r | {
  n=0; weeks=0; seen=":"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    n=$((n + 1))
    day="$(basename "$f" | sed -n 's/^library-\([0-9]\{8\}\)-[0-9]\{6\}\.db$/\1/p')"
    week=""
    [ -n "$day" ] && week="$(date -j -f '%Y%m%d' "$day" '+%G-%V' 2>/dev/null)" || true
    keep=0
    [ "$n" -le "$KEEP_DAILY" ] && keep=1
    if [ -z "$week" ]; then
      keep=1                               # undatable: never prune it
    else
      case "$seen" in
        *":$week:"*) ;;                    # this week already has its newest
        *) seen="$seen$week:"
           if [ "$weeks" -lt "$KEEP_WEEKLY" ]; then weeks=$((weeks + 1)); keep=1; fi ;;
      esac
    fi
    if [ "$keep" -eq 0 ]; then
      drop_db "$f"
      log "  pruned $(basename "$f")"
    fi
  done
}

COUNT="$(ls -1 "$DEST"/library-*.db 2>/dev/null | wc -l | tr -d ' ')"
log "  $COUNT snapshot(s) retained in $DEST (daily=$KEEP_DAILY, weekly=$KEEP_WEEKLY)"

# ── off-disk: is Time Machine still copying all of this? ─────────────────
# Runs LAST on purpose: the local snapshot and its retention are finished,
# so nothing here can cost a backup. Anything wrong → exit 1 → EXIT trap
# → backup_failed alert naming the reason.
tm_check() {
  case "${TM_MAX_AGE_DAYS}" in
    ''|*[!0-9]*) fail "BACKUP_TM_MAX_AGE_DAYS='${TM_MAX_AGE_DAYS}' is not a whole number of days"; return 1 ;;
    0) return 0 ;;
  esac
  # Time Machine's own record, NOT `tmutil latestbackup`: under launchd that
  # needs Full Disk Access, and without it stalls ~26 s and then refuses
  # (measured 2026-11-08), which would be a false alarm every week. This
  # plist is world-readable and lists each destination's SnapshotDates (UTC).
  local prefs="${TM_PREFS:-/Library/Preferences/com.apple.TimeMachine.plist}"
  [ -f "${prefs}" ] || return 0                     # not macOS, or never set up
  command -v defaults >/dev/null 2>&1 || return 0
  local dump newest tm_epoch now age
  dump="$(defaults read "${prefs%.plist}" 2>&1)" || true
  if ! printf '%s' "${dump}" | grep -q 'DestinationID'; then
    log "  Time Machine: no destination configured — off-disk check skipped"
    return 0
  fi
  # Newest date inside any SnapshotDates array, across every destination.
  newest="$(printf '%s\n' "${dump}" \
      | awk '/SnapshotDates/ {f=1; next} f && /\)/ {f=0} f' \
      | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}' \
      | sort | tail -1)" || true
  if [ -z "${newest}" ]; then
    fail "local snapshot OK, but Time Machine has no completed backup on record — is the disk attached?"
    return 1
  fi
  tm_epoch="$(date -j -u -f '%Y-%m-%d %H:%M:%S' "${newest}" '+%s' 2>/dev/null)" || true
  if [ -z "${tm_epoch}" ]; then
    fail "local snapshot OK, but Time Machine's backup date '${newest}' could not be read"
    return 1
  fi
  now="$(date '+%s')"
  age=$(( (now - tm_epoch) / 86400 ))
  if [ "${age}" -gt "${TM_MAX_AGE_DAYS}" ]; then
    fail "local snapshot OK, but Time Machine's newest backup is ${age} days old (${newest} UTC; limit ${TM_MAX_AGE_DAYS}) — is the disk attached?"
    return 1
  fi
  log "  Time Machine: newest backup ${newest} UTC (${age} d old, limit ${TM_MAX_AGE_DAYS})"
}
tm_check || exit 1
