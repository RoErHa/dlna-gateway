#!/usr/bin/env python3
"""
correct_year_drift.py — find tracks whose effective year drifted to a
reissue/compilation date and rewrite metadata_overrides.year back to
the song's original recording year (as evidenced by an earlier
appearance of the SAME song under a different album in the library).

Background
==========
AcoustID resolves a fingerprint to ONE MusicBrainz recording entry.
For songs that have been compiled, remastered, and re-released
multiple times, MB has dozens of recording entries with different
first-release-dates — AcoustID can pick the 2001 compilation's
recording entry instead of the 1979 studio original. The
COALESCE/MIN logic in the now-playing display and the decade view
can't recover from that on its own when BOTH file_year AND mb_year
agree on the reissue date.

But: if the same audio fingerprint is mounted on multiple albums in
the user's library (an extremely common pattern — original + 1-3
compilations + a Greatest Hits), the EARLIEST instance is almost
always the true original. This tool walks (artist, title) groups,
finds the earliest plausible year per group, and proposes year
corrections for the later instances.

Identification (v3)
===================
- Effective year per row = MIN(file_year, mb_year) when both present,
  else whichever is set.
- earliest_plausible per (artist, title) group = MIN(eff) where
  eff >= 1950 AND the row is NOT a live recording.
- Live filter: album or title matches any case-insensitive marker
  from LIVE_MARKERS (Pulse, Wembley, Earls Court, MTV Unplugged, etc.)
- A row is a CANDIDATE when:
  - eff is set,
  - eff - earliest_plausible >= 3,
  - the row is NOT live.

Persistence
===========
Each candidate gets metadata_overrides.year = earliest_plausible,
source='manual'. Same path as the PWA's edit modal. Survives
re-index and AcoustID re-runs (manual beats acoustid).

Usage
=====
    python3 tools/correct_year_drift.py                  # dry-run, report only
    python3 tools/correct_year_drift.py --top 50         # limit preview
    python3 tools/correct_year_drift.py --apply          # interactive confirm
    python3 tools/correct_year_drift.py --apply -y       # non-interactive (e.g. cron)
"""
import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

_REPO = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO / "library.db"

# Album/title substrings that mark a row as a live recording. Lowercased.
# Care: many of these (e.g. "live", "session") risk false positives. The
# tool always previews candidates before --apply commits anything.
LIVE_MARKERS = [
    'live', 'in concert', 'on tour', 'at the ', 'at hyde park',
    'pulse', 'earls court', 'wembley', 'madison square', 'fillmore',
    'royal albert', 'wall live', 'unplugged', 'mtv unplugged',
    '(live ', '[live ', '- live', 'session', 'bootleg',
]

_YEAR_FLOOR = 1950   # popular-recorded-music floor; pre-1950 tags are
                     # overwhelmingly file-tag errors in this corpus.
_DRIFT_THRESHOLD = 3  # eff - earliest_plausible >= this → candidate


def _live_clause(table_alias: str) -> str:
    parts = []
    for m in LIVE_MARKERS:
        e = m.replace("'", "''")
        parts.append(f"lower({table_alias}.album) LIKE '%{e}%'")
        parts.append(f"lower({table_alias}.title) LIKE '%{e}%'")
    return "(" + " OR ".join(parts) + ")"


def find_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Return the full list of mis-dated rows. Each dict has:
       url, artist, album, title, eff, should, src."""
    live = _live_clause("t")
    q = f"""
    WITH norm AS (
      SELECT t.url, t.artist, t.album, t.title, m.source AS src,
             lower(trim(t.artist)) AS k_artist,
             lower(trim(t.title))  AS k_title,
             COALESCE(
               CASE WHEN t.year > 0 AND m.year > 0 THEN MIN(t.year, m.year) END,
               NULLIF(t.year, 0), NULLIF(m.year, 0)
             ) AS eff,
             CASE WHEN {live} THEN 1 ELSE 0 END AS is_live
        FROM tracks t LEFT JOIN metadata_overrides m ON m.url = t.url
       WHERE t.artist != '' AND t.title != ''
    ),
    groups AS (
      SELECT k_artist, k_title, MIN(eff) AS earliest_plausible
        FROM norm
       WHERE eff IS NOT NULL AND eff >= {_YEAR_FLOOR} AND is_live = 0
       GROUP BY k_artist, k_title
    )
    SELECT n.url, n.artist, n.album, n.title, n.src,
           n.eff AS eff, g.earliest_plausible AS should
      FROM norm n JOIN groups g
        ON n.k_artist = g.k_artist AND n.k_title = g.k_title
     WHERE n.eff IS NOT NULL
       AND n.eff - g.earliest_plausible >= {_DRIFT_THRESHOLD}
       AND n.is_live = 0
     ORDER BY n.artist COLLATE NOCASE,
              n.album  COLLATE NOCASE,
              n.title  COLLATE NOCASE
    """
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(q)]


def apply_corrections(conn: sqlite3.Connection,
                      candidates: list[dict]) -> int:
    """Write metadata_overrides.year = should, source='manual', for each
    candidate. Merges with any existing override row (preserves other
    fields). Returns the number of rows actually changed."""
    n = 0
    cur = conn.cursor()
    for c in candidates:
        existing = cur.execute(
            "SELECT artist, album, title, genre FROM metadata_overrides "
            "WHERE url=?", (c["url"],)).fetchone()
        if existing:
            cur.execute(
                "UPDATE metadata_overrides "
                "SET year=?, source='manual', updated_at=datetime('now') "
                "WHERE url=?", (c["should"], c["url"]))
        else:
            # Fill from current tracks row so the override carries
            # plausible non-year fields (the indexer / AcoustID may
            # later compete on those, which is fine).
            tr = cur.execute(
                "SELECT artist, album, title, genre FROM tracks WHERE url=?",
                (c["url"],)).fetchone()
            base = tr if tr else ("", "", "", "")
            cur.execute(
                "INSERT INTO metadata_overrides "
                "(url, artist, album, title, genre, year, source, updated_at) "
                "VALUES (?,?,?,?,?,?, 'manual', datetime('now'))",
                (c["url"], base[0], base[1], base[2], base[3], c["should"]))
        n += cur.rowcount or 0
    return n


def _preview(candidates: list[dict], limit: int):
    n = len(candidates)
    if n == 0:
        print("No mis-dated rows found.")
        return
    print(f"Found {n:,} candidate row(s) across "
          f"{len({(c['artist'].lower(), c['title'].lower()) for c in candidates}):,} "
          f"song group(s).")
    show = candidates if limit <= 0 or n <= limit else candidates[:limit]
    print()
    print(f"{'artist':<22} {'album':<36} {'eff':>5}→{'should':<5}  src      title")
    for c in show:
        ar = (c['artist'] or '')[:22]
        al = (c['album'] or '')[:36]
        ti = (c['title'] or '')[:46]
        print(f"  {ar:<22} {al:<36} {c['eff']:>5}→{c['should']:<5}  "
              f"{(c['src'] or '-'):<8} '{ti}'")
    if limit > 0 and n > limit:
        print(f"  … ({n - limit:,} more not shown — pass --top 0 for all)")


def main(argv: Iterable[str] = None) -> int:
    p = argparse.ArgumentParser(
        description="Correct year drift in metadata_overrides by "
                    "comparing same-song instances across albums.")
    p.add_argument("--db",  default=str(_DEFAULT_DB),
                   help="Path to library.db (default: %(default)s)")
    p.add_argument("--top", type=int, default=30,
                   help="Preview first N candidates (0 = all; default 30)")
    p.add_argument("--apply", action="store_true",
                   help="Write the corrections. Default is dry-run.")
    p.add_argument("-y", "--yes", action="store_true",
                   help="Skip the confirmation prompt before applying.")
    args = p.parse_args(argv)

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"ERROR: library.db not found at {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    print(f"DB: {db_path}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"Live markers: {len(LIVE_MARKERS)} terms")
    print(f"Drift threshold: >= {_DRIFT_THRESHOLD} years")
    print()

    candidates = find_candidates(conn)
    _preview(candidates, args.top)

    if not args.apply:
        print()
        print("Dry-run — pass --apply to write corrections.")
        conn.close()
        return 0

    if not candidates:
        conn.close()
        return 0

    if not args.yes:
        print()
        try:
            ans = input(f"Apply year correction to {len(candidates):,} "
                        f"row(s)? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            ans = "n"
        if ans not in ("y", "yes"):
            print("Aborted.")
            conn.close()
            return 1

    try:
        with conn:                       # transactional
            n = apply_corrections(conn, candidates)
        print(f"\nApplied {n:,} row update(s) to metadata_overrides.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
