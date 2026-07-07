#!/usr/bin/env python3
"""Sync Immich's person recognition onto the gateway's videos.

Immich never writes person/face data into the original files — it lives
in Immich's own Postgres. This tool pulls it over the Immich REST API
and stores it in the gateway's `video_people` table, which feeds the
"👤 By person" browse (LG/DLNA) and the PWA people grouping.

Matching is by CONTENT CHECKSUM, not path: Immich's originalPath is a
Docker-container path and many person-tagged assets live in an external
library — unreliable to map. Immich returns each asset's SHA1 (base64);
we SHA1 the GWMovies files once (cached in `<dest>/.immich-import.db`,
table `video_sha1`, alongside the importer's BLAKE2b cache) and match on
that. The matched file's path relative to GWMovies gives the gateway
video id directly (sha1(rel_path)[:16] — dlna_video_index.video_id).

Scope: person-tagged videos that exist ONLY in Immich's external
library (not imported into GWMovies) can't be matched — they're counted
as "unmatched". Import them first (tools/immich_import.py) if wanted.

Per-person writes are a SYNC (replace, not merge): re-running after new
Immich tagging updates everything; links to videos Immich no longer
lists are dropped. Two Immich persons sharing one name are merged.

Config: IMMICH_URL + IMMICH_API_KEY from .env (loaded via dlna_config).
DRY-RUN by default; --apply writes.

    python3 tools/immich_people_sync.py             # preview
    python3 tools/immich_people_sync.py --apply     # sync
    python3 -m unittest tools.test_immich_people_sync -v
"""
import argparse
import base64
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

VIDEO_UDN = "uuid:localfs-movies"
DEFAULT_DB = os.path.join(PROJECT, "library.db")
DEFAULT_DEST = "/Volumes/SAMDATA/GWMovies"
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".3gp", ".avi", ".mkv", ".webm",
              ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg"}


def b64_to_hex(s: str) -> str:
    """Immich checksums are base64 SHA1 — our index is hex. '' on garbage."""
    try:
        raw = base64.b64decode(s or "", validate=True)
    except (ValueError, TypeError):
        return ""
    return raw.hex() if raw else ""


# ── Immich REST (fetch = injected callable → unit-testable) ───────

def make_fetcher(base_url: str, api_key: str, timeout: float = 30.0):
    """fetch(method, path, body=None) → parsed JSON, x-api-key auth."""
    def fetch(method, path, body=None):
        req = urllib.request.Request(
            base_url.rstrip("/") + path,
            data=(json.dumps(body).encode() if body is not None else None),
            headers={"x-api-key": api_key, "Accept": "application/json",
                     "Content-Type": "application/json"},
            method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    return fetch


def fetch_people(fetch) -> list:
    """All NAMED persons [{id, name}, …] (unnamed faces are noise)."""
    people, page = [], 1
    while True:
        data = fetch("GET", f"/api/people?page={page}&size=500")
        if isinstance(data, list):                 # older API shape
            batch, more = data, False
        else:
            batch = data.get("people") or []
            more = bool(data.get("hasNextPage"))
        people += [{"id": p["id"], "name": (p.get("name") or "").strip()}
                   for p in batch]
        if not more or not batch:
            break
        page += 1
    return [p for p in people if p["name"]]


def fetch_person_video_assets(fetch, person_id: str) -> list:
    """Every VIDEO asset Immich lists for a person (paginated)."""
    out, page = [], 1
    while True:
        data = fetch("POST", "/api/search/metadata",
                     {"personIds": [person_id], "type": "VIDEO",
                      "page": page, "size": 250})
        assets = (data.get("assets") or {})
        out += assets.get("items") or []
        nxt = assets.get("nextPage")
        if not nxt:
            break
        page = int(nxt)
    return out


# ── GWMovies SHA1 index (cached — hashing 1000+ videos is the slow
#    part; (path, size, mtime) unchanged → trust the cached digest) ──

def build_sha1_index(dest: Path, conn) -> dict:
    """{hex_sha1: rel_path} for every video file under dest."""
    conn.execute("""CREATE TABLE IF NOT EXISTS video_sha1(
        path TEXT PRIMARY KEY, size INTEGER, mtime REAL, sha1 TEXT)""")
    idx = {}
    for p in sorted(dest.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        st = p.stat()
        row = conn.execute(
            "SELECT sha1 FROM video_sha1 WHERE path=? AND size=? AND mtime=?",
            (str(p), st.st_size, st.st_mtime)).fetchone()
        if row:
            digest = row[0]
        else:
            h = hashlib.sha1()
            with open(p, "rb") as fh:
                while True:
                    b = fh.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
            digest = h.hexdigest()
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO video_sha1 VALUES (?,?,?,?)",
                    (str(p), st.st_size, st.st_mtime, digest))
        idx[digest] = str(p.relative_to(dest))
    return idx


_SUFFIX_RE = None


def _norm_name(name: str) -> str:
    """Lowercased filename with the importer's ' (N)' collision suffix
    stripped — 'IMG_9 (2).MOV' matches Immich's 'IMG_9.MOV'."""
    global _SUFFIX_RE
    import re
    if _SUFFIX_RE is None:
        _SUFFIX_RE = re.compile(r"^(.*) \(\d+\)(\.[^.]+)$")
    m = _SUFFIX_RE.match(name)
    if m:
        name = m.group(1) + m.group(2)
    return name.lower()


def build_name_size_index(dest: Path) -> dict:
    """{(normalized_name, size): rel_path} — the checksum fallback.
    Live finding 2026-07-07: Immich's stored SHA1 can differ from the
    bytes on disk (files metadata-edited in place after indexing; same
    size, different hash), so checksums alone under-match the external
    library. Ambiguous keys (two files, same norm name + size) are
    dropped rather than guessed."""
    idx, dupes = {}, set()
    for p in sorted(dest.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
            continue
        key = (_norm_name(p.name), p.stat().st_size)
        if key in idx or key in dupes:
            idx.pop(key, None)
            dupes.add(key)
            continue
        idx[key] = str(p.relative_to(dest))
    return idx


# ── the sync ──────────────────────────────────────────────────────

def sync_people(db, fetch, people, sha1_index, *, name_size_index=None,
                apply=False, udn=VIDEO_UDN, verbose=False) -> dict:
    """Match every named person's video assets to GWMovies — by checksum
    first, then (name, size) via a per-asset GET for the stale-checksum
    class — and (with apply) replace their video_people row sets. Persons
    sharing a name are merged. Returns stats."""
    import dlna_video_index
    by_name = {}
    stats = {"people": 0, "matched": 0, "matched_by_name": 0,
             "unmatched": 0, "written": 0}
    for person in people:
        stats["people"] += 1
        assets = fetch_person_video_assets(fetch, person["id"])
        vids, by_name_n, missed = set(), 0, 0
        for a in assets:
            rel = sha1_index.get(b64_to_hex(a.get("checksum") or ""))
            if not rel and name_size_index:
                try:
                    full = fetch("GET", f"/api/assets/{a.get('id')}")
                except Exception:                          # noqa: BLE001
                    full = {}
                size = ((full.get("exifInfo") or {})
                        .get("fileSizeInByte"))
                name = full.get("originalFileName") or ""
                if name and size:
                    rel = name_size_index.get((_norm_name(name), size))
                    if rel:
                        by_name_n += 1
            if rel:
                vids.add(dlna_video_index.video_id(rel))
            else:
                missed += 1
        stats["matched"] += len(vids)
        stats["matched_by_name"] += by_name_n
        stats["unmatched"] += missed
        entry = by_name.setdefault(person["name"],
                                   {"id": person["id"], "vids": set()})
        entry["vids"] |= vids
        print(f"  {person['name']:<28} {len(assets):3d} video asset(s) → "
              f"{len(vids)} in GWMovies"
              + (f" ({by_name_n} by name+size)" if by_name_n else "")
              + (f" ({missed} unmatched)" if missed else ""))
    if apply:
        for name, entry in sorted(by_name.items()):
            stats["written"] += db.video_people_replace(
                name, entry["id"], sorted(entry["vids"]))
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Sync Immich person tags onto gateway videos "
                    "(dry-run by default).")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--udn", default=VIDEO_UDN)
    ap.add_argument("--dest", default=DEFAULT_DEST,
                    help="the gateway video root (GWMovies)")
    ap.add_argument("--apply", action="store_true",
                    help="write video_people (default: preview only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    import dlna_config                                     # noqa: F401 (.env)
    base_url = (os.environ.get("IMMICH_URL") or "").strip()
    api_key = (os.environ.get("IMMICH_API_KEY") or "").strip()
    if not base_url or not api_key:
        print("FATAL: IMMICH_URL / IMMICH_API_KEY not set (.env)")
        return 2
    dest = Path(args.dest)
    if not dest.is_dir():
        print(f"FATAL: video root not mounted: {dest}")
        return 2
    if not os.path.isfile(args.db):
        print(f"FATAL: DB not found: {args.db}")
        return 2

    from dlna_library import LibraryDB
    db = LibraryDB(args.db)
    fetch = make_fetcher(base_url, api_key)

    print(f"Immich: {base_url}")
    people = fetch_people(fetch)
    print(f"named persons: {len(people)}")

    t0 = time.monotonic()
    cache = sqlite3.connect(dest / ".immich-import.db")
    index = build_sha1_index(dest, cache)
    name_size = build_name_size_index(dest)
    print(f"GWMovies SHA1 index: {len(index)} file(s) "
          f"({time.monotonic() - t0:.0f}s)")

    stats = sync_people(db, fetch, people, index,
                        name_size_index=name_size, apply=args.apply,
                        udn=args.udn, verbose=args.verbose)
    print(f"\npersons: {stats['people']} · asset matches: "
          f"{stats['matched']} ({stats['matched_by_name']} by name+size) "
          f"· unmatched: {stats['unmatched']}")
    if args.apply:
        rows = db.video_people_list(args.udn)
        print(f"video_people written: {stats['written']} row(s), "
              f"{len(rows)} browsable person(s)")
    else:
        print("DRY-RUN — re-run with --apply to write video_people.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
