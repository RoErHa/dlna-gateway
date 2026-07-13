#!/usr/bin/env python3
"""
tools/openlibrary_books.py — enrich audiobook metadata from OpenLibrary.

For every BOOK in the audiobooks LocalFs library (a book = its folder =
`album_key`), asks the OpenLibrary API for the canonical **author**,
**title**, and — the piece the file tags never carry — the **series
name + number in the series** ("Night's Dawn #1"). Results go into the
`book_meta` table, a DISPLAY-layer overlay (never written into `tracks`
or the files): the PWA fetches `/api/book_meta_all` and annotates the
audiobooks browse with it.

How a book is looked up:
  1. Guess (author, title) from the book's own tags (majority artist +
     album across its chapters), falling back to parsing the folder
     name ("57 - The Reality Dysfunction - Peter F Hamilton - 1996").
  2. `GET /search.json?title=…&author=…` (both argument orders are
     tried when the guess is ambiguous). The best doc must pass a fuzzy
     title-similarity floor — a weak match becomes `notfound`, never a
     wrong overlay.
  3. Series: `GET /works/<key>/editions.json` — editions carry a
     `series` field ("Discworld (13)", "Name #3", "Name, Book 3", …).
     Parsed with a majority vote across editions; entries WITH a number
     beat bare names. Number is REAL (novellas are #1.5).

Cache semantics (same contract as album_art / lyrics / song_year_cache):
one row per album_key; `notfound` is a STICKY negative (delete the row
to retry); `manual` rows are never overwritten. Re-runs only hit books
with no row yet, so running after adding new books is cheap and safe.

Rate limit ~1 req/s with an identifying User-Agent (contact email from
.env, same contract as the MusicBrainz fetcher). DRY-RUN by default.

Usage:
    python3 tools/openlibrary_books.py                # dry-run preview
    python3 tools/openlibrary_books.py --apply        # fetch + write
    python3 tools/openlibrary_books.py --limit 10 -v  # small test batch
    python3 tools/openlibrary_books.py --refetch --apply   # redo non-manual
    python3 -m unittest tools.test_openlibrary_books -v
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from difflib import SequenceMatcher
from typing import Optional

_OL = "https://openlibrary.org"
_RATE_SEC = 1.0
_TIMEOUT = 15.0
_FUZZY_FLOOR = 0.60
_EDITIONS_LIMIT = 50

_last_call = 0.0


def _user_agent() -> str:
    email = os.environ.get("GATEWAY_CONTACT_EMAIL", "contact@example.com")
    return f"DLNAGateway/1.0 ( {email} )"


def _norm(s: Optional[str]) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s.lower().strip())


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# ── Folder-name → (author, title) guessing ─────────────────────────

_IDX_RE = re.compile(r"^\d{1,3}\s*[-.]\s*")          # "57 - " list index
_YEAR_RE = re.compile(r"\s*[-,]?\s*\(?\b(19|20)\d{2}\)?\s*$")
_PAREN_RE = re.compile(r"\s*[(\[][^)\]]*[)\]]\s*$")  # trailing (…) / […]


def parse_book_folder(album_key: str) -> tuple[str, str]:
    """Best-effort (author, title) from a book folder name. Handles the
    common layouts: "Author - Title", "NN - Title - Author - YYYY",
    "Title (narrator)". Either side may come back '' — the search then
    runs title-only."""
    leaf = album_key.split("/")[-1]
    leaf = _IDX_RE.sub("", leaf)
    # Strip trailing year + parentheticals (repeat — both may be present).
    prev = None
    while prev != leaf:
        prev = leaf
        leaf = _YEAR_RE.sub("", leaf).strip()
        leaf = _PAREN_RE.sub("", leaf).strip(" -,")
    parts = [p.strip() for p in leaf.split(" - ") if p.strip()]
    if len(parts) >= 2:
        # "Author - Title" is the dominant layout; a trailing part that
        # looks like a person name (2-4 capitalised words, no digits)
        # flips it to "Title - Author".
        last = parts[-1]
        looks_person = (re.fullmatch(r"[A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){1,3}",
                                     last) is not None
                        and not any(ch.isdigit() for ch in last))
        first_wordy = len(parts[0].split()) >= 3
        if looks_person and first_wordy:
            return last, " - ".join(parts[:-1])
        return parts[0], " - ".join(parts[1:])
    return "", leaf


# ── Series-string parsing ───────────────────────────────────────────

_SERIES_PATTERNS = (
    # "Name (3)" / "Name (Book 3)" / "Name (vol. 3)"
    re.compile(r"^(?P<name>.+?)\s*\(\s*(?:book|volume|vol\.?|part|no\.?|#)?"
               r"\s*(?P<num>\d+(?:\.\d+)?)\s*\)$", re.I),
    # "Name #3"
    re.compile(r"^(?P<name>.+?)\s*#\s*(?P<num>\d+(?:\.\d+)?)$"),
    # "Name ; 3" / "Name, 3" / "Name, Book 3" / "Name -- bk. 3" /
    # "Name Book 3" / "Name vol 3"
    re.compile(r"^(?P<name>.+?)(?:\s*[;,:]\s*|\s*[-–—]+\s*|\s+)"
               r"(?:book|volume|vol\.?|part|no\.?|bk\.?)?\s*"
               r"(?P<num>\d+(?:\.\d+)?)$", re.I),
    # "Book 3 of Name"
    re.compile(r"^(?:book|volume|vol\.?|part)\s*(?P<num>\d+(?:\.\d+)?)"
               r"\s+of\s+(?P<name>.+)$", re.I),
)


def parse_series(s: str) -> tuple[str, Optional[float]]:
    """One OpenLibrary edition `series` string → (name, number|None).
    Unparseable number → bare name; empty → ('', None)."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    if not s:
        return "", None
    for pat in _SERIES_PATTERNS:
        m = pat.match(s)
        if m:
            name = m.group("name").strip(" ,;:-–—")
            # Trailing marker words left of the number ("… -- bk.")
            name = re.sub(r"[\s,;:–—-]*(?:book|volume|vol\.?|part|no\.?|bk\.?)$",
                          "", name, flags=re.I).strip(" ,;:-–—")
            try:
                num = float(m.group("num"))
            except (TypeError, ValueError):
                num = None
            if name:
                return name, num
    return s.strip(" ,;:-"), None


# A "series number" above this is a publisher CATALOG number ("Frye
# annotated #1249"), not a position in a story series — the whole entry
# is publisher bookkeeping and gets dropped.
_MAX_SERIES_SEQ = 100

# Publisher/imprint series are edition bookkeeping, not story series —
# normalised substring blocklist (first live sweep: "Penguin
# twentieth-century classics" ×10, "SF Masterworks" ×7, "Oxford
# Bookworms"). Curated, deliberately short.
_PUBLISHER_SERIES = (
    "penguin", "masterworks", "oxford bookworms", "everyman",
    "vintage classics", "twentieth-century classics", "modern library",
    "wordsworth classics", "book club", "great books",
)


def _is_publisher_series(name: str) -> bool:
    n = _norm(name)
    return any(p in n for p in _PUBLISHER_SERIES)


def _ascii_ratio(s: str) -> float:
    return sum(1 for c in s if ord(c) < 128) / max(1, len(s))


def extract_series(editions: list) -> tuple[str, Optional[float]]:
    """Majority-vote a series (name, number) out of a work's editions.
    Editions carry `series` as a list of strings; numbered entries beat
    bare names; the most common normalised name wins; its most common
    number wins. Returns ('', None) when no edition names a series."""
    votes: Counter = Counter()
    numbers: dict[str, Counter] = {}
    display: dict[str, str] = {}
    for ed in editions:
        for raw in (ed.get("series") or []):
            # An edition can cram several series into ONE string
            # ("Mistborn, Era 2… (#1), The Mistborn Saga (#4), The
            # Cosmere #16") — split on '), ' into separate candidates.
            frags = re.split(r"\)\s*,\s*", str(raw))
            if len(frags) > 1:
                frags = [f if f.endswith(")") or "(" not in f else f + ")"
                         for f in frags]
            for frag in frags:
                name, num = parse_series(frag)
                if not name or len(name) < 3:
                    continue   # "v." and friends
                if num is not None and num > _MAX_SERIES_SEQ:
                    continue   # catalog number, not a series position
                if _is_publisher_series(name):
                    continue   # imprint bookkeeping, not a story series
                key = _norm(name)
                votes[key] += 1
                display.setdefault(key, name)
                if num is not None:
                    numbers.setdefault(key, Counter())[num] += 1
    if not votes:
        return "", None
    # Rank: numbered beats bare; more-Latin-script beats a translated
    # edition's series name (Armenian Narnia); then vote count.
    ranked = sorted(
        votes.items(),
        key=lambda kv: (kv[0] in numbers,
                        _ascii_ratio(display[kv[0]]),
                        kv[1]),
        reverse=True)
    key = ranked[0][0]
    num = numbers[key].most_common(1)[0][0] if key in numbers else None
    return display[key], num


# ── OpenLibrary HTTP ────────────────────────────────────────────────

def _http_json(url: str) -> Optional[dict]:
    """Rate-limited GET → parsed JSON, None on any failure (the caller
    treats None as a transient error: NOT cached, retried next run)."""
    global _last_call
    wait = _RATE_SEC - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:                                   # noqa: BLE001
        print(f"    ! http error: {e}", file=sys.stderr)
        return None


def ol_search(title: str, author: str) -> Optional[list]:
    """None = transport failure (transient — do NOT cache as notfound);
    [] = OpenLibrary answered and genuinely has no match."""
    q = {"title": title, "limit": "10",
         "fields": "key,title,author_name,first_publish_year"}
    if author:
        q["author"] = author
    url = f"{_OL}/search.json?{urllib.parse.urlencode(q)}"
    data = _http_json(url)
    if data is None:
        return None
    return data.get("docs") or []


def ol_editions(work_key: str) -> list:
    url = f"{_OL}{work_key}/editions.json?limit={_EDITIONS_LIMIT}"
    data = _http_json(url)
    return (data or {}).get("entries") or []


def pick_best_doc(docs: list, title: str, author: str,
                  floor: float = _FUZZY_FLOOR) -> Optional[dict]:
    """Best search doc passing the fuzzy floor on title; when we have an
    author guess, some author token must overlap too (guards 'The
    Reality Dysfunction' the novel vs an unrelated same-title work)."""
    best, best_score = None, 0.0
    author_tokens = set(_norm(author).split()) if author else set()
    for d in docs:
        score = _similar(d.get("title") or "", title)
        if score < floor or score <= best_score:
            continue
        if author_tokens:
            doc_tokens = set()
            for a in (d.get("author_name") or []):
                doc_tokens |= set(_norm(a).split())
            if not (author_tokens & doc_tokens):
                continue
        best, best_score = d, score
    return best


# ── Library access (raw sqlite3 — deliberately NOT dlna_library:
#    importing it would run migrations on the live DB at import) ─────

def audiobook_books(conn: sqlite3.Connection, udn: str) -> list:
    """[(album_key, majority_artist, majority_album)] for every
    non-root-level book. Root-level single files (album_key='') are
    skipped — their identity is a lone file, better fixed by giving
    them a folder."""
    rows = conn.execute(
        "SELECT album_key, artist, album FROM tracks "
        "WHERE udn=? AND album_key != ''", (udn,)).fetchall()
    by_book: dict[str, tuple[Counter, Counter]] = {}
    for album_key, artist, album in rows:
        a, b = by_book.setdefault(album_key, (Counter(), Counter()))
        if artist:
            a[artist] += 1
        if album:
            b[album] += 1
    out = []
    for key, (artists, albums) in sorted(by_book.items()):
        out.append((key,
                    artists.most_common(1)[0][0] if artists else "",
                    albums.most_common(1)[0][0] if albums else ""))
    return out


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Same DDL as LibraryDB._init_schema — the tool must work against
    a DB the gateway hasn't migrated yet (it runs standalone and
    deliberately does NOT import dlna_library, whose import would run
    ALL migrations against the live DB)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS book_meta (
            album_key  TEXT PRIMARY KEY,
            author     TEXT,
            title      TEXT,
            series     TEXT,
            series_seq REAL,
            source     TEXT NOT NULL,
            fetched_at INTEGER NOT NULL
        )""")
    conn.commit()


def existing_meta(conn: sqlite3.Connection) -> dict:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT album_key, source FROM book_meta")}


def write_meta(conn: sqlite3.Connection, album_key: str, author, title,
               series, series_seq, source: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO book_meta "
        "(album_key, author, title, series, series_seq, source, fetched_at) "
        "VALUES (?,?,?,?,?,?, strftime('%s','now'))",
        (album_key, author, title, series, series_seq, source))


def lookup_book(album_key: str, tag_artist: str, tag_album: str,
                verbose: bool = False) -> Optional[dict]:
    """Full OL lookup for one book. Returns
    {author, title, series, series_seq} on a confident match,
    {} for a confident MISS (cache as notfound), None on transient
    error (do not cache)."""
    f_author, f_title = parse_book_folder(album_key)
    # Tag guesses first (usually cleaner), folder guesses as fallback.
    guesses = []
    if tag_album:
        guesses.append((tag_album, tag_artist or f_author))
    if f_title and _norm(f_title) != _norm(tag_album):
        guesses.append((f_title, f_author or tag_artist))
    if not guesses:
        return {}

    saw_transient = False
    for title, author in guesses:
        docs = ol_search(title, author)
        if docs is None:
            saw_transient = True
            continue
        doc = pick_best_doc(docs, title, author)
        if doc is None and author and len(title.split()) >= 3:
            # Author guess may be a narrator — retry title-only, but only
            # for multi-word titles and at a stricter floor: short titles
            # false-match too easily ("Alpha" → "Alphas" by Lisi Harrison).
            docs2 = ol_search(title, "")
            if docs2 is None:
                saw_transient = True
            else:
                doc = pick_best_doc(docs2, title, "", floor=0.75)
        if doc is None:
            continue
        ol_title = doc.get("title") or title
        ol_author = ", ".join(doc.get("author_name") or []) or author
        series, seq = "", None
        work_key = doc.get("key") or ""
        if work_key.startswith("/works/"):
            editions = ol_editions(work_key)
            series, seq = extract_series(editions)
        if verbose:
            tag = f"  📚 {series}" + (f" #{seq:g}" if seq is not None else "")
            print(f"    → OL: {ol_author!r} / {ol_title!r}"
                  f"{tag if series else ''}")
        return {"author": ol_author, "title": ol_title,
                "series": series or None, "series_seq": seq}
    return None if saw_transient else {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="library.db")
    ap.add_argument("--udn", default="",
                    help="audiobooks UDN (default: derived from "
                         "$AUDIOBOOKS_ROOT)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N lookups (0 = all)")
    ap.add_argument("--refetch", action="store_true",
                    help="re-query books that already have a non-manual row")
    ap.add_argument("--apply", action="store_true",
                    help="write results (default: dry-run)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(args.db):
        print(f"error: {args.db} not found", file=sys.stderr)
        return 2

    udn = args.udn
    if not udn:
        root = os.environ.get("AUDIOBOOKS_ROOT", "").strip()
        if not root:
            # .env is loaded by dlna_config in the gateway; tools read the
            # file directly so they work in a bare shell too.
            try:
                for line in open(".env", encoding="utf-8"):
                    if line.strip().startswith("AUDIOBOOKS_ROOT="):
                        root = line.split("=", 1)[1].strip()
            except OSError:
                pass
        if not root:
            print("error: no --udn and $AUDIOBOOKS_ROOT unset", file=sys.stderr)
            return 2
        import hashlib
        from pathlib import Path
        h = hashlib.sha1(str(Path(root).resolve()).encode()).hexdigest()
        udn = f"uuid:localfs-{h[:32]}"

    conn = sqlite3.connect(args.db)
    ensure_schema(conn)
    books = audiobook_books(conn, udn)
    cached = existing_meta(conn)
    stats = Counter()
    todo = []
    for key, artist, album in books:
        src = cached.get(key)
        if src == "manual":
            stats["skipped_manual"] += 1
        elif src and not args.refetch:
            stats["skipped_cached"] += 1
        else:
            todo.append((key, artist, album))

    print(f"audiobook books under {udn[:24]}…: {len(books)}")
    print(f"  cached (skip): {stats['skipped_cached']}   "
          f"manual (never touched): {stats['skipped_manual']}")
    print(f"  to look up: {len(todo)}"
          f"{f'  (limited to {args.limit})' if args.limit else ''}")
    if not args.apply:
        print("\nDRY-RUN — no lookups, no writes. Re-run with --apply.")
        for key, artist, album in todo[:15]:
            fa, ft = parse_book_folder(key)
            print(f"  {key[:64]}")
            print(f"    guess: {artist or fa!r} / {album or ft!r}")
        if len(todo) > 15:
            print(f"  … and {len(todo) - 15} more")
        return 0

    n = 0
    for key, artist, album in todo:
        if args.limit and n >= args.limit:
            break
        n += 1
        print(f"[{n}/{args.limit or len(todo)}] {key[:70]}")
        meta = lookup_book(key, artist, album, verbose=args.verbose)
        if meta is None:
            stats["transient_error"] += 1
            continue
        if not meta:
            write_meta(conn, key, None, None, None, None, "notfound")
            conn.commit()
            stats["notfound"] += 1
            continue
        write_meta(conn, key, meta["author"], meta["title"],
                   meta["series"], meta["series_seq"], "openlibrary")
        conn.commit()
        stats["found_series" if meta["series"] else "found_no_series"] += 1

    print(f"\ndone: series found={stats['found_series']}  "
          f"matched, no series={stats['found_no_series']}  "
          f"notfound={stats['notfound']}  "
          f"transient (will retry)={stats['transient_error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
