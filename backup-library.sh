#!/usr/bin/env bash
# =============================================================================
# backup-library.sh — weekly snapshot of library.db
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
# Retention: keeps the newest $KEEP snapshots (default 8 ≈ two months) and
# prunes older ones — an unbounded backup directory is its own failure.
#
# USAGE
#   ./backup-library.sh              # take a snapshot now
#   ./backup-library.sh --list       # show what exists
#   ./backup-library.sh --keep 12    # override retention for this run
#   KEEP=4 ./backup-library.sh       # same, via env
#
# RESTORE (deliberately manual — clobbering a live index should be a choice)
#   launchctl bootout gui/$(id -u)/com.roha.dlna-gateway
#   cp ~/dlna-gateway-backups/library-YYYYMMDD-HHMMSS.db library.db
#   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.roha.dlna-gateway.plist
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

DB="${LIBRARY_DB:-library.db}"
DEST="${BACKUP_DIR:-$HOME/dlna-gateway-backups}"
KEEP="${KEEP:-8}"
LOG="library-backup.log"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"; }

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
    --keep) KEEP="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# ── preconditions ────────────────────────────────────────────────────────
if [ ! -f "$DB" ]; then
  log "✗ FAILED: $DB not found (cwd $(pwd))"
  exit 1
fi
command -v sqlite3 >/dev/null 2>&1 || { log "✗ FAILED: sqlite3 not on PATH"; exit 1; }
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
  log "✗ FAILED: sqlite3 .backup errored"
  drop_db "$OUT.part"
  exit 1
fi

# ── verify before trusting it ────────────────────────────────────────────
CHECK="$(sqlite3 "$OUT.part" 'PRAGMA integrity_check;' 2>&1 | head -1)"
if [ "$CHECK" != "ok" ]; then
  log "✗ FAILED: integrity_check on the snapshot said: $CHECK"
  drop_db "$OUT.part"
  exit 1
fi

# Sanity-check the CONTENT too: an empty-but-valid database passes
# integrity_check happily, and would be a silent disaster to keep as the
# newest backup while pruning a good one.
TRACKS="$(sqlite3 "$OUT.part" 'SELECT count(*) FROM tracks;' 2>/dev/null || echo 0)"
if [ "${TRACKS:-0}" -lt 1 ]; then
  log "✗ FAILED: snapshot holds $TRACKS tracks — refusing to keep it"
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
ls -1t "$DEST"/library-*.db 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r f; do
  [ -n "$f" ] || continue
  drop_db "$f"
  log "  pruned $(basename "$f")"
done

COUNT="$(ls -1 "$DEST"/library-*.db 2>/dev/null | wc -l | tr -d ' ')"
log "  $COUNT snapshot(s) retained in $DEST (keep=$KEEP)"
