#!/usr/bin/env python3
"""
improve_song_years.py — backfill the "original recording year" for
every (artist, title) in the library via MusicBrainz recording
search, then propagate the discovered year onto matching tracks via
metadata_overrides.

Why this exists
===============
AcoustID resolves a fingerprint to ONE MusicBrainz recording entry,
and that recording's `first-release-date` is the date *that specific
recording* (master/edition) was first released. For songs that have
been re-released many times — anthologies, expanded editions,
compilations — AcoustID often picks a later-edition recording and
the "MB original year" we store in metadata_overrides.year ends up
being the reissue date.

Previously we had two mitigations:

  1. tools/correct_year_drift.py — compares same-(artist, title)
     groups *within the library* and rewrites later instances to
     the earliest plausible year. Only works for songs the user
     owns multiple copies of (~2.4k songs).
  2. The decade/now-playing display uses MIN(file_year, mb_year)
     to suppress later-edition MB years that are clearly wrong.

This tool adds a third leg: **search MB for ALL recordings of the
song** and use the EARLIEST first-release-date among them. So if
"What a Wonderful World" exists in MB as a 1967 recording AND a
1998 compilation recording AND a 2017 anniversary recording, we use
1967 — even though the user only owns the 1998 compilation copy.

Algorithm per (artist, title):
  1. MB search recording?query=artist:"X" AND recording:"Y"
  2. Paginate through ALL pages (limit=100 per page)
  3. Collect first-release-date years
  4. min_year = MIN(years)
  5. Cache in song_year_cache table — keyed by normalised
     (artist, title), sticky-negative if MB returned nothing usable

Cache table
===========
    song_year_cache(
      artist_key TEXT NOT NULL,    -- _norm_title(artist)
      title_key  TEXT NOT NULL,    -- _norm_title(title)
      year       INTEGER,          -- MIN MB year, NULL on no-match
      source     TEXT NOT NULL,    -- 'mb_recording' | 'notfound'
      n_matches  INTEGER NOT NULL DEFAULT 0,
      fetched_at INTEGER NOT NULL,
      PRIMARY KEY (artist_key, title_key)
    )

A row of ANY source means "we've already queried MB for this
song". 'notfound' is sticky so we don't re-hammer MB. Drop a
notfound row to force a re-lookup.

Apply step
==========
After lookups complete, walks tracks where the existing effective
year > cached year for that (artist, title), and writes
metadata_overrides.year = cached year with source='manual'. Same
slot the PWA's edit modal writes to — survives re-index, beats
AcoustID's wrong year on next worker pass.

Usage
=====
    # Default: lookup ALL (artist, title) groups not yet cached.
    # Dry-run preview, no MB requests, no DB writes.
    python3 tools/improve_song_years.py

    # Actually query MB and populate the cache.
    python3 tools/improve_song_years.py --lookup

    # Stop after N lookups (good for testing scope first).
    python3 tools/improve_song_years.py --lookup --limit 500

    # After cache is populated: apply cached years to tracks.
    python3 tools/improve_song_years.py --apply

    # Both phases in one go (cache then apply).
    python3 tools/improve_song_years.py --lookup --apply

    # Force re-query a single song (drop the cache row first):
    sqlite3 library.db "DELETE FROM song_year_cache WHERE artist_key='louis armstrong' AND title_key='what a wonderful world'"
"""
import argparse
import json
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from collections.abc import Iterable

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO / "library.db"


# ── Normalisation (mirrors dlna_library._norm_title) ─────────────

def _norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = (s.replace("‘", "'").replace("’", "'")
          .replace("‚", "'").replace("‛", "'")
          .replace("“", '"').replace("”", '"')
          .replace("´", "'").replace("`", "'"))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


# ── Schema ───────────────────────────────────────────────────────

def _ensure_schema(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS song_year_cache (
            artist_key TEXT NOT NULL,
            title_key  TEXT NOT NULL,
            year       INTEGER,
            source     TEXT NOT NULL,
            n_matches  INTEGER NOT NULL DEFAULT 0,
            fetched_at INTEGER NOT NULL,
            PRIMARY KEY (artist_key, title_key)
        )
    """)
    conn.commit()


# ── MB query (paginated, rate-limited) ───────────────────────────

import os
_UA = f"DLNAGateway/1.0 ( {os.environ.get('GATEWAY_CONTACT_EMAIL', 'you@example.com')} )"
_RATE_LIMIT_SEC = 1.1
_TIMEOUT = 12.0
_MAX_PAGES = 5         # cap at 500 recordings — diminishing returns past that
_PAGE_SIZE = 100
_LAST_REQUEST_T = 0.0


class TransientError(Exception):
    pass


def _mb_get(url: str) -> dict | None:
    """GET a MB endpoint, returning parsed JSON or None on permanent
    failure. Raises TransientError on 5xx/network errors so the caller
    can decide whether to retry / bail / skip."""
    global _LAST_REQUEST_T
    elapsed = time.time() - _LAST_REQUEST_T
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)
    _LAST_REQUEST_T = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if 500 <= e.code < 600:
            raise TransientError(f"HTTP {e.code}") from e
        return None      # 4xx, garbled etc.
    except (TimeoutError, urllib.error.URLError, OSError) as e:
        raise TransientError(str(e)) from e


def search_song_year(artist: str, title: str) -> tuple[int | None, int]:
    """Search MB for all recordings of (artist, title), return
    (MIN_year, n_matches). MIN_year is None when no recording has a
    usable first-release-date."""
    q = f'artist:"{_mb_escape(artist)}" AND recording:"{_mb_escape(title)}"'
    years = []
    total = None
    for page in range(_MAX_PAGES):
        offset = page * _PAGE_SIZE
        url = ("https://musicbrainz.org/ws/2/recording/"
               f"?query={urllib.parse.quote(q)}"
               f"&fmt=json&limit={_PAGE_SIZE}&offset={offset}")
        data = _mb_get(url)
        if not data:
            break
        if total is None:
            total = data.get("count", 0)
        for rec in data.get("recordings", []):
            frd = rec.get("first-release-date", "")
            if frd and frd[:4].isdigit():
                y = int(frd[:4])
                if 1900 <= y <= 2100:
                    years.append(y)
        if offset + _PAGE_SIZE >= (total or 0):
            break
    return (min(years) if years else None, total or 0)


_MB_ESCAPE_RE = re.compile(r'(["\\])')
def _mb_escape(s: str) -> str:
    return _MB_ESCAPE_RE.sub(r"\\\1", s or "")


# ── Candidate selection ──────────────────────────────────────────

# Skip rows whose tags are obviously filename-derived or placeholder.
# These never match in MB and just waste queries.
_UNKNOWN_RE     = re.compile(r"^\s*[\(\[]?\s*unknown", re.IGNORECASE)
# Title starts with a track-number prefix like "01 - " / "10 ".
_TRACKNUM_RE    = re.compile(r"^\s*\d{1,3}\s*[-\s_.]")


def _is_skippable(artist: str, title: str) -> bool:
    """Heuristic: skip filename-derived phantom rows that MB will
    never match. (Unknown Artist) is the dead giveaway; title starting
    with a track-number prefix ("01 - …") is another."""
    if _UNKNOWN_RE.match(artist or "") or _UNKNOWN_RE.match(title or ""):
        return True
    if _TRACKNUM_RE.match(title or ""):
        return True
    return False


def find_candidates(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return list of (artist, title) groups not yet cached. Uses the
    EXACT artist/title as stored in the most recent row of each group
    (the MB query needs the human-readable form, not the normalised
    key — normalisation is only the cache key)."""
    conn.row_factory = sqlite3.Row
    cached = set()
    for r in conn.execute("SELECT artist_key, title_key FROM song_year_cache"):
        cached.add((r["artist_key"], r["title_key"]))
    seen_groups = set()
    out = []
    for r in conn.execute("""
        SELECT t.artist, t.title FROM tracks t
         WHERE t.artist != '' AND t.title != ''
         ORDER BY t.artist COLLATE NOCASE, t.title COLLATE NOCASE
    """):
        ar, ti = r["artist"], r["title"]
        if _is_skippable(ar, ti):
            continue
        ka, kt = _norm(ar), _norm(ti)
        if (ka, kt) in cached or (ka, kt) in seen_groups:
            continue
        seen_groups.add((ka, kt))
        out.append((ar, ti))
    return out


# ── Lookup phase ─────────────────────────────────────────────────

def _eta(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{seconds/60:.1f}min"
    return f"{seconds/3600:.1f}h"


def run_lookup(conn: sqlite3.Connection, candidates: list, limit: int,
               verbose: bool) -> dict:
    stats = {"queried": 0, "hits": 0, "notfound": 0, "transient": 0}
    n = len(candidates) if limit <= 0 else min(limit, len(candidates))
    if n == 0:
        return stats
    eta_total = n * 1.5
    print(f"Looking up {n:,} song(s) — estimated runtime ~{_eta(eta_total)} "
          f"at MB's 1.1s rate limit.")
    print()
    t_start = time.time()
    for _i, (artist, title) in enumerate(candidates[:n]):
        ka, kt = _norm(artist), _norm(title)
        if not ka or not kt:
            continue
        try:
            year, n_matches = search_song_year(artist, title)
        except TransientError as e:
            stats["transient"] += 1
            if verbose:
                print(f"  ↺ {artist[:30]} / {title[:40]} — transient ({e})")
            continue
        if year is not None:
            stats["hits"] += 1
            src = "mb_recording"
        else:
            stats["notfound"] += 1
            src = "notfound"
        conn.execute(
            "INSERT OR REPLACE INTO song_year_cache "
            "(artist_key, title_key, year, source, n_matches, fetched_at) "
            "VALUES (?,?,?,?,?, strftime('%s','now'))",
            (ka, kt, year, src, n_matches))
        conn.commit()
        stats["queried"] += 1
        if verbose or stats["queried"] % 50 == 0:
            marker = "✓" if year else "—"
            extra = f"  → {year}" if year else "  no match"
            elapsed = time.time() - t_start
            rate = stats["queried"] / max(elapsed, 0.1)
            remaining = (n - stats["queried"]) / max(rate, 0.01)
            print(f"  [{stats['queried']}/{n}]  {marker}  "
                  f"'{artist[:25]:25s}' / '{title[:35]:35s}'{extra}  "
                  f"(eta {_eta(remaining)})")
    return stats


# ── Apply phase ──────────────────────────────────────────────────

def _eff_year_now(conn: sqlite3.Connection, url: str) -> int | None:
    """MIN(file_year, mb_year) with NULLs treated as missing."""
    r = conn.execute(
        "SELECT t.year file_y, m.year mb_y "
        "FROM tracks t LEFT JOIN metadata_overrides m ON m.url=t.url "
        "WHERE t.url=?", (url,)).fetchone()
    if not r:
        return None
    fy, my = r[0], r[1]
    if fy and my:
        return min(fy, my)
    return fy or my or None


def run_apply(conn: sqlite3.Connection, verbose: bool) -> dict:
    conn.row_factory = sqlite3.Row
    stats = {"checked": 0, "applied": 0, "already_ok": 0, "skipped_manual": 0}
    # For each cached song with a hit, find tracks whose effective
    # year is later than the cached year and update them.
    cur = conn.execute(
        "SELECT artist_key, title_key, year FROM song_year_cache "
        "WHERE source='mb_recording' AND year IS NOT NULL")
    for ak, tk, mb_year in cur.fetchall():
        # Walk tracks matching this (artist, title) — normalise on-the-fly
        # because tracks has no _norm column.
        rows = conn.execute(
            "SELECT t.url, t.artist, t.title, t.year file_y, "
            "       m.year mb_y, m.source m_src "
            "FROM tracks t LEFT JOIN metadata_overrides m ON m.url=t.url"
        ).fetchall()
        for r in rows:
            if _norm(r["artist"]) != ak or _norm(r["title"]) != tk:
                continue
            stats["checked"] += 1
            # Don't trample manual edits.
            if r["m_src"] == "manual":
                stats["skipped_manual"] += 1
                continue
            fy, my = r["file_y"], r["mb_y"]
            current_eff = min(fy or 9999, my or 9999)
            if current_eff <= mb_year:
                stats["already_ok"] += 1
                continue
            # Write metadata_overrides.year = mb_year, source='manual'
            conn.execute(
                "INSERT INTO metadata_overrides "
                "(url, artist, album, title, genre, year, source, updated_at) "
                "SELECT url, artist, album, title, genre, ?, 'manual', "
                "       datetime('now') FROM tracks WHERE url=? "
                "ON CONFLICT(url) DO UPDATE SET "
                "year=excluded.year, source='manual', "
                "updated_at=datetime('now')",
                (mb_year, r["url"]))
            stats["applied"] += 1
            if verbose:
                print(f"  applied {mb_year} to '{r['artist'][:25]}' / "
                      f"'{r['title'][:35]}'  (was eff={current_eff})")
        # Single commit per group keeps things bounded.
        conn.commit()
    return stats


# ── CLI ──────────────────────────────────────────────────────────

def main(argv: Iterable[str] = None) -> int:
    p = argparse.ArgumentParser(
        description="Backfill original-recording-year for all songs "
                    "via MusicBrainz, then apply to tracks.")
    p.add_argument("--db", default=str(_DEFAULT_DB),
                   help="library.db (default: %(default)s)")
    p.add_argument("--lookup", action="store_true",
                   help="Query MB for uncached (artist, title) groups.")
    p.add_argument("--apply",  action="store_true",
                   help="Write cached years onto tracks via "
                        "metadata_overrides (source='manual').")
    p.add_argument("--limit",  type=int, default=0,
                   help="Stop lookup after N MB queries (0 = unlimited).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"ERROR: library.db not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    _ensure_schema(conn)

    cached_n = conn.execute(
        "SELECT COUNT(*) FROM song_year_cache").fetchone()[0]
    hits_n = conn.execute(
        "SELECT COUNT(*) FROM song_year_cache WHERE source='mb_recording'"
    ).fetchone()[0]
    print(f"DB: {db_path}")
    print(f"song_year_cache: {cached_n:,} entries  ({hits_n:,} hits)")

    if args.lookup:
        candidates = find_candidates(conn)
        print(f"Uncached (artist, title) groups: {len(candidates):,}")
        if not candidates:
            print("Nothing to do.")
        else:
            stats = run_lookup(conn, candidates, args.limit, args.verbose)
            print()
            print(f"Lookup results: queried={stats['queried']:,}  "
                  f"hits={stats['hits']:,}  notfound={stats['notfound']:,}  "
                  f"transient={stats['transient']:,}")

    if args.apply:
        print()
        stats = run_apply(conn, args.verbose)
        print(f"Apply results: checked={stats['checked']:,}  "
              f"applied={stats['applied']:,}  "
              f"already_ok={stats['already_ok']:,}  "
              f"skipped_manual={stats['skipped_manual']:,}")

    if not args.lookup and not args.apply:
        print()
        print("Dry-run preview: pass --lookup to query MB, "
              "--apply to write cached years onto tracks.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
