#!/usr/bin/env python3
"""
dlna_library_languages.py — `LanguagesMixin`: which language each
audiobook is in, and the books of one language, for the PWA's 🌐
letter-bar entry.

Two kinds of answer, kept apart on purpose:

  * **Asserted** — a file's language tag, or a person's choice. Stored in
    `book_language` (survives `clear(udn)`, like `book_meta`); `manual`
    always beats `tag`.
  * **Detected** — `dlna_book_language.detect_language` over the book's
    path, album tag and chapter titles. NEVER stored. It is recomputed on
    every read (~0.1 s for 11k chapters), so a retag or a rescan is
    reflected at once and there is no cache to go stale.

A book nothing can place reports `''`, which the PWA shows as Unknown.
That bucket is the to-do list for `tools/book_language.py --set`, not a
failure to hide.

A book is its FOLDER (`album_key`), as everywhere else in LocalFs, so the
album rows returned here are the same shape `browse_letter` returns for
the Albums mode and the PWA renders them with the same code.
"""
from __future__ import annotations

import time
from collections import defaultdict

from dlna_book_language import detect_language, normalize_language
from dlna_library_sql import (
    _ALBUM_EDITION_YEAR,
    _ALBUM_ORIGINAL_YEAR,
    _dedup_clause,
    _localfs_album_artist,
    _localfs_album_group,
    _localfs_album_name,
)

SOURCES = ("tag", "manual")


class LanguagesMixin:
    """See module docstring. Mixed into `LibraryDB`."""

    # ── asserted languages ───────────────────────────────────────

    def book_language_set(self, album_key: str, language: str,
                          source: str = "manual") -> bool:
        """Record an asserted language. Refuses a blank key, an unknown
        source or a value that names no language, and a `tag` never
        replaces a `manual` row — a person's choice outranks a tagger's."""
        code = normalize_language(language)
        if not album_key or not code or source not in SOURCES:
            return False
        with self._pool.write() as conn:
            row = conn.execute(
                "SELECT source FROM book_language WHERE album_key=?",
                (album_key,)).fetchone()
            if row and row["source"] == "manual" and source != "manual":
                return False
            conn.execute(
                "INSERT OR REPLACE INTO book_language "
                "(album_key, language, source, updated_at) VALUES (?,?,?,?)",
                (album_key, code, source, int(time.time())))
        return True

    def book_language_clear(self, album_key: str) -> bool:
        with self._pool.write() as conn:
            cur = conn.execute(
                "DELETE FROM book_language WHERE album_key=?", (album_key,))
        return cur.rowcount > 0

    # ── resolution ───────────────────────────────────────────────

    def _book_evidence(self, udn: str, album_key: str | None = None):
        """(texts, names, asserted) per album_key — the words detection
        reads, and the stored assertions. One book when `album_key` is
        given, every book under `udn` otherwise."""
        texts: dict = defaultdict(set)
        names: dict = defaultdict(set)
        where, args = "udn=?", [udn]
        if album_key is not None:
            where, args = "udn=? AND album_key=?", [udn, album_key]
        with self._pool.read() as conn:
            for r in conn.execute(
                    "SELECT DISTINCT album_key, title, album, artist "
                    f"FROM tracks WHERE {where}", args):
                key = r["album_key"]
                texts[key].update((r["title"], r["album"], *key.split("/")))
                names[key].add(r["artist"])
            for key, author in conn.execute(
                    "SELECT album_key, author FROM book_meta "
                    "WHERE source != 'notfound'"):
                if key in names and author:
                    names[key].add(author)
            asserted = {r["album_key"]: (r["language"], r["source"])
                        for r in conn.execute(
                            "SELECT album_key, language, source "
                            "FROM book_language")}
        return texts, names, asserted

    def book_languages(self, udn: str) -> dict:
        """album_key → {language, source} for every book under `udn`.
        source ∈ {'manual', 'tag', 'detected'}; a detected `''` is
        Unknown."""
        texts, names, asserted = self._book_evidence(udn)
        out = {}
        for key in texts:
            if key in asserted:
                lang, src = asserted[key]
            else:
                lang, src = detect_language(texts[key], names[key]), "detected"
            out[key] = {"language": lang, "source": src}
        return out

    def book_language_of(self, url: str, books_udn: str) -> dict | None:
        """The language state of the book a chapter `url` belongs to, for
        the edit form: {album_key, language, source, detected}. `detected`
        is what the words say regardless of any stored row, so the form
        can offer "Auto" honestly. None when the track is not a book
        under `books_udn`, or is a root-level file (no folder to key)."""
        track = self.track_by_url(url)
        if (not track or not books_udn or track["udn"] != books_udn
                or not track["album_key"]):
            return None
        key = track["album_key"]
        texts, names, asserted = self._book_evidence(books_udn, key)
        detected = detect_language(texts[key], names[key])
        lang, src = asserted.get(key, (detected, "detected"))
        return {"album_key": key, "language": lang, "source": src,
                "detected": detected}

    def _book_album_rows(self, udn: str) -> list[dict]:
        """Every folder-album under `udn`, in `browse_letter`'s Albums
        shape, sorted by display name."""
        t = "t"
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.album_key,
                           {_localfs_album_name(t)} as album,
                           {_localfs_album_artist(t)} as artist,
                           COUNT(*) as track_count, MAX(t.art) as art,
                           {_ALBUM_EDITION_YEAR} as year,
                           {_ALBUM_ORIGINAL_YEAR} as year_original
                    FROM tracks t
                    LEFT JOIN (SELECT url, year FROM metadata_overrides) m
                           ON m.url = t.url
                    WHERE t.udn=? AND t.album_key!='' AND {_dedup_clause(t)}
                    GROUP BY {_localfs_album_group(t)}
                    ORDER BY album COLLATE NOCASE""", (udn,)).fetchall()
        return [dict(r) for r in rows]

    def book_language_facets(self, udn: str) -> list[dict]:
        """[{language, books}] — most books first, Unknown ('') last."""
        langs = self.book_languages(udn)
        counts: dict = defaultdict(int)
        for row in self._book_album_rows(udn):
            counts[langs.get(row["album_key"], {}).get("language", "")] += 1
        return sorted(({"language": k, "books": v} for k, v in counts.items()),
                      key=lambda f: (f["language"] == "", -f["books"],
                                     f["language"]))

    def book_language_albums(self, udn: str, language: str) -> list[dict]:
        """The books of one language (`''` = Unknown), each carrying its
        `language_source` so a caller can tell a guess from an assertion."""
        langs = self.book_languages(udn)
        out = []
        for row in self._book_album_rows(udn):
            info = langs.get(row["album_key"],
                             {"language": "", "source": "detected"})
            if info["language"] == language:
                out.append({**row, "language": info["language"],
                            "language_source": info["source"]})
        return out
