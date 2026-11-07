#!/usr/bin/env python3
"""Link new Immich videos into the gateway's video root.

The pipeline the gateway already provides: anything landing in
`/Volumes/SAMDATA/GWMovies` is picked up by the 5-minute periodic video
scan, GPS-reverse-geocoded, titled `<location>_<YYYYMMDD>_<HHMM>` and
served in the Videos section (see docs/VIDEO_SUPPORT.md). The one manual
step was bringing phone videos over from Immich: this tool automates it.

  * Each new video becomes a SYMLINK to its Immich original, not a copy, so
    it costs no disk or backup space. The file server follows these links
    only into `LOCALFS_VIDEO_LINK_ROOTS`, so every source must be listed there.
  * Sources: all of Immich's ORIGINALS. The external libraries come first
    because their paths never change, then `library/` + `upload/` under the
    Immich data volume. `encoded-video/` (transcodes), `thumbs/`, `profile/`
    and `backups/` are never touched.
  * Dedup is by CONTENT HASH (BLAKE2b), recorded in a cache DB at
    `<dest>/.immich-import.db`: files already in GWMovies (however they
    got there, whatever their name) are registered on first run and
    never linked again; sources already processed are skipped by
    (path, size, mtime) without re-reading them. Re-running after new
    phone uploads only links what's new: safe as a habit.
  * Immich can move files inside its own storage, which breaks a link. A run
    finds the moved file by its hash and re-points the SAME link, so its video
    id, people tags and location overrides survive.
  * Only videos LONGER than 3 seconds come in, so Live-Photo motion clips and
    other very short clips don't flood the TV. `--min-seconds 0` includes them.
    `--prune-short` removes existing links that are too short: the originals
    stay untouched, the clips stay out on later runs, and real files are left alone.
  * Filenames are preserved (the gateway titles by metadata, not name);
    a name collision with DIFFERENT content gets a ` (2)` suffix.
  * DRY-RUN by default; --apply links. The hash cache is warmed even
    in dry-run (that's the slow part) so the following --apply is fast.

    python3 tools/immich_import.py                # preview
    python3 tools/immich_import.py --apply        # link new videos
    python3 tools/immich_import.py --min-seconds 0   # include very short clips
    python3 tools/immich_import.py --prune-short     # preview removing short links
"""
import argparse
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SOURCE = "/Volumes/SAMDATA-1TB/IMMICH-UPLOAD"
DEFAULT_DEST = "/Volumes/SAMDATA/GWMovies"
SOURCE_SUBDIRS = ("library", "upload")
# (root, subdirs) in preference order: when the same video exists twice, the
# link goes to the first, and external libraries never move their files.
DEFAULT_SOURCES = (
    ("/Volumes/SAMDATA-1TB/PHOTOS-ALL", ("",)),
    ("/Volumes/SAMDATA-1TB/PHOTOS-Mar", ("",)),
    (DEFAULT_SOURCE, SOURCE_SUBDIRS),
)
DEFAULT_MIN_SECONDS = 3.0
# Deleting a video in Immich leaves one dead link, which is normal. Many at once
# means a lost permission or a moved library, which the nightly job must report.
DEAD_LINK_ALARM = 0.05
PROJECT = Path(__file__).resolve().parent.parent
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

    def seen_hash(self, path: Path):
        row = self.conn.execute("SELECT hash FROM dest_seen WHERE path=?",
                                (str(path),)).fetchone()
        return row[0] if row else None

    def src_hash(self, path: Path):
        row = self.conn.execute("SELECT hash FROM src_seen WHERE path=?",
                                (str(path),)).fetchone()
        return row[0] if row else None

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
        base = (source / sub) if sub else source   # '' = the source root
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
    while os.path.lexists(out):   # a broken link still occupies its name
        out = dest_dir / f"{stem} ({n}){suffix}"
        n += 1
    return out


def make_link(src: Path, out: Path) -> None:
    """Point `out` at `src`, and prove the link really leads to that file."""
    out.symlink_to(src.resolve())
    if not os.path.samefile(out, src):
        out.unlink()
        raise OSError(f"link does not lead to {src}")


def listed_link_roots() -> list[str]:
    """`LOCALFS_VIDEO_LINK_ROOTS`, from the environment or the project's .env."""
    raw = os.environ.get("LOCALFS_VIDEO_LINK_ROOTS")
    if raw is None:
        try:
            for line in (PROJECT / ".env").read_text().splitlines():
                if line.startswith("LOCALFS_VIDEO_LINK_ROOTS="):
                    raw = line.split("=", 1)[1]
        except OSError:
            pass
    return [r.strip() for r in (raw or "").split(":") if r.strip()]


def unservable(source: Path, subdirs, roots: list[str]) -> list[str]:
    """Source folders the file server would refuse to follow links into."""
    bases = [(source / sub) if sub else source for sub in subdirs]
    return [str(b) for b in bases
            if not any(b == Path(r) or Path(r) in b.parents for r in roots)]


def prune_short_links(dest: Path, min_seconds: float, apply: bool) -> int:
    """Remove symlinks to clips not longer than the cutoff. Only ever unlinks a
    symlink, never a real file, and keeps a clip whose length can't be read."""
    removed = kept_unknown = 0
    for p in sorted(dest.rglob("*")):
        if not p.is_symlink() or p.suffix.lower() not in VIDEO_EXTS or not p.exists():
            continue
        dur = video_duration(p)
        if dur < 0:
            kept_unknown += 1
        elif dur <= min_seconds:
            removed += 1
            if apply:
                p.unlink()
    verb = "removed" if apply else "would remove"
    print(f"{verb}: {removed} link(s) of {min_seconds:g}s or less · "
          f"kept, length unreadable: {kept_unknown}")
    return removed


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Link new Immich videos into the gateway video root "
                    "(dry-run by default).")
    ap.add_argument("--source", default=None,
                    help="one source root (default: every Immich library)")
    ap.add_argument("--dest", default=DEFAULT_DEST)
    ap.add_argument("--subdirs", default=None,
                    help="comma list of source subdirs to walk (default "
                         "'library,upload' = Immich's originals); pass '' "
                         "to walk the source root itself (a plain folder "
                         "tree like an external library)")
    ap.add_argument("--apply", action="store_true",
                    help="actually link (default: preview only)")
    ap.add_argument("--min-seconds", type=float, default=DEFAULT_MIN_SECONDS,
                    help="only take videos longer than this; the default keeps "
                         "out Live-Photo motion clips, 0 = everything")
    ap.add_argument("--prune-short", action="store_true",
                    help="remove links in dest that are not longer than "
                         "--min-seconds, then stop (preview unless --apply)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N new imports (0 = no limit)")
    ap.add_argument("--retry-short", action="store_true",
                    help="re-evaluate sources previously skipped as short "
                         "(use after LOWERING --min-seconds — the cache "
                         "doesn't record which cutoff marked them)")
    ap.add_argument("--reimport-deleted", action="store_true",
                    help="dedup against files PRESENT in dest now, not "
                         "everything ever seen — content once in dest but "
                         "since deleted gets linked again (default keeps "
                         "deliberately-deleted clips gone forever)")
    args = ap.parse_args(argv)

    dest = Path(args.dest)
    if args.source:
        if args.subdirs is None:
            subdirs = SOURCE_SUBDIRS
        else:
            subdirs = tuple(x.strip() for x in args.subdirs.split(",")) \
                if args.subdirs else ("",)
        sources = ((args.source, subdirs),)
    else:
        sources = DEFAULT_SOURCES
    sources = tuple((Path(root), subs) for root, subs in sources)
    for source, _subs in sources:
        if not source.is_dir():
            print(f"FATAL: source not mounted: {source}"); return 2
    if not dest.is_dir():
        print(f"FATAL: dest not mounted: {dest}"); return 2
    roots = listed_link_roots()
    refused = [b for s, subs in sources for b in unservable(s, subs, roots)]
    if refused:
        print("FATAL: not in LOCALFS_VIDEO_LINK_ROOTS, so these links would "
              f"not play: {', '.join(refused)}")
        return 2

    if args.prune_short:
        prune_short_links(dest, args.min_seconds, args.apply)
        return 0

    cache = Cache(dest / ".immich-import.db")
    if args.retry_short:
        with cache.conn:
            n = cache.conn.execute(
                "DELETE FROM src_seen WHERE status='short'").rowcount
        print(f"--retry-short: {n} previously-short source(s) will be "
              f"re-evaluated")
    if args.reimport_deleted:
        # stale 'duplicate' verdicts must be re-evaluated too — they were
        # decided against the everything-ever-seen hash set
        with cache.conn:
            n = cache.conn.execute(
                "DELETE FROM src_seen WHERE status='duplicate'").rowcount
        print(f"--reimport-deleted: {n} previously-duplicate source(s) "
              f"will be re-evaluated against files present now")

    # 1. Register everything already in the video root (incremental —
    #    only new/changed files get hashed). is_file() follows symlinks, so a
    #    link counts as the video it leads to.
    dest_files = [p for p in dest.rglob("*")
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    print(f"registering {len(dest_files)} existing files in {dest} …")
    present = set()
    for p in dest_files:
        present.add(cache.register_dest(p))
    # Default dedup set = every content hash EVER seen in dest, so clips
    # the user deleted from the video root never come back on a re-run.
    # --reimport-deleted narrows it to what's physically present now.
    have = present if args.reimport_deleted else cache.dest_hashes()
    # Links whose file moved, by what they used to lead to. Sources were checked
    # as mounted above, so a broken link here means the file really is elsewhere.
    broken = {}
    links = dead = 0
    for p in dest.rglob("*"):
        if not p.is_symlink():
            continue
        links += 1
        if not p.exists():
            dead += 1
            h = cache.seen_hash(p)
            if h:
                broken[h] = p

    def repair(h: str, src: Path) -> bool:
        link = broken.get(h)
        if link is None:
            return False
        stats["relinked"] += 1
        if args.apply:
            link.unlink()
            make_link(src, link)
            cache.record_src(src, h, "imported", link.name)
            print(f"  ~ {link.name}  →  {src}")
        else:
            print(f"  would re-point: {link.name}  →  {src}")
        del broken[h]
        return True

    # 2. Walk Immich originals.
    stats = {"new": 0, "dup": 0, "seen": 0, "short": 0, "relinked": 0}
    t0 = time.monotonic()
    limit_hit = False
    for source, subdirs in sources:
        for src in iter_source_videos(source, subdirs=subdirs):
            prior = cache.src_status(src)
            if prior is not None:
                stats["seen"] += 1
                repair(cache.src_hash(src), src)
                continue
            if args.min_seconds > 0:
                dur = video_duration(src)
                if 0 <= dur <= args.min_seconds:
                    cache.record_src(src, "", "short")
                    stats["short"] += 1
                    continue
            h = file_hash(src)
            if repair(h, src):
                continue
            if h in have:
                cache.record_src(src, h, "duplicate")
                stats["dup"] += 1
                continue
            stats["new"] += 1
            rel = src.relative_to(source)
            if args.apply:
                out = unique_dest(dest, src.name)
                make_link(src, out)
                cache.register_dest(out)
                cache.record_src(src, h, "imported", out.name)
                print(f"  + {source.name}/{rel}  →  {out.name}")
            else:
                print(f"  would link: {source.name}/{rel}")
            have.add(h)    # the same video in a later library is a duplicate
            if args.limit and stats["new"] >= args.limit:
                print(f"--limit {args.limit} reached, stopping")
                limit_hit = True
                break
        if limit_hit:
            break

    el = time.monotonic() - t0
    verb = "linked" if args.apply else "would link"
    print(f"\n{verb}: {stats['new']} · re-pointed: {stats['relinked']} · "
          f"already in dest (content): {stats['dup']} · previously processed: "
          f"{stats['seen']} · skipped short: {stats['short']}  ({el:.0f}s)")
    if broken:
        print(f"{len(broken)} link(s) lead nowhere and no matching file was found: "
              + ", ".join(p.name for p in list(broken.values())[:5]))
    if not args.apply and (stats["new"] or stats["relinked"]):
        print("DRY-RUN — re-run with --apply to link. The gateway's "
              "periodic scan picks new files up within 5 minutes.")
    still_dead = dead - stats["relinked"]
    if links and still_dead / links > DEAD_LINK_ALARM:
        print(f"ALARM: {still_dead} of {links} links lead nowhere")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
