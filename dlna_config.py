#!/usr/bin/env python3
"""
dlna_config.py — paths, logging, config persistence.

All other modules import from here so paths are consistent.

Standalone test:
    python dlna_config.py
"""
import json
import logging
import os
from logging.handlers import TimedRotatingFileHandler

# ── Paths ─────────────────────────────────────────────────────────
# Everything lives next to this file (which lives next to gateway.py)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE   = os.path.join(_BASE_DIR, "library.db")
CFG_FILE  = os.path.join(_BASE_DIR, "config.json")
LOG_FILE  = os.path.join(_BASE_DIR, "gateway.log")

# ── Version ───────────────────────────────────────────────────────
# Release-line marker. 1.x lives on `main`; this `2.0` branch carries
# the transport/architecture refresh (see REQUIREMENTS_2.0.md and
# docs/BUILDING_2.0.md). Surfaced at /api/version and in the
# PWA header so a side-by-side 1.x / 2.0 instance is tellable apart.
# Override with $APP_VERSION for ad-hoc builds.
VERSION = os.environ.get("APP_VERSION", "2.0.0-alpha.1")

# ── .env loader (optional) ────────────────────────────────────────
# Load <repo>/.env into os.environ BEFORE any other module reads it.
# Imported here because dlna_config is the first module imported by
# dlna_gateway and by every api_*/dlna_* module. python-dotenv only
# sets variables that aren't already in os.environ, so launchd
# plist / systemd EnvironmentFile values still win.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_BASE_DIR, ".env"))
except ImportError:
    pass   # python-dotenv is optional; deployments setting env via
           # other means don't need it.
# Ensure the base directory exists as soon as this module is imported.
# LibraryDB is a module-level singleton that calls _connect() before
# setup_logging() runs, so we cannot rely on setup_logging to create it.
os.makedirs(_BASE_DIR, exist_ok=True)
M3U_TMP   = "/tmp/dlna-gw-current.m3u"   # current playback playlist (IINA local)
IINA_M3U  = "/tmp/dlna-gw-iina.m3u"      # HTTP-served M3U for remote IINA
IPC_SOCK  = "/tmp/dlna-gw-mpv.sock"       # mpv / IINA IPC socket


# ── Open-file limit ───────────────────────────────────────────────
def raise_fd_limit(target: int = 8192) -> None:
    """Raise this process's open-file SOFT limit toward `target`. Best-effort
    — never raises.

    macOS's default soft RLIMIT_NOFILE is 256. The 1.x gateway gets 8192 from
    its launchd plist (SoftResourceLimits/NumberOfFiles); a SHELL-launched
    process (run-2.0.sh / run-2.0-asgi.sh) instead inherits the Terminal's 256.
    Under Hypercorn's request threadpool + the boot-time LocalFs scan the
    gateway opens enough concurrent FDs (each WAL connection = db + -wal + -shm,
    plus sockets + scanned files) to blow past 256 → EMFILE → sqlite3
    'unable to open database file' (see db_pool.py's FD-exhaustion note). Call
    this at every entrypoint so all launchers match 1.x's headroom."""
    try:
        import resource
    except ImportError:
        return                      # non-unix; nothing to do
    log = logging.getLogger("dlna.config")
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            log.info(f"Raised open-file soft limit {soft} → {want} (hard={hard})")
    except (ValueError, OSError) as e:
        log.warning(f"Could not raise open-file limit: {e}")


# ── Logging ───────────────────────────────────────────────────────

def setup_logging(debug: bool = False) -> logging.Logger:
    """
    Configure root logger: rich console (if available) + daily log file.
    Returns the 'dlna' logger that all modules use as a parent.
    """
    level = logging.DEBUG if debug else logging.INFO
    os.makedirs(_BASE_DIR, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        LOG_FILE, when="midnight", interval=1, backupCount=7,
        encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"))
    file_handler.setLevel(level)

    try:
        from rich.logging import RichHandler
        console = RichHandler(rich_tracebacks=True, show_path=False,
                              log_time_format="[%H:%M:%S]")
    except ImportError:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"))

    console.setLevel(level)

    logging.basicConfig(level=level, handlers=[console, file_handler],
                        force=True)

    root = logging.getLogger("dlna")
    root.info(f"Logging → {LOG_FILE}  (debug={debug})")
    return root


# ── Config persistence ────────────────────────────────────────────

def load_config() -> dict:
    """Load JSON config; return {} on any error."""
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data: dict):
    """Atomically overwrite config.json."""
    os.makedirs(_BASE_DIR, exist_ok=True)
    tmp = CFG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, CFG_FILE)


# ── Self-test ─────────────────────────────────────────────────────

def _test():
    log = setup_logging(debug=True)
    log.info("=== dlna_config self-test ===")
    log.info(f"BASE_DIR : {_BASE_DIR}")
    log.info(f"DB_FILE  : {DB_FILE}  (exists={os.path.exists(DB_FILE)})")
    log.info(f"CFG_FILE : {CFG_FILE}  (exists={os.path.exists(CFG_FILE)})")
    log.info(f"LOG_FILE : {LOG_FILE}")
    cfg = load_config()
    log.info(f"Config   : {cfg}")
    log.info("PASS — dlna_config OK")


if __name__ == "__main__":
    _test()
