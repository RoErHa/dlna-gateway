#!/usr/bin/env python3
"""
beets_enrich.py — run the beets tag-in-place enrichment batch.

Implements the "beets import" stage from docs/enrichment.md: beets reads
MusicBrainz + AcoustID and writes clean tags + MBIDs **into the files**,
in place. The gateway's mutagen indexer then reads those enriched tags on
the next re-index — beets is an upstream batch stage, never a live
metadata authority (that would re-create the AssetUPnP dual-source-of-
truth problem).

This is a thin, SAFE wrapper around the external `beet` CLI. Its whole job
is to guarantee the non-negotiable invariant from the doc — tag IN PLACE:

    import.write = yes   import.copy = no   import.move = no

…before it ever lets beets touch the library, and to keep beets' own
library.db (~/.config/beets/library.db) separate from the gateway's
library.db. It does NOT reimplement beets.

Typical flow:

    # 0. one-time deps (see requirements.txt → "beets enrichment toolchain")
    brew install chromaprint beets      # fpcalc + beets (keg-venv'd, survives
                                        # Homebrew python upgrades — a plain
                                        # `pip3 install beets` gets WIPED by them)
    # the formula ships WITHOUT these two plugin deps; put them in the keg
    # (re-run after any `brew upgrade beets`):
    BEETS_KEG=$(brew --prefix beets)/libexec
    $BEETS_KEG/bin/python -m pip install --prefix $BEETS_KEG \
        musicbrainzngs pyacoustid

    # 1. write the prog-tuned, tag-in-place config (backs up any existing)
    python3 tools/beets_enrich.py --write-config

    # 2. interactive review pass (beets prompts per album; safe to quit)
    python3 tools/beets_enrich.py

    # 3. automated bulk pass (auto-accept only strong matches, skip the
    #    rest for Picard / manual — see docs/enrichment.md §5)
    python3 tools/beets_enrich.py --quiet

    # 4. re-run later — incremental:yes skips done dirs
    python3 tools/beets_enrich.py --quiet

    # one album, forcing a revisit of an already-imported dir
    python3 tools/beets_enrich.py --album "/Volumes/SAMDATA/Music/Focus/Moving Waves" --revisit

    # tag a batch, then kick a gateway re-index of the LocalFs library
    python3 tools/beets_enrich.py --quiet --reindex

    # show what WOULD run, plus the safety report, without invoking beets
    python3 tools/beets_enrich.py --dry-run

Quiet mode says just "Skipping." on an untagged album? Two known causes,
diagnose with `beet -v import -q <dir>`:

  * `chroma: acoustid album candidates: 0` — the fingerprint lookups use
    beets' SHARED bundled AcoustID key, which gets rate-limited (error
    code 14); every lookup then fails silently and beets falls back to
    text-matching whatever tags exist (garbage on a bare rip → skip).
  * The release genuinely isn't on MusicBrainz — check first:
    https://musicbrainz.org/search

Proven fix for bare/untagged files (2026-07-02, Nena "Best of the Best
Gold"): find the release on MB, pre-tag minimal TEXT tags with mutagen
(artist, the exact MB album title, title = filename stem), then re-run
`--quiet --revisit` — the text search matches without fingerprints and
beets writes the full canonical tags (umlauts, tracknumbers, MBIDs).
After tagging, a gateway restart is enough: the boot-time LocalFs rescan
picks up new/changed files incrementally (no force rebuild needed).
"""
import argparse
import json
import os
import pickle
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_MUSIC_ROOT = "/Volumes/SAMDATA/Music"
DEFAULT_GATEWAY = "http://127.0.0.1:8765"
DEFAULT_CONFIG = Path.home() / ".config" / "beets" / "config.yaml"
BEETS_LIBRARY = Path.home() / ".config" / "beets" / "library.db"
STATE_PICKLE = Path.home() / ".config" / "beets" / "state.pickle"

# launchd-style minimal PATH safety, mirroring dlna_acoustid._find_fpcalc.
# Path(sys.executable).parent catches a `beet` pip-installed into the same
# venv as the interpreter running this tool (the common case here) even when
# the venv isn't activated on PATH.
BEET_FALLBACKS = (str(Path(sys.executable).parent / "beet"),
                  "/opt/homebrew/bin/beet", "/usr/local/bin/beet",
                  str(Path.home() / "Library/Python/3.11/bin/beet"),
                  str(Path.home() / ".local/bin/beet"))
FPCALC_FALLBACKS = ("/opt/homebrew/bin/fpcalc", "/usr/local/bin/fpcalc")

# Audio extensions Chromaprint/fpcalc cannot decode (docs/enrichment.md §6).
DSD_EXTS = (".dsf", ".dff")


def find_binary(name: str, fallbacks: tuple[str, ...] = ()) -> str | None:
    """shutil.which + explicit Homebrew/user fallbacks → path or None."""
    found = shutil.which(name)
    if found:
        return found
    for cand in fallbacks:
        if cand and os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def default_config_yaml(music_root: str,
                        beets_library: str = str(BEETS_LIBRARY)) -> str:
    """The prog-tuned, tag-in-place beets config from docs/enrichment.md §3.

    `directory` is harmless while copy/move are off; `library` points at
    beets' OWN db, deliberately separate from the gateway's library.db.
    The `scrub` plugin is intentionally absent (§3 warning: it strips
    existing tags and can wipe metadata the override logic depends on).
    The `musicbrainz` plugin is REQUIRED on beets 2.x — MusicBrainz was
    pluginized, so without it beets has no metadata source and matches
    NOTHING ("No matching release found" for every album). It needs the
    `musicbrainzngs` package installed.
    """
    return f"""\
# Generated by tools/beets_enrich.py — tag-in-place enrichment.
# See docs/enrichment.md. Do NOT add the `scrub` plugin (it strips tags).
directory: {music_root}            # unused while copy/move are off; harmless
library:   {beets_library}

# `musicbrainz` is the metadata source — REQUIRED on beets 2.x (needs the
# musicbrainzngs package). Without it beets matches nothing.
plugins: musicbrainz chroma fetchart embedart info missing duplicates

import:
  write: yes          # write tags INTO the files  (the whole point)
  copy: no            # leave files where they are…
  move: no            # …do not move them either   → tag-in-place
  resume: ask
  incremental: yes    # record done dirs, skip on re-run (re-runnable batch)
  timid: no           # NOT baked in: beets rejects -q (--quiet) together with
                      # timid. Use the --timid CLI flag for a per-match review
                      # pass; --quiet for the auto-accept bulk pass.
  duplicate_action: skip

# release selection — bias toward the *original* release, not a reissue
original_date: yes
original_year: yes
per_disc_numbering: no

match:
  preferred:
    media: ['CD', 'Digital Media|File', 'Vinyl']
  strong_rec_thresh: 0.80   # auto-accept threshold; lower = --quiet accepts
                            # more (0.90 skipped this library's rips wholesale)

chroma:
  auto: yes           # AcoustID fingerprint fallback when tags are wrong
fetchart:
  auto: yes
embedart:
  auto: yes
"""


def parse_import_flags(config_text: str) -> dict:
    """Extract write/copy/move from the config text as bools (None if the
    key is absent — we never assume a beets default for these)."""
    out = {"write": None, "copy": None, "move": None}
    for key in out:
        m = re.search(rf"^\s*{key}:\s*(yes|no|true|false)\b",
                      config_text, re.M | re.I)
        if m:
            out[key] = m.group(1).lower() in ("yes", "true")
    return out


def verify_inplace(config_text: str) -> tuple[bool, list[str]]:
    """Enforce the tag-in-place invariant: write:yes, copy:no, move:no.

    A missing key is treated as a problem (not a safe default) so the
    user is pushed to --write-config rather than relying on beets'
    defaults (copy defaults to yes — that would NOT be in place)."""
    flags = parse_import_flags(config_text)
    problems: list[str] = []
    if flags["write"] is not True:
        problems.append("import.write must be 'yes' (write tags into files)")
    if flags["copy"] is not False:
        problems.append("import.copy must be 'no' (do not duplicate files)")
    if flags["move"] is not False:
        problems.append("import.move must be 'no' (do not relocate files)")
    return (not problems, problems)


def config_forces_timid(config_text: str) -> bool:
    """True if the config sets import.timid yes/true. beets refuses to run
    `-q`/--quiet while timid is on ('can't be both quiet and timid')."""
    return bool(re.search(r"^\s*timid:\s*(yes|true)\b",
                          config_text, re.M | re.I))


def config_has_musicbrainz_plugin(config_text: str) -> bool:
    """True if the `plugins:` line enables `musicbrainz`. On beets 2.x this
    is the metadata source; without it beets matches NOTHING."""
    m = re.search(r"^\s*plugins:\s*(.+)$", config_text, re.M)
    return bool(m) and "musicbrainz" in m.group(1).split()


def beet_python(beet_path: str) -> str | None:
    """The interpreter a `beet` console-script runs under, from its shebang
    (so we can probe beets' OWN environment, not whatever runs this tool)."""
    try:
        with open(beet_path, errors="replace") as f:
            first = f.readline()
        if first.startswith("#!"):
            interp = first[2:].strip().split()[0]
            if os.path.isfile(interp):
                return interp
    except Exception:                            # noqa: BLE001
        pass
    return None


def module_importable(python: str, module: str) -> bool:
    """Whether `import <module>` succeeds under the given interpreter."""
    try:
        return subprocess.run([python, "-c", f"import {module}"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).returncode == 0
    except Exception:                            # noqa: BLE001
        return False


def parse_library_path(config_text: str) -> str | None:
    """The `library:` path from the beets config (~-expanded), or None."""
    m = re.search(r"^\s*library:\s*(.+?)\s*$", config_text, re.M)
    return os.path.expanduser(m.group(1).strip()) if m else None


def beets_lib_counts(lib_path: str) -> tuple[int, int] | None:
    """(items, albums) in the beets library.db. (0, 0) if it doesn't exist
    yet; None on any read error."""
    if not os.path.exists(lib_path):
        return (0, 0)
    try:
        con = sqlite3.connect(lib_path)
        items = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        albums = con.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
        con.close()
        return (items, albums)
    except Exception:                            # noqa: BLE001
        return None


def taghistory_count(state_path: str) -> int | None:
    """How many dirs beets has marked done (incremental). None on error.
    taghistory is a plain set, so no beets classes are needed to unpickle."""
    if not os.path.exists(state_path):
        return 0
    try:
        with open(state_path, "rb") as f:
            st = pickle.load(f)
        return len(st.get("taghistory") or ())
    except Exception:                            # noqa: BLE001
        return None


def format_import_summary(before, after, th_before, th_after) -> str:
    """Build the post-run summary from before/after (items, albums) counts
    and before/after taghistory sizes. Pure → unit-testable."""
    lines = ["── beets import summary ─────────────────────────"]
    imported_albums = imported_items = None
    if before and after:
        imported_items = after[0] - before[0]
        imported_albums = after[1] - before[1]
        lines.append(f"  imported this run: {imported_albums} album(s), "
                     f"{imported_items} track(s)")
    processed = None
    if th_before is not None and th_after is not None:
        processed = th_after - th_before
        lines.append(f"  directories newly processed this run: {processed}")
    if imported_albums == 0 and processed:
        lines.append(f"  → all {processed} skipped (no match ≥ "
                     "strong_rec_thresh). Lower the threshold or run "
                     "interactively (drop --quiet);")
        lines.append("    already-seen dirs then need --revisit to "
                     "re-process.")
    elif imported_albums and processed and processed > imported_albums:
        lines.append(f"  ~{processed - imported_albums} dir(s) skipped "
                     "(no confident match)")
    return "\n".join(lines)


def build_import_cmd(beet: str, target: str, quiet: bool = False,
                     timid: bool = False, revisit: bool = False) -> list[str]:
    """Construct the `beet import …` argv (no beets behaviour here)."""
    cmd = [beet, "import"]
    if quiet:
        cmd.append("-q")          # auto-accept strong matches, no prompts
    if timid:
        cmd.append("--timid")     # prompt per match (more granular)
    if revisit:
        cmd.append("-I")          # noincremental: revisit an imported dir
    cmd.append(target)
    return cmd


def pick_localfs_udn(servers: list,
                     override: str | None = None) -> tuple[str | None,
                                                              str | None]:
    """Choose which server to re-index. Prefer an explicit override, then
    the LocalFs server (udn 'uuid:localfs-*'), then a sole server.
    Returns (udn, error)."""
    if override:
        return override, None
    udns = [s.get("udn", "") for s in servers if s.get("udn")]
    localfs = [u for u in udns if u.startswith("uuid:localfs")]
    if localfs:
        return localfs[0], None
    if len(udns) == 1:
        return udns[0], None
    if not udns:
        return None, "no servers known to the gateway"
    return None, ("multiple servers and no LocalFs one found; pass --udn "
                  f"(known: {', '.join(udns)})")


# The gateway 301-redirects HTTP API calls to HTTPS, and its Tailscale cert
# is issued for the *.ts.net hostname — so following that redirect to a
# loopback host (127.0.0.1) fails TLS verification with an IP-mismatch
# CERTIFICATE_VERIFY_FAILED. These are the gateway talking to ITSELF on
# localhost, so cert verification adds nothing; use an unverified context so
# the http→https redirect resolves. (Loopback only — not for remote hosts.)
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1", "[::1]")


def _gateway_ssl_context(gateway: str) -> ssl.SSLContext | None:
    """Unverified TLS context for a loopback gateway base, else None
    (remote hosts keep normal verification)."""
    host = urllib.parse.urlsplit(gateway).hostname or ""
    if host in _LOOPBACK_HOSTS:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return None


def trigger_reindex(gateway: str, udn: str | None,
                    timeout: float = 10.0) -> tuple[bool, str]:
    """GET /api/servers to resolve the LocalFs udn (unless given), then
    POST /api/index/rebuild?udn=… . Returns (ok, message)."""
    base = gateway.rstrip("/")
    ctx = _gateway_ssl_context(base)
    try:
        with urllib.request.urlopen(base + "/api/servers",
                                    timeout=timeout, context=ctx) as r:
            servers = json.loads(r.read().decode("utf-8"))
    except Exception as e:                       # noqa: BLE001
        return False, f"could not reach {base}/api/servers: {e}"
    target_udn, err = pick_localfs_udn(servers, udn)
    if err:
        return False, err
    url = base + "/api/index/rebuild?udn=" + urllib.parse.quote(target_udn)
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            body = r.read().decode("utf-8")
        return True, f"reindex started for {target_udn}: {body}"
    except Exception as e:                       # noqa: BLE001
        return False, f"POST {url} failed: {e}"


def _count_dsd(root: Path, limit: int = 5000) -> int:
    n = 0
    for _dirpath, _dirs, files in os.walk(root, followlinks=False):
        for f in files:
            if f.lower().endswith(DSD_EXTS):
                n += 1
                if n >= limit:
                    return n
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the beets tag-in-place enrichment batch "
                    "(see docs/enrichment.md).")
    ap.add_argument("--music-root", default=DEFAULT_MUSIC_ROOT,
                    help=f"library root (default {DEFAULT_MUSIC_ROOT})")
    ap.add_argument("--album", default=None,
                    help="import a single album dir instead of the whole root")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG),
                    help=f"beets config path (default {DEFAULT_CONFIG})")
    ap.add_argument("--write-config", action="store_true",
                    help="write the prog-tuned tag-in-place config "
                         "(backs up any existing) and exit")
    ap.add_argument("--quiet", action="store_true",
                    help="auto-accept strong matches, no prompts (bulk pass)")
    ap.add_argument("--timid", action="store_true",
                    help="prompt per match (more granular than default)")
    ap.add_argument("--revisit", action="store_true",
                    help="re-import a dir already recorded as done "
                         "(-I / noincremental)")
    ap.add_argument("--reindex", action="store_true",
                    help="after import, POST /api/index/rebuild to the gateway")
    ap.add_argument("--gateway", default=DEFAULT_GATEWAY,
                    help=f"gateway base URL (default {DEFAULT_GATEWAY})")
    ap.add_argument("--udn", default=None,
                    help="server UDN to reindex (default: auto-pick LocalFs)")
    ap.add_argument("-n", "--dry-run", action="store_true",
                    help="print the command + safety report; do not run beets")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="skip the pre-write confirmation prompt")
    args = ap.parse_args(argv)

    if args.quiet and args.timid:
        print("error: --quiet and --timid are contradictory", file=sys.stderr)
        return 2

    cfg_path = Path(args.config).expanduser()

    # ── --write-config: write the canonical config and stop ──────────
    if args.write_config:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        if cfg_path.exists():
            bak = cfg_path.with_suffix(cfg_path.suffix + ".bak")
            shutil.copy2(cfg_path, bak)
            print(f"backed up existing config → {bak}")
        cfg_path.write_text(default_config_yaml(args.music_root))
        print(f"wrote tag-in-place config → {cfg_path}")
        print("review it, then run:  python3 tools/beets_enrich.py")
        return 0

    # ── prerequisites ────────────────────────────────────────────────
    beet = find_binary("beet", BEET_FALLBACKS)
    if not beet:
        print("error: `beet` not found. Install it:\n"
              "    brew install chromaprint beets\n"
              "    # then add the plugin deps the formula omits "
              "(redo after `brew upgrade beets`):\n"
              "    BEETS_KEG=$(brew --prefix beets)/libexec\n"
              "    $BEETS_KEG/bin/python -m pip install --prefix $BEETS_KEG "
              "musicbrainzngs pyacoustid\n"
              "    (avoid `pip3 install beets` — Homebrew python upgrades "
              "wipe it)", file=sys.stderr)
        return 2

    if not cfg_path.exists():
        print(f"error: no beets config at {cfg_path}\n"
              "    run:  python3 tools/beets_enrich.py --write-config",
              file=sys.stderr)
        return 2

    cfg_text = cfg_path.read_text()

    # the safety gate — never let a non-in-place config touch the library
    ok, problems = verify_inplace(cfg_text)
    if not ok:
        print(f"error: {cfg_path} is NOT tag-in-place safe:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print("  fix it, or regenerate with --write-config", file=sys.stderr)
        return 2

    # beets rejects --quiet while the config forces timid; catch it here
    # with a clear message instead of beets' cryptic "can't be both" error.
    if args.quiet and config_forces_timid(cfg_text):
        print(f"error: --quiet conflicts with 'timid: yes' in {cfg_path} "
              "(beets can't be both quiet and timid).\n"
              "    regenerate the config:  python3 tools/beets_enrich.py "
              "--write-config\n"
              "    (or set 'timid: no' there). Use --timid for a per-match "
              "review pass.", file=sys.stderr)
        return 2

    # beets 2.x pluginized MusicBrainz: without the `musicbrainz` plugin the
    # importer has NO metadata source and silently matches nothing (0 imports,
    # exit 0) — the failure mode that wasted two multi-hour runs. Guard it.
    if not config_has_musicbrainz_plugin(cfg_text):
        print(f"error: {cfg_path} has no `musicbrainz` plugin in its "
              "plugins: line.\n"
              "    On beets 2.x that's the metadata source — without it "
              "beets matches NOTHING.\n"
              "    regenerate the config:  python3 tools/beets_enrich.py "
              "--write-config", file=sys.stderr)
        return 2

    # …and the plugin needs the musicbrainzngs package in beets' OWN env.
    # The Homebrew keg venv has no pip script (--without-pip), so use
    # `python -m pip --prefix <venv>` — lands in the keg's site-packages,
    # not the global one. Re-needed after any `brew upgrade beets`.
    bpy = beet_python(beet)
    if bpy and not module_importable(bpy, "musicbrainzngs"):
        prefix = os.path.dirname(os.path.dirname(bpy))
        print("error: the musicbrainz plugin needs the `musicbrainzngs` "
              "package, which isn't installed in beets' environment.\n"
              f"    install it:  {bpy} -m pip install --prefix {prefix} "
              "musicbrainzngs pyacoustid", file=sys.stderr)
        return 2

    target = Path(args.album).expanduser() if args.album \
        else Path(args.music_root).expanduser()
    if not target.exists():
        print(f"error: target does not exist (drive not mounted?): {target}",
              file=sys.stderr)
        return 2

    if not find_binary("fpcalc", FPCALC_FALLBACKS):
        print("warning: fpcalc not found — the chroma (AcoustID) plugin "
              "will be inactive; beets will tag by existing tags only "
              "(see docs/enrichment.md §6).", file=sys.stderr)

    dsd = _count_dsd(target)
    if dsd:
        print(f"note: {dsd}+ DSD file(s) under target — fingerprinting can't "
              "decode DSD; those tag by existing metadata or fall to Picard "
              "(docs/enrichment.md §6).")

    cmd = build_import_cmd(beet, str(target), quiet=args.quiet,
                           timid=args.timid, revisit=args.revisit)

    print("safety: import.write=yes copy=no move=no  (verified, tag-in-place)")
    print("command:", " ".join(cmd))

    if args.dry_run:
        print("dry-run: not invoking beets.")
        if args.reindex:
            print(f"dry-run: would POST {args.gateway}/api/index/rebuild")
        return 0

    # ── §7 backup warning + confirmation before in-place writes ──────
    if not args.yes:
        print("\nbeets will WRITE tags into the files in place. This is not "
              "trivially reversible — keep a snapshot/backup first "
              "(docs/enrichment.md §7).")
        resp = input(f"Proceed importing {target}? [y/N] ").strip().lower()
        if resp not in ("y", "yes"):
            print("aborted.")
            return 1

    # snapshot beets' own library + incremental state to report a summary
    lib_path = parse_library_path(cfg_text) or str(BEETS_LIBRARY)
    before = beets_lib_counts(lib_path)
    th_before = taghistory_count(str(STATE_PICKLE))

    rc = subprocess.run(cmd).returncode
    if rc != 0:
        print(f"beets exited {rc}", file=sys.stderr)
        return rc

    # ── import summary (so a "did-nothing" run is obvious, not silent) ──
    after = beets_lib_counts(lib_path)
    th_after = taghistory_count(str(STATE_PICKLE))
    print("\n" + format_import_summary(before, after, th_before, th_after))

    if args.reindex:
        time.sleep(1)
        ok, msg = trigger_reindex(args.gateway, args.udn)
        print(("reindex: " if ok else "reindex FAILED: ") + msg,
              file=sys.stderr if not ok else sys.stdout)
        if not ok:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
