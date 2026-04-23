#!/usr/bin/env python3
"""
dlna_library.py — SQLite music library index, FTS5 search, playlists.

Class LibraryDB handles all DB operations.
Class Indexer crawls a MediaServer and populates the DB.

Standalone test:
    python dlna_library.py
"""
import http.client
import json
import logging
import os
import random
import sqlite3
import threading
import time
import urllib.parse
import uuid
from typing import Optional

from dlna_config import DB_FILE
from db_pool import Pool

log = logging.getLogger("dlna.library")

FAVOURITES_ID = "__favourites__"

# ── LibraryDB ─────────────────────────────────────────────────────

class LibraryDB:
    """
    Thread-safe SQLite wrapper for:
      - Track index (tracks + FTS5)
      - Playlists (playlists + playlist_tracks)

    Uses db_pool.Pool for connection management:
      - Reads are concurrent (WAL mode)
      - Writes are serialized (write lock)
    """

    def __init__(self, db_file: str = DB_FILE):
        self._pool = Pool(db_file)
        self._init_schema()

    # ── Schema ────────────────────────────────────────────────────

    def _init_schema(self):
        self._pool.execute_script("""
                CREATE TABLE IF NOT EXISTS tracks (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    udn      TEXT NOT NULL,
                    obj_id   TEXT,
                    url      TEXT NOT NULL,
                    title    TEXT,
                    artist   TEXT,
                    album    TEXT,
                    duration TEXT,
                    art      TEXT,
                    mime     TEXT,
                    genre    TEXT DEFAULT '',
                    file_path TEXT DEFAULT '',
                    UNIQUE(udn, artist, album, title)
                );
                -- Genre migration: add column if upgrading from older schema
                
                CREATE TABLE IF NOT EXISTS metadata_overrides (
                    url       TEXT PRIMARY KEY,
                    artist    TEXT,
                    album     TEXT,
                    title     TEXT,
                    genre     TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                );
                -- Per-album cover art cache. Survives re-index so an album
                -- whose art was found once stays adorned forever (until the
                -- row is manually cleared). Populated by sibling-track
                -- harvest (Phase A) and, later, external lookups (Phase B).
                -- source: 'sibling' = derived from a track in the same album,
                --         'musicbrainz' / 'manual' = filled in later phases.
                CREATE TABLE IF NOT EXISTS album_art (
                    artist     TEXT NOT NULL,
                    album      TEXT NOT NULL,
                    art_url    TEXT NOT NULL,
                    source     TEXT DEFAULT 'sibling',
                    updated_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (artist, album)
                );
                CREATE TABLE IF NOT EXISTS index_meta (
                    udn        TEXT PRIMARY KEY,
                    indexed_at TEXT
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS tracks_fts USING fts5(
                    title, artist, album,
                    content=tracks, content_rowid=id,
                    tokenize='unicode61 remove_diacritics 1'
                );
                -- Migration: add genre column to existing DBs
                -- (no-op if column already exists)
                CREATE TRIGGER IF NOT EXISTS tracks_ai
                    AFTER INSERT ON tracks BEGIN
                        INSERT INTO tracks_fts(rowid, title, artist, album)
                        VALUES (new.id, new.title, new.artist, new.album);
                    END;
                CREATE TRIGGER IF NOT EXISTS tracks_ad
                    AFTER DELETE ON tracks BEGIN
                        INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                        VALUES ('delete', old.id, old.title, old.artist, old.album);
                    END;

                CREATE TABLE IF NOT EXISTS playlists (
                    id         TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    sort_order INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS playlist_tracks (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    pl_id      TEXT NOT NULL
                               REFERENCES playlists(id) ON DELETE CASCADE,
                    url        TEXT NOT NULL,
                    title      TEXT,
                    artist     TEXT,
                    album      TEXT,
                    duration   TEXT,
                    art        TEXT,
                    added_at   TEXT DEFAULT (datetime('now')),
                    UNIQUE(pl_id, url)
                );

                -- Persistent device role memory: survives restarts.
                -- is_server / is_renderer are booleans (0/1).
                -- Once a UDN is marked as a renderer it is never indexed.
                -- location stores the DeviceDescription URL for direct re-probe.
                -- host stores the IP so combined devices with multiple UDNs
                --   (like Naim Uniti) are matched by host, not just UDN.
                CREATE TABLE IF NOT EXISTS device_roles (
                    udn         TEXT PRIMARY KEY,
                    name        TEXT,
                    location    TEXT,
                    host        TEXT,
                    is_server   INTEGER NOT NULL DEFAULT 0,
                    is_renderer INTEGER NOT NULL DEFAULT 0,
                    first_seen  TEXT DEFAULT (datetime('now')),
                    last_seen   TEXT DEFAULT (datetime('now'))
                );
            """)
        # Migrations: add new columns to existing DBs (safe no-ops if present)
        for col_sql in [
            "ALTER TABLE tracks ADD COLUMN genre TEXT DEFAULT ''",
            "ALTER TABLE tracks ADD COLUMN file_path TEXT DEFAULT ''",
        ]:
            try:
                with self._pool.write() as conn:
                    conn.execute(col_sql)
                log.info(f"DB migration: {col_sql[:60]}")
            except Exception:
                pass  # column already exists
        # Ensure Favourites always exists
        with self._pool.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO playlists (id, name, sort_order) "
                "VALUES (?,?,?)",
                (FAVOURITES_ID, "⭐ Favourites", -1))
            self._migrate_json(conn)
            self._migrate_device_roles(conn)
            # One-shot album-art backfill across all existing tracks. Cheap
            # and idempotent — harvest is INSERT OR IGNORE, backfill only
            # touches tracks whose art is empty AND whose album_art exists.
            harvested, filled = self._backfill_album_art(conn)
            if harvested or filled:
                log.info(f"album_art backfill: harvested={harvested} "
                         f"album(s), filled={filled} track(s)")
        log.debug(f"LibraryDB ready: {self._pool.db_file}")

    def _backfill_album_art(self, conn: sqlite3.Connection,
                            udn: str = "") -> tuple:
        """
        Harvest per-album cover art from tracks that have it into the
        album_art cache, then apply the cache onto sibling tracks of the
        same (artist, album) that don't.

        Idempotent. Safe to re-run on every index pass — INSERT OR IGNORE
        keeps the first art URL seen per album, and the UPDATE only fires
        on tracks whose art is empty.

        Pass udn to scope to a single server's tracks; pass "" to operate
        across all UDNs (used at startup migration).

        Returns (harvested_albums, filled_tracks) for logging.
        """
        udn_clause, params = ("", ())
        if udn:
            udn_clause = " AND udn = ?"
            params     = (udn,)

        before_rowid = conn.execute(
            "SELECT COALESCE(MAX(ROWID), 0) FROM album_art").fetchone()[0]
        conn.execute(f"""
            INSERT OR IGNORE INTO album_art (artist, album, art_url, source)
            SELECT artist, album, MIN(art), 'sibling'
              FROM tracks
             WHERE artist != '' AND album != '' AND art != ''{udn_clause}
             GROUP BY artist, album
        """, params)

        new_rows = conn.execute(
            "SELECT artist, album, source FROM album_art WHERE ROWID > ?",
            (before_rowid,)).fetchall()
        harvested = len(new_rows)
        scope = f"[{udn[:12]}…]" if udn else "[migration]"
        for r in new_rows:
            log.info(f"album-art ← harvest {scope} "
                     f"{r['artist']!r} / {r['album']!r} "
                     f"[source={r['source']}]")

        cur = conn.execute(f"""
            UPDATE tracks
               SET art = (
                   SELECT art_url FROM album_art
                    WHERE album_art.artist = tracks.artist
                      AND album_art.album  = tracks.album)
             WHERE (art IS NULL OR art = '')
               AND EXISTS (
                   SELECT 1 FROM album_art
                    WHERE album_art.artist = tracks.artist
                      AND album_art.album  = tracks.album){udn_clause}
        """, params)
        return harvested, cur.rowcount or 0

    def _migrate_json(self, conn: sqlite3.Connection):
        """One-time import from old playlists.json if present."""
        import json
        old = os.path.join(os.path.dirname(self._pool.db_file), "playlists.json")
        if not os.path.exists(old):
            return
        try:
            with open(old, encoding="utf-8") as f:
                data = json.load(f)
            for pl_id, pl in data.items():
                conn.execute(
                    "INSERT OR IGNORE INTO playlists (id, name) VALUES (?,?)",
                    (pl_id, pl.get("name", pl_id)))
                for t in pl.get("tracks", []):
                    conn.execute(
                        "INSERT OR IGNORE INTO playlist_tracks "
                        "(pl_id, url, title, artist, album, duration, art) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (pl_id, t.get("url",""), t.get("title",""),
                         t.get("artist",""), t.get("album",""),
                         t.get("duration",""), t.get("art","")))
            conn.commit()
            os.rename(old, old + ".migrated")
            log.info("Migrated playlists.json → SQLite")
        except Exception as e:
            log.warning(f"JSON playlist migration: {e}")

    def _migrate_device_roles(self, conn: sqlite3.Connection):
        """Add columns to device_roles if upgrading from older schema."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(device_roles)")}
        if "location" not in cols:
            conn.execute("ALTER TABLE device_roles ADD COLUMN location TEXT")
            conn.commit()
            log.info("DB migration: added location column to device_roles")
        if "host" not in cols:
            conn.execute("ALTER TABLE device_roles ADD COLUMN host TEXT")
            conn.commit()
            log.info("DB migration: added host column to device_roles")

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

    # ── Track index ───────────────────────────────────────────────

    def track_count(self, udn: str) -> int:
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM tracks WHERE udn=?", (udn,)).fetchone()
            return row[0] if row else 0

    def album_count(self, udn: str) -> int:
        """Distinct (artist, album) pairs — matches AssetUPnP's display count."""
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM "
                "(SELECT DISTINCT artist, album FROM tracks WHERE udn=?)",
                (udn,)).fetchone()
            return row[0] if row else 0

    def upsert_tracks(self, udn: str, tracks: list) -> int:
        """
        Insert tracks, deduplicating on (udn, artist, album, title).
        AssetUPnP serves the same file via multiple container paths;
        we keep the first URL seen and ignore subsequent duplicates.
        Returns number of rows actually inserted.
        """
        if not tracks:
            return 0
        rows = [dict(
            udn=udn,
            obj_id=t.get("id", ""),
            url=t.get("url", ""),
            title=t.get("title", ""),
            artist=t.get("artist", ""),
            album=t.get("album", ""),
            duration=t.get("duration", ""),
            art=t.get("art", ""),
            mime=t.get("mime", ""),
            genre=t.get("genre", ""),
            file_path=t.get("file_path", ""),
        ) for t in tracks if t.get("url")]

        with self._pool.write() as conn:
            before = conn.execute("SELECT changes()").fetchone()[0]
            # Step 1: insert new tracks (skip duplicates)
            conn.executemany(
                "INSERT OR IGNORE INTO tracks "
                "(udn, obj_id, url, title, artist, album, duration, art, mime, genre, file_path) "
                "VALUES (:udn,:obj_id,:url,:title,:artist,:album,:duration,:art,:mime,:genre,:file_path)",
                rows)
            inserted = conn.execute("SELECT changes()").fetchone()[0]
            # Step 2: update genre + art on already-indexed tracks
            # (safe UPDATE preserves FTS5 triggers, picks up new metadata on re-index)
            conn.executemany(
                "UPDATE tracks SET genre=:genre, art=:art "
                "WHERE udn=:udn AND artist=:artist AND album=:album AND title=:title "
                "  AND (genre='' OR genre IS NULL)",
                rows)
            # Apply any saved metadata overrides (survive re-index)
            conn.execute("""
                UPDATE tracks SET
                    artist    = COALESCE((SELECT artist FROM metadata_overrides WHERE url=tracks.url), artist),
                    album     = COALESCE((SELECT album  FROM metadata_overrides WHERE url=tracks.url), album),
                    title     = COALESCE((SELECT title  FROM metadata_overrides WHERE url=tracks.url), title),
                    genre     = COALESCE((SELECT genre  FROM metadata_overrides WHERE url=tracks.url), genre)
                WHERE udn=?
                  AND url IN (SELECT url FROM metadata_overrides)
            """, (udn,))

            # Harvest new album art from this index pass and backfill any
            # sibling tracks that ended up without it. The album_art cache
            # means re-indexing never loses art we've already resolved.
            harvested, filled = self._backfill_album_art(conn, udn=udn)
            if harvested or filled:
                log.info(f"album_art [{udn[:12]}…]: harvested={harvested} "
                         f"album(s), filled={filled} track(s)")

            inserted = inserted  # already captured above
        return inserted

    def clear(self, udn: str):
        """
        Wipe track index for this UDN. Playlists untouched.
        Forces FTS5 rebuild so shadow tables are clean.
        """
        with self._pool.write() as conn:
            conn.execute("DELETE FROM tracks WHERE udn=?", (udn,))
            conn.execute("DELETE FROM index_meta WHERE udn=?", (udn,))
            conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

        log.info(f"Track index cleared for {udn}")

    def mark_indexed(self, udn: str):
        with self._pool.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (udn, indexed_at) "
                "VALUES (?, datetime('now'))", (udn,))

    def rebuild_fts(self):
        """Force a full FTS5 shadow-table rebuild."""
        with self._pool.write() as conn:
            conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

        log.info("FTS5 rebuild complete")

    # ── FTS5 search ───────────────────────────────────────────────

    def search(self, udn: str, query: str, limit: int = 300) -> dict:
        """
        Full-text search returning tracks, distinct albums, distinct artists.
        """
        safe  = query.replace('"', '""')
        fts_q = f'"{safe}"'
        with self._pool.read() as conn:

            tracks = conn.execute(
                """SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, 'audio' as type
                   FROM tracks_fts f
                   JOIN tracks t ON t.id = f.rowid
                   WHERE tracks_fts MATCH ? AND t.udn = ?
                   ORDER BY t.artist, t.album, t.title
                   LIMIT ?""",
                (fts_q, udn, limit)).fetchall()

            albums = conn.execute(
                """SELECT t.artist, t.album,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks_fts f
                   JOIN tracks t ON t.id = f.rowid
                   WHERE tracks_fts MATCH ? AND t.udn = ?
                     AND t.album != ''
                   GROUP BY t.artist, t.album
                   ORDER BY t.artist, t.album
                   LIMIT 100""",
                (fts_q, udn)).fetchall()

            artists = conn.execute(
                """SELECT t.artist,
                          COUNT(DISTINCT t.album) as album_count,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks_fts f
                   JOIN tracks t ON t.id = f.rowid
                   WHERE tracks_fts MATCH ? AND t.udn = ?
                     AND t.artist != ''
                   GROUP BY t.artist
                   ORDER BY t.artist
                   LIMIT 50""",
                (fts_q, udn)).fetchall()

        return {
            "tracks":  [dict(r) for r in tracks],
            "albums":  [dict(r) for r in albums],
            "artists": [dict(r) for r in artists],
        }

    def all_artists(self, udn: str) -> list:
        """Return all artists with album/track counts from SQLite."""
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT artist,
                          COUNT(DISTINCT album) as album_count,
                          COUNT(*) as track_count,
                          MAX(art) as art
                   FROM tracks
                   WHERE udn=? AND artist != ''
                   GROUP BY artist
                   ORDER BY artist COLLATE NOCASE""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]

    def album_tracks(self, udn: str, artist: str, album: str) -> list:
        """Return all tracks for a given (artist, album) pair."""
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT obj_id as id, url, title, artist, album,
                          duration, art, mime, genre, 'audio' as type
                   FROM tracks
                   WHERE udn=? AND album=?
                     AND (? = '' OR artist=?)
                   ORDER BY title""",
                (udn, album, artist, artist)).fetchall()
        return [dict(r) for r in rows]

    def all_albums(self, udn: str) -> list:
        """All distinct albums, grouping compilations under 'Various Artists'."""
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT album,
                          CASE WHEN COUNT(DISTINCT artist) > 1
                               THEN 'Various Artists'
                               ELSE MAX(artist) END as artist,
                          COUNT(*) as track_count,
                          MAX(art) as art
                   FROM tracks
                   WHERE udn=? AND album != ''
                   GROUP BY album
                   ORDER BY album COLLATE NOCASE""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]

    def artist_albums(self, udn: str, artist: str) -> list:
        """All albums for a given artist, A-Z."""
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT album, artist,
                          COUNT(*) as track_count,
                          MAX(art) as art
                   FROM tracks
                   WHERE udn=? AND artist=?
                   GROUP BY album
                   ORDER BY album COLLATE NOCASE""",
                (udn, artist)).fetchall()
        return [dict(r) for r in rows]

    def browse_letter(self, udn: str, mode: str, letter: str,
                      offset: int = 0, limit: int = 100) -> dict:
        """
        Return paginated Artists, Albums or Tracks starting with `letter`.
        letter: 'A'..'Z', '0' (digits), or '#' (everything else).
        mode: 'artists' | 'albums' | 'tracks'
        Returns: {items, total, offset, limit, letter, mode}
        """
        if letter == "0":
            like, where_extra = None, "AND SUBSTR(UPPER({col}),1,1) BETWEEN '0' AND '9'"
        elif letter == "#":
            like, where_extra = None, (
                "AND SUBSTR(UPPER({col}),1,1) NOT BETWEEN 'A' AND 'Z' "
                "AND SUBSTR(UPPER({col}),1,1) NOT BETWEEN '0' AND '9'")
        else:
            like = letter.upper() + "%"
            where_extra = "AND UPPER({col}) LIKE ?"

        def _q(col, select, group_by=""):
            we = where_extra.format(col=col)
            params = [udn] + ([like] if like else [])
            cnt_q = f"SELECT COUNT(*) FROM (SELECT {col} FROM tracks WHERE udn=? {we} AND {col}!='' {group_by})"
            tot = conn.execute(cnt_q, params).fetchone()[0]
            rows = conn.execute(
                f"""SELECT {select} FROM tracks
                    WHERE udn=? {we} AND {col}!=''
                    {group_by}
                    ORDER BY {col} COLLATE NOCASE
                    LIMIT ? OFFSET ?""",
                params + [limit, offset]).fetchall()
            return tot, rows

        with self._pool.read() as conn:
            if mode == "artists":
                total, rows = _q(
                    "artist",
                    "artist, COUNT(DISTINCT album) as album_count, COUNT(*) as track_count, MAX(art) as art",
                    "GROUP BY artist")
                items = [dict(r) for r in rows]
            elif mode == "albums":
                total, rows = _q(
                    "album",
                    """album,
                       CASE WHEN COUNT(DISTINCT artist)>1 THEN 'Various Artists'
                            ELSE MAX(artist) END as artist,
                       COUNT(*) as track_count, MAX(art) as art""",
                    "GROUP BY album")
                items = [dict(r) for r in rows]
            elif mode == "genres":
                total, rows = _q(
                    "genre",
                    "genre, COUNT(DISTINCT album) as album_count, COUNT(*) as track_count",
                    "GROUP BY genre")
                items = [dict(r) for r in rows]
            else:  # tracks
                total, rows = _q(
                    "title",
                    "obj_id as id, url, title, artist, album, duration, art, mime, genre, 'audio' as type",
                    "")
                items = [dict(r) for r in rows]

        return {"items": items, "total": total, "offset": offset,
                "limit": limit, "letter": letter, "mode": mode}

    def all_genres(self, udn: str) -> list:
        """All distinct genres with album/track counts, A-Z."""
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT genre,
                          COUNT(DISTINCT album) as album_count,
                          COUNT(*) as track_count
                   FROM tracks
                   WHERE udn=? AND genre != ''
                   GROUP BY genre
                   ORDER BY genre COLLATE NOCASE""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]

    def genre_albums(self, udn: str, genre: str) -> list:
        """All albums in a genre, grouping compilations under 'Various Artists'."""
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT album,
                          CASE WHEN COUNT(DISTINCT artist)>1 THEN 'Various Artists'
                               ELSE MAX(artist) END as artist,
                          COUNT(*) as track_count,
                          MAX(art) as art
                   FROM tracks
                   WHERE udn=? AND genre=?
                   GROUP BY album
                   ORDER BY album COLLATE NOCASE""",
                (udn, genre)).fetchall()
        return [dict(r) for r in rows]

    # ── Metadata editing ─────────────────────────────────────────

    def update_track_meta(self, url: str,
                          artist: str = None, album: str = None,
                          title: str = None, genre: str = None) -> bool:
        """
        Update artist/album/title/genre for a track in the DB.
        Only provided (non-None) fields are changed.
        Also saves to metadata_overrides so edits survive re-index.
        Returns True if any row was updated.
        """
        fields = {k: v for k, v in
                  [("artist", artist), ("album", album),
                   ("title", title), ("genre", genre)]
                  if v is not None}
        if not fields:
            return False
        set_clause = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values())
        with self._pool.write() as conn:
            conn.execute(
                f"UPDATE tracks SET {set_clause} WHERE url=?",
                vals + [url])
            # Upsert into overrides — merge with any existing overrides
            existing = conn.execute(
                "SELECT artist, album, title, genre FROM metadata_overrides WHERE url=?",
                (url,)).fetchone()
            if existing:
                merged = dict(existing)
                merged.update(fields)
                conn.execute(
                    "UPDATE metadata_overrides "
                    "SET artist=?, album=?, title=?, genre=?, updated_at=datetime('now') "
                    "WHERE url=?",
                    (merged["artist"], merged["album"],
                     merged["title"], merged["genre"], url))
            else:
                # Fill blanks from current track record
                row = conn.execute(
                    "SELECT artist, album, title, genre FROM tracks WHERE url=?",
                    (url,)).fetchone()
                base = dict(row) if row else {"artist":"","album":"","title":"","genre":""}
                base.update(fields)
                conn.execute(
                    "INSERT INTO metadata_overrides (url, artist, album, title, genre) "
                    "VALUES (?,?,?,?,?)",
                    (url, base["artist"], base["album"],
                     base["title"], base["genre"]))

            changed = conn.execute("SELECT changes()").fetchone()[0]
        return changed > 0

    def get_track_file_path(self, url: str) -> str:
        """Return stored file_path for a track URL, or empty string."""
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT file_path FROM tracks WHERE url=?", (url,)).fetchone()
        return (row["file_path"] or "") if row else ""

    def genre_tracks(self, udn: str, genre: str) -> list:
        """All tracks in a genre."""
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT obj_id as id, url, title, artist, album,
                          duration, art, mime, genre, 'audio' as type
                   FROM tracks
                   WHERE udn=? AND genre=?
                   ORDER BY album COLLATE NOCASE, title COLLATE NOCASE""",
                (udn, genre)).fetchall()
        return [dict(r) for r in rows]

    def random_tracks(self, udn: str, limit: int = 100) -> list:
        """Return `limit` tracks picked randomly from the whole library."""
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT obj_id as id, url, title, artist, album,
                          duration, art, mime, 'audio' as type
                   FROM tracks
                   WHERE udn=?
                   ORDER BY RANDOM()
                   LIMIT ?""",
                (udn, limit)).fetchall()
        return [dict(r) for r in rows]

    # ── Playlists ─────────────────────────────────────────────────

    def pl_list(self) -> list:
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT p.id, p.name, COUNT(pt.id) as count
                   FROM playlists p
                   LEFT JOIN playlist_tracks pt ON pt.pl_id = p.id
                   GROUP BY p.id
                   ORDER BY p.sort_order, p.name""").fetchall()
        return [dict(r) for r in rows]

    def pl_get(self, pl_id: str) -> Optional[dict]:
        with self._pool.read() as conn:
            pl = conn.execute(
                "SELECT id, name FROM playlists WHERE id=?",
                (pl_id,)).fetchone()
            if not pl:
                return None
            tracks = conn.execute(
                """SELECT url, title, artist, album, duration, art
                   FROM playlist_tracks WHERE pl_id=?
                   ORDER BY added_at""", (pl_id,)).fetchall()
        return {"id": pl["id"], "name": pl["name"],
                "tracks": [dict(t) for t in tracks]}

    def pl_create(self, name: str) -> str:
        pl_id = str(uuid.uuid4())[:8]
        with self._pool.write() as conn:
            conn.execute("INSERT INTO playlists (id, name) VALUES (?,?)",
                         (pl_id, name))

        return pl_id

    def pl_delete(self, pl_id: str) -> bool:
        if pl_id == FAVOURITES_ID:
            return False
        with self._pool.write() as conn:
            cur = conn.execute("DELETE FROM playlists WHERE id=?", (pl_id,))

        return cur.rowcount > 0

    def pl_add_track(self, pl_id: str, track: dict) -> str:
        """Returns 'added', 'duplicate', or 'not_found'."""
        with self._pool.write() as conn:
            pl = conn.execute(
                "SELECT id FROM playlists WHERE id=?", (pl_id,)).fetchone()
            if not pl:
                return "not_found"
            try:
                conn.execute(
                    "INSERT INTO playlist_tracks "
                    "(pl_id, url, title, artist, album, duration, art) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (pl_id, track.get("url",""), track.get("title",""),
                     track.get("artist",""), track.get("album",""),
                     track.get("duration",""), track.get("art","")))

                return "added"
            except sqlite3.IntegrityError:
                return "duplicate"

    def pl_remove_track(self, pl_id: str, url: str) -> bool:
        with self._pool.write() as conn:
            cur = conn.execute(
                "DELETE FROM playlist_tracks WHERE pl_id=? AND url=?",
                (pl_id, url))

        return cur.rowcount > 0

    def pl_to_m3u(self, pl_id: str, shuffle: bool = False,
                  output_path: str = "/tmp/dlna-gw-pl.m3u") -> Optional[str]:
        pl = self.pl_get(pl_id)
        if not pl or not pl["tracks"]:
            return None
        tracks = list(pl["tracks"])
        if shuffle:
            random.shuffle(tracks)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for t in tracks:
                f.write(f"#EXTINF:-1,{t.get('title','')}\n{t['url']}\n")
        return output_path

    def tracks_to_m3u(self, tracks: list,
                      output_path: str = "/tmp/dlna-gw-current.m3u") -> str:
        """Write a list of track dicts to an M3U file. Returns the path."""
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for t in tracks:
                dur  = t.get("duration", "")
                secs = _dur_to_secs(dur)
                f.write(f"#EXTINF:{secs},{t.get('title','')}\n{t['url']}\n")
        return output_path

def _dur_to_secs(dur: str) -> int:
    """'H:MM:SS' → integer seconds, -1 if unparseable."""
    try:
        parts = [float(x) for x in dur.split(":")]
        if len(parts) == 3:
            return int(parts[0] * 3600 + parts[1] * 60 + parts[2])
        if len(parts) == 2:
            return int(parts[0] * 60 + parts[1])
    except Exception:
        pass
    return -1

# ── Composition root ──────────────────────────────────────────────
# The LibraryDB singleton is the shared DB handle. Indexer and
# AlbumArtFetcher and DeviceRoleCache are owned components, each
# wired to DB here so their modules don't need to know about
# singleton patterns (and they stay unit-testable in isolation).

DB = LibraryDB()

from dlna_devices      import DeviceRoleCache
from dlna_indexer      import Indexer, IndexState  # noqa: F401 re-exported
from dlna_art_fetcher  import AlbumArtFetcher

DEVICE_ROLES = DeviceRoleCache(DB)
INDEXER      = Indexer(DB)
ART_FETCHER  = AlbumArtFetcher(DB)


# ── Standalone test ───────────────────────────────────────────────

def _test():
    from dlna_config import setup_logging
    setup_logging(debug=True)
    log.info("=== dlna_library self-test ===")

    db = LibraryDB()
    log.info(f"DB file : {db._db_file}  exists={os.path.exists(db._db_file)}")

    pls = db.pl_list()
    log.info(f"Playlists ({len(pls)}):")
    for p in pls:
        log.info(f"  {p['name']}  ({p['count']} tracks)")

    # Check all UDNs
    with db._lock:
        conn = db._connect()
        udns = conn.execute(
            "SELECT udn, COUNT(*) as n FROM tracks GROUP BY udn").fetchall()

    if udns:
        log.info(f"Track index:")
        for row in udns:
            log.info(f"  {row['udn'][:40]}  → {row['n']} tracks")
            log.info(f"    Albums: {db.album_count(row['udn'])}")
    else:
        log.info("Track index: empty (not yet indexed)")

    log.info("PASS — dlna_library OK")

if __name__ == "__main__":
    _test()