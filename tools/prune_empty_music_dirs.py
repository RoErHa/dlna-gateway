#!/usr/bin/env python3
"""
prune_empty_music_dirs.py — remove directories that contain no music.

Walks a music folder and moves to Trash any directory whose entire
subtree contains no music files AND no ancestor in the chain (between
the directory and the music root, exclusive of root) has music files
either. This preserves "support" subdirectories like
album/scans/ or album/coverart/ as long as the album itself has music,
while pruning truly stray non-music directories.

Default behaviour: Trash via macOS osascript (recoverable from Finder).
Use --hard-delete for permanent rm -rf. Use --dry-run to preview.

Usage:
    python3 tools/prune_empty_music_dirs.py /path/to/Music
    python3 tools/prune_empty_music_dirs.py ~/Music --dry-run
    python3 tools/prune_empty_music_dirs.py ~/Music -v --limit 200
    python3 tools/prune_empty_music_dirs.py ~/Music --exts mp3,flac,ogg
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# Pure-audio extensions per user spec — .mp4 deliberately excluded so
# music-video MP4s do NOT mark a folder as "kept".
DEFAULT_EXTS = {
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac",
    ".wav", ".wma", ".ape", ".aiff", ".aif",
    ".dff", ".dsf", ".alac",
}


class Stats:
    def __init__(self) -> None:
        self.dirs_visited:   int = 0
        self.dirs_kept:      int = 0
        self.dirs_to_delete: list[Path] = []
        self.errors:         list[tuple[Path, str]] = []
        self.limit_reached:  bool = False


def _has_music_file(dir_path: Path, exts: set) -> bool:
    """True iff at least one file directly in dir_path has a music ext."""
    try:
        for entry in os.scandir(dir_path):
            if entry.is_file(follow_symlinks=False):
                if Path(entry.name).suffix.lower() in exts:
                    return True
    except (OSError, PermissionError):
        # Treat unreadable dirs as "has music" — better to skip than delete.
        return True
    return False


def _subtree_has_music(dir_path: Path, exts: set) -> bool:
    """True iff any file in dir_path or any descendant has a music ext.
    Used to decide whether a directory is an "album root" — i.e. a
    folder under which everything (including sibling support dirs like
    scans/, coverart/, booklets/) should be preserved. Walking the
    subtree handles the multi-disc case where music lives one level
    deeper (Album/CD1/track.flac) but cover art sits at album level
    (Album/scans/cover.jpg)."""
    for dirpath, _dirnames, filenames in os.walk(dir_path, followlinks=False):
        for name in filenames:
            if Path(name).suffix.lower() in exts:
                return True
    return False


def _list_subdirs(dir_path: Path) -> list[Path]:
    """Direct subdirectories of dir_path (no symlink follow)."""
    try:
        return sorted(Path(e.path) for e in os.scandir(dir_path)
                      if e.is_dir(follow_symlinks=False))
    except (OSError, PermissionError):
        return []


def _walk(dir_path: Path, ancestor_is_album: bool, exts: set,
          stats: Stats, limit: int, verbose: bool) -> bool:
    """Top-down recursion. Returns True if dir_path is kept.

    Protection rule: as soon as we descend into a directory whose
    SUBTREE contains any music file, that directory is "an album root"
    and ALL of its descendants (including non-music siblings of music)
    are preserved. This protects album/scans/, album/coverart/,
    album/booklet/ even in multi-disc setups where music lives one
    level deeper than the cover-art folder.

    The root music folder itself does NOT count as an album root —
    loose tracks at root don't grant blanket protection to root's
    other subdirs.
    """
    if stats.limit_reached:
        return True  # treat unprocessed dirs as kept

    stats.dirs_visited += 1
    if limit and stats.dirs_visited >= limit:
        stats.limit_reached = True

    if ancestor_is_album:
        # Inside an album zone — preserve. Recurse only if verbose so
        # logging is complete; otherwise the whole subtree is implicitly
        # kept and we save the os.walk.
        stats.dirs_kept += 1
        if verbose:
            print(f"KEEP   (in album)      {dir_path}")
            for sub in _list_subdirs(dir_path):
                _walk(sub, True, exts, stats, limit, verbose)
        return True

    if _subtree_has_music(dir_path, exts):
        # Album root: subtree contains music somewhere. Keep + protect.
        if verbose:
            print(f"KEEP   (album root)    {dir_path}")
        stats.dirs_kept += 1
        for sub in _list_subdirs(dir_path):
            _walk(sub, True, exts, stats, limit, verbose)
        return True

    # Subtree has zero music files → delete the whole branch.
    print(f"DELETE                 {dir_path}")
    stats.dirs_to_delete.append(dir_path)
    return False


def _trash_via_osascript(path: Path) -> None:
    """Move directory to macOS Trash. Recoverable from Finder for ~30
    days. Raises subprocess.CalledProcessError on failure."""
    posix = path.resolve().as_posix()
    # Escape backslashes and quotes for the AppleScript string literal.
    escaped = posix.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Finder" to delete POSIX file "{escaped}"'
    subprocess.run(["osascript", "-e", script],
                   check=True, capture_output=True)


def _hard_delete(path: Path) -> None:
    import shutil
    shutil.rmtree(path)


def _parse_exts(arg: str) -> set:
    out = set()
    for tok in arg.split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        if not tok.startswith("."):
            tok = "." + tok
        out.add(tok)
    return out


def main(argv: Iterable[str] = None) -> int:
    p = argparse.ArgumentParser(
        description="Move-to-Trash directories that contain no music.")
    p.add_argument("root",
                   help="Music folder to scan (no default, for safety)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print decisions without acting")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Log every kept directory too (default: only deletions)")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="Stop after evaluating N directories (0 = no limit)")
    p.add_argument("--exts",
                   default=",".join(sorted(e.lstrip(".")
                                           for e in DEFAULT_EXTS)),
                   help="Comma-separated music extension list")
    p.add_argument("--hard-delete", action="store_true",
                   help="Permanent rm -rf instead of Trash (NOT recoverable)")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip confirmation prompt")
    args = p.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2

    exts = _parse_exts(args.exts)
    if not exts:
        print("ERROR: no music extensions to match", file=sys.stderr)
        return 2

    stats = Stats()
    print(f"Scanning           {root}")
    print(f"Music extensions:  {' '.join(sorted(exts))}")
    if args.limit:
        print(f"Limit:             {args.limit} directories")
    print(f"Mode:              {'DRY RUN' if args.dry_run else 'TRASH' if not args.hard_delete else 'HARD DELETE (permanent)'}")
    print()

    # Walk the root's children. The root itself is NEVER treated as
    # an "album root" — loose music files at root don't grant blanket
    # protection to root's other subdirs. Protection activates only
    # once we descend into a subdir whose subtree contains music.
    for child in _list_subdirs(root):
        _walk(child, ancestor_is_album=False, exts=exts,
              stats=stats, limit=args.limit, verbose=args.verbose)

    print()
    print(f"Visited:   {stats.dirs_visited} directories")
    print(f"Kept:      {stats.dirs_kept}")
    print(f"To delete: {len(stats.dirs_to_delete)}")
    if stats.limit_reached:
        print(f"⚠ limit of {args.limit} dirs reached — stopping early "
              f"(no deletions executed)")
        return 0

    if not stats.dirs_to_delete:
        print("\nNothing to delete. Done.")
        return 0

    print("\nFirst 20 directories to delete:")
    for d in stats.dirs_to_delete[:20]:
        print(f"  {d}")
    if len(stats.dirs_to_delete) > 20:
        print(f"  … and {len(stats.dirs_to_delete) - 20} more")

    if args.dry_run:
        print("\nDry run — no changes made.")
        return 0

    if not args.yes:
        method = "PERMANENTLY rm -rf" if args.hard_delete else "move to Trash"
        ans = input(
            f"\nProceed to {method} the {len(stats.dirs_to_delete)} "
            f"dirs above? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 1

    print()
    n_ok = n_fail = 0
    for d in stats.dirs_to_delete:
        try:
            if args.hard_delete:
                _hard_delete(d)
            else:
                _trash_via_osascript(d)
            n_ok += 1
        except subprocess.CalledProcessError as e:
            err = (e.stderr.decode(errors="replace").strip()
                   if e.stderr else str(e))
            print(f"FAILED  {d}: {err}", file=sys.stderr)
            stats.errors.append((d, err))
            n_fail += 1
        except Exception as e:
            print(f"FAILED  {d}: {e}", file=sys.stderr)
            stats.errors.append((d, str(e)))
            n_fail += 1

    print(f"\nDone. Removed: {n_ok}, Failed: {n_fail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
