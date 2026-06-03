#!/usr/bin/env python3
"""
post_beets_reindex.py — the "make beets' work visible" step.

After `tools/beets_enrich.py` has written clean tags INTO the files in
place, two things have to happen before the gateway actually shows them:

  1. Drop the AcoustID `metadata_overrides` rows. LocalFs track URLs are
     PATH-based (`sha1(rel_path)`), so a beets-tagged file keeps the same
     URL — which means the COALESCE pass in `LibraryDB.upsert_tracks`
     (dlna_library.py) lays the OLD `source='acoustid'` override straight
     back on top of beets' fresh tags and masks them. Clearing the
     acoustid overrides lets the file tags show through.
  2. Re-index the LocalFs library so the mutagen indexer re-reads the
     enriched tags (POST /api/index/rebuild — `clear(udn)` + full crawl).

This tool does both in the right order (clean, THEN reindex).

The manual-override safety: ONLY `source='acoustid'` rows are deleted.
`source='manual'` (your edits + the year-drift / improve_song_years
corrections) is NEVER touched — those legitimately win over beets.
`notfound` / `video_skip` rows carry NULL metadata, so they can't mask
anything and are left alone too.

DRY-RUN by default — prints the breakdown and what it WOULD do. Pass
`--apply` to actually delete + reindex (auto-backs-up library.db first).

Usage:
    # preview (default): show override breakdown + planned actions
    python3 tools/post_beets_reindex.py
    python3 tools/post_beets_reindex.py --dry-run        # explicit alias

    # do it: backup library.db, drop acoustid overrides, kick a reindex
    python3 tools/post_beets_reindex.py --apply

    # non-interactive (skip the confirm prompt)
    python3 tools/post_beets_reindex.py --apply -y

    # only clean overrides, don't reindex (e.g. reindex separately)
    python3 tools/post_beets_reindex.py --apply --no-reindex

    # only reindex, keep all overrides (no DB mutation)
    python3 tools/post_beets_reindex.py --apply --no-clean

We chose Option A — beets is the SOLE metadata authority and the gateway's
AcoustID worker stays off. As a guard, this tool REFUSES to clear overrides
while ACOUSTID_API_KEY is set (the worker would just re-create them on the
next startup scan). Unset it first (`launchctl unsetenv ACOUSTID_API_KEY`),
or override with --ignore-acoustid-key.

    # point at a non-default gateway / server / DB
    python3 tools/post_beets_reindex.py --apply --gateway http://host:8765 \
        --udn uuid:localfs-... --db /path/to/library.db
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

# Reuse the reindex helpers from beets_enrich (same tools/ dir). Support both
# `python3 tools/post_beets_reindex.py` (tools/ on sys.path[0]) and a
# `tools.post_beets_reindex` package import (tests / -m).
try:
    from beets_enrich import (gateway_acoustid_enabled,  # noqa: F401
                              pick_localfs_udn, trigger_reindex)
except ImportError:                                  # pragma: no cover
    from tools.beets_enrich import (gateway_acoustid_enabled,  # noqa: F401
                                    pick_localfs_udn, trigger_reindex)

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "library.db"
_DEFAULT_GATEWAY = "http://127.0.0.1:8765"


def _acoustid_key_active(gateway: str = "") -> bool:
    """True if the AcoustID worker is (or would be) live — clearing the
    acoustid overrides then is pointless, because the gateway's 120s startup
    scan re-fingerprints every now-bare track and re-creates them, re-masking
    beets (Option A: beets is the sole authority, so AcoustID must stay off).

    PRIMARY signal: ask the running gateway via GET /api/acoustid/status —
    its `enabled` reflects the actually-loaded key regardless of source
    (.env via python-dotenv, launchctl setenv, or the plist). This is the
    only reliable check: the 2026-06-03 incident was a key in `.env` that a
    local `os.environ` / `launchctl getenv` check could not see.

    FALLBACK (gateway unreachable / field missing): this process's own env
    and `launchctl getenv`. These miss the .env source, so they're a weak
    backstop, not the truth."""
    if gateway:
        enabled = gateway_acoustid_enabled(gateway)
        if enabled is not None:
            return enabled              # authoritative
    if os.environ.get("ACOUSTID_API_KEY", "").strip():
        return True
    try:
        out = subprocess.run(["launchctl", "getenv", "ACOUSTID_API_KEY"],
                             capture_output=True, text=True, timeout=5)
        return bool(out.stdout.strip())
    except Exception:                                # noqa: BLE001
        return False


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"library.db not found at {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _report_overrides(conn: sqlite3.Connection) -> None:
    """Print a breakdown of metadata_overrides by source so the user sees
    exactly what survives (manual) and what gets cleared (acoustid)."""
    cur = conn.execute(
        "SELECT source, COUNT(*) AS n FROM metadata_overrides "
        "GROUP BY source ORDER BY n DESC")
    rows = cur.fetchall()
    print("metadata_overrides breakdown:")
    if not rows:
        print("  (table empty)")
        print()
        return
    for r in rows:
        keep = "  ← CLEARED" if r["source"] == "acoustid" else \
               ("  (kept — never touched)" if r["source"] == "manual" else
                "  (kept — NULL metadata, masks nothing)")
        print(f"  {r['source']:>11s}  {r['n']:>7,d}{keep}")
    print()


def _count_acoustid(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM metadata_overrides "
        "WHERE source='acoustid'").fetchone()[0]


def _delete_acoustid(conn: sqlite3.Connection) -> int:
    """Delete ONLY source='acoustid' rows. Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM metadata_overrides WHERE source='acoustid'")
    conn.commit()
    return cur.rowcount or 0


def _backup_db(db_path: Path) -> Path:
    """Copy library.db → library.db.<epoch>.bak before mutating."""
    bak = db_path.with_suffix(db_path.suffix + f".{int(time.time())}.bak")
    shutil.copy2(db_path, bak)
    return bak


def main(argv: Optional[Iterable[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="Post-beets: drop AcoustID metadata_overrides (so beets' "
                    "fresh tags aren't masked) and reindex the LocalFs "
                    "library. Dry-run by default; --apply to act.")
    p.add_argument("--db", default=str(_DEFAULT_DB),
                   help="Path to library.db (default: %(default)s)")
    p.add_argument("--gateway", default=_DEFAULT_GATEWAY,
                   help="Gateway base URL (default: %(default)s)")
    p.add_argument("--udn", default=None,
                   help="Server UDN to reindex (default: auto-pick LocalFs)")
    p.add_argument("--apply", action="store_true",
                   help="Actually delete acoustid overrides + reindex "
                        "(default is a dry-run preview)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="Explicit preview (the default when --apply is absent)")
    p.add_argument("--no-clean", action="store_true",
                   help="Skip clearing acoustid overrides (reindex only)")
    p.add_argument("--no-reindex", action="store_true",
                   help="Skip the reindex (clean overrides only)")
    p.add_argument("--no-backup", action="store_true",
                   help="Skip the library.db backup before deleting")
    p.add_argument("--ignore-acoustid-key", action="store_true",
                   help="Proceed even if ACOUSTID_API_KEY is set (default: "
                        "refuse to clear overrides while the worker is live)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip the confirmation prompt")
    args = p.parse_args(argv)

    if args.no_clean and args.no_reindex:
        print("error: --no-clean and --no-reindex together leave nothing to "
              "do.", file=sys.stderr)
        return 2

    apply = args.apply and not args.dry_run
    db_path = Path(args.db).expanduser().resolve()

    try:
        conn = _connect(db_path)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        print(f"DB:      {db_path}")
        print(f"Gateway: {args.gateway}")
        print(f"Mode:    {'APPLY' if apply else 'dry-run (preview only)'}")
        print()

        # The acoustid-worker footgun only matters when we're clearing.
        key_active = (not args.no_clean) and _acoustid_key_active(args.gateway)

        n_acoustid = _count_acoustid(conn)
        if not args.no_clean:
            _report_overrides(conn)
            print(f"Step 1 — clear overrides: {n_acoustid:,} "
                  f"source='acoustid' row(s) would be deleted "
                  "(manual / notfound / video_skip kept).")
            if key_active:
                print("\n  ⚠ The gateway's AcoustID worker is LIVE "
                      "(/api/acoustid/status: enabled).\n"
                      "    Clearing overrides now is futile: the 120s startup "
                      "scan will re-fingerprint\n"
                      "    every bare track and re-create them, re-masking "
                      "beets. We chose Option A\n"
                      "    (beets is the sole authority), so turn AcoustID off "
                      "first. The key may come\n"
                      "    from .env (comment the ACOUSTID_API_KEY line) OR "
                      "launchctl setenv — check\n"
                      "    /api/acoustid/status after restarting. Override with "
                      "--ignore-acoustid-key.")
        else:
            print("Step 1 — clear overrides: SKIPPED (--no-clean).")

        if not args.no_reindex:
            who = args.udn or "auto-pick uuid:localfs-*"
            print(f"Step 2 — reindex: POST {args.gateway}/api/index/rebuild "
                  f"({who}).")
        else:
            print("Step 2 — reindex: SKIPPED (--no-reindex).")
        print()

        if not apply:
            print("Dry-run; nothing changed. Re-run with --apply to act.")
            return 0

        # ── guard: refuse to clear while the acoustid worker is live ─────
        if key_active and not args.ignore_acoustid_key:
            print("error: the gateway's AcoustID worker is enabled "
                  "(/api/acoustid/status) — refusing to clear overrides "
                  "(they'd be re-created on the next startup scan).\n"
                  "       turn it off first — the key is usually in .env "
                  "(comment the ACOUSTID_API_KEY line) or launchctl setenv — "
                  "then restart the gateway,\n"
                  "       or pass --ignore-acoustid-key / --no-clean.",
                  file=sys.stderr)
            return 2

        # ── confirmation ────────────────────────────────────────────────
        if not args.yes:
            try:
                ans = input("Proceed? This deletes the acoustid overrides "
                            "and starts a full reindex. [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans not in ("y", "yes"):
                print("Aborted.")
                return 1

        # ── Step 1: clean ───────────────────────────────────────────────
        if not args.no_clean:
            if n_acoustid and not args.no_backup:
                bak = _backup_db(db_path)
                print(f"backed up library.db → {bak}")
            deleted = _delete_acoustid(conn)
            print(f"deleted {deleted:,} source='acoustid' override row(s).")
        # close the DB handle before the gateway re-crawls it.
        conn.close()

        # ── Step 2: reindex ─────────────────────────────────────────────
        if not args.no_reindex:
            ok, msg = trigger_reindex(args.gateway, args.udn)
            print(("reindex: " if ok else "reindex FAILED: ") + msg,
                  file=sys.stdout if ok else sys.stderr)
            if not ok:
                return 1
            print("\nReindex started in the background. Watch progress with:")
            print("  tail -f gateway.log    # or the PWA index bar")
        return 0
    finally:
        try:
            conn.close()
        except Exception:                            # noqa: BLE001
            pass


if __name__ == "__main__":
    raise SystemExit(main())
