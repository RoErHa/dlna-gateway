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

# ── .env loader ───────────────────────────────────────────────────
# .env is THE configuration file (2026-07-13 — user decision: single
# source, safe clean-slate installs; the LaunchAgent plist keeps only
# PATH + ProgramArguments). It MUST load before ANY config key is read
# — including VERSION below — and it must load even when python-dotenv
# isn't installed: the old optional-import silently skipped loading,
# which cost real debugging time (see CLAUDE.md ".env caveat"). The
# fallback parser handles the KEY=VALUE subset we use (comments, blank
# lines, optional quotes). Existing os.environ values always win, so a
# shell export / launchd setenv still overrides for ad-hoc runs.
def _load_env_file(path: str) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                if key and key not in os.environ:
                    os.environ[key] = val
    except OSError:
        pass   # no .env — process env / defaults apply


_load_env_file(os.path.join(_BASE_DIR, ".env"))

# ── Version ───────────────────────────────────────────────────────
# Release-line marker. 1.x lives on `main`; this `2.0` branch carries
# the transport/architecture refresh (see REQUIREMENTS_2.0.md and
# docs/BUILDING_2.0.md). Surfaced at /api/version and in the
# PWA header so a side-by-side 1.x / 2.0 instance is tellable apart.
# Set via $APP_VERSION (.env).
VERSION = os.environ.get("APP_VERSION", "2.0.0-alpha.1")
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
        else:
            # Always log the effective limit — otherwise a silent no-op leaves
            # us guessing whether the gateway has 256 or 8192 FDs of headroom.
            log.info(f"Open-file soft limit already {soft} (hard={hard})")
    except (ValueError, OSError) as e:
        log.warning(f"Could not raise open-file limit: {e}")


# ── Logging ───────────────────────────────────────────────────────

def close_quietly(resource, what: str = "connection") -> None:
    """Close `resource`, swallowing any failure — but audibly.

    This replaces ~30 anonymous `try: conn.close() / except Exception:
    pass` blocks scattered through the SOAP, proxy and pool code
    (2026-08-20). Every one of them sits in a `finally` or an error path
    where the REAL error has already been logged, so a failure to close
    an already-dead socket is genuinely uninteresting — but an
    unexplained bare `pass` is indistinguishable from a defect being
    swallowed, and there is no way to see the pattern is deliberate.

    Named + logged at DEBUG makes the decision explicit, greppable, and
    observable when a socket leak is actually suspected
    (`GATEWAY_DEBUG=1`, then `grep close_quietly gateway.log`).

    Deliberately catches broadly: this runs during cleanup, frequently
    while another exception is propagating, and must never replace the
    original failure with one of its own. It also never RAISES from the
    logging call itself for the same reason."""
    if resource is None:
        return
    try:
        resource.close()
    except Exception as e:                       # noqa: BLE001 — see docstring
        try:
            logging.getLogger("dlna.config").debug(
                f"close_quietly: {what} close failed "
                f"({type(e).__name__}: {e}) — ignored")
        except (OSError, ValueError):
            pass       # a broken logging handler must not mask the cleanup


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
    except FileNotFoundError:
        return {}                      # no config yet — expected, not an error
    except (OSError, ValueError) as e:
        # A CORRUPT config silently reading as {} is how a whole set of
        # settings goes missing with no clue why. Never silent.
        logging.getLogger("dlna.config").warning(
            f"config {CFG_FILE} unreadable ({type(e).__name__}: {e}) — "
            f"continuing with defaults; settings in it are being IGNORED")
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
