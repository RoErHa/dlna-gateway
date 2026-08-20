#!/usr/bin/env python3
"""
find_corrupt_audio.py — find audio files whose magic bytes are wrong.

Walks a music folder and reports every audio file whose header doesn't
match its extension (or that is zero bytes / starts with 16 zero bytes).
Catches the failure mode the AcoustID worker surfaced: FLACs that are
12-14 MB on disk but start with zeros where the `fLaC` magic should be,
indicating a corrupt rip or copy.

Default behaviour: SCAN ONLY. No file is touched. A text list of
suspect paths is written to ./corrupt-audio.txt — feed it into your
own deletion script, or use --trash / --hard-delete to act in-place.

Usage:
    python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music
    python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music -v
    python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --limit 1000
    python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --trash
    python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music --hard-delete -y
    python3 tools/find_corrupt_audio.py /Volumes/SAMDATA/Music \\
        --exts flac,mp3,m4a --out broken.txt
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path
from collections.abc import Iterable

# Pure-audio extensions per the project default. .mp4 deliberately
# excluded — music-video MP4s aren't part of this scan.
DEFAULT_EXTS = {
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac",
    ".wav", ".wma", ".ape", ".aiff", ".aif",
    ".dff", ".dsf", ".alac",
}

# Per-extension magic-byte validators. Each returns True if the first
# `header` bytes look like a valid file of that type. Header is always
# 16 bytes (we read once and dispatch). All validators are lenient on
# ID3 tags prepended to files that aren't supposed to have them — some
# tagging tools do this; we don't want false positives.
def _v_flac(h: bytes) -> bool:
    return h.startswith((b"fLaC", b"ID3"))

def _v_mp3(h: bytes) -> bool:
    if h.startswith(b"ID3"):
        return True
    # MPEG audio frame sync: 11 bits set in the first 2 bytes
    # (0xFF, then top 3 bits of next byte). Covers MPEG1/2/2.5 L1/2/3.
    return len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0

def _v_ogg(h: bytes) -> bool:
    return h.startswith(b"OggS")

def _v_mp4(h: bytes) -> bool:
    # ISO BMFF / MP4: bytes 0-3 are box size, bytes 4-7 are "ftyp".
    return len(h) >= 8 and h[4:8] == b"ftyp"

def _v_aac(h: bytes) -> bool:
    # Raw AAC ADTS frames: sync word 0xFFF (12 bits).
    if len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xF0) == 0xF0:
        return True
    # AAC files sometimes ship as MP4/.m4a in disguise; accept ftyp too.
    return _v_mp4(h) or h.startswith(b"ID3")

def _v_wav(h: bytes) -> bool:
    return len(h) >= 12 and h[:4] == b"RIFF" and h[8:12] == b"WAVE"

def _v_aiff(h: bytes) -> bool:
    return len(h) >= 12 and h[:4] == b"FORM" and h[8:12] in (b"AIFF", b"AIFC")

def _v_wma(h: bytes) -> bool:
    # ASF GUID — the standard WMA/WMV container.
    return h.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11")

def _v_ape(h: bytes) -> bool:
    return h.startswith(b"MAC ")

def _v_dsf(h: bytes) -> bool:
    return h.startswith(b"DSD ")

def _v_dff(h: bytes) -> bool:
    return h.startswith(b"FRM8")

# Extension → validator. Lower-case keys (we lowercase before lookup).
_VALIDATORS = {
    ".flac": _v_flac,
    ".mp3":  _v_mp3,
    ".ogg":  _v_ogg,
    ".opus": _v_ogg,                 # opus uses the Ogg container
    ".m4a":  _v_mp4,
    ".alac": _v_mp4,                 # ALAC is virtually always in MP4
    ".aac":  _v_aac,
    ".wav":  _v_wav,
    ".aiff": _v_aiff,
    ".aif":  _v_aiff,
    ".wma":  _v_wma,
    ".ape":  _v_ape,
    ".dsf":  _v_dsf,
    ".dff":  _v_dff,
}


class Stats:
    def __init__(self) -> None:
        self.files_scanned: int  = 0
        self.files_corrupt: list[tuple[Path, str]] = []   # (path, reason)
        self.read_errors:   list[tuple[Path, str]] = []
        self.limit_reached: bool = False


def _classify(path: Path) -> str | None:
    """Return a corruption reason string, or None if file looks valid.

    Reasons:
      - "zero-size"          : 0 bytes on disk
      - "zero-header"        : first 16 bytes are all 0x00
      - "magic-mismatch:<ext>": header doesn't match the validator for
                                this extension
      - "unreadable: <err>"  : I/O / permission error reading file"""
    try:
        size = path.stat().st_size
    except (OSError, PermissionError) as e:
        return f"unreadable: {e}"
    if size == 0:
        return "zero-size"
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except (OSError, PermissionError) as e:
        return f"unreadable: {e}"
    # All-zero header — exactly the failure mode found on 2026-05-25.
    if head and head == b"\x00" * len(head):
        return "zero-header"
    ext = path.suffix.lower()
    v = _VALIDATORS.get(ext)
    if v is None:
        # Unknown audio extension — we shouldn't normally land here
        # because the walker filters by ext, but be defensive.
        return None
    if not v(head):
        return f"magic-mismatch:{ext}"
    return None


def _scan(root: Path, exts: set, stats: Stats,
          limit: int, verbose: bool) -> None:
    """Walk root depth-first; classify every file whose extension is in
    `exts`. Symlinks are NOT followed (avoids loops + accidental scans
    of mounted volumes the user didn't intend)."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Sort for deterministic output (test stability + nicer log).
        dirnames.sort()
        for name in sorted(filenames):
            ext = Path(name).suffix.lower()
            if ext not in exts:
                continue
            stats.files_scanned += 1
            full = Path(dirpath) / name
            reason = _classify(full)
            if reason is None:
                if verbose:
                    print(f"OK            {full}")
            elif reason.startswith("unreadable:"):
                stats.read_errors.append((full, reason))
                print(f"READ-ERR      {full}  ({reason})")
            else:
                stats.files_corrupt.append((full, reason))
                print(f"CORRUPT       {full}  [{reason}]")
            if limit and stats.files_scanned >= limit:
                stats.limit_reached = True
                return


def _trash_via_osascript(path: Path) -> None:
    posix = path.resolve().as_posix()
    escaped = posix.replace("\\", "\\\\").replace('"', '\\"')
    script = f'tell application "Finder" to delete POSIX file "{escaped}"'
    subprocess.run(["osascript", "-e", script],
                   check=True, capture_output=True)


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
        description="Find audio files whose magic bytes don't match "
                    "their extension (or that are zero-sized / "
                    "all-zero-header).")
    p.add_argument("root",
                   help="Music folder to scan (no default, for safety)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Also log every OK file (default: only corrupt)")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="Stop after scanning N files (0 = no limit). "
                        "When the limit is hit, the run is reported as "
                        "PARTIAL and any --trash/--hard-delete flag is "
                        "ignored — you only act on a complete picture.")
    p.add_argument("--exts",
                   default=",".join(sorted(e.lstrip(".")
                                           for e in DEFAULT_EXTS)),
                   help="Comma-separated audio extension list")
    p.add_argument("--out", default="corrupt-audio.txt",
                   help="Write corrupt paths to this file (default: "
                        "corrupt-audio.txt in CWD). Always written, even "
                        "in trash/hard-delete modes — useful as an audit "
                        "log. Pass /dev/null to suppress.")
    p.add_argument("--trash", action="store_true",
                   help="Move corrupt files to the macOS Trash. "
                        "Recoverable from Finder for ~30 days.")
    p.add_argument("--hard-delete", action="store_true",
                   help="Permanent rm instead of Trash. NOT recoverable. "
                        "Use only if you really know.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip the confirmation prompt before deleting")
    args = p.parse_args(argv)

    if args.trash and args.hard_delete:
        print("ERROR: --trash and --hard-delete are mutually exclusive",
              file=sys.stderr)
        return 2

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: not a directory: {root}", file=sys.stderr)
        return 2

    exts = _parse_exts(args.exts)
    if not exts:
        print("ERROR: no audio extensions to match", file=sys.stderr)
        return 2

    mode = "LIST ONLY"
    if args.trash:        mode = "TRASH"
    if args.hard_delete:  mode = "HARD DELETE (permanent)"

    stats = Stats()
    print(f"Scanning           {root}")
    print(f"Audio extensions:  {' '.join(sorted(exts))}")
    if args.limit:
        print(f"Limit:             {args.limit} files")
    print(f"Mode:              {mode}")
    print(f"List file:         {args.out}")
    print()

    _scan(root, exts, stats, args.limit, args.verbose)

    print()
    print(f"Scanned:   {stats.files_scanned} files")
    print(f"Corrupt:   {len(stats.files_corrupt)}")
    print(f"Read errs: {len(stats.read_errors)}")

    # Always write the list, even in trash/delete modes — provides an
    # audit trail of what was acted on.
    if stats.files_corrupt and args.out and args.out != "/dev/null":
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                for path, reason in stats.files_corrupt:
                    f.write(f"{reason}\t{path}\n")
            print(f"List file: wrote {len(stats.files_corrupt)} entries "
                  f"to {args.out}")
        except OSError as e:
            print(f"WARNING: could not write list file {args.out}: {e}",
                  file=sys.stderr)

    if stats.limit_reached:
        print(f"\n⚠ limit of {args.limit} files reached — stopping early "
              f"(no deletions executed; rerun without --limit to see "
              f"the full picture)")
        return 0

    if not stats.files_corrupt:
        print("\nNothing flagged. Done.")
        return 0

    if not (args.trash or args.hard_delete):
        # Default mode — just listed, no action taken.
        return 0

    # Act. Always confirm unless -y.
    method = "PERMANENTLY delete" if args.hard_delete else "move to Trash"
    print(f"\nWill {method} {len(stats.files_corrupt)} file(s).")
    if not args.yes:
        print("First 20:")
        for path, reason in stats.files_corrupt[:20]:
            print(f"  [{reason}] {path}")
        try:
            ans = input(f"\nProceed with {method} for "
                        f"{len(stats.files_corrupt)} files? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans != "y":
            print("Aborted.")
            return 1

    ok = fail = 0
    for path, _reason in stats.files_corrupt:
        try:
            if args.hard_delete:
                path.unlink()
            else:
                _trash_via_osascript(path)
            ok += 1
        except (OSError, subprocess.CalledProcessError) as e:
            fail += 1
            print(f"FAIL          {path}  ({e})", file=sys.stderr)
    print(f"\nDone. {ok} {method.lower()}d, {fail} failed.")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
