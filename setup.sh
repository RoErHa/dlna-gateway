#!/usr/bin/env bash
# =============================================================================
# setup.sh  —  DLNA / UPnP → IINA Gateway
#
# Usage:
#   chmod +x setup.sh && ./setup.sh              # set up only
#   ./setup.sh --run                             # set up + start
#   ./setup.sh --run --no-browser                # don't auto-open browser
#   ./setup.sh --run --debug                     # verbose logging
#   ./setup.sh --run --probe http://<ip>/desc.xml  # add server manually
#   ./setup.sh --run --list-devices              # show known devices, exit
#   ./setup.sh --run --reset-devices             # wipe device DB, exit
#   ./setup.sh --restart                         # refresh deps + restart launchd gateway
#
# ── Mac Mini (always-on server) ───────────────────────────────────────────────
# First-time setup on Mac Mini:
#   ./setup.sh --run --no-browser --probe http://localhost:26125/DeviceDescription.xml
#
# Remote access (iPhone/iPad via Tailscale):
#   http://<macmini-tailscale-ip>:8765/
#   Output selector → "📱 Browser" for in-browser playback
#
# ── Auto-start at login (launchd) ────────────────────────────────────────────
#   cp com.roha.dlna-gateway.plist ~/Library/LaunchAgents/
#   launchctl load ~/Library/LaunchAgents/com.roha.dlna-gateway.plist
#   launchctl list | grep dlna          # verify
#   tail -f ~/dlna-gateway/gateway.log  # view log
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
GATEWAY="$SCRIPT_DIR/dlna_gateway.py"
REQS="$SCRIPT_DIR/requirements.txt"
LAUNCHD_LABEL="com.roha.dlna-gateway"
MIN_MINOR=9
PYTHON=""

# ── Colours ───────────────────────────────────────────────────────────────────
R='\033[0;31m' G='\033[0;32m' Y='\033[1;33m' C='\033[0;36m' B='\033[1m' N='\033[0m'
ok()   { echo -e "${G}✓${N}  $*"; }
info() { echo -e "${C}›${N}  $*"; }
warn() { echo -e "${Y}!${N}  $*"; }
die()  { echo -e "${R}✗${N}  $*" >&2; exit 1; }

# ── Find Python 3.9+ ─────────────────────────────────────────────────────────
find_python() {
    for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
        if command -v "$cand" &>/dev/null; then
            minor=$("$cand" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)
            if [ "$minor" -ge "$MIN_MINOR" ]; then
                PYTHON="$cand"
                return
            fi
        fi
    done
    die "Python 3.$MIN_MINOR+ not found. Install it and re-run."
}

# ── Check if existing venv is healthy ────────────────────────────────────────
venv_healthy() {
    # Returns 0 (true) only if the venv python actually executes without error
    [ -f "$VENV_PY" ] && "$VENV_PY" -c "import sys" 2>/dev/null
}

# ── Create / update venv ──────────────────────────────────────────────────────
setup_venv() {
    if [ -d "$VENV_DIR" ]; then
        if venv_healthy; then
            info "Venv exists and is healthy"
        else
            warn "Venv is broken (Python version mismatch or missing library) — rebuilding…"
            rm -rf "$VENV_DIR"
            info "Creating fresh venv at .venv/ …"
            "$PYTHON" -m venv "$VENV_DIR"
            ok "Venv created"
        fi
    else
        info "Creating venv at .venv/ …"
        "$PYTHON" -m venv "$VENV_DIR"
        ok "Venv created"
    fi

    # Always use the venv python directly — never rely on system pip or
    # on 'python' being in PATH (Mac Mini may only have 'python3').
    info "Ensuring pip is available …"
    "$VENV_PY" -m ensurepip --upgrade 2>/dev/null || true
    "$VENV_PY" -m pip install --quiet --upgrade pip
    ok "pip ready"

    if [ -f "$REQS" ]; then
        info "Installing from requirements.txt …"
        "$VENV_PY" -m pip install --quiet -r "$REQS"
        ok "Dependencies installed"
    else
        warn "requirements.txt not found — skipping"
    fi
}

check_files() {
    [ -f "$GATEWAY" ] || die "dlna_gateway.py not found in $SCRIPT_DIR"
}

print_done() {
    echo
    echo -e "${B}Setup complete.${N}"
    echo
    echo "  Run gateway : ./setup.sh --run"
    echo
    echo "  Options:"
    echo "    --port 8765       HTTP port (default 8765)"
    echo "    --no-browser      don't open browser automatically"
    echo "    --debug           verbose logging"
    echo
    echo "  Local:   http://localhost:8765/"
    echo "  Remote:  http://<tailscale-ip>:8765/   (output: 📱 Browser)"
    echo
}

# ── Restart the launchd-managed gateway ────────────────────────────────────────
# The gateway runs under launchd (LaunchAgent $LAUNCHD_LABEL). A bare
# `kill <pid> && ./setup.sh --run` is wrong — launchd respawns the old copy
# before the manual one starts, causing a port conflict. kickstart -k is the
# launchd-correct restart.
restart_gateway() {
    local target="gui/$(id -u)/$LAUNCHD_LABEL"
    if ! launchctl print "$target" &>/dev/null; then
        die "LaunchAgent $LAUNCHD_LABEL not loaded — install it first:
       cp $LAUNCHD_LABEL.plist ~/Library/LaunchAgents/
       launchctl load ~/Library/LaunchAgents/$LAUNCHD_LABEL.plist"
    fi
    info "Restarting $LAUNCHD_LABEL …"
    launchctl kickstart -k "$target"
    ok "Gateway restarted (launchctl kickstart -k $target)"
}

# ── Argument parsing ──────────────────────────────────────────────────────────
RUN=false
RESTART=false
FWD=()

for arg in "$@"; do
    case "$arg" in
        --run)     RUN=true ;;
        --restart) RESTART=true ;;
        *)         FWD+=("$arg") ;;
    esac
done

# ── Main ──────────────────────────────────────────────────────────────────────
echo
echo -e "${B}┌──────────────────────────────────────────┐${N}"
echo -e "${B}│  DLNA / UPnP  →  IINA  Gateway  Setup   │${N}"
echo -e "${B}└──────────────────────────────────────────┘${N}"
echo

find_python
info "Python: $PYTHON  ($("$PYTHON" --version))"

check_files
setup_venv

if $RESTART; then
    echo
    restart_gateway
    echo
    info "Tail the log with: tail -f $SCRIPT_DIR/gateway.log"
elif $RUN; then
    echo
    echo -e "${B}Starting gateway…${N}"
    echo
    exec "$VENV_PY" "$GATEWAY" ${FWD[@]+"${FWD[@]}"}
else
    print_done
fi
