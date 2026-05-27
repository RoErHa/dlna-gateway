#!/usr/bin/env python3
"""
find_duplicate_audio.py — locate duplicate audio FILES on disk.

A "duplicate" here is two-or-more physical audio files on SAMDATA that
contain the **same recording**. AcoustID's content-based fingerprinting
identified them all to the same (artist, album, title) so they all live
in `metadata_overrides` with identical post-correction metadata; only
the URL (and therefore the file on disk) differs.

For each duplicate group, the tool picks a WINNER by:
  1. higher  bit_depth   (24 > 16)
  2. higher  sample_rate (96000 > 44100)
  3. larger  file_size   (proxy for less compression / higher bitrate)
  4. alphabetical path  (deterministic tiebreaker)

The OTHER files in each group are "losers" — candidates for Trash.

**"Lose nothing" guarantee:**
- Single-file groups (unique recordings) are NEVER touched.
- A 16-bit file is kept whenever it's the only copy of that recording.
- Default mode is SCAN ONLY — writes `duplicate-audio.txt` and exits.
- `--trash` moves to macOS Trash (recoverable for ~30 days).
- `--hard-delete` permanent rm (NOT recoverable — opt-in only).
- Both require confirmation prompt unless `-y`.

URL→path mapping strategy: HEAD each URL to get `Content-Length`, then
walk SAMDATA and match each URL to a disk file by exact file_size.
File size is essentially unique for audio files (especially hi-res
multi-MB files); collisions are rare and we skip ambiguous matches with
a warning.

Usage:
    # Default — scan only, write list:
    python3 tools/find_duplicate_audio.py /Volumes/SAMDATA/Music

    # Verbose:
    python3 tools/find_duplicate_audio.py /Volumes/SAMDATA/Music -v

    # Trash all losers with confirmation:
    python3 tools/find_duplicate_audio.py /Volumes/SAMDATA/Music --trash

    # Non-interactive permanent delete:
    python3 tools/find_duplicate_audio.py /Volumes/SAMDATA/Music --hard-delete -y
"""
import argparse
import http.client
import os
import sqlite3
import subprocess
import sys
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Iterable, List, Optional

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO / "library.db"

# Audio extensions worth scanning on disk. Mirror find_corrupt_audio.py.
AUDIO_EXTS = {
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac",
    ".wav", ".wma", ".ape", ".aiff", ".aif",
    ".dff", ".dsf", ".alac",
}


# ── DB-side duplicate identification ──────────────────────────────

def find_duplicate_groups(conn: sqlite3.Connection) -> list:
    """Query metadata_overrides for groups of files identified as the
    same recording. Returns a list of dicts:
      {
        "artist": str, "album": str, "title": str,
        "members": [
          {"url": ..., "bit_depth": ..., "sample_rate": ...}, ...
        ]
      }

    Restricts to source='acoustid' rows so manual edits / video_skips
    don't get accidentally grouped. Filters out groups where (artist,
    album, title) has any NULL/empty component — those can't be
    confidently identified as duplicates."""
    rows = conn.execute("""
        SELECT m.artist, m.album, m.title,
               t.url, t.bit_depth, t.sample_rate
          FROM metadata_overrides m
          JOIN tracks t ON t.url = m.url
         WHERE m.source = 'acoustid'
           AND m.artist IS NOT NULL AND m.artist != ''
           AND m.album  IS NOT NULL AND m.album  != ''
           AND m.title  IS NOT NULL AND m.title  != ''
         ORDER BY m.artist, m.album, m.title
    """).fetchall()
    groups_dict = defaultdict(list)
    for r in rows:
        key = (r["artist"], r["album"], r["title"])
        groups_dict[key].append({
            "url": r["url"],
            "bit_depth": r["bit_depth"],
            "sample_rate": r["sample_rate"],
        })
    groups = []
    for (artist, album, title), members in groups_dict.items():
        if len(members) < 2:
            continue
        groups.append({
            "artist": artist, "album": album, "title": title,
            "members": members,
        })
    return groups


# ── HTTP HEAD for Content-Length ──────────────────────────────────

def get_url_size(url: str, timeout: float = 5.0) -> Optional[int]:
    """HEAD request to fetch Content-Length. Returns None on any failure
    (timeout, non-200, missing header)."""
    try:
        parts = urllib.parse.urlparse(url)
        if parts.scheme not in ("http", "https"):
            return None
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(parts.netloc, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(parts.netloc, timeout=timeout)
        path = parts.path + (("?" + parts.query) if parts.query else "")
        try:
            conn.request("HEAD", path)
            resp = conn.getresponse()
            resp.read()
            if resp.status != 200:
                return None
            cl = resp.getheader("Content-Length")
            return int(cl) if cl is not None else None
        finally:
            conn.close()
    except Exception:
        return None


# ── Disk walk + size index ────────────────────────────────────────

def build_disk_size_index(root: Path) -> dict:
    """Walk root, returning {file_size_bytes: [Path, ...]} for all
    audio files. Symlinks not followed. Unreadable files skipped."""
    index = defaultdict(list)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            ext = Path(name).suffix.lower()
            if ext not in AUDIO_EXTS:
                continue
            p = Path(dirpath) / name
            try:
                size = p.stat().st_size
            except (OSError, PermissionError):
                continue
            if size > 0:
                index[size].append(p)
    return index


# ── Ranking + winner selection ────────────────────────────────────

def _rank_key(member: dict) -> tuple:
    """Higher quality = numerically larger tuple. NULL bit_depth/
    sample_rate count as 0 (lowest)."""
    bd = member.get("bit_depth") or 0
    sr = member.get("sample_rate") or 0
    sz = member.get("file_size") or 0
    # Negate everything so sorting ascending picks the WINNER first.
    return (-bd, -sr, -sz, member["url"])


def rank_groups(groups: list) -> list:
    """Add winner/losers fields. AssetUPnP serves each physical file
    via multiple URLs (Artist / Album / Genre browse paths), so two
    "duplicate URLs" in metadata_overrides routinely resolve to the
    SAME disk path. We deduplicate by path here — within a group, all
    URLs sharing a resolved path collapse into a single representative.
    Then we sort the unique paths by quality and pick a winner.

    This protects against the catastrophic outcome where `--trash`
    would kill the winner's path because a URL-aliased loser pointed
    at the same file."""
    for g in groups:
        # Bucket members by their resolved file path.
        by_path = defaultdict(list)
        unresolved = []
        for m in g["members"]:
            if m.get("path") is not None:
                by_path[str(m["path"])].append(m)
            else:
                unresolved.append(m)
        # One representative per unique path. All URLs sharing a path
        # are aliases for the same physical file.
        unique = []
        for path_str, ms in by_path.items():
            rep = dict(ms[0])
            rep["_aliased_urls"] = [m["url"] for m in ms]
            unique.append(rep)
        unique.sort(key=_rank_key)
        g["winner"]     = unique[0] if unique else None
        g["losers"]     = unique[1:]
        g["unresolved"] = unresolved
    return groups


# ── URL → file path resolution ────────────────────────────────────

def resolve_paths(groups: list, size_index: dict, verbose: bool = False):
    """For each member URL in each group, set member["path"] to the
    matching disk file by file_size. None on ambiguous / missing.

    Stats logged: how many resolved cleanly, how many ambiguous,
    how many absent."""
    stats = {"resolved": 0, "ambiguous": 0, "missing": 0}
    for g in groups:
        for m in g["members"]:
            sz = m.get("file_size")
            if not sz:
                m["path"] = None
                stats["missing"] += 1
                continue
            candidates = size_index.get(sz, [])
            if len(candidates) == 1:
                m["path"] = candidates[0]
                stats["resolved"] += 1
            elif len(candidates) > 1:
                # Ambiguous — multiple files with this exact size.
                # Don't pick one; record as ambiguous and skip from
                # action. User can resolve manually.
                m["path"] = None
                m["_ambiguous_candidates"] = candidates
                stats["ambiguous"] += 1
                if verbose:
                    print(f"AMBIGUOUS  size={sz}  {len(candidates)} files",
                          file=sys.stderr)
                    for c in candidates[:3]:
                        print(f"             {c}", file=sys.stderr)
            else:
                m["path"] = None
                stats["missing"] += 1
                if verbose:
                    print(f"NOT FOUND  size={sz}  url={m['url'][:80]}",
                          file=sys.stderr)
    return stats


# ── Trash / delete actions ────────────────────────────────────────

def _trash_via_osascript(path: Path) -> None:
    posix = path.resolve().as_posix()
    escaped = posix.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Finder" to delete POSIX file "{escaped}"'
    subprocess.run(["osascript", "-e", script],
                   check=True, capture_output=True)


def _hard_delete(path: Path) -> None:
    path.unlink()


# ── Report writer ─────────────────────────────────────────────────

def write_report(groups: list, out_path: Path) -> int:
    """Write the candidates list to a tab-separated file.

    Each group emits:
      KEEP   <winner_path>  <artist>  <album>  <title>  b<bd>/f<sr>/sz<size>  aliases=<n>
      TRASH  <loser_path>   ... (one per unique loser path)
      (blank line)

    `aliases=N` on a KEEP line means N URLs in metadata_overrides
    point at this file via different AssetUPnP browse paths — they
    collapse into one disk entry and the action only Trashes the
    SINGLE file, breaking those N URLs simultaneously (the gateway
    rebuild-index then cleans them up).

    Returns the number of unique-loser-file TRASH rows."""
    n_losers = 0
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            for g in groups:
                if g["winner"] is None:
                    continue
                for m in [g["winner"]] + g["losers"]:
                    role = "KEEP " if m is g["winner"] else "TRASH"
                    p = str(m.get("path") or "<unresolved>")
                    bd = m.get("bit_depth") or "?"
                    sr = m.get("sample_rate") or "?"
                    sz = m.get("file_size") or "?"
                    n_alias = len(m.get("_aliased_urls", []))
                    f.write(f"{role}\t{p}\t{g['artist']}\t{g['album']}\t"
                            f"{g['title']}\tb{bd}/f{sr}/sz{sz}"
                            f"\taliases={n_alias}\n")
                    if role == "TRASH":
                        n_losers += 1
                f.write("\n")
    except OSError as e:
        print(f"WARNING: could not write list file {out_path}: {e}",
              file=sys.stderr)
    return n_losers


# ── Main ──────────────────────────────────────────────────────────

def main(argv: Iterable[str] = None) -> int:
    p = argparse.ArgumentParser(
        description="Find duplicate audio files (same recording, "
                    "multiple copies on disk).")
    p.add_argument("root",
                   help="Music folder to scan (no default, for safety)")
    p.add_argument("--db", default=str(_DEFAULT_DB),
                   help="Path to library.db (default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose progress logging")
    p.add_argument("--out", default="duplicate-audio.txt",
                   help="Write candidates list here "
                        "(default: duplicate-audio.txt in CWD)")
    p.add_argument("--trash", action="store_true",
                   help="Move loser files to macOS Trash via osascript")
    p.add_argument("--hard-delete", action="store_true",
                   help="Permanent rm instead of Trash. NOT recoverable.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip the confirmation prompt before deleting")
    p.add_argument("--head-timeout", type=float, default=5.0,
                   help="HTTP HEAD timeout per URL in seconds (default 5)")
    args = p.parse_args(argv)

    if args.trash and args.hard_delete:
        print("ERROR: --trash and --hard-delete are mutually exclusive",
              file=sys.stderr)
        return 2

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"ERROR: library.db not found at {db_path}", file=sys.stderr)
        return 2

    mode = "REPORT ONLY"
    if args.trash:       mode = "TRASH"
    if args.hard_delete: mode = "HARD DELETE (permanent)"

    print(f"Scanning           {root}")
    print(f"DB:                {db_path}")
    print(f"Audio extensions:  {' '.join(sorted(AUDIO_EXTS))}")
    print(f"Mode:              {mode}")
    print(f"List file:         {args.out}")
    print()

    # Phase 1: DB query
    print("Phase 1/4: querying DB for duplicate groups…")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        groups = find_duplicate_groups(conn)
    finally:
        conn.close()
    n_members = sum(len(g["members"]) for g in groups)
    n_losers = n_members - len(groups)
    print(f"  {len(groups):,} duplicate groups, "
          f"{n_members:,} member files "
          f"({n_losers:,} losers if all act)")

    if not groups:
        print("\nNo duplicate groups found. Done.")
        return 0

    # Phase 2: HEAD each URL for Content-Length
    print(f"\nPhase 2/4: HEAD each URL for file size "
          f"({n_members:,} requests, ~{int(n_members * 0.1)}s)…")
    n_head_ok = 0
    n_head_fail = 0
    for i, g in enumerate(groups):
        for m in g["members"]:
            sz = get_url_size(m["url"], timeout=args.head_timeout)
            m["file_size"] = sz
            if sz is not None:
                n_head_ok += 1
            else:
                n_head_fail += 1
        if args.verbose and (i + 1) % 100 == 0:
            print(f"  …{i+1}/{len(groups)} groups", file=sys.stderr)
    print(f"  HEAD: {n_head_ok:,} resolved, {n_head_fail:,} failed")

    # Phase 3: walk disk, resolve paths, THEN rank.
    # Order matters — rank_groups dedupes by resolved path (multiple
    # URLs of the same physical file collapse into one entry), so the
    # resolution must happen first.
    print(f"\nPhase 3/4: walking disk + ranking…", flush=True)
    size_index = build_disk_size_index(root)
    n_disk_files = sum(len(v) for v in size_index.values())
    print(f"  disk walk: {n_disk_files:,} audio files indexed by size",
          flush=True)
    resolve_stats = resolve_paths(groups, size_index, verbose=args.verbose)
    print(f"  URL→path: {resolve_stats['resolved']:,} resolved, "
          f"{resolve_stats['ambiguous']:,} ambiguous, "
          f"{resolve_stats['missing']:,} missing", flush=True)
    rank_groups(groups)
    n_alias_collapse = sum(len(g["members"]) -
                           (1 if g["winner"] else 0) -
                           len(g["losers"]) -
                           len(g["unresolved"])
                           for g in groups)
    print(f"  URL aliases collapsed (multiple URLs → same file): "
          f"{n_alias_collapse:,}", flush=True)

    # Phase 4: write report
    print(f"\nPhase 4/4: writing report…")
    n_losers_written = write_report(groups, Path(args.out).resolve())
    print(f"  wrote {n_losers_written:,} loser entries to "
          f"{args.out} (winners also listed for reference)")

    # Decide action. Each group's `losers` is now a list of unique
    # disk paths (URL aliases already collapsed via rank_groups). Each
    # loser already has path set — None paths went into "unresolved".
    actionable_losers = [
        (g, m) for g in groups
        if g["winner"] is not None
        for m in g["losers"]
        if m.get("path") is not None
    ]
    n_unresolved = sum(len(g["unresolved"]) for g in groups)
    print()
    print(f"=== Summary ===", flush=True)
    print(f"  duplicate groups:                {len(groups):,}")
    print(f"  unique loser files actionable:   {len(actionable_losers):,}")
    print(f"  member URLs unresolved (no path): {n_unresolved:,}  (skipped)")

    if not (args.trash or args.hard_delete):
        print("\nReport-only mode. Use --trash or --hard-delete to act.")
        return 0

    if not actionable_losers:
        print("\nNo actionable losers (all path-unresolved). Done.")
        return 0

    method = "PERMANENTLY delete" if args.hard_delete else "move to Trash"
    print(f"\nWill {method} {len(actionable_losers):,} loser file(s).")
    if not args.yes:
        print("First 20:")
        for g, m in actionable_losers[:20]:
            print(f"  {m['path']}  ({g['artist']} / {g['title']})")
        try:
            ans = input(f"\nProceed with {method} for "
                        f"{len(actionable_losers):,} files? "
                        f"[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans != "y":
            print("Aborted.")
            return 1

    ok = fail = 0
    for g, m in actionable_losers:
        try:
            if args.hard_delete:
                _hard_delete(m["path"])
            else:
                _trash_via_osascript(m["path"])
            ok += 1
        except (OSError, subprocess.CalledProcessError) as e:
            fail += 1
            print(f"FAIL  {m['path']}  ({e})", file=sys.stderr)
    print(f"\nDone. {ok:,} {method.lower()}d, {fail:,} failed.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
