#!/usr/bin/env python3
"""
dlna_library_collections.py — `CollectionsMixin`: the user-owned
state that is deliberately DECOUPLED from `tracks` — playlists (in
`PlaylistsMixin`, dlna_library_playlists.py), album favourites, radio
favourites, lyrics, audiobook playback positions, OpenLibrary book
metadata, and the device-role cache.

Split out of dlna_library.py (2026-08-20). See dlna_library_schema.py
for why these are mixins rather than collaborators.

THE INVARIANT THIS FILE EXISTS TO PROTECT: none of these tables is
touched by `clear(udn)`. A rebuild-index wipes and repopulates `tracks`
only — playlists, favourites, lyrics, play counts and audiobook
bookmarks all survive it. Anything added here must keep that property
(and, like the rest, key off a stable URL / album_key rather than a
`tracks.id`).
"""
from __future__ import annotations

import logging
import time

from dlna_book_key import SEP, book_key
from dlna_library_playlists import PlaylistsMixin
from dlna_library_worklist import WorklistMixin

log = logging.getLogger("dlna.library")


class CollectionsMixin(PlaylistsMixin, WorklistMixin):
    """See module docstring. Mixed into `LibraryDB`; never instantiated
    on its own — it relies on `self._pool` from the host class."""

    # ── Device role memory ────────────────────────────────────────

    def role_set(self, udn: str, name: str, location: str = "",
                 host: str = "",
                 is_server: bool = False, is_renderer: bool = False):
        """Persist that this UDN is a server and/or renderer, with its location URL and host."""
        with self._pool.write() as conn:
            conn.execute("""
                INSERT INTO device_roles (udn, name, location, host, is_server, is_renderer)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(udn) DO UPDATE SET
                    name        = excluded.name,
                    location    = COALESCE(NULLIF(excluded.location,''), location),
                    host        = COALESCE(NULLIF(excluded.host,''), host),
                    is_server   = MAX(is_server,   excluded.is_server),
                    is_renderer = MAX(is_renderer, excluded.is_renderer),
                    last_seen   = datetime('now')
            """, (udn, name, location, host, int(is_server), int(is_renderer)))
    def roles_load(self) -> dict:
        """
        Load all known device roles into a dict keyed by UDN.
        Also builds a host→roles index for cross-UDN matching.
        Returns: {udn: {"name", "location", "host", "is_server", "is_renderer"}}
        """
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT udn, name, location, host, is_server, is_renderer "
                "FROM device_roles"
            ).fetchall()
        return {
            r["udn"]: {
                "name":        r["name"],
                "location":    r["location"] or "",
                "host":        r["host"] or "",
                "is_server":   bool(r["is_server"]),
                "is_renderer": bool(r["is_renderer"]),
            }
            for r in rows
        }
    def roles_all(self) -> list:
        """Return all device role rows as a list of dicts (for --list-devices)."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT udn, name, location, host, is_server, is_renderer, "
                "first_seen, last_seen "
                "FROM device_roles ORDER BY last_seen DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def roles_clear(self) -> int:
        """Wipe the device_roles table (for `--reset-devices`). Returns the
        row count removed.

        Lives here, beside the other `roles_*` SQL, rather than in the CLI:
        until 2026-08-26 `dlna_gateway.main()` opened its own connection via
        `DB._connect()` — a method `db_pool.Pool` retired when it took over
        connection management — so the documented `--reset-devices` command
        had been raising AttributeError ever since, unnoticed because
        nothing exercised it."""
        with self._pool.write() as conn:
            n = conn.execute("SELECT COUNT(*) FROM device_roles").fetchone()[0]
            conn.execute("DELETE FROM device_roles")
        return int(n)

    def roles_delete(self, udn: str) -> int:
        """Remove ONE device's row (`POST /api/renderers/forget`)."""
        with self._pool.write() as conn:
            return conn.execute("DELETE FROM device_roles WHERE udn=?",
                                (udn,)).rowcount

    def get_lyrics(self, url: str):
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT plain, synced, source, fetched_at "
                "FROM lyrics WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None
    def set_lyrics(self, url: str, plain, synced, source: str):
        import time as _t
        with self._pool.write() as conn:
            conn.execute(
                "INSERT INTO lyrics(url, plain, synced, source, fetched_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(url) DO UPDATE SET "
                "plain=excluded.plain, synced=excluded.synced, "
                "source=excluded.source, fetched_at=excluded.fetched_at",
                (url, plain, synced, source, int(_t.time())))
    # ── Album favourites ──────────────────────────────────────────
    # Distinct from the track-level Favourites playlist: these are
    # whole-album entries keyed by (artist, album) so they survive
    # clear(udn) and re-indexing. Same persistence pattern as
    # album_art, play_counts, lyrics.

    def album_fav_add(self, artist: str, album: str,
                       album_key: str = "") -> bool:
        """Mark an album as favourite. Idempotent — re-adding is a no-op
        and doesn't bump added_at. Returns True if a new row was created.
        `album_key` (LocalFs folder identity) makes a compilation
        favouritable as one album; empty for (artist, album)-keyed
        sources."""
        if not album:
            return False
        with self._pool.write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO album_favourites "
                "(artist, album, album_key, added_at) VALUES (?,?,?,?)",
                (artist, album, album_key, int(time.time())))
        return cur.rowcount > 0
    def album_fav_remove(self, artist: str, album: str,
                         album_key: str = "") -> bool:
        with self._pool.write() as conn:
            if album_key:
                cur = conn.execute(
                    "DELETE FROM album_favourites WHERE album_key=?",
                    (album_key,))
            else:
                cur = conn.execute(
                    "DELETE FROM album_favourites "
                    "WHERE artist=? AND album=? AND album_key=''",
                    (artist, album))
        return cur.rowcount > 0
    def album_fav_is(self, artist: str, album: str,
                     album_key: str = "") -> bool:
        with self._pool.read() as conn:
            if album_key:
                row = conn.execute(
                    "SELECT 1 FROM album_favourites WHERE album_key=? LIMIT 1",
                    (album_key,)).fetchone()
            else:
                row = conn.execute(
                    "SELECT 1 FROM album_favourites "
                    "WHERE artist=? AND album=? AND album_key='' LIMIT 1",
                    (artist, album)).fetchone()
        return row is not None
    def album_fav_list(self) -> list:
        """Return all favourited albums with art, track_count, and the
        UDN of a server that holds them. Albums with zero matching tracks
        across any server (e.g. server gone away) still appear so the user
        can prune them. Sorted most-recently-added first. A LocalFs
        favourite (album_key set) matches its tracks by FOLDER; others by
        (artist, album)."""
        with self._pool.read() as conn:
            rows = conn.execute("""
                SELECT
                    f.artist,
                    f.album,
                    f.album_key,
                    f.added_at,
                    COALESCE(aa.art_url, MAX(t.art), '') AS art,
                    COUNT(t.id)                          AS track_count,
                    COALESCE(MAX(t.udn), '')             AS udn
                FROM album_favourites f
                LEFT JOIN tracks t
                       ON (f.album_key != '' AND t.album_key = f.album_key)
                       OR (f.album_key  = '' AND t.artist = f.artist
                           AND t.album = f.album)
                LEFT JOIN album_art aa
                       ON aa.artist = f.artist AND aa.album = f.album
                GROUP BY f.artist, f.album, f.album_key
                ORDER BY f.added_at DESC
            """).fetchall()
        return [dict(r) for r in rows]

    # ── Audiobook resume positions ────────────────────────────────
    # One row per book (album_key). Survives clear(udn) — same contract
    # as play_counts / lyrics. The FINISHED decision is the client's
    # (it knows chapter index + duration); the server stores it, and any
    # save with finished=False clears the flag (re-listening).

    def position_set(self, album_key: str, url: str, position_sec,
                     duration_sec=None, finished: bool = False) -> bool:
        """Upsert the resume position for a book. Returns False on
        missing keys or a non-numeric position (never raises — the PWA
        fires these every ~20s and a bad payload must not 500)."""
        if not album_key or not url:
            return False
        try:
            pos = max(0.0, float(position_sec))
        except (TypeError, ValueError):
            return False
        try:
            dur = max(0.0, float(duration_sec)) if duration_sec is not None \
                else None
        except (TypeError, ValueError):
            dur = None
        with self._pool.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO playback_positions "
                "(album_key, url, position_sec, duration_sec, finished, "
                " updated_at) VALUES (?,?,?,?,?, strftime('%s','now'))",
                (album_key, url, pos, dur, 1 if finished else 0))
        return True
    def position_get(self, album_key: str) -> dict | None:
        """The bookmark under this key. Asked for a FOLDER that holds
        several books (dlna_book_key), it answers with the newest bookmark
        of any book in it — that is what the folder's ▶ Continue resumes."""
        if not album_key:
            return None
        prefix = album_key + SEP
        with self._pool.read() as conn:
            r = conn.execute(
                "SELECT album_key, url, position_sec, duration_sec, "
                "       finished, updated_at "
                "FROM playback_positions WHERE album_key=? "
                "   OR substr(album_key, 1, ?)=? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (album_key, len(prefix), prefix)).fetchone()
        return dict(r) if r else None
    def book_key_of(self, track: dict) -> str:
        """The resume key for one tracks row (dlna_book_key.book_key) —
        for callers that hold a single track, not its folder's list."""
        ak = track.get("album_key") or ""
        albums = self.folder_albums(track.get("udn", ""), ak) if ak else []
        return book_key(ak, track.get("album", ""), track.get("url", ""),
                        albums)
    def folder_albums(self, udn: str, album_key: str) -> list:
        with self._pool.read() as conn:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT album FROM tracks WHERE udn=? AND album_key=?",
                (udn, album_key))]
    def position_clear(self, album_key: str) -> bool:
        with self._pool.write() as conn:
            cur = conn.execute(
                "DELETE FROM playback_positions WHERE album_key=?",
                (album_key,))
        return cur.rowcount > 0
    # ── Audiobook metadata overlay (OpenLibrary) ─────────────────
    # Display-layer only — canonical author/title + series name/number
    # per book. 'manual' rows are never overwritten by tool runs; a
    # 'notfound' row is the sticky negative (delete it to retry).

    def book_meta_set(self, album_key: str, *, author=None, title=None,
                      series=None, series_seq=None,
                      source: str = "openlibrary") -> bool:
        if not album_key or not source:
            return False
        with self._pool.write() as conn:
            existing = conn.execute(
                "SELECT source FROM book_meta WHERE album_key=?",
                (album_key,)).fetchone()
            if existing and existing["source"] == "manual" \
                    and source != "manual":
                return False   # user edits always win
            conn.execute(
                "INSERT OR REPLACE INTO book_meta "
                "(album_key, author, title, series, series_seq, source, "
                " fetched_at) VALUES (?,?,?,?,?,?, strftime('%s','now'))",
                (album_key, author, title, series, series_seq, source))
        return True
    def book_meta_get(self, album_key: str) -> dict | None:
        if not album_key:
            return None
        with self._pool.read() as conn:
            r = conn.execute(
                "SELECT album_key, author, title, series, series_seq, "
                "       source FROM book_meta WHERE album_key=?",
                (album_key,)).fetchone()
        return dict(r) if r else None
    def book_meta_all(self) -> dict:
        """album_key → meta for every POSITIVE row (notfound rows are
        cache bookkeeping, not display data). Small — one row per book —
        so the PWA fetches the whole map on source switch."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT album_key, author, title, series, series_seq, "
                "       source FROM book_meta WHERE source != 'notfound'"
            ).fetchall()
        return {r["album_key"]: dict(r) for r in rows}
    def positions_list(self, limit: int = 50) -> list:
        """In-progress books, newest first — powers a future
        continue-listening shelf. Includes finished rows (the caller
        filters); joins nothing so orphaned rows still appear."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT album_key, url, position_sec, duration_sec, "
                "       finished, updated_at "
                "FROM playback_positions "
                "ORDER BY updated_at DESC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
        return [dict(r) for r in rows]
