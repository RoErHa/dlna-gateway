#!/usr/bin/env python3
"""Copy new videos from Immich's originals into the gateway's video root.

The pipeline the gateway already provides: anything landing in
`/Volumes/SAMDATA/GWMovies` is picked up by the 5-minute periodic video
scan, GPS-reverse-geocoded, titled `<location>_<YYYYMMDD>_<HHMM>` and
served in the Videos section (see docs/VIDEO_SUPPORT.md). The one manual
step was copying phone videos out of Immich — this tool automates it.

  * Sources: Immich's ORIGINALS — `library/` + `upload/` under the
    Immich data volume. `encoded-video/` (transcodes), `thumbs/`,
    `profile/` and `backups/` are never touched.
  * Dedup is by CONTENT HASH (BLAKE2b), recorded in a cache DB at
    `<dest>/.immich-import.db`: files already in GWMovies (however they
    got there, whatever their name) are registered on first run and
    never copied again; sources already processed are skipped by
    (path, size, mtime) without re-reading them. Re-running after new
    phone uploads only copies what's new — safe as a habit.
  * Filenames are preserved (the gateway titles by metadata, not name);
    a name collision with DIFFERENT content gets a ` (2)` suffix.
  * DRY-RUN by default; --apply copies. The hash cache is warmed even
    in dry-run (that's the slow part) so the following --apply is fast.

    python3 tools/immich_import.py                # preview
    python3 tools/immich_import.py --apply        # copy new videos
    python3 tools/immich_import.py --min-seconds 10   # skip Live-Photo clips
"""
import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SOURCE = "/Volumes/SAMDATA-1TB/IMMICH-UPLOAD"
DEFAULT_DEST = "/Volumes/SAMDATA/GWMovies"
SOURCE_SUBDIRS = ("library", "upload")
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".webm",
              ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg"}
FFPROBE = "/opt/homebrew/bin/ffprobe"


def file_hash(path: Path, chunk=1 << 20) -> str:
    h = hashlib.blake2b(digest_size=20)
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def video_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30).stdout.strip()
        return float(out)
    except Exception:                                     # noqa: BLE001
        return -1.0          # unknowable → treated as long enough


class Cache:
    """Content-hash memory at <dest>/.immich-import.db — the "already
    processed" record that makes re-runs incremental and duplicate-proof."""

    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS dest_files(
                hash TEXT PRIMARY KEY, name TEXT, size INTEGER);
            CREATE TABLE IF NOT EXISTS dest_seen(
                path TEXT PRIMARY KEY, size INTEGER, mtime REAL, hash TEXT);
            CREATE TABLE IF NOT EXISTS src_seen(
                path TEXT PRIMARY KEY, size INTEGER, mtime REAL, hash TEXT,
                status TEXT, dest_name TEXT, at INTEGER);
        """)

    def dest_hashes(self):
        return {r[0] for r in
                self.conn.execute("SELECT hash FROM dest_files")}

    def register_dest(self, path: Path):
        """Hash a dest file unless (path, size, mtime) is already known."""
        st = path.stat()
        row = self.conn.execute(
            "SELECT hash FROM dest_seen WHERE path=? AND size=? AND mtime=?",
            (str(path), st.st_size, st.st_mtime)).fetchone()
        if row:
            return row[0]
        h = file_hash(path)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO dest_seen VALUES (?,?,?,?)",
                (str(path), st.st_size, st.st_mtime, h))
            self.conn.execute(
                "INSERT OR REPLACE INTO dest_files VALUES (?,?,?)",
                (h, path.name, st.st_size))
        return h

    def src_status(self, path: Path):
        st = path.stat()
        row = self.conn.execute(
            "SELECT status FROM src_seen WHERE path=? AND size=? AND mtime=?",
            (str(path), st.st_size, st.st_mtime)).fetchone()
        return row[0] if row else None

    def record_src(self, path: Path, h: str, status: str, dest_name=""):
        st = path.stat()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO src_seen VALUES (?,?,?,?,?,?,?)",
                (str(path), st.st_size, st.st_mtime, h, status, dest_name,
                 int(time.time())))


def iter_source_videos(source: Path, subdirs=SOURCE_SUBDIRS):
    for sub in subdirs:
        base = source / sub
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for f in filenames:
                if Path(f).suffix.lower() in VIDEO_EXTS:
                    yield Path(dirpath) / f


def unique_dest(dest_dir: Path, name: str) -> Path:
    out = dest_dir / name
    stem, suffix = out.stem, out.suffix
    n = 2
    while out.exists():
        out = dest_dir / f"{stem} ({n}){suffix}"
        n += 1
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Copy new Immich videos into the gateway video root "
                    "(dry-run by default).")
    ap.add_argument("--source", default=DEFAULT_SOURCE)
    ap.add_argument("--dest", default=DEFAULT_DEST)
    ap.add_argument("--apply", action="store_true",
                    help="actually copy (default: preview only)")
    ap.add_argument("--min-seconds", type=float, default=0,
                    help="skip clips shorter than this (e.g. 10 to skip "
                         "Live-Photo motion clips); default 0 = all")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N new imports (0 = no limit)")
    args = ap.parse_args(argv)

    source, dest = Path(args.source), Path(args.dest)
    if not source.is_dir():
        print(f"FATAL: source not mounted: {source}"); return 2
    if not dest.is_dir():
        print(f"FATAL: dest not mounted: {dest}"); return 2

    cache = Cache(dest / ".immich-import.db")

    # 1. Register everything already in the video root (incremental —
    #    only new/changed files get hashed).
    dest_files = [p for p in dest.rglob("*")
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    print(f"registering {len(dest_files)} existing files in {dest} …")
    for p in dest_files:
        cache.register_dest(p)
    have = cache.dest_hashes()

    # 2. Walk Immich originals.
    stats = {"new": 0, "dup": 0, "seen": 0, "short": 0}
    t0 = time.monotonic()
    for src in iter_source_videos(source):
        prior = cache.src_status(src)
        if prior is not None:
            stats["seen"] += 1
            continue
        if args.min_seconds > 0:
            dur = video_duration(src)
            if 0 <= dur < args.min_seconds:
                cache.record_src(src, "", "short")
                stats["short"] += 1
                continue
        h = file_hash(src)
        if h in have:
            cache.record_src(src, h, "duplicate")
            stats["dup"] += 1
            continue
        stats["new"] += 1
        rel = src.relative_to(source)
        if args.apply:
            out = unique_dest(dest, src.name)
            shutil.copy2(src, out)
            cache.register_dest(out)
            cache.record_src(src, h, "imported", out.name)
            have.add(h)
            print(f"  + {rel}  →  {out.name}")
        else:
            print(f"  would copy: {rel}")
        if args.limit and stats["new"] >= args.limit:
            print(f"--limit {args.limit} reached, stopping")
            break

    el = time.monotonic() - t0
    verb = "imported" if args.apply else "would import"
    print(f"\n{verb}: {stats['new']} · already in dest (content): "
          f"{stats['dup']} · previously processed: {stats['seen']} · "
          f"skipped short: {stats['short']}  ({el:.0f}s)")
    if not args.apply and stats["new"]:
        print("DRY-RUN — re-run with --apply to copy. The gateway's "
              "periodic scan picks new files up within 5 minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
