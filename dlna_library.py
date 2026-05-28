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
import re
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


# AssetUPnP encodes the source file's bit depth and sample rate in
# the URL path (e.g. `/c2/b16/f44100/...` or `/c2/b24/f96000/...`).
# We parse these out at index time to populate tracks.bit_depth and
# tracks.sample_rate, which participate in the UNIQUE constraint so
# 16-bit and 24-bit copies of the same (artist, album, title)
# coexist as distinct rows. Browse-side queries then prefer the
# higher-quality version. Other UPnP MediaServers usually don't
# embed these in the URL; for them we leave both columns NULL.
_AUDIO_PARAMS_RE = re.compile(r"/b(\d+)/f(\d+)/")
_D_ID_RE         = re.compile(r"/(d-?\d+)-co")


def _parse_audio_params(url: str):
    """Return (bit_depth, sample_rate) parsed from an AssetUPnP-style
    URL, or (None, None) if the pattern doesn't match. Both values
    are integers when present (bit_depth in bits, sample_rate in Hz)."""
    if not url:
        return None, None
    m = _AUDIO_PARAMS_RE.search(url)
    if not m:
        return None, None
    try:
        return int(m.group(1)), int(m.group(2))
    except (ValueError, TypeError):
        return None, None


def _d_id(url: str):
    """Extract the d-id portion of an AssetUPnP URL, or None for
    non-AssetUPnP URLs. Used as one half of the (d_id, lower(title))
    dedup key in upsert_tracks — see the 'AssetUPnP virtual albums'
    note in the docstring for upsert_tracks for the why."""
    if not url:
        return None
    m = _D_ID_RE.search(url)
    return m.group(1) if m else None


# Lazy unicodedata import keeps module load cheap on the hot path.
_unicodedata = None

def _norm_title(s):
    """Normalise a track title for dedup keying.

    Strips combining marks, replaces curly typographic apostrophes /
    quote marks with ASCII equivalents, collapses whitespace,
    lowercases. Same song with different typographic renderings
    (e.g. "Art for Art's Sake" with ASCII apostrophe vs the same
    string with curly U+2019) maps to one key.

    Does NOT strip bracketed annotations — "Wiggle It" and "Wiggle It
    (club mix)" stay distinct because they're genuinely different
    recordings."""
    global _unicodedata
    if not s:
        return ""
    if _unicodedata is None:
        import unicodedata as _unicodedata
    s = _unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not _unicodedata.combining(c))
    # Curly apostrophes/quotes → ASCII. The bytes that bit us live in
    # b"\xe2\x80\x99" (U+2019) and friends; doing this after NFKD
    # because NFKD does NOT decompose the smart-quote characters.
    s = (s.replace("‘", "'").replace("’", "'")
          .replace("‚", "'").replace("‛", "'")
          .replace("“", '"').replace("”", '"')
          .replace("´", "'").replace("`", "'"))
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _dedup_clause(outer_alias: str = "t") -> str:
    """SQL fragment that filters out lower-quality duplicates from
    a tracks-table query.

    Excludes the current row when a same-(udn,artist,album,title) row
    with strictly higher (bit_depth, sample_rate) exists. NULL values
    are treated as 0 (lowest), so any non-NULL beats a NULL — giving
    AssetUPnP-served tracks a clean prefer-24-bit, prefer-higher-rate
    ordering, without affecting non-AssetUPnP servers (all NULL → all
    treated as equal → all survive).

    Use only in BROWSE views — listings the user sees in the UI.
    The AcoustID worker, playlists, and radio scans should NOT dedup
    (they need to process / play every URL the user has)."""
    a = outer_alias
    return f"""NOT EXISTS (
        SELECT 1 FROM tracks _hq
         WHERE _hq.udn    = {a}.udn
           AND _hq.artist = {a}.artist
           AND _hq.album  = {a}.album
           AND _hq.title  = {a}.title
           AND (   COALESCE(_hq.bit_depth, 0)   >  COALESCE({a}.bit_depth, 0)
                OR (    COALESCE(_hq.bit_depth, 0)   = COALESCE({a}.bit_depth, 0)
                    AND COALESCE(_hq.sample_rate, 0) > COALESCE({a}.sample_rate, 0)))
    )"""


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
                -- bit_depth + sample_rate are parsed from the URL by
                -- _parse_audio_params (AssetUPnP pattern /b<NN>/f<MMMMM>/).
                -- They participate in the UNIQUE so a 16-bit and 24-bit
                -- copy of the same (artist, album, title) coexist as
                -- distinct rows — the gateway's browse-side filter then
                -- shows only the higher-quality winner in browse listings.
                -- For non-AssetUPnP MediaServers that don't encode these
                -- in the URL, both columns stay NULL; SQLite treats NULLs
                -- as distinct in UNIQUE, so such tracks don't collide
                -- with each other either.
                CREATE TABLE IF NOT EXISTS tracks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    udn         TEXT NOT NULL,
                    obj_id      TEXT,
                    url         TEXT NOT NULL,
                    title       TEXT,
                    artist      TEXT,
                    album       TEXT,
                    duration    TEXT,
                    art         TEXT,
                    mime        TEXT,
                    genre       TEXT DEFAULT '',
                    file_path   TEXT DEFAULT '',
                    bit_depth   INTEGER,
                    sample_rate INTEGER,
                    year        INTEGER,    -- file-tag year (DIDL-Lite dc:date)
                    UNIQUE(udn, artist, album, title, bit_depth, sample_rate)
                );
                -- Genre migration: add column if upgrading from older schema
                
                CREATE TABLE IF NOT EXISTS metadata_overrides (
                    url       TEXT PRIMARY KEY,
                    artist    TEXT,
                    album     TEXT,
                    title     TEXT,
                    genre     TEXT,
                    year      INTEGER,   -- original release year (MusicBrainz)
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
                -- Radio play counts. Independent of `tracks` (keyed by
                -- URL, no FK) so rebuild-index doesn't affect it — play
                -- history persists forever, like album_art. Radio
                -- ordering biases toward lowest count so the full
                -- library cycles through over time.
                CREATE TABLE IF NOT EXISTS play_counts (
                    url         TEXT PRIMARY KEY,
                    count       INTEGER NOT NULL DEFAULT 0,
                    last_played INTEGER
                );
                -- Per-track loudness analysis (Phase 1 normalization).
                -- Independent of `tracks` (keyed by URL, no FK) so it
                -- survives clear(udn) — same invariant as album_art and
                -- play_counts. lufs IS NULL marks a sticky negative
                -- cache (scan failed, don't retry forever).
                -- peak_db is the measured true-peak (dBTP) and drives
                -- gain_db = TARGET_PEAK_DBTP - peak_db (clamped ±2 dB);
                -- lufs is kept as an informational side measurement.
                CREATE TABLE IF NOT EXISTS track_loudness (
                    url        TEXT PRIMARY KEY,
                    lufs       REAL,
                    peak_db    REAL,
                    gain_db    REAL DEFAULT 0.0,
                    scanned_at INTEGER NOT NULL
                );
                -- On-demand lyrics cache. Same survival contract as the
                -- other auxiliary tables: keyed by URL, no FK, untouched
                -- by clear(udn). source='notfound' is a sticky negative
                -- cache — to retry a single track:
                --   DELETE FROM lyrics WHERE source='notfound' AND url=?
                CREATE TABLE IF NOT EXISTS lyrics (
                    url        TEXT PRIMARY KEY,
                    plain      TEXT,
                    synced     TEXT,
                    source     TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL
                );
                -- User-favourited albums. Identity = (artist, album) so it
                -- survives clear(udn) and re-indexing — same convention as
                -- album_art / play_counts / lyrics. Distinct from the
                -- track-level "⭐ Favourites" playlist.
                CREATE TABLE IF NOT EXISTS album_favourites (
                    artist     TEXT NOT NULL,
                    album      TEXT NOT NULL,
                    added_at   INTEGER NOT NULL,
                    PRIMARY KEY (artist, album)
                );
                -- User-favourited internet-radio stations. Capped at
                -- RADIO_FAV_MAX (25), enforced server-side in
                -- radio_fav_add(). Identity = radio-browser stationuuid,
                -- so favourites survive clear(udn) / re-indexing — same
                -- convention as album_favourites. Radio has no udn; the
                -- radio-browser catalogue itself is never persisted.
                CREATE TABLE IF NOT EXISTS radio_favourites (
                    station_uuid TEXT PRIMARY KEY,
                    name         TEXT NOT NULL,
                    stream_url   TEXT NOT NULL,
                    homepage     TEXT,
                    favicon      TEXT,
                    codec        TEXT,
                    bitrate      INTEGER,
                    country      TEXT,
                    tags         TEXT,
                    added_at     INTEGER NOT NULL,
                    sort_order   INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS index_meta (
                    udn        TEXT PRIMARY KEY,
                    indexed_at TEXT
                );
                -- URL uniqueness per UDN is enforced by
                -- idx_tracks_udn_url, but the CREATE INDEX is run by
                -- `_migrate_unique_url` (which first dedupes existing
                -- rows) NOT here — putting it in execute_script would
                -- fail on existing DBs with pre-fix dupes.
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
            # 2026-05-25: metadata_overrides.source distinguishes user edits
            # ('manual') from the AcoustID background worker's writes
            # ('acoustid', 'notfound'). Existence of a row replaces the need
            # for a separate `meta_update` flag — same sticky-negative
            # convention as album_art / lyrics.
            "ALTER TABLE metadata_overrides ADD COLUMN source TEXT "
            "NOT NULL DEFAULT 'manual'",
            # 2026-05-26: year columns. tracks.year is the file-tag year
            # (DIDL-Lite dc:date / upnp:originalTrackDate — the edition you
            # own). metadata_overrides.year is the ORIGINAL release year
            # captured from MusicBrainz release-group's first-release-date
            # by the AcoustID worker. Frontend prefers the MB override year;
            # falls back to file-tag year; annotates "(remastered)" when
            # tag year - override year >= 3.
            "ALTER TABLE tracks ADD COLUMN year INTEGER",
            "ALTER TABLE metadata_overrides ADD COLUMN year INTEGER",
        ]:
            try:
                with self._pool.write() as conn:
                    conn.execute(col_sql)
                log.info(f"DB migration: {col_sql[:60]}")
            except Exception:
                pass  # column already exists
        # Loudness mode switch: LUFS-based → true-peak normalisation.
        # When peak_db is added to an existing DB, the old gain_db values
        # were computed from LUFS — wipe them so the scanner re-analyses
        # every track and stores the true peak.
        try:
            with self._pool.write() as conn:
                conn.execute("ALTER TABLE track_loudness ADD COLUMN peak_db REAL")
                conn.execute("DELETE FROM track_loudness")
            log.info("DB migration: track_loudness.peak_db added — "
                     "existing rows wiped, re-scan will run on next trigger")
        except Exception:
            pass  # column already exists; nothing to migrate
        # 2026-05-25: widen tracks UNIQUE to include bit_depth + sample_rate
        # so 16-bit and 24-bit copies of the same album coexist without
        # collision. Detect old schema by absence of the bit_depth column;
        # if present, this migration is a no-op. Backfills the new columns
        # by parsing existing rows' URLs.
        with self._pool.write() as conn:
            self._migrate_widen_tracks_unique(conn)
        # 2026-05-25 follow-up: the widened UNIQUE accidentally let same-URL
        # duplicates accumulate. The indexer's INSERT OR IGNORE uses the
        # wider tuple, but same URL with raw-vs-corrected metadata becomes
        # two distinct tuples → both insert → ~18k phantom dup rows. Add
        # a UNIQUE(udn, url) index so URL uniqueness is enforced
        # independently. Deduplicate existing rows first (keep MIN(id) per
        # (udn,url) — those are the migration-inserted, AcoustID-corrected
        # ones, since the indexer-inserted dupes have higher AUTOINCREMENT
        # ids and raw metadata).
        with self._pool.write() as conn:
            self._migrate_unique_url(conn)
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

    def _migrate_widen_tracks_unique(self, conn: sqlite3.Connection):
        """One-time migration: widen `tracks` UNIQUE from
        (udn,artist,album,title) to (udn,artist,album,title,bit_depth,sample_rate)
        so 16/24-bit copies of the same album don't collide.

        Detection: if `tracks.bit_depth` column already exists, the
        migration has run. Otherwise: drop FTS triggers, rename old
        table, create new table with widened UNIQUE and the two new
        columns, INSERT-with-parsed-URLs into new, drop old, recreate
        triggers, rebuild FTS5 index. Idempotent.

        Heavy on row count but fast in practice (~100ms on 25k rows).
        Locks the DB briefly — acceptable at startup."""
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='tracks'").fetchone()
        if not sql_row:
            # Fresh DB — CREATE TABLE in _init_schema already used new schema.
            return
        if "bit_depth" in (sql_row[0] or ""):
            # Already migrated.
            return

        old_rows = conn.execute(
            "SELECT id, udn, obj_id, url, title, artist, album, duration, "
            "       art, mime, genre, file_path "
            "  FROM tracks").fetchall()
        n_old = len(old_rows)
        log.info(f"DB migration: widening tracks UNIQUE; rebuilding "
                 f"{n_old:,} row(s) with parsed bit_depth + sample_rate")

        # Triggers reference the table being renamed — drop first so
        # they don't end up pointing at the obsolete renamed table.
        conn.execute("DROP TRIGGER IF EXISTS tracks_ai")
        conn.execute("DROP TRIGGER IF EXISTS tracks_ad")
        conn.execute("ALTER TABLE tracks RENAME TO _tracks_pre_widen")
        conn.execute("""
            CREATE TABLE tracks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                udn         TEXT NOT NULL,
                obj_id      TEXT,
                url         TEXT NOT NULL,
                title       TEXT,
                artist      TEXT,
                album       TEXT,
                duration    TEXT,
                art         TEXT,
                mime        TEXT,
                genre       TEXT DEFAULT '',
                file_path   TEXT DEFAULT '',
                bit_depth   INTEGER,
                sample_rate INTEGER,
                UNIQUE(udn, artist, album, title, bit_depth, sample_rate)
            )
        """)
        inserts = []
        for r in old_rows:
            bd, sr = _parse_audio_params(r["url"])
            inserts.append((r["id"], r["udn"], r["obj_id"], r["url"],
                            r["title"], r["artist"], r["album"],
                            r["duration"], r["art"], r["mime"],
                            r["genre"], r["file_path"], bd, sr))
        conn.executemany(
            "INSERT OR IGNORE INTO tracks "
            "(id, udn, obj_id, url, title, artist, album, duration, art, "
            " mime, genre, file_path, bit_depth, sample_rate) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", inserts)
        n_new = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

        conn.execute("DROP TABLE _tracks_pre_widen")
        # Recreate the FTS5 sync triggers (they were dropped above).
        conn.execute("""
            CREATE TRIGGER tracks_ai
                AFTER INSERT ON tracks BEGIN
                    INSERT INTO tracks_fts(rowid, title, artist, album)
                    VALUES (new.id, new.title, new.artist, new.album);
                END
        """)
        conn.execute("""
            CREATE TRIGGER tracks_ad
                AFTER DELETE ON tracks BEGIN
                    INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                    VALUES ('delete', old.id, old.title, old.artist, old.album);
                END
        """)
        conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

        skipped = n_old - n_new
        log.info(f"DB migration: tracks rebuilt — {n_old:,} → {n_new:,} rows "
                 f"({skipped} skipped on new UNIQUE), FTS5 rebuilt")

    def _migrate_unique_url(self, conn: sqlite3.Connection):
        """Enforce `UNIQUE(udn, url)` so the indexer can't accumulate
        same-URL duplicates when a URL's raw vs AcoustID-corrected
        metadata differ. Pre-2026-05-25 the narrow UNIQUE collapsed
        these implicitly; after the bit_depth/sample_rate widening
        they survive as phantom dup rows. This migration:

          1. DELETEs duplicate rows, keeping the MIN(id) per (udn,url)
             (the original migration-inserted, AcoustID-corrected row).
          2. CREATEs UNIQUE INDEX idx_tracks_udn_url.

        Idempotent: detects the index by name and no-ops if present.
        FTS triggers are dropped during the delete (avoiding 18k trigger
        invocations) then recreated; FTS5 is rebuilt at the tail."""
        idx = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_tracks_udn_url'"
        ).fetchone()
        if idx:
            return

        n_dup = conn.execute("""
            SELECT COUNT(*) FROM tracks
             WHERE id NOT IN (
               SELECT MIN(id) FROM tracks GROUP BY udn, url
             )
        """).fetchone()[0]

        if n_dup > 0:
            log.info(f"DB migration: removing {n_dup:,} same-URL "
                     f"duplicate rows from tracks "
                     f"(keeping MIN(id) per (udn,url))")
            # Drop triggers so the FTS5 ad trigger doesn't fire per row.
            conn.execute("DROP TRIGGER IF EXISTS tracks_ai")
            conn.execute("DROP TRIGGER IF EXISTS tracks_ad")
            conn.execute("""
                DELETE FROM tracks
                 WHERE id NOT IN (
                   SELECT MIN(id) FROM tracks GROUP BY udn, url
                 )
            """)

        conn.execute(
            "CREATE UNIQUE INDEX idx_tracks_udn_url ON tracks(udn, url)")
        log.info("DB migration: added UNIQUE(udn, url) index")

        if n_dup > 0:
            # Recreate triggers + rebuild FTS5 from the surviving rows.
            conn.execute("""
                CREATE TRIGGER tracks_ai
                    AFTER INSERT ON tracks BEGIN
                        INSERT INTO tracks_fts(rowid, title, artist, album)
                        VALUES (new.id, new.title, new.artist, new.album);
                    END
            """)
            conn.execute("""
                CREATE TRIGGER tracks_ad
                    AFTER DELETE ON tracks BEGIN
                        INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                        VALUES ('delete', old.id, old.title, old.artist, old.album);
                    END
            """)
            conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

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
        Insert tracks, deduplicating on (udn, d_id, lower(title)) where
        d_id is the `d-<n>` AssetUPnP URL segment.

        Why d_id+title and not just url: AssetUPnP exposes the SAME
        physical file under multiple "browse-tree paths" — both the
        real album (e.g. "Kasabian") AND any compilation albums the
        user has set up ("Music From the OC: Mix 5"). Each path gets
        a different `co-<hash>` segment in the URL, but the `d-<n>`
        part stays the same. Without this dedup, the index sees the
        same file 2x and the row count balloons (confirmed 2026-05-28:
        22k physical files → 40k rows). HTTP HEAD of the duplicate
        URLs confirms byte-identical Content-Length on both sides.

        Dedup is in Python (within-batch) and against any pre-existing
        rows for this UDN. URLs without a recognisable d-id (non-
        AssetUPnP servers) fall through to the wide UNIQUE constraint
        and never trigger d-id dedup. The FIRST URL seen wins; later
        aliases for the same (d_id, title) are skipped.

        Returns number of rows actually inserted.
        """
        if not tracks:
            return 0
        # Parse bit_depth + sample_rate from the URL at row-build time.
        # AssetUPnP URLs include `/b<bits>/f<rate>/`; non-AssetUPnP
        # servers usually don't, in which case both stay None and the
        # UNIQUE treats NULLs as distinct (so no cross-server collisions).
        # year is the file-tag year (DIDL-Lite dc:date / upnp:originalTrackDate),
        # parsed in dlna_content._parse_didl.
        def _make_row(t: dict) -> dict:
            url = t.get("url", "")
            bd, sr = _parse_audio_params(url)
            return dict(
                udn=udn,
                obj_id=t.get("id", ""),
                url=url,
                title=t.get("title", ""),
                artist=t.get("artist", ""),
                album=t.get("album", ""),
                duration=t.get("duration", ""),
                art=t.get("art", ""),
                mime=t.get("mime", ""),
                genre=t.get("genre", ""),
                file_path=t.get("file_path", ""),
                bit_depth=bd,
                sample_rate=sr,
                year=t.get("year"),
            )
        rows_raw = [_make_row(t) for t in tracks if t.get("url")]

        with self._pool.write() as conn:
            # Build the (d_id, _norm_title(title)) dedup set: existing
            # rows for this UDN + within-batch tracking. Non-AssetUPnP
            # URLs have d_id=None and are NOT deduped this way — they
            # fall through to the wider UNIQUE constraint.
            #
            # The post-COALESCE-mismatch race that motivated an earlier
            # override-aware path is already resolved by _norm_title's
            # apostrophe/diacritic normalisation: the new raw title and
            # the existing post-COALESCE title both normalise to the
            # same key. Considering the override title here would over-
            # collapse legitimately-distinct recordings that happen to
            # share a d-id (e.g. 3 Doors Down "Be Like That" vs
            # "Be Like That (acoustic)"), so we don't.
            seen: set[tuple[str, str]] = set()
            for (existing_url, existing_title) in conn.execute(
                "SELECT url, title FROM tracks WHERE udn=?", (udn,)
            ).fetchall():
                d = _d_id(existing_url)
                if d:
                    seen.add((d, _norm_title(existing_title)))

            rows: list[dict] = []
            n_aliased = 0
            for r in rows_raw:
                d = _d_id(r["url"])
                if d:
                    key = (d, _norm_title(r["title"]))
                    if key in seen:
                        n_aliased += 1
                        continue
                    seen.add(key)
                rows.append(r)
            if n_aliased:
                log.info(f"upsert_tracks [{udn[:12]}…]: dropped "
                         f"{n_aliased} alias row(s) "
                         f"(same d-id + title via different browse path)")

            before = conn.execute("SELECT changes()").fetchone()[0]
            # Step 1: insert new tracks (skip duplicates)
            conn.executemany(
                "INSERT OR IGNORE INTO tracks "
                "(udn, obj_id, url, title, artist, album, duration, art, "
                " mime, genre, file_path, bit_depth, sample_rate, year) "
                "VALUES (:udn,:obj_id,:url,:title,:artist,:album,:duration,"
                "        :art,:mime,:genre,:file_path,:bit_depth,:sample_rate,"
                "        :year)",
                rows)
            inserted = conn.execute("SELECT changes()").fetchone()[0]
            # Step 2: update genre + art on already-indexed tracks
            # (safe UPDATE preserves FTS5 triggers, picks up new metadata on re-index)
            conn.executemany(
                "UPDATE tracks SET genre=:genre, art=:art "
                "WHERE udn=:udn AND artist=:artist AND album=:album AND title=:title "
                "  AND (genre='' OR genre IS NULL)",
                rows)
            # Apply any saved metadata overrides (survive re-index).
            # OR IGNORE tolerates UNIQUE(udn,artist,album,title) collisions:
            # the AcoustID worker may have resolved two different track URLs
            # to the same metadata (duplicate uploads, 16-bit + 24-bit pairs,
            # compilation appearances). Without OR IGNORE a SINGLE colliding
            # row aborts the entire UPDATE → indexer crashes → tracks table
            # stays empty after clear(). User-visible impact of the IGNORE:
            # one of the colliding duplicates keeps its pre-override metadata
            # in the tracks row. That's identical to the live-update path in
            # LibraryDB.metadata_override_set (which catches IntegrityError
            # for the same reason).
            conn.execute("""
                UPDATE OR IGNORE tracks SET
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
        Browse-side dedup is applied: lower-quality 16-bit duplicates of
        a 24-bit track are hidden. See `_dedup_clause` docstring.
        """
        safe  = query.replace('"', '""')
        fts_q = f'"{safe}"'
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:

            tracks = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, 'audio' as type
                   FROM tracks_fts f
                   JOIN tracks t ON t.id = f.rowid
                   WHERE tracks_fts MATCH ? AND t.udn = ?
                     AND {dedup}
                   ORDER BY t.artist, t.album, t.title
                   LIMIT ?""",
                (fts_q, udn, limit)).fetchall()

            albums = conn.execute(
                f"""SELECT t.artist, t.album,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks_fts f
                   JOIN tracks t ON t.id = f.rowid
                   WHERE tracks_fts MATCH ? AND t.udn = ?
                     AND t.album != ''
                     AND {dedup}
                   GROUP BY t.artist, t.album
                   ORDER BY t.artist, t.album
                   LIMIT 100""",
                (fts_q, udn)).fetchall()

            artists = conn.execute(
                f"""SELECT t.artist,
                          COUNT(DISTINCT t.album) as album_count,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks_fts f
                   JOIN tracks t ON t.id = f.rowid
                   WHERE tracks_fts MATCH ? AND t.udn = ?
                     AND t.artist != ''
                     AND {dedup}
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
        """Return all artists with album/track counts. Track count is
        the browse-visible (deduped) count."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.artist,
                          COUNT(DISTINCT t.album) as album_count,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks t
                   WHERE t.udn=? AND t.artist != ''
                     AND {dedup}
                   GROUP BY t.artist
                   ORDER BY t.artist COLLATE NOCASE""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]

    def album_tracks(self, udn: str, artist: str, album: str) -> list:
        """Return all tracks for a given (artist, album) pair, with
        lower-quality 16/24-bit duplicates hidden from the browse view.
        See `_dedup_clause` for the winner-selection rule."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, t.genre, 'audio' as type
                   FROM tracks t
                   WHERE t.udn=? AND t.album=?
                     AND (? = '' OR t.artist=?)
                     AND {dedup}
                   ORDER BY t.title""",
                (udn, album, artist, artist)).fetchall()
        return [dict(r) for r in rows]

    def all_albums(self, udn: str) -> list:
        """All distinct albums, grouping compilations under 'Various Artists'.
        Track count reflects browse-visible (deduped) tracks only."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.album,
                          CASE WHEN COUNT(DISTINCT t.artist) > 1
                               THEN 'Various Artists'
                               ELSE MAX(t.artist) END as artist,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks t
                   WHERE t.udn=? AND t.album != ''
                     AND {dedup}
                   GROUP BY t.album
                   ORDER BY t.album COLLATE NOCASE""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]

    def artist_albums(self, udn: str, artist: str) -> list:
        """All albums for a given artist, A-Z. Track count is the
        browse-visible (deduped) count."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.album, t.artist,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks t
                   WHERE t.udn=? AND t.artist=?
                     AND {dedup}
                   GROUP BY t.album
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
        """All distinct genres with album/track counts, A-Z. Counts are
        browse-visible (deduped)."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.genre,
                          COUNT(DISTINCT t.album) as album_count,
                          COUNT(*) as track_count
                   FROM tracks t
                   WHERE t.udn=? AND t.genre != ''
                     AND {dedup}
                   GROUP BY t.genre
                   ORDER BY t.genre COLLATE NOCASE""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]

    def genre_albums(self, udn: str, genre: str) -> list:
        """All albums in a genre, grouping compilations under 'Various Artists'.
        Track count is browse-visible (deduped)."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.album,
                          CASE WHEN COUNT(DISTINCT t.artist)>1 THEN 'Various Artists'
                               ELSE MAX(t.artist) END as artist,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                   FROM tracks t
                   WHERE t.udn=? AND t.genre=?
                     AND {dedup}
                   GROUP BY t.album
                   ORDER BY t.album COLLATE NOCASE""",
                (udn, genre)).fetchall()
        return [dict(r) for r in rows]

    # ── Decade browse ────────────────────────────────────────────
    # A track's "effective year" for decade bucketing is
    # MIN(file_year, mb_year) when both are present, else whichever is
    # set. Rationale: AcoustID often resolves a fingerprint to a LATER
    # re-release recording on MusicBrainz (an anniversary edition's
    # /recording/<id>'s first-release-date is when *that* recording
    # was first released, e.g. 2002 for the 30th-anniversary remaster
    # of a 1972 album). When mb_year > file_year, the file tag is the
    # more reliable signal — so we take the smaller of the two.
    # Tracks with no year at all are excluded from decade listings
    # (would show as "decade 0" otherwise). Now-playing displays a
    # different rule (prefer mb_year as "the original recording",
    # annotate as "(remastered)" when file>>mb) — see _renderNpYear.
    _EFFECTIVE_YEAR = (
        "COALESCE("
        "CASE WHEN t.year > 0 AND m.year > 0 THEN MIN(t.year, m.year) END, "
        "NULLIF(t.year, 0), "
        "NULLIF(m.year, 0))"
    )

    def all_decades(self, udn: str) -> list:
        """All decades present in the library with track + album counts.
        Browse-side dedup is applied: a 16-bit duplicate of a 24-bit
        track doesn't double-count. Ordered chronologically (oldest to
        newest)."""
        dedup = _dedup_clause("t")
        eff = self._EFFECTIVE_YEAR
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT ({eff} / 10) * 10 AS decade,
                          COUNT(*) AS track_count,
                          COUNT(DISTINCT t.album) AS album_count
                     FROM tracks t
                LEFT JOIN metadata_overrides m ON m.url = t.url
                    WHERE t.udn = ?
                      AND {eff} IS NOT NULL
                      AND {dedup}
                 GROUP BY decade
                 ORDER BY decade ASC""",
                (udn,)).fetchall()
        return [dict(r) for r in rows]

    def decade_albums(self, udn: str, decade: int) -> list:
        """All albums whose effective year falls in [decade, decade+10).
        Grouping mirrors `all_albums`: compilations with multiple
        distinct artists collapse to 'Various Artists'."""
        dedup = _dedup_clause("t")
        eff = self._EFFECTIVE_YEAR
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.album,
                          CASE WHEN COUNT(DISTINCT t.artist) > 1
                               THEN 'Various Artists'
                               ELSE MAX(t.artist) END as artist,
                          COUNT(*) as track_count,
                          MAX(t.art) as art
                     FROM tracks t
                LEFT JOIN metadata_overrides m ON m.url = t.url
                    WHERE t.udn = ? AND t.album != ''
                      AND ({eff} / 10) * 10 = ?
                      AND {dedup}
                 GROUP BY t.album
                 ORDER BY t.album COLLATE NOCASE""",
                (udn, decade)).fetchall()
        return [dict(r) for r in rows]

    def artist_tracks(self, udn: str, artist: str) -> list:
        """All tracks by a given artist, with browse-side dedup applied
        (16-bit hidden when 24-bit exists). Sorted by album then title
        so a "Play all" of this list plays the artist's catalogue in a
        reasonable order. Used by the artist-view "Play all" button —
        replaces the prior search-and-filter hack."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, t.genre, 'audio' as type
                     FROM tracks t
                    WHERE t.udn = ? AND t.artist = ?
                      AND {dedup}
                 ORDER BY t.album COLLATE NOCASE,
                          t.title COLLATE NOCASE""",
                (udn, artist)).fetchall()
        return [dict(r) for r in rows]

    def decade_tracks(self, udn: str, decade: int) -> list:
        """Flat list of every track whose effective year falls in
        [decade, decade+10)."""
        dedup = _dedup_clause("t")
        eff = self._EFFECTIVE_YEAR
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, t.genre, 'audio' as type
                     FROM tracks t
                LEFT JOIN metadata_overrides m ON m.url = t.url
                    WHERE t.udn = ?
                      AND ({eff} / 10) * 10 = ?
                      AND {dedup}
                 ORDER BY t.artist COLLATE NOCASE,
                          t.album COLLATE NOCASE,
                          t.title COLLATE NOCASE""",
                (udn, decade)).fetchall()
        return [dict(r) for r in rows]

    # ── Metadata editing ─────────────────────────────────────────

    # Sentinel distinguishing "year omitted" from "year=None (clear it)".
    _YEAR_UNSET = object()

    def update_track_meta(self, url: str,
                          artist: str = None, album: str = None,
                          title: str = None, genre: str = None,
                          year=_YEAR_UNSET) -> bool:
        """
        Update artist/album/title/genre/year for a track in the DB.
        For string fields, only provided (non-None) values are changed.
        For year: pass `_YEAR_UNSET` (the default) to leave untouched,
        an int to set, or None to clear the override.
        All edits land in metadata_overrides with source='manual' so
        they survive re-index. Artist/album/title/genre are also
        pushed onto the live tracks row; year is NOT — tracks.year
        stays the file-tag year, the user's year sets the override.
        Returns True if any row was updated.
        """
        str_fields = {k: v for k, v in
                      [("artist", artist), ("album", album),
                       ("title", title), ("genre", genre)]
                      if v is not None}
        year_touched = year is not self._YEAR_UNSET
        if not str_fields and not year_touched:
            return False
        with self._pool.write() as conn:
            # 1. Live tracks row: only string fields go here (year on
            # tracks is the file-tag year, untouched by user edits).
            if str_fields:
                set_clause = ", ".join(f"{k}=?" for k in str_fields)
                conn.execute(
                    f"UPDATE tracks SET {set_clause} WHERE url=?",
                    list(str_fields.values()) + [url])
            # 2. metadata_overrides upsert (single source of truth).
            existing = conn.execute(
                "SELECT artist, album, title, genre, year "
                "FROM metadata_overrides WHERE url=?", (url,)).fetchone()
            if existing:
                merged = dict(existing)
                merged.update(str_fields)
                if year_touched:
                    merged["year"] = year
                conn.execute(
                    "UPDATE metadata_overrides "
                    "SET artist=?, album=?, title=?, genre=?, year=?, "
                    "    source='manual', updated_at=datetime('now') "
                    "WHERE url=?",
                    (merged["artist"], merged["album"], merged["title"],
                     merged["genre"], merged["year"], url))
            else:
                # Fill blanks from current track record
                row = conn.execute(
                    "SELECT artist, album, title, genre FROM tracks WHERE url=?",
                    (url,)).fetchone()
                base = dict(row) if row else {"artist":"","album":"","title":"","genre":""}
                base.update(str_fields)
                yr = year if year_touched else None
                conn.execute(
                    "INSERT INTO metadata_overrides "
                    "(url, artist, album, title, genre, year, source) "
                    "VALUES (?,?,?,?,?,?, 'manual')",
                    (url, base["artist"], base["album"],
                     base["title"], base["genre"], yr))

            changed = conn.execute("SELECT changes()").fetchone()[0]
        return changed > 0

    def get_track_file_path(self, url: str) -> str:
        """Return stored file_path for a track URL, or empty string."""
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT file_path FROM tracks WHERE url=?", (url,)).fetchone()
        return (row["file_path"] or "") if row else ""

    # ── AcoustID metadata enrichment (used by dlna_acoustid) ──────

    def bare_metadata_tracks(self, winners_only: bool = False) -> list:
        """Tracks that have no `metadata_overrides` row of any source
        (including 'notfound' sticky negatives). Mirrors the contract
        of LoudnessScanner.bare_tracks / AlbumArtFetcher.bare_albums:
        anything cached in any form is excluded so the worker doesn't
        re-hit the AcoustID API for tracks it already processed.

        With `winners_only=True`, also filters by `_dedup_clause` so a
        16-bit duplicate of a 24-bit track doesn't get its own fpcalc +
        AcoustID + MB pass. The worker calls with True; the override
        is then propagated to lower-quality siblings via SQL after the
        main run completes (see `propagate_overrides_to_siblings`).
        Saves ~7 hours of work on a hi-res-heavy library.

        Returns a list of (url,) tuples. URLs only — the AcoustID
        lookup needs duration, but fpcalc reports it from the audio
        itself, so we don't carry the DB-side value through."""
        dedup_filter = f"AND {_dedup_clause('t')}" if winners_only else ""
        with self._pool.read() as conn:
            rows = conn.execute(f"""
                SELECT t.url
                  FROM tracks t
                 WHERE t.url != ''
                   AND NOT EXISTS (
                       SELECT 1 FROM metadata_overrides m WHERE m.url = t.url)
                   {dedup_filter}
                 GROUP BY t.url
                 ORDER BY t.id
            """).fetchall()
        return [(r["url"],) for r in rows]

    def propagate_overrides_to_siblings(self) -> int:
        """For each track WITHOUT a metadata_overrides row, find a
        higher-quality sibling (same udn+artist+album+title, higher
        bit_depth/sample_rate) that DOES have an override, and copy
        that override to the unprocessed sibling. INSERT OR IGNORE
        never touches existing rows.

        Returns the number of rows inserted.

        Pairs with `bare_metadata_tracks(winners_only=True)`: the
        worker processes only dedup winners, then this propagates the
        winner's metadata to the 16-bit / lower-sample-rate siblings
        so playlists / radio / Subsonic / direct URL lookups all see
        the same metadata regardless of which quality variant they
        happen to reference."""
        with self._pool.write() as conn:
            cur = conn.execute("""
                INSERT OR IGNORE INTO metadata_overrides
                    (url, artist, album, title, year, source)
                SELECT lower.url, m.artist, m.album, m.title, m.year, m.source
                  FROM tracks lower
                  JOIN tracks winner
                    ON winner.udn    = lower.udn
                   AND winner.artist = lower.artist
                   AND winner.album  = lower.album
                   AND winner.title  = lower.title
                   AND lower.url != winner.url
                   AND (   COALESCE(winner.bit_depth, 0)   >  COALESCE(lower.bit_depth, 0)
                        OR (    COALESCE(winner.bit_depth, 0)   = COALESCE(lower.bit_depth, 0)
                            AND COALESCE(winner.sample_rate, 0) > COALESCE(lower.sample_rate, 0)))
                  JOIN metadata_overrides m ON m.url = winner.url
                 WHERE NOT EXISTS (
                       SELECT 1 FROM metadata_overrides ml WHERE ml.url = lower.url)
            """)
            return cur.rowcount or 0

    def metadata_override_set(self, url: str, source: str,
                              artist: Optional[str] = None,
                              album: Optional[str] = None,
                              title: Optional[str] = None,
                              genre: Optional[str] = None,
                              year: Optional[int] = None,
                              update_tracks: bool = True) -> bool:
        """Write a metadata_overrides row from a non-user source (e.g.
        AcoustID match) AND apply it onto `tracks` so the UI sees it
        without waiting for the next re-index. Only non-None fields are
        carried through — a missing album stays as whatever `tracks`
        already had.

        `year` here is the **original release year** (MusicBrainz
        release-group's first-release-date); `tracks.year` is the
        file-tag/edition year. The two are kept as separate fields so
        the frontend can render "1987 (remastered)" when they differ.

        Use `metadata_override_mark_notfound()` for sticky negatives;
        this method is for positive matches only.

        Returns True if a row was written (always True for non-empty
        URLs)."""
        if not url:
            return False
        # Keep `year` separate from `fields` — it's stored in
        # metadata_overrides ONLY (it's the original release year), not
        # in `tracks` (where year means file-tag/edition year, a
        # different concept). The "push onto tracks" UPDATE below uses
        # `fields` which must NOT include year.
        fields = {k: v for k, v in
                  (("artist", artist), ("album", album),
                   ("title", title), ("genre", genre))
                  if v is not None and v != ""}
        override_year = year if (year is not None and year > 0) else None
        with self._pool.write() as conn:
            # Upsert into overrides preserving any non-overwritten fields.
            existing = conn.execute(
                "SELECT artist, album, title, genre, year FROM metadata_overrides "
                "WHERE url=?", (url,)).fetchone()
            if existing:
                merged = dict(existing)
                merged.update(fields)
                if override_year is not None:
                    merged["year"] = override_year
                conn.execute(
                    "UPDATE metadata_overrides "
                    "SET artist=?, album=?, title=?, genre=?, year=?, source=?, "
                    "    updated_at=datetime('now') WHERE url=?",
                    (merged["artist"], merged["album"], merged["title"],
                     merged["genre"], merged["year"], source, url))
            else:
                row = conn.execute(
                    "SELECT artist, album, title, genre FROM tracks WHERE url=?",
                    (url,)).fetchone()
                base = dict(row) if row else {"artist": "", "album": "",
                                              "title": "", "genre": ""}
                base.update(fields)
                conn.execute(
                    "INSERT INTO metadata_overrides "
                    "(url, artist, album, title, genre, year, source) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (url, base["artist"], base["album"], base["title"],
                     base["genre"], override_year, source))
            # Push the change onto tracks too — only the fields the caller
            # supplied; others are untouched. The Indexer's COALESCE pass
            # would do this on next re-index, but that may be weeks away.
            #
            # Catch UNIQUE(udn,artist,album,title) collisions: if two tracks
            # would end up with identical artist/album/title after this
            # update (e.g. AcoustID returned the same metadata for two
            # duplicate-uploads), the override row is still the source of
            # truth — we just can't push it onto `tracks` live. A future
            # re-index will skip it too (same constraint); user-visible
            # impact is that one of the duplicates keeps its old metadata
            # in the tracks list. Acceptable; flagged at WARN so it's
            # diagnosable.
            if fields and update_tracks:
                # The AcoustID worker passes update_tracks=False during
                # its bulk run so sibling-by-(artist,album,title) matching
                # in `propagate_overrides_to_siblings` works. After the
                # propagate, `sync_tracks_from_overrides` does the bulk
                # tracks update in one pass. For interactive user edits
                # (update_track_meta), keep the inline tracks push.
                set_clause = ", ".join(f"{k}=?" for k in fields)
                try:
                    conn.execute(
                        f"UPDATE tracks SET {set_clause} WHERE url=?",
                        list(fields.values()) + [url])
                except sqlite3.IntegrityError as e:
                    log.warning(
                        f"metadata_override_set: tracks UPDATE collided on "
                        f"UNIQUE(udn,artist,album,title) for {url[:80]} "
                        f"({e}); override row saved, tracks row unchanged")
        return True

    def sync_tracks_from_overrides(self) -> int:
        """Bulk-update every tracks row to match its metadata_overrides
        row (where one exists). Used by the AcoustID worker after a
        full pass to push override values onto tracks in one go — done
        separately from per-track override writes so siblings stay
        matchable by (artist, album, title) during the run.

        Mirrors the COALESCE UPDATE in upsert_tracks (uses OR IGNORE
        for the same UNIQUE-collision tolerance). Returns the number
        of rows affected."""
        with self._pool.write() as conn:
            cur = conn.execute("""
                UPDATE OR IGNORE tracks SET
                    artist = COALESCE((SELECT artist FROM metadata_overrides WHERE url=tracks.url), artist),
                    album  = COALESCE((SELECT album  FROM metadata_overrides WHERE url=tracks.url), album),
                    title  = COALESCE((SELECT title  FROM metadata_overrides WHERE url=tracks.url), title),
                    genre  = COALESCE((SELECT genre  FROM metadata_overrides WHERE url=tracks.url), genre)
                 WHERE url IN (SELECT url FROM metadata_overrides)
            """)
            return cur.rowcount or 0

    def metadata_override_mark_notfound(self, url: str) -> bool:
        """Write a sticky-negative row so the AcoustID worker doesn't
        retry this URL on every restart. INSERT OR IGNORE — never
        overwrites a real ('manual' / 'acoustid') override.

        Same convention as `album_art.source='notfound'` and
        `lyrics.source='notfound'`. To force a retry on one track:
            DELETE FROM metadata_overrides
             WHERE source='notfound' AND url='…'
        """
        if not url:
            return False
        with self._pool.write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO metadata_overrides "
                "(url, artist, album, title, genre, source) "
                "VALUES (?, NULL, NULL, NULL, NULL, 'notfound')", (url,))
        return (cur.rowcount or 0) > 0

    def gain_db_for_url(self, url: str) -> float:
        """Per-track loudness gain in dB. Returns 0.0 for unknown tracks
        (don't fail-fast — missing analysis just means no normalization
        applied yet)."""
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT gain_db FROM track_loudness WHERE url=?",
                (url,)).fetchone()
        return float(row["gain_db"]) if row and row["gain_db"] is not None else 0.0

    def track_meta_by_url(self, url: str):
        """Metadata for a single track, used by /api/track_meta and the
        Subsonic /rest/* methods. Returns both year fields separately:
          - `year`: file-tag year from DIDL-Lite (the edition you own)
          - `year_original`: MusicBrainz first-release-date year if the
            AcoustID worker has filled it in (the recording's original
            release year).
        Frontend prefers `year_original`; if it differs from `year` by
        more than 2 years, renders as e.g. '1987 (remastered)'."""
        with self._pool.read() as conn:
            row = conn.execute("""
                SELECT t.title, t.artist, t.album, t.duration, t.year,
                       m.year AS year_original
                  FROM tracks t
             LEFT JOIN metadata_overrides m ON m.url = t.url
                 WHERE t.url = ? LIMIT 1
            """, (url,)).fetchone()
        return dict(row) if row else None

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

    def genre_tracks(self, udn: str, genre: str) -> list:
        """All tracks in a genre, browse-visible (deduped)."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            rows = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, t.genre, 'audio' as type
                   FROM tracks t
                   WHERE t.udn=? AND t.genre=?
                     AND {dedup}
                   ORDER BY t.album COLLATE NOCASE, t.title COLLATE NOCASE""",
                (udn, genre)).fetchall()
        return [dict(r) for r in rows]

    def radio_tracks(self, udn: str, limit: int = 100) -> list:
        """Pick `limit` tracks for the Radio feature, biased toward those
        the user hasn't heard often — lowest play count wins, random
        tiebreak within a tier. Bumps each selected track's count in
        play_counts so the NEXT radio call picks different tracks.

        Rebuild-index doesn't touch play_counts, so play history persists
        forever (same invariant as album_art). The table is keyed by URL;
        if the upstream server ever renames URLs, those entries become
        orphaned and that track resets to count=0 — acceptable because
        this is a soft preference, not a correctness requirement."""
        with self._pool.write() as conn:
            rows = conn.execute(
                """SELECT t.obj_id AS id, t.url, t.title, t.artist, t.album,
                          t.duration, t.art, t.mime, 'audio' AS type
                     FROM tracks t
                     LEFT JOIN play_counts p ON p.url = t.url
                    WHERE t.udn = ?
                    ORDER BY COALESCE(p.count, 0) ASC, RANDOM()
                    LIMIT ?""",
                (udn, limit)).fetchall()
            # Bulk-increment the selected URLs so next radio call picks
            # fresher material. Using an UPSERT so brand-new URLs insert
            # at count=1 and existing URLs increment in place.
            conn.executemany(
                """INSERT INTO play_counts (url, count, last_played)
                   VALUES (?, 1, strftime('%s','now'))
                   ON CONFLICT(url) DO UPDATE
                      SET count = count + 1,
                          last_played = strftime('%s','now')""",
                [(r["url"],) for r in rows if r["url"]])
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

    # ── Album favourites ──────────────────────────────────────────
    # Distinct from the track-level Favourites playlist: these are
    # whole-album entries keyed by (artist, album) so they survive
    # clear(udn) and re-indexing. Same persistence pattern as
    # album_art, play_counts, lyrics, track_loudness.

    def album_fav_add(self, artist: str, album: str) -> bool:
        """Mark an album as favourite. Idempotent — re-adding is a no-op
        and doesn't bump added_at. Returns True if a new row was created."""
        if not album:
            return False
        with self._pool.write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO album_favourites "
                "(artist, album, added_at) VALUES (?,?,?)",
                (artist, album, int(time.time())))
        return cur.rowcount > 0

    def album_fav_remove(self, artist: str, album: str) -> bool:
        with self._pool.write() as conn:
            cur = conn.execute(
                "DELETE FROM album_favourites WHERE artist=? AND album=?",
                (artist, album))
        return cur.rowcount > 0

    def album_fav_is(self, artist: str, album: str) -> bool:
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM album_favourites "
                "WHERE artist=? AND album=? LIMIT 1",
                (artist, album)).fetchone()
        return row is not None

    def album_fav_list(self) -> list:
        """Return all favourited albums with art, track_count, and the
        UDN of a server that holds them. Albums with zero matching tracks
        across any server (e.g. server gone away) still appear so the user
        can prune them. Sorted most-recently-added first."""
        with self._pool.read() as conn:
            rows = conn.execute("""
                SELECT
                    f.artist,
                    f.album,
                    f.added_at,
                    COALESCE(aa.art_url, MAX(t.art), '') AS art,
                    COUNT(t.id)                          AS track_count,
                    COALESCE(MAX(t.udn), '')             AS udn
                FROM album_favourites f
                LEFT JOIN tracks t
                       ON t.artist = f.artist AND t.album = f.album
                LEFT JOIN album_art aa
                       ON aa.artist = f.artist AND aa.album = f.album
                GROUP BY f.artist, f.album
                ORDER BY f.added_at DESC
            """).fetchall()
        return [dict(r) for r in rows]

    # ── Internet-radio favourites ────────────────────────────────
    # Saved stations from radio-browser.info, capped at RADIO_FAV_MAX.
    # Identity = station_uuid so they survive clear(udn) / re-indexing,
    # same persistence pattern as album_favourites. The radio-browser
    # catalogue itself is never stored — only the user's favourites.

    RADIO_FAV_MAX = 25

    def radio_fav_count(self) -> int:
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM radio_favourites").fetchone()
        return row["n"] if row else 0

    def radio_fav_is(self, station_uuid: str) -> bool:
        if not station_uuid:
            return False
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM radio_favourites WHERE station_uuid=? LIMIT 1",
                (station_uuid,)).fetchone()
        return row is not None

    def radio_fav_add(self, station: dict) -> str:
        """Add a station to favourites. Returns one of:
          'ok'     — new row created
          'exists' — already favourited (idempotent no-op)
          'full'   — at RADIO_FAV_MAX and this is a NEW station
          'bad'    — missing station_uuid / name / stream_url

        The 25-cap is enforced HERE, server-side — never trust the
        client. Re-adding an existing favourite is always allowed and
        never counts against the cap.
        """
        uuid   = (station.get("station_uuid") or "").strip()
        name   = (station.get("name") or "").strip()
        stream = (station.get("stream_url") or "").strip()
        if not uuid or not name or not stream:
            return "bad"
        if self.radio_fav_is(uuid):
            return "exists"
        if self.radio_fav_count() >= self.RADIO_FAV_MAX:
            return "full"
        try:
            bitrate = int(station.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        with self._pool.write() as conn:
            # New favourites land last in the preset order.
            row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 "
                               "AS nxt FROM radio_favourites").fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO radio_favourites "
                "(station_uuid, name, stream_url, homepage, favicon, "
                " codec, bitrate, country, tags, added_at, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uuid, name, stream,
                 station.get("homepage") or "",
                 station.get("favicon")  or "",
                 station.get("codec")    or "",
                 bitrate,
                 station.get("country")  or "",
                 station.get("tags")     or "",
                 int(time.time()), row["nxt"] if row else 0))
        return "ok"

    def radio_fav_remove(self, station_uuid: str) -> bool:
        with self._pool.write() as conn:
            cur = conn.execute(
                "DELETE FROM radio_favourites WHERE station_uuid=?",
                (station_uuid,))
        return cur.rowcount > 0

    def radio_fav_list(self) -> list:
        """All favourited stations, ordered by sort_order then added_at."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT station_uuid, name, stream_url, homepage, favicon, "
                "       codec, bitrate, country, tags, added_at, sort_order "
                "FROM radio_favourites "
                "ORDER BY sort_order ASC, added_at ASC").fetchall()
        return [dict(r) for r in rows]

    def radio_fav_reorder(self, uuid_list: list) -> bool:
        """Persist a new preset ordering: each UUID's sort_order is set
        to its index in uuid_list. UUIDs not in the favourites table are
        silently ignored; favourites not named keep their old order
        value (so they sort after the listed ones if those start at 0)."""
        if not uuid_list:
            return False
        with self._pool.write() as conn:
            for i, uuid in enumerate(uuid_list):
                conn.execute(
                    "UPDATE radio_favourites SET sort_order=? "
                    "WHERE station_uuid=?", (i, uuid))
        return True

    def repair_fts(self) -> None:
        """Drop, recreate, and rebuild ``tracks_fts`` from the ``tracks``
        table — used to recover from FTS5 shadow-table corruption (the
        recurring ``database disk image is malformed`` error in
        ``clear()`` / ``upsert_tracks()`` that ``PRAGMA integrity_check``
        does NOT catch because it only validates B-trees, not FTS5
        internals).

        Track rows are not touched. ``tracks_fts`` is an external-content
        FTS table (``content=tracks``), so a full rebuild produces an
        identical index from the live row data. The ``tracks_ai`` /
        ``tracks_ad`` triggers reference ``tracks_fts`` by name and keep
        working after the recreate.
        """
        with self._pool.write() as conn:
            conn.execute("DROP TABLE IF EXISTS tracks_fts")
            conn.execute(
                "CREATE VIRTUAL TABLE tracks_fts USING fts5("
                "title, artist, album, content=tracks, content_rowid=id, "
                "tokenize='unicode61 remove_diacritics 1')")
            conn.execute("INSERT INTO tracks_fts(tracks_fts) "
                         "VALUES('rebuild')")
        log.warning("LibraryDB.repair_fts: tracks_fts dropped, recreated, "
                    "and rebuilt from tracks")

    def radio_fav_update(self, station_uuid: str, *, name: str = None,
                         stream_url: str = None,
                         homepage: str = None) -> bool:
        """Update an existing favourite's editable fields — backs
        Subsonic's updateInternetRadioStation. Only non-None arguments
        are written. Returns True if a row was changed."""
        sets, vals = [], []
        if name is not None:
            sets.append("name=?");       vals.append(name)
        if stream_url is not None:
            sets.append("stream_url=?"); vals.append(stream_url)
        if homepage is not None:
            sets.append("homepage=?");   vals.append(homepage)
        if not sets:
            return False
        vals.append(station_uuid)
        with self._pool.write() as conn:
            cur = conn.execute(
                f"UPDATE radio_favourites SET {', '.join(sets)} "
                f"WHERE station_uuid=?", vals)
        return cur.rowcount > 0


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
from dlna_loudness     import LoudnessScanner
from dlna_acoustid     import AcoustIDFetcher

DEVICE_ROLES     = DeviceRoleCache(DB)
INDEXER          = Indexer(DB)
ART_FETCHER      = AlbumArtFetcher(DB)
LOUDNESS_SCANNER = LoudnessScanner(DB)
ACOUSTID_FETCHER = AcoustIDFetcher(DB, api_key=os.environ.get("ACOUSTID_API_KEY"))


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