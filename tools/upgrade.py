#!/usr/bin/env python3
"""
tools/upgrade.py — bring the app's whole toolchain to its newest versions,
PROVE the result with the test gates, and roll back in one step if it fails.

Why: security fixes land upstream continuously (Node, SQLite, Python, the
pip packages, the one vendored JS library), and a hand-run upgrade touches
five package managers in an order that matters. This does them all, then
lets the suite decide whether the result is kept.

What "the toolchain" is — and where each piece actually lives:

  1. Homebrew   python@3.x, and the libraries it LOADS AT RUNTIME — asked of
                `brew deps`, never hardcoded: on 2026-10-23 python 3.14.9
                moved from openssl@3 to openssl@4 (`_sqlite3.so` likewise
                links /opt/homebrew/opt/sqlite, so `brew upgrade sqlite` +
                a restart patches SQLite with no venv rebuild). Also ffmpeg (video), chromaprint + beets
                (enrichment) and node (dev: Appium's host).
  2. pip        runtime deps — a FRESH venv, resolved EAGERLY so transitive
                packages (h2, idna, certifi…) move too, within the major
                caps requirements.txt sets on purpose. → requirements.lock
  3. dev        requirements-dev.txt pins + requirements-dev.lock (the
                full closure) + the Playwright browsers. Note
                Playwright bundles its OWN Node binary: the only way to
                patch that copy is to upgrade the playwright package.
  4. hls.js     static/vendor/hls.min.js, from the npm registry tarball,
                sha512-verified against the registry's `integrity` before
                a byte is written. Stays on its current major unless
                --allow-major (a major is an API change for app.js).
  5. npm        global appium (dev, opt-in smoke layer), if installed.

The app does not run Node itself — nothing in the gateway or the PWA does.

DRY-RUN BY DEFAULT: prints what is outdated plus any KNOWN vulnerabilities
(osv.dev) in what is installed now. `--apply` performs the upgrade:

  backup library.db → brew upgrade → fresh .venv (old one MOVED aside, not
  deleted) → regen lock + dev pins → hls.js → playwright browsers →
  run_all --offline → --frontend-only → restart gateway → health probe.

Any failure from the venv step on restores the previous venv and files and
restarts the gateway on them. The previous set stays in
~/dlna-gateway-backups/upgrade-prev until the next --apply, so
`--rollback` can undo a run that passed the gates but misbehaves live.
Homebrew upgrades are NOT rolled back (brew has no downgrade); that is why
brew cleanup is deferred (HOMEBREW_NO_INSTALL_CLEANUP) — the running
gateway would otherwise lose the stdlib files it lazily imports from.

Usage:
    python3 tools/upgrade.py                  # report only
    python3 tools/upgrade.py --check          # exit 1 if DO items or NEW ones
    python3 tools/upgrade.py --apply
    python3 tools/upgrade.py --python 3.15    # dry run of the minor move
    python3 tools/upgrade.py --apply --python 3.15   # do it
    python3 tools/upgrade.py --rollback
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV = os.path.join(PROJECT, ".venv")
VENV_PY = os.path.join(VENV, "bin", "python")
REQS = os.path.join(PROJECT, "requirements.txt")
DEV_REQS = os.path.join(PROJECT, "requirements-dev.txt")
LOCK = os.path.join(PROJECT, "requirements.lock")
DEV_LOCK = os.path.join(PROJECT, "requirements-dev.lock")
HLS = os.path.join(PROJECT, "static", "vendor", "hls.min.js")
# Files an --apply rewrites; saved before, restored on failure/--rollback.
README = os.path.join(PROJECT, "README.md")  # the version strip, rewritten pre-gates
TRACKED = (LOCK, DEV_LOCK, DEV_REQS, HLS, README)

BACKUPS = os.path.expanduser(os.environ.get("BACKUP_DIR", "~/dlna-gateway-backups"))
PREV = os.path.join(BACKUPS, "upgrade-prev")
LABEL = "com.roha.dlna-gateway"
HEALTH_URL = "http://127.0.0.1:8765/api/version"

# Formulae the app or its tooling uses. python@3.x and ITS dependencies
# (sqlite, openssl@N, …) are added at run time by python_formulae().
FORMULAE = ("ffmpeg", "chromaprint", "beets", "node")
BEETS_PLUGIN_DEPS = ("musicbrainzngs", "pyacoustid")
HLS_MEMBER = "package/dist/hls.min.js"
UA = "dlna-gateway-upgrade/1.0"
# Last week's DECIDE/WAIT items, so --check pings only on what is NEW.
ATTENTION_STATE = os.path.join(BACKUPS, "upgrade-attention.json")
KINDS = ("DO", "DECIDE", "WAIT", "NOTE")


# ── pure helpers (tools/test_upgrade.py) ─────────────────────────────────

def vtuple(v: str) -> tuple[int, ...] | None:
    """'1.7.3' → (1, 7, 3); a pre-release ('1.7.0-rc.3') → None."""
    if not re.fullmatch(r"\d+(\.\d+)*", v):
        return None
    return tuple(int(p) for p in v.split("."))


def pick_hls_version(versions, current: str, allow_major: bool) -> str | None:
    """Newest stable release to move to, or None when `current` is it.
    Without allow_major, stays on current's major even when latest left it."""
    cur = vtuple(current)
    if cur is None:
        return None
    stable = [t for t in (vtuple(v) for v in versions) if t]
    if not allow_major:
        stable = [t for t in stable if t[0] == cur[0]]
    best = max(stable, default=None)
    if best is None or best <= cur:
        return None
    return ".".join(map(str, best))


def hls_version_in(js: str) -> str | None:
    """The version string compiled into a hls.min.js build.

    Primary: the debug-log string every build carries ("…in hls.js version
    1.7.3'"). The `version` getter is only a fallback — 1.5.x returns a
    literal there, 1.7.x returns a minified variable (`var rs="1.7.3"`)."""
    m = (re.search(r"hls\.js version (\d+\.\d+\.\d+[^\"'\s]*)", js)
         or re.search(r'version"?[^"]{0,40}?return\s*"(\d+\.\d+\.\d+[^"]*)"', js))
    return m.group(1) if m else None


def sri_ok(blob: bytes, integrity: str) -> bool:
    """Subresource-Integrity check, the format npm publishes (sha512-<b64>)."""
    algo, _, want = integrity.partition("-")
    if algo not in ("sha512", "sha384", "sha256") or not want:
        return False
    got = base64.b64encode(hashlib.new(algo, blob).digest()).decode()
    return got == want


def tar_member(tgz: bytes, name: str) -> bytes:
    """One file out of a .tgz, read in memory — nothing touches the disk,
    so a hostile path in the archive can never be written anywhere."""
    with tarfile.open(fileobj=io.BytesIO(tgz), mode="r:gz") as tf:
        member = tf.getmember(name)
        if not member.isfile():
            raise ValueError(f"{name} is not a regular file in the tarball")
        return tf.extractfile(member).read()


def unpinned(dev_text: str) -> list[str]:
    """The package names requirements-dev.txt pins, without their pins."""
    out = []
    for line in dev_text.splitlines():
        spec = line.split("#", 1)[0].strip()
        if spec:
            out.append(re.split(r"[=<>!~;\[ ]", spec, maxsplit=1)[0])
    return out


def rewrite_pins(dev_text: str, installed: dict[str, str],
                 comments: dict[str, str] | None = None) -> str:
    """Re-pin every `name==ver` line to `installed`, keeping the layout.
    A trailing comment is kept unless the version moved and `comments`
    supplies a fresh one (the Playwright browser revisions line)."""
    comments = comments or {}
    out = []
    for line in dev_text.splitlines(keepends=True):
        m = re.match(r"^([A-Za-z0-9_.\-]+)==(\S+)([ \t]*)(#.*)?(\n?)$", line)
        key = m and m.group(1).lower().replace("_", "-")
        if not m or key not in installed or installed[key] == m.group(2):
            out.append(line)
            continue
        name, _, gap, comment, nl = m.groups()
        new = f"{name}=={installed[key]}"
        if key in comments:
            new += f"        # {comments[key]}"
        elif comment:
            new += (gap or " ") + comment
        out.append(new + nl)
    return "".join(out)


def browser_comment(dry_run: str) -> str | None:
    """`playwright install --dry-run chromium webkit` → the revisions note
    requirements-dev.txt carries, e.g. 'browsers: chromium r1234 (151), …'."""
    chrom = re.search(r"Chrome for Testing (\d+)\S* \(playwright chromium v(\d+)\)", dry_run)
    wk = re.search(r"WebKit (\S+) \(playwright webkit v(\d+)\)", dry_run)
    if not (chrom and wk):
        return None
    return (f"browsers: chromium r{chrom.group(2)} ({chrom.group(1)}), "
            f"webkit r{wk.group(2)} ({wk.group(1)})")


def brew_vkey(v: str) -> tuple[int, ...]:
    """Brew versions, comparably: '1.6.1_1' → (1, 6, 1, 1), '2026-09-25' →
    (2026, 9, 25). The `_N` revision counts, so 9.0.2 < 9.0.2_1."""
    return tuple(int(p) for p in re.findall(r"\d+", v))


def brew_moves(formulae: list[dict]) -> tuple[list[tuple[str, str]], list[str]]:
    """Split `brew outdated --json=v2` into (upgrades, held).

    HELD = brew's stable is OLDER than what is installed. Happens when a
    formula is repointed to another release line — openssl@3 moved to the
    3.5 LTS (alias openssl@3.5) while 3.6.5 was installed. `brew upgrade`
    would then DOWNGRADE, which is a decision for a person, not a script."""
    upgrades, held = [], []
    for f in formulae:
        have = max(f["installed_versions"], key=brew_vkey)
        line = f"{f['name']} {', '.join(f['installed_versions'])} → {f['current_version']}"
        if brew_vkey(f["current_version"]) < brew_vkey(have):
            held.append(line + "  (a DOWNGRADE — held; decide by hand)")
        else:
            upgrades.append((f["name"], line))
    return upgrades, held


def osv_findings(packages: list[tuple[str, str, str]], results: list) -> dict:
    """Pair an osv.dev querybatch answer with the (eco, name, ver) asked."""
    out = {}
    for (eco, name, ver), res in zip(packages, results, strict=True):
        ids = sorted(v["id"] for v in (res or {}).get("vulns", []))
        if ids:
            out[f"{name} {ver} ({eco})"] = ids
    return out


def wheel_fits(filename: str, minor: tuple[int, int]) -> bool:
    """Would this wheel install on CPython `minor` on Apple-silicon macOS?

    Accepts a pure wheel (py3-none-any), a wheel built for that exact
    CPython (cp315-cp315), or a stable-ABI wheel (cp3N-abi3, N <= minor).
    The free-threaded ABI (cp315t) is not ours and does not count."""
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    if len(parts) < 5:
        return False
    pys, abi, plats = parts[-3].split("."), parts[-2], parts[-1].split(".")
    if not any(p == "any" or (p.startswith("macosx") and p.endswith(("arm64", "universal2")))
               for p in plats):
        return False
    cp = f"cp{minor[0]}{minor[1]}"
    if abi == "none":
        return any(t in ("py3", f"py{minor[0]}{minor[1]}", cp) for t in pys)
    if abi == cp:
        return True
    if abi == "abi3":
        return any(re.fullmatch(r"cp3(\d+)", t) and int(t[3:]) <= minor[1] for t in pys)
    return False


def readiness(files_by_pkg: dict[str, tuple[str, list[str]]],
              minor: tuple[int, int]) -> list[str]:
    """Blockers for moving to `minor`, from each package's latest release
    files: [] means every package has a wheel that installs."""
    out = []
    for name, (ver, files) in sorted(files_by_pkg.items()):
        if any(wheel_fits(f, minor) for f in files):
            continue
        how = ("source only — pip would have to COMPILE it"
               if any(f.endswith((".tar.gz", ".zip")) for f in files) else "nothing installable")
        out.append(f"{name} {ver} ({how})")
    return out


def caps(reqs_text: str) -> dict[str, str]:
    """Upper bounds requirements.txt sets on purpose: {'fastapi': '1.0', …}."""
    out = {}
    for line in reqs_text.splitlines():
        spec = line.split("#", 1)[0].strip()
        m = re.match(r"([A-Za-z0-9_.\-]+)\S*?,?<\s*([0-9.]+)", spec)
        if m:
            out[m.group(1).lower()] = m.group(2)
    return out


def new_attention(previous: list[str], now: list[str]) -> list[str]:
    """Items that were not on last week's list — what is worth a ping."""
    seen = set(previous)
    return [item for item in now if item not in seen]


# ── side-effecting helpers ───────────────────────────────────────────────

def say(msg: str) -> None:
    print(msg, flush=True)


def run(cmd, check=True, capture=False, env=None, quiet=False) -> subprocess.CompletedProcess:
    if not quiet:
        say("   $ " + " ".join(cmd))
    return subprocess.run(cmd, check=check, text=True, env=env,
                          capture_output=capture)


def fetch(url: str, data: bytes | None = None, headers=None, timeout=30) -> bytes:
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def brew_env() -> dict:
    return {**os.environ, "HOMEBREW_NO_INSTALL_CLEANUP": "1",
            "HOMEBREW_NO_ENV_HINTS": "1"}


def venv_minor() -> str | None:
    if not os.path.exists(VENV_PY):
        return None
    r = run([VENV_PY, "-c", "import sys;print('%d.%d'%sys.version_info[:2])"],
            check=False, capture=True, quiet=True)
    return r.stdout.strip() or None


def runtime_report(py: str) -> str:
    r = run([py, "-c", "import sys,sqlite3,ssl;print(sys.version.split()[0],"
             "'· sqlite',sqlite3.sqlite_version,'·',ssl.OPENSSL_VERSION)"],
            check=False, capture=True, quiet=True)
    return r.stdout.strip() or r.stderr.strip()


def brew_outdated(names: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    installed = run(["brew", "list", "--formula", "-1"], capture=True,
                    quiet=True).stdout.split()
    names = [n for n in names if n in installed]
    r = run(["brew", "outdated", "--json=v2", *names], check=False,
            capture=True, quiet=True)
    try:
        data = json.loads(r.stdout or "{}").get("formulae", [])
    except ValueError:
        return [], []
    return brew_moves(data)


def python_formulae(minor: str) -> list[str]:
    """python@<minor> plus what it links — from brew, because the answer
    changes under you (openssl@3 → openssl@4 within one patch release)."""
    r = run(["brew", "deps", f"python@{minor}"], check=False, capture=True, quiet=True)
    return [f"python@{minor}", *r.stdout.split()]


def newest_python() -> str | None:
    r = run(["brew", "info", "--json=v2", "python@3"], check=False,
            capture=True, quiet=True)
    try:
        return json.loads(r.stdout)["formulae"][0]["name"].split("@")[1]
    except (ValueError, KeyError, IndexError):
        return None


def resolve_pip(py: str) -> list[tuple[str, str]]:
    """What a fresh eager install of runtime+dev would pick, as (name, ver)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write("\n".join(unpinned(open(DEV_REQS, encoding="utf-8").read())))
    try:
        r = run([py, "-m", "pip", "install", "--dry-run", "--quiet", "--report", "-",
                 "--ignore-installed", "-r", REQS, "-r", fh.name],
                check=False, capture=True, quiet=True)
    finally:
        os.unlink(fh.name)
    if r.returncode:
        raise RuntimeError("pip could not resolve:\n" + r.stderr[-2000:])
    report = json.loads(r.stdout)
    return [(i["metadata"]["name"].lower().replace("_", "-"), i["metadata"]["version"])
            for i in report.get("install", [])]


def installed_pip(py: str) -> dict[str, str]:
    r = run([py, "-m", "pip", "list", "--format=json"], check=False,
            capture=True, quiet=True)
    try:
        return {p["name"].lower().replace("_", "-"): p["version"]
                for p in json.loads(r.stdout or "[]")}
    except ValueError:
        return {}


def hls_plan(allow_major: bool):
    """(current, target, tarball_url, integrity) — target None if current."""
    current = hls_version_in(open(HLS, encoding="utf-8", errors="replace").read())
    meta = json.loads(fetch("https://registry.npmjs.org/hls.js",
                            headers={"Accept": "application/vnd.npm.install-v1+json"}))
    target = pick_hls_version(meta["versions"], current or "", allow_major)
    latest = meta["dist-tags"]["latest"]
    if not target:
        return current, None, None, None, latest
    dist = meta["versions"][target]["dist"]
    return current, target, dist["tarball"], dist["integrity"], latest


def pypi_latest(names: list[str]) -> dict[str, tuple[str, list[str]]]:
    """name → (latest version, its release filenames), from PyPI's JSON."""
    from concurrent.futures import ThreadPoolExecutor

    def one(name):
        try:
            data = json.loads(fetch(f"https://pypi.org/pypi/{name}/json", timeout=20))
        except (OSError, ValueError) as e:
            say(f"  ! PyPI lookup failed for {name}: {e}")
            return name, None
        return name, (data["info"]["version"], [u["filename"] for u in data["urls"]])

    with ThreadPoolExecutor(max_workers=8) as pool:
        return {n: v for n, v in pool.map(one, names) if v}


def rehearse_builds(minor: str, blockers: list[str]) -> tuple[list[str], list[str]]:
    """Try each SOURCE-ONLY blocker for real: build it in a throwaway venv
    on python@<minor>. Returns (still blocked, verified to build here).

    "No wheel" is a risk, not a verdict: watchdog has had no macOS wheel
    since cp313 and pip has compiled it fine on 3.14 all along (2026-10-23).
    Needs python@<minor> installed; without it the blockers stand."""
    py = f"/opt/homebrew/opt/python@{minor}/bin/python{minor}"
    source_only = [b for b in blockers if "source only" in b]
    if not source_only or not os.path.exists(py):
        return blockers, []
    built = []
    with tempfile.TemporaryDirectory(prefix="upgrade-rehearse-") as tmp:
        venv_py = os.path.join(tmp, "bin", "python")
        run([py, "-m", "venv", tmp], quiet=True)
        for b in source_only:
            name, ver = b.split()[:2]
            say(f"  … building {name} {ver} from source on {minor} (throwaway venv)")
            r = run([venv_py, "-m", "pip", "install", "--quiet", "--no-deps",
                     f"{name}=={ver}"], check=False, capture=True, quiet=True)
            if r.returncode == 0:
                built.append(f"{name} {ver}")
    still = [b for b in blockers if " ".join(b.split()[:2]) not in built]
    return still, built


def osv_check(pkgs: list[tuple[str, str, str]]) -> dict:
    body = json.dumps({"queries": [
        {"package": {"name": n, "ecosystem": e}, "version": v} for e, n, v in pkgs]})
    data = json.loads(fetch("https://api.osv.dev/v1/querybatch", data=body.encode(),
                            headers={"Content-Type": "application/json"}))
    return osv_findings(pkgs, data.get("results", []))


def appium_outdated() -> str | None:
    if not shutil.which("npm"):
        return None
    r = run(["npm", "outdated", "-g", "--json", "appium"], check=False,
            capture=True, quiet=True)
    try:
        info = json.loads(r.stdout or "{}").get("appium")
    except ValueError:
        return None
    return info and f"appium {info['current']} → {info['latest']}"


# ── report ───────────────────────────────────────────────────────────────

def report(args) -> int:
    """Print every layer, then the ATTENTION list: each finding sorted into
    DO (safe, gated — run --apply), DECIDE (a person's call), WAIT (should
    not be done yet, and why) or NOTE (information)."""
    items: list[tuple[str, str]] = []
    minor = venv_minor()
    say(f"Runtime now: {runtime_report(VENV_PY) if minor else 'no .venv'}")
    have = installed_pip(VENV_PY)

    target = args.python or newest_python()
    if minor and target and vtuple(target) > vtuple(minor):
        say(f"\nPython {target} readiness (venv is on {minor}) — latest release of "
            f"each installed package, checked for a {target} wheel on PyPI:")
        names = [n for n in have if n not in ("pip", "setuptools")]
        blockers = readiness(pypi_latest(names), vtuple(target)[:2])
        blockers, built = rehearse_builds(target, blockers)
        for b in built:
            say(f"  ✓ {b} — no wheel, but VERIFIED to compile here")
        for b in blockers:
            say(f"  ✗ {b}")
        if blockers:
            items.append(("WAIT", f"Python {target}: not yet — no {target} wheel for "
                                  f"{len(blockers)} package(s): "
                                  + ", ".join(b.split()[0] for b in blockers)))
        else:
            say(f"  every package installs on {target}")
            via = (f" ({', '.join(b.split()[0] for b in built)} compiles from source)"
                   if built else "")
            items.append(("DECIDE", f"Python {target}: READY{via} — move with "
                                    f"`--apply --python {target}` (gated, rolls back)"))

    say("\nHomebrew:")
    upgrades, held = brew_outdated([*python_formulae(minor or "3"), *FORMULAE])
    for line in [u[1] for u in upgrades] + held or ["(all current)"]:
        say(f"  {line}")
    if upgrades:
        items.append(("DO", f"brew: {len(upgrades)} formula(e) — "
                            + ", ".join(n for n, _ in upgrades)))
    for line in held:
        items.append(("DECIDE", "brew " + line.replace("  (a DOWNGRADE — held; decide by hand)",
                                                       " is a DOWNGRADE — held")))

    say("\npip (fresh resolve, runtime + dev, within requirements.txt caps):")
    moves = [(n, have.get(n), v) for n, v in resolve_pip(VENV_PY) if have.get(n) != v]
    for n, old, new in moves:
        say(f"  {n} {old or 'new'} → {new}")
    if not moves:
        say("  (all current)")
    if moves:
        items.append(("DO", f"pip: {len(moves)} package(s)"))
    capped = caps(open(REQS, encoding="utf-8").read())
    for name, (latest, _) in sorted(pypi_latest(list(capped)).items()):
        if vtuple(latest) and vtuple(latest) >= vtuple(capped[name]):
            items.append(("WAIT", f"{name} {latest} is out, but requirements.txt caps it "
                                  f"<{capped[name]} on purpose — raise the cap as its own change"))

    say("\nhls.js (static/vendor):")
    cur, hls_target, _, _, latest = hls_plan(args.allow_major)
    if hls_target:
        say(f"  {cur} → {hls_target}")
        items.append(("DO", f"hls.js {cur} → {hls_target}"))
    else:
        say(f"  {cur} (current{'' if cur == latest else f'; {latest} needs --allow-major'})")
        if cur != latest:
            items.append(("WAIT", f"hls.js {latest} is a new MAJOR — review app.js's use of "
                                  "it first, then --allow-major"))

    if (line := appium_outdated()):
        say(f"\nnpm -g:\n  {line}")
        items.append(("DO", line))

    say("\nKnown vulnerabilities in what is installed now (osv.dev):")
    pkgs = [("PyPI", n, v) for n, v in sorted(have.items())]
    if cur:
        pkgs.append(("npm", "hls.js", cur))
    vulns = osv_check(pkgs)
    for pkg, ids in vulns.items():
        say(f"  ✗ {pkg}: {', '.join(ids)}")
    if not vulns:
        say("  none known")
    say("  (Homebrew formulae are not in osv.dev — upgrading them is the check.)")
    moving = {n for n, _, _ in moves} | ({"hls.js"} if hls_target else set())
    fixed = [p for p in vulns if p.split()[0] in moving]
    unfixed = [p for p in vulns if p.split()[0] not in moving]
    if fixed:
        items.append(("DO", f"{len(fixed)} vulnerable package(s) get a fixed release: "
                            + ", ".join(p.split()[0] for p in fixed)))
    for p in unfixed:
        items.append(("NOTE", f"{p} is vulnerable and has NO fixed release yet — watch it"))

    # The README's version strip: its PLATFORM half (python, sqlite, openssl,
    # node, ffmpeg) moves with brew and is watched here, not in the suite.
    r = run([VENV_PY, os.path.join("tools", "versions.py")], check=False,
            capture=True, quiet=True)
    if r.returncode:
        items.append(("DO", "README version strip is stale — "
                            "`.venv/bin/python tools/versions.py --write`"))

    manifest = os.path.join(PREV, "manifest.json")
    if os.path.exists(manifest):
        saved = json.load(open(manifest)).get("saved_at", "?")
        items.append(("NOTE", f"rollback point from {saved} kept in {PREV} "
                              "(`--rollback`; replaced by the next --apply)"))

    say("\nAttention")
    for kind in KINDS:
        for k, text in items:
            if k == kind:
                say(f"  {kind:<7} {text}")
    if not items:
        say("  nothing — all current, nothing known-vulnerable")
    counts = " · ".join(f"{sum(k == kind for k, _ in items)} {kind.lower()}" for kind in KINDS)
    say(f"Attention: {counts}")
    say("Dry run — nothing changed.")

    if not args.check:
        return 0
    watch = [f"{k} {t}" for k, t in items if k in ("DECIDE", "WAIT")]
    try:
        previous = json.load(open(ATTENTION_STATE))
    except (OSError, ValueError):
        previous = []
    fresh = new_attention(previous, watch)
    os.makedirs(BACKUPS, exist_ok=True)
    with open(ATTENTION_STATE, "w") as fh:
        json.dump(watch, fh, indent=1)
    for item in fresh:
        say(f"NEW since last check: {item}")
    act = any(k == "DO" for k, _ in items) or fresh
    return 1 if act else 0


# ── apply / rollback ─────────────────────────────────────────────────────

def save_previous(minor: str) -> None:
    if os.path.isdir(PREV):
        shutil.rmtree(PREV)
    os.makedirs(os.path.join(PREV, "files"))
    for path in TRACKED:
        shutil.copy2(path, os.path.join(PREV, "files", os.path.basename(path)))
    os.rename(VENV, os.path.join(PREV, "venv"))   # same volume → instant
    with open(os.path.join(PREV, "manifest.json"), "w") as fh:
        json.dump({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "python": minor}, fh)


def restore_previous() -> None:
    saved = os.path.join(PREV, "venv")
    if not os.path.isdir(saved):
        raise SystemExit(f"nothing to roll back to ({saved} is missing)")
    if os.path.isdir(VENV):
        shutil.rmtree(VENV)
    os.rename(saved, VENV)
    for path in TRACKED:
        shutil.copy2(os.path.join(PREV, "files", os.path.basename(path)), path)
    if os.path.exists(os.path.join(VENV, "bin", "playwright")):
        # The new playwright may have garbage-collected the old browsers.
        run([os.path.join(VENV, "bin", "playwright"), "install", "chromium", "webkit"],
            check=False)
    say("   restored previous .venv, both locks, requirements-dev.txt, hls.min.js")


def restart_gateway() -> bool:
    uid = os.getuid()
    if run(["launchctl", "print", f"gui/{uid}/{LABEL}"], check=False,
           capture=True, quiet=True).returncode:
        say(f"   {LABEL} is not loaded — not restarting")
        return True
    run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"])
    deadline = time.time() + 90
    while time.time() < deadline:
        time.sleep(3)
        try:
            fetch(HEALTH_URL, timeout=5)
            say(f"   gateway answers {HEALTH_URL}")
            return True
        except OSError:
            continue
    say(f"   ✗ gateway did not answer {HEALTH_URL} within 90 s")
    return False


def gates(log_path: str, frontend: bool) -> bool:
    cmds = [[VENV_PY, "tests/run_all.py", "--offline"]]
    if frontend:
        cmds.append([VENV_PY, "tests/run_all.py", "--frontend-only"])
    with open(log_path, "w") as log:
        for cmd in cmds:
            say("   $ " + " ".join(cmd))
            r = subprocess.run(cmd, cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT)
            if r.returncode:
                log.flush()
                tail = open(log_path, encoding="utf-8", errors="replace").read()[-3000:]
                say(f"   ✗ gate failed (full log: {log_path})\n{tail}")
                return False
    return True


def upgrade_venv(python: str) -> None:
    run([python, "-m", "venv", VENV])
    run([VENV_PY, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    dev_names = unpinned(open(DEV_REQS, encoding="utf-8").read())
    run([VENV_PY, "-m", "pip", "install", "--quiet", "--upgrade",
         "--upgrade-strategy", "eager", "-r", REQS, *dev_names])
    run([VENV_PY, os.path.join("tools", "regen_lock.py")])

    have = installed_pip(VENV_PY)
    comments = {}
    if "playwright" in have:
        run([os.path.join(VENV, "bin", "playwright"), "install", "chromium", "webkit"])
        dry = run([os.path.join(VENV, "bin", "playwright"), "install", "--dry-run",
                   "chromium", "webkit"], capture=True, quiet=True).stdout
        if (note := browser_comment(dry)):
            comments["playwright"] = note
    text = open(DEV_REQS, encoding="utf-8").read()
    with open(DEV_REQS, "w", encoding="utf-8") as fh:
        fh.write(rewrite_pins(text, have, comments))


def upgrade_hls(allow_major: bool) -> None:
    cur, target, url, integrity, _ = hls_plan(allow_major)
    if not target:
        say(f"   hls.js {cur} is current")
        return
    tgz = fetch(url, timeout=60)
    if not sri_ok(tgz, integrity):
        raise RuntimeError(f"hls.js {target} tarball FAILED its integrity check — not installed")
    js = tar_member(tgz, HLS_MEMBER)
    if hls_version_in(js.decode("utf-8", "replace")) != target:
        raise RuntimeError(f"hls.js tarball does not carry version {target}")
    with open(HLS, "wb") as fh:
        fh.write(js)
    say(f"   hls.js {cur} → {target} (sha512 verified)")


# Run in this order inside the snapshot; any failure rolls all of it back.
# `versions` BEFORE `gates`: tests/test_versions.py checks the README strip
# against the NEW locks (epub-reader's first --apply was refused for this).
GUARDED_STEPS = ("venv", "hls", "versions", "gates")


def apply(args) -> int:
    minor = venv_minor()
    if not minor:
        raise SystemExit("no working .venv — run ./setup.sh first")
    target_minor = args.python or minor
    say(f"Before: {runtime_report(VENV_PY)}")

    say("\n1. Backing up library.db")
    run([os.path.join(PROJECT, "backup-library.sh")])

    say("\n2. Homebrew")
    run(["brew", "update", "--quiet"], env=brew_env())
    if target_minor != minor:
        run(["brew", "install", f"python@{target_minor}"], env=brew_env())
    installed = run(["brew", "list", "--formula", "-1"], capture=True, quiet=True).stdout.split()
    wanted = [f for f in (*python_formulae(target_minor), *FORMULAE) if f in installed]
    upgrades, held = brew_outdated(wanted)
    outdated = {name for name, _ in upgrades}
    for line in held:
        say(f"   ! {line}")
    # Pin held formulae for the duration: `brew upgrade python@3.14` would
    # otherwise pull the held downgrade in as a dependency.
    pinned = [line.split()[0] for line in held]
    for name in pinned:
        run(["brew", "pin", name], check=False)
    try:
        if outdated:
            run(["brew", "upgrade", *sorted(outdated)], env=brew_env())
    finally:
        for name in pinned:
            run(["brew", "unpin", name], check=False)
    if "beets" in outdated:
        # The keg is replaced on upgrade and loses its plugin deps.
        keg = run(["brew", "--prefix", "beets"], capture=True, quiet=True).stdout.strip()
        libexec = os.path.join(keg, "libexec")
        run([os.path.join(libexec, "bin", "python"), "-m", "pip", "install",
             "--quiet", "--prefix", libexec, *BEETS_PLUGIN_DEPS], check=False)
    if (line := appium_outdated()):
        say(f"   {line}")
        run(["npm", "install", "-g", "appium@latest"], check=False)

    python = f"/opt/homebrew/opt/python@{target_minor}/bin/python{target_minor}"
    if not os.path.exists(python):
        raise SystemExit(f"{python} not found")

    say(f"\n3. Fresh .venv on Python {target_minor} (previous kept in {PREV})")
    save_previous(minor)
    log_path = os.path.join(PREV, "gates.log")
    steps = {
        "venv": lambda: upgrade_venv(python),
        "hls": lambda: upgrade_hls(args.allow_major),
        "versions": lambda: run([VENV_PY, os.path.join("tools", "versions.py"), "--write"]),
        "gates": lambda: gates(log_path, frontend=not args.no_frontend),
    }
    try:
        ok = True
        for i, step in enumerate(GUARDED_STEPS, start=4):
            say(f"\n{i}. {step}")
            if steps[step]() is False:
                ok = False
                break
    except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
        say(f"   ✗ {e}")
        ok = False

    if not ok:
        say("\n✗ Upgrade FAILED — rolling back")
        restore_previous()
        if not args.no_restart:
            restart_gateway()
        return 1

    say("\n8. Restart")
    if not args.no_restart and not restart_gateway():
        say("\n✗ Gateway unhealthy after restart — rolling back")
        restore_previous()
        restart_gateway()
        return 1

    say(f"\nAfter: {runtime_report(VENV_PY)}")
    vulns = osv_check([("PyPI", n, v) for n, v in sorted(installed_pip(VENV_PY).items())])
    for pkg, ids in vulns.items():
        say(f"  still affected (no fixed release yet?): {pkg}: {', '.join(ids)}")
    say("\n✓ Upgrade passed every gate. Review and commit:\n"
        "    git diff --stat\n"
        "    git commit -am 'chore(deps): upgrade toolchain (tools/upgrade.py)'\n"
        "  Undo, if it misbehaves live: python3 tools/upgrade.py --rollback")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="perform the upgrade")
    mode.add_argument("--check", action="store_true",
                      help="report; exit 1 if anything is outdated or vulnerable")
    mode.add_argument("--rollback", action="store_true",
                      help="restore the venv + files saved by the last --apply")
    ap.add_argument("--python", metavar="3.N",
                    help="with --apply: rebuild the venv on this minor (brew installs "
                         "it); without: a DRY RUN of that move — which packages lack a wheel")
    ap.add_argument("--allow-major", action="store_true",
                    help="let hls.js cross a major version")
    ap.add_argument("--no-frontend", action="store_true",
                    help="skip the Playwright UI suite gate")
    ap.add_argument("--no-restart", action="store_true",
                    help="do not restart the launchd gateway")
    args = ap.parse_args()
    os.chdir(PROJECT)

    if args.rollback:
        restore_previous()
        return 0 if args.no_restart or restart_gateway() else 1
    if args.apply:
        return apply(args)
    return report(args)


if __name__ == "__main__":
    sys.exit(main())
