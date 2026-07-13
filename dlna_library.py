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


def _is_localfs(udn: str) -> bool:
    """LocalFs sources own a `file_path` per row and a populated
    `album_key`, so their album browse groups by FOLDER. Everything else
    (UPnP / Subsonic-fed) keeps the legacy (artist, album) grouping."""
    return udn.startswith("uuid:localfs-")


def _localfs_album_name(a: str = "t") -> str:
    """SQL aggregate expression for a folder-grouped album's DISPLAY name:
    the album tag when the folder is tag-consistent (normal albums), else
    the folder's own leaf name (Various-Artists comps where every track
    carries its original album tag). The leaf is the segment of `album_key`
    after the last '/' — `rtrim(path, replace(path,'/',''))` strips back to
    and including the last slash, which `replace` then removes."""
    return (f"CASE WHEN COUNT(DISTINCT {a}.album)=1 THEN MAX({a}.album) "
            f"ELSE replace({a}.album_key, "
            f"rtrim({a}.album_key, replace({a}.album_key,'/','')), '') END")


def _localfs_album_artist(a: str = "t") -> str:
    """SQL aggregate: 'Various Artists' when a folder spans >1 performer,
    else the single performer."""
    return (f"CASE WHEN COUNT(DISTINCT {a}.artist)>1 THEN 'Various Artists' "
            f"ELSE MAX({a}.artist) END")


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
                -- album_key (the LocalFs FOLDER identity) joined the UNIQUE
                -- 2026-07-12: two DISTINCT files in different folders with
                -- identical tags (duplicate editions, scene comps sharing
                -- tag tuples) are both real and both indexable. UPnP rows
                -- keep album_key='' so their dedup semantics are unchanged.
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
                    album_key   TEXT DEFAULT '',
                    UNIQUE(udn, artist, album, title, album_key, bit_depth, sample_rate)
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
                    album_key  TEXT NOT NULL DEFAULT '',
                    added_at   INTEGER NOT NULL,
                    PRIMARY KEY (artist, album, album_key)
                );
                -- Audiobook resume positions (2026-07-13). One row per
                -- BOOK (album_key = its folder): which chapter file was
                -- playing and how far in. Independent of `tracks` so it
                -- survives clear(udn)/re-index — same contract as
                -- play_counts / lyrics / album_art. finished=1 → the PWA
                -- offers "start over"; any normal save clears it. A
                -- renamed book folder orphans its row harmlessly.
                CREATE TABLE IF NOT EXISTS playback_positions (
                    album_key    TEXT PRIMARY KEY,
                    url          TEXT NOT NULL,
                    position_sec REAL NOT NULL,
                    duration_sec REAL,
                    finished     INTEGER NOT NULL DEFAULT 0,
                    updated_at   INTEGER NOT NULL
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
                -- LocalFs scanner's per-file cache (Phase 2 of the AssetUPnP
                -- migration). Lets incremental rescans skip unchanged files.
                -- Path is the absolute path on disk. Stable obj_id is
                -- sha1(rel_path)[:16] — survives renumbering across rescans
                -- the way AssetUPnP's d-id never did.
                CREATE TABLE IF NOT EXISTS localfs_files (
                    path         TEXT PRIMARY KEY,
                    mtime        REAL    NOT NULL,
                    size         INTEGER NOT NULL,
                    track_id     TEXT    NOT NULL,
                    last_scanned INTEGER NOT NULL
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
                -- tracks_au (AFTER UPDATE FTS sync) is created by
                -- _migrate_fts_update_trigger below — kept out of this
                -- script so the migration can detect first-run and do a
                -- one-time FTS rebuild on existing DBs.

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

                -- Video library (V1) — SEPARATE from audio `tracks`. Populated
                -- by the scan over LOCALFS_VIDEO_ROOT (/Volumes/SAMDATA/GWMovies)
                -- under its own udn (uuid:localfs-movies). id = sha1(rel_path)[:16]
                -- (path-stable). Deliberately kept out of the audio browse + the
                -- Naim's UPnP tree. `title` = embedded title or the constructed
                -- <place>_YYYYMMDD_HHMM.ext; `location` = raw GPS (ISO6709),
                -- `location_name` = geocoded place; `created` = capture time
                -- (or mtime fallback); `poster` = extracted-frame id (nullable).
                CREATE TABLE IF NOT EXISTS videos (
                    id            TEXT PRIMARY KEY,
                    udn           TEXT NOT NULL,
                    url           TEXT NOT NULL,
                    title         TEXT NOT NULL,
                    file_path     TEXT NOT NULL,
                    folder        TEXT DEFAULT '',
                    duration      REAL,
                    width         INTEGER,
                    height        INTEGER,
                    vcodec        TEXT,
                    acodec        TEXT,
                    container     TEXT,
                    mime          TEXT,
                    size          INTEGER,
                    mtime         REAL,
                    created       TEXT,
                    location      TEXT,
                    location_name TEXT,
                    country       TEXT,
                    poster        TEXT,
                    added_at      INTEGER NOT NULL
                );

                -- Reverse-geocode cache: GPS coords -> place name, keyed by
                -- ROUNDED coords (~111 m at 3 dp) so each place is fetched from
                -- Nominatim once, ever. Sticky like album_art: place='' means
                -- "looked up, no name" (don't re-query). Survives clear/rebuild.
                CREATE TABLE IF NOT EXISTS geocode_cache (
                    lat_key    REAL NOT NULL,
                    lon_key    REAL NOT NULL,
                    place      TEXT,
                    country    TEXT,
                    fetched_at INTEGER NOT NULL,
                    PRIMARY KEY (lat_key, lon_key)
                );

                -- Inferred/manual locations for GPS-less videos (Plan A,
                -- 2026-07-07). The scanner derives `videos` rows from file
                -- metadata and these files have NO GPS, so a force rescan
                -- would wipe an inferred location — the override is the
                -- durable copy, re-applied at the end of every video scan
                -- (dlna_video_index.apply_location_overrides). Keyed by the
                -- path-stable video id; survives clear_videos like
                -- album_art / play_counts / lyrics. 'manual' beats any
                -- 'inferred_*' source. location_name '' = country-only.
                CREATE TABLE IF NOT EXISTS video_location_overrides (
                    video_id      TEXT PRIMARY KEY,
                    location_name TEXT,
                    country       TEXT,
                    source        TEXT NOT NULL,
                    updated_at    INTEGER NOT NULL
                );

                -- Persons recognised by Immich (Plan B, 2026-07-07),
                -- synced from its REST API by tools/immich_people_sync.py
                -- (Immich keeps face data in ITS Postgres, never in the
                -- files — checksum-matched to our videos). person_id =
                -- the Immich person uuid. Survives clear_videos like
                -- video_location_overrides; keyed by the path-stable
                -- video id.
                CREATE TABLE IF NOT EXISTS video_people (
                    video_id   TEXT NOT NULL,
                    person     TEXT NOT NULL,
                    person_id  TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (video_id, person)
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
            # 2026-05-31: folder-based album identity for LocalFs. The
            # per-track `artist` is the performer, so an artist-keyed
            # browse fragments a compilation into one album per performer.
            # album_key = the track's containing folder (disc subfolders
            # folded), relative to the music root — written by
            # LocalFsProvider so the browse layer can group an album by
            # its folder rather than by (artist, album). Empty for UPnP
            # rows (no file_path) and root-level loose files.
            "ALTER TABLE tracks ADD COLUMN album_key TEXT DEFAULT ''",
            # 2026-07-06: video titles are country_location_date_time — the
            # geocode cache learns the ISO country code. NULL = pre-migration
            # row (the geocoder upgrades it with ONE re-fetch on next use);
            # '' = fetched, no country (sticky).
            "ALTER TABLE geocode_cache ADD COLUMN country TEXT",
            # 2026-07-06 v2: country on the video row itself so the By
            # location browse can group country → location (titles alone
            # are too fragile to group by). Backfilled from geocode_cache.
            "ALTER TABLE videos ADD COLUMN country TEXT",
        ]:
            try:
                with self._pool.write() as conn:
                    conn.execute(col_sql)
                log.info(f"DB migration: {col_sql[:60]}")
            except Exception:
                pass  # column already exists
        # Loudness normalization removed (2026-05-31): peak-mode gain gave
        # negligible perceptual benefit, was already disabled in the
        # playback path, and broke bit-perfect on the browser path. Drop
        # the now-unused measurements table. Idempotent.
        try:
            with self._pool.write() as conn:
                conn.execute("DROP TABLE IF EXISTS track_loudness")
            log.info("DB migration: dropped track_loudness "
                     "(loudness normalization removed)")
        except Exception:
            pass
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
        # 2026-07-12: widen the tracks UNIQUE with album_key so distinct
        # files in different folders with identical tags coexist (the
        # completeness-audit fix). Table rebuild; drops all FTS triggers.
        with self._pool.write() as conn:
            self._migrate_widen_unique_album_key(conn)
        # 2026-07-12: AFTER UPDATE FTS sync trigger. Must run AFTER the
        # table-rebuild migrations above (they only recreate ai/ad; on a
        # legacy DB the trigger doesn't exist yet when they run).
        with self._pool.write() as conn:
            self._migrate_fts_update_trigger(conn)
        # Ensure Favourites always exists
        with self._pool.write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO playlists (id, name, sort_order) "
                "VALUES (?,?,?)",
                (FAVOURITES_ID, "⭐ Favourites", -1))
            self._migrate_json(conn)
            self._migrate_device_roles(conn)
            self._migrate_album_fav_key(conn)
            # Covering index for the folder-grouped Albums browse query
            # (GROUP BY album_key within a udn). Removes the TEMP B-TREE the
            # GROUP BY otherwise builds — roughly halves the cold
            # /api/browse_letter albums query (~150 ms → ~70 ms). Created
            # here (after the table-rebuild migrations, so it isn't dropped)
            # and idempotent. NB: regenerate schema.sql when this changes —
            # tests/test_schema_sync.py enforces it.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tracks_udn_album_key "
                         "ON tracks(udn, album_key)")
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
                album_key   TEXT DEFAULT '',
                UNIQUE(udn, artist, album, title, bit_depth, sample_rate)
            )
        """)
        inserts = []
        for r in old_rows:
            bd, sr = _parse_audio_params(r["url"])
            # album_key may predate this row if the column-add migration
            # ran first (it normally does); carry it, default '' otherwise.
            ak = r["album_key"] if "album_key" in r.keys() else ""
            inserts.append((r["id"], r["udn"], r["obj_id"], r["url"],
                            r["title"], r["artist"], r["album"],
                            r["duration"], r["art"], r["mime"],
                            r["genre"], r["file_path"], bd, sr, ak))
        conn.executemany(
            "INSERT OR IGNORE INTO tracks "
            "(id, udn, obj_id, url, title, artist, album, duration, art, "
            " mime, genre, file_path, bit_depth, sample_rate, album_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", inserts)
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

    _TRACK_COLS = ("id, udn, obj_id, url, title, artist, album, duration, "
                   "art, mime, genre, file_path, bit_depth, sample_rate, "
                   "year, album_key")

    def _migrate_widen_unique_album_key(self, conn: sqlite3.Connection):
        """2026-07-12: widen the tracks UNIQUE to include `album_key`.

        The 2026-07-12 completeness audit found 236 on-disk files with NO
        tracks row: the old UNIQUE(udn, artist, album, title, bit_depth,
        sample_rate) swallowed any file whose tag tuple collided with a
        file in ANOTHER folder — duplicate-edition folders (deluxe vs
        standard) and scene compilations sharing tag tuples. Those are
        genuinely distinct files; for LocalFs every physical file deserves
        a row (per-file URL uniqueness is idx_tracks_udn_url's job, and
        browse-level dupe hiding is _dedup_clause's job — the UNIQUE was
        doing display work at the wrong layer). Adding the FOLDER identity
        to the UNIQUE admits them; UPnP rows carry album_key='' so their
        collision semantics are byte-identical to before.

        Table-rebuild migration, same shape as _migrate_widen_tracks_unique.
        Detects by inspecting the UNIQUE clause in sqlite_master (the
        album_key COLUMN exists on old DBs — only the constraint changes).
        Recreates idx_tracks_udn_url + idx_tracks_udn_album_key (dropped
        with the old table) and the ai/ad triggers; tracks_au is left to
        _migrate_fts_update_trigger, which runs right after and does the
        FTS rebuild on (re)creating it."""
        sql_row = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='tracks'").fetchone()
        if not sql_row:
            return
        m = re.search(r"UNIQUE\s*\(([^)]*)\)", sql_row[0] or "")
        if m and "album_key" in m.group(1):
            return  # already widened (or fresh DB)

        n_old = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        log.info(f"DB migration: widening tracks UNIQUE with album_key; "
                 f"rebuilding {n_old:,} row(s)")

        conn.execute("DROP TRIGGER IF EXISTS tracks_ai")
        conn.execute("DROP TRIGGER IF EXISTS tracks_ad")
        conn.execute("DROP TRIGGER IF EXISTS tracks_au")
        conn.execute("ALTER TABLE tracks RENAME TO _tracks_pre_akwiden")
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
                year        INTEGER,
                album_key   TEXT DEFAULT '',
                UNIQUE(udn, artist, album, title, album_key, bit_depth, sample_rate)
            )
        """)
        # Column list built against what the OLD table actually has: a DB
        # that just went through _migrate_widen_tracks_unique in this same
        # boot is missing `year` (that rebuild predates the column and
        # drops it; the ALTER loop re-adds it only on the NEXT boot).
        old_cols = {r[1] for r in
                    conn.execute("PRAGMA table_info(_tracks_pre_akwiden)")}
        # album_key backfills as '' (NULLs are DISTINCT in UNIQUE — a NULL
        # would exempt those rows from collision checks entirely).
        select_cols = ", ".join(
            c if c in old_cols
            else ("'' AS album_key" if c == "album_key" else f"NULL AS {c}")
            for c in self._TRACK_COLS.split(", "))
        conn.execute(
            f"INSERT OR IGNORE INTO tracks ({self._TRACK_COLS}) "
            f"SELECT {select_cols} FROM _tracks_pre_akwiden")
        conn.execute("DROP TABLE _tracks_pre_akwiden")
        # Indexes were attached to the renamed table — recreate both.
        # (_migrate_unique_url already no-opped this boot on seeing the old
        # index, and the album_key index's IF NOT EXISTS runs later; doing
        # them here keeps this migration self-contained.)
        conn.execute(
            "CREATE UNIQUE INDEX idx_tracks_udn_url ON tracks(udn, url)")
        conn.execute(
            "CREATE INDEX idx_tracks_udn_album_key ON tracks(udn, album_key)")
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

        n_new = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        log.info(f"DB migration: tracks UNIQUE widened with album_key — "
                 f"{n_old:,} → {n_new:,} rows, FTS5 rebuilt")

    def _migrate_fts_update_trigger(self, conn: sqlite3.Connection):
        """2026-07-12: add `tracks_au`, the AFTER UPDATE FTS sync trigger.

        The original schema had only tracks_ai/tracks_ad, so ANY in-place
        UPDATE of title/artist/album left tracks_fts stale until the next
        full rebuild — the metadata-refresh pass in upsert_tracks (the
        retagged-file fix this trigger ships with), the overrides COALESCE
        pass, and metadata_override_set's live push all desynced it.
        Scoped to the three indexed columns so genre/art updates and the
        LocalFs URL-heal don't churn FTS.

        One-time on existing DBs: rebuilds FTS after creating the trigger
        to flush any desync accumulated before it existed — an external-
        content 'delete' whose old values don't match the indexed ones
        corrupts the index rather than cleaning it, so the trigger must
        start from a known-good state. Idempotent (detects by name)."""
        if conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND name='tracks_au'").fetchone():
            return
        conn.execute("""
            CREATE TRIGGER tracks_au
                AFTER UPDATE OF title, artist, album ON tracks BEGIN
                    INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                    VALUES ('delete', old.id, old.title, old.artist, old.album);
                    INSERT INTO tracks_fts(rowid, title, artist, album)
                    VALUES (new.id, new.title, new.artist, new.album);
                END
        """)
        conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")
        log.info("DB migration: added tracks_au FTS update trigger; "
                 "FTS5 rebuilt")

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

    def _migrate_album_fav_key(self, conn: sqlite3.Connection):
        """Add `album_key` to album_favourites and widen the PK to
        (artist, album, album_key) so a LocalFs compilation can be
        favourited by FOLDER. Many comps share artist='Various Artists'
        plus a repeatable display name, so (artist, album) alone would
        collide. Idempotent: detects the column's absence; rebuilds the
        table (it's tiny), carrying existing rows forward with
        album_key=''. SQLite can't ALTER a PRIMARY KEY in place, hence the
        rebuild."""
        cols = {row[1] for row in
                conn.execute("PRAGMA table_info(album_favourites)")}
        if not cols or "album_key" in cols:
            return  # fresh DB already has the new shape, or already migrated
        conn.executescript("""
            CREATE TABLE album_favourites_new (
                artist     TEXT NOT NULL,
                album      TEXT NOT NULL,
                album_key  TEXT NOT NULL DEFAULT '',
                added_at   INTEGER NOT NULL,
                PRIMARY KEY (artist, album, album_key)
            );
            INSERT OR IGNORE INTO album_favourites_new
                (artist, album, album_key, added_at)
                SELECT artist, album, '', added_at FROM album_favourites;
            DROP TABLE album_favourites;
            ALTER TABLE album_favourites_new RENAME TO album_favourites;
        """)
        conn.commit()
        log.info("DB migration: album_favourites gained album_key "
                 "(PK widened to artist, album, album_key)")

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
        """Number of albums for the source's browse view. LocalFs counts
        distinct FOLDERS (album_key) — matching the folder-grouped Albums
        list; other sources count distinct (artist, album) pairs, matching
        AssetUPnP's display count."""
        with self._pool.read() as conn:
            if _is_localfs(udn):
                row = conn.execute(
                    "SELECT COUNT(DISTINCT album_key) FROM tracks "
                    "WHERE udn=? AND album_key != ''", (udn,)).fetchone()
            else:
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
            # bit_depth + sample_rate: prefer caller-supplied values
            # (LocalFsProvider reads them straight from the audio
            # container via mutagen) and fall back to the AssetUPnP
            # URL-pattern parser. UPnP items don't have the fields →
            # URL parse; LocalFs items do → mutagen wins.
            bd_in = t.get("bit_depth")
            sr_in = t.get("sample_rate")
            bd_url, sr_url = _parse_audio_params(url)
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
                bit_depth=bd_in if bd_in is not None else bd_url,
                sample_rate=sr_in if sr_in is not None else sr_url,
                year=t.get("year"),
                album_key=t.get("album_key", ""),
            )
        rows_raw = [_make_row(t) for t in tracks if t.get("url")]
        # Mass INSERTs fire the FTS triggers; heal-and-retry on the
        # recurring shadow-table corruption. Body is retry-safe
        # (INSERT OR IGNORE / UPDATE OR IGNORE throughout).
        return self.run_with_fts_heal(self._upsert_tracks_body, udn, rows_raw)

    def _upsert_tracks_body(self, udn: str, rows_raw: list) -> int:
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
                " mime, genre, file_path, bit_depth, sample_rate, year, "
                " album_key) "
                "VALUES (:udn,:obj_id,:url,:title,:artist,:album,:duration,"
                "        :art,:mime,:genre,:file_path,:bit_depth,:sample_rate,"
                "        :year,:album_key)",
                rows)
            inserted = conn.execute("SELECT changes()").fetchone()[0]
            # Step 2a: refresh metadata on already-indexed URLs. Step 1's
            # INSERT OR IGNORE swallows rows whose URL already exists, and
            # before 2026-07-12 only genre/art were then patched — so
            # in-place retagging (beets) was invisible to any rescan and
            # the workaround was DELETE FROM tracks + rebuild. LocalFs
            # URLs are path-based (sha1(rel_path)), stable across retags,
            # so (udn, url) is the right key. The change-guard keeps this
            # a no-op for untouched rows (no FTS trigger churn on a force
            # rescan); IS NOT is the null-safe comparison for the
            # nullable year/bit_depth/sample_rate. Incoming-empty genre
            # and art never blank an existing value (art may have been
            # backfilled from album_art; genre from an override). OR
            # IGNORE tolerates the wide-UNIQUE collision — same trade-off
            # as the overrides pass below: the colliding row keeps its
            # old metadata.
            refresh_cur = conn.executemany(
                "UPDATE OR IGNORE tracks SET "
                "  obj_id=:obj_id, title=:title, artist=:artist, "
                "  album=:album, duration=:duration, mime=:mime, "
                "  year=:year, bit_depth=:bit_depth, "
                "  sample_rate=:sample_rate, album_key=:album_key, "
                "  file_path=:file_path, "
                "  genre = CASE WHEN :genre != '' THEN :genre ELSE genre END, "
                "  art   = CASE WHEN :art   != '' THEN :art   ELSE art   END "
                "WHERE udn=:udn AND url=:url "
                "  AND (obj_id IS NOT :obj_id OR title IS NOT :title "
                "       OR artist IS NOT :artist OR album IS NOT :album "
                "       OR duration IS NOT :duration OR mime IS NOT :mime "
                "       OR year IS NOT :year OR bit_depth IS NOT :bit_depth "
                "       OR sample_rate IS NOT :sample_rate "
                "       OR album_key IS NOT :album_key "
                "       OR file_path IS NOT :file_path "
                "       OR (:genre != '' AND genre IS NOT :genre) "
                "       OR (:art != '' AND art IS NOT :art))",
                rows)
            refreshed = max(refresh_cur.rowcount, 0)
            if refreshed:
                log.info(f"upsert_tracks [{udn[:12]}…]: refreshed metadata "
                         f"on {refreshed} existing row(s)")
            # Step 2b: update genre + art on already-indexed tracks keyed
            # by identity (covers the UPnP case where the same file
            # arrives via a different URL; only fills empty genre)
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

        The mass DELETE fires the FTS delete triggers row-by-row — on a
        corrupted tracks_fts that raises "database disk image is
        malformed" before the rebuild line is ever reached (the 5th/6th
        real-world occurrences, 2026-07-03). Routed through
        run_with_fts_heal so the corruption self-heals.
        """
        self.run_with_fts_heal(self._clear_body, udn)
        log.info(f"Track index cleared for {udn}")

    def _clear_body(self, udn: str):
        with self._pool.write() as conn:
            conn.execute("DELETE FROM tracks WHERE udn=?", (udn,))
            conn.execute("DELETE FROM index_meta WHERE udn=?", (udn,))
            conn.execute("INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild')")

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

    # ── Video library (V1) ────────────────────────────────────────
    # Separate from `tracks` — populated by the GWMovies scan, never mixed
    # into the audio browse / the Naim's UPnP tree.
    _VIDEO_COLS = ("id", "udn", "url", "title", "file_path", "folder",
                   "duration", "width", "height", "vcodec", "acodec",
                   "container", "mime", "size", "mtime", "created",
                   "location", "location_name", "country", "poster")

    def upsert_videos(self, udn: str, rows: list) -> int:
        """Insert/replace video rows (keyed by id). Returns rows written."""
        if not rows:
            return 0
        cols = self._VIDEO_COLS
        placeholders = ", ".join("?" * len(cols))
        sql = (f"INSERT OR REPLACE INTO videos ({', '.join(cols)}, added_at) "
               f"VALUES ({placeholders}, strftime('%s','now'))")
        n = 0
        with self._pool.write() as conn:
            for r in rows:
                r = {**r, "udn": r.get("udn", udn)}
                conn.execute(sql, [r.get(c) for c in cols])
                n += 1
        return n

    def all_videos(self, udn: str) -> list:
        """All videos for a udn, newest capture first."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE udn=? "
                "ORDER BY created DESC, title COLLATE NOCASE", (udn,)).fetchall()
        return [dict(r) for r in rows]

    def video_by_id(self, vid: str):
        with self._pool.read() as conn:
            r = conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        return dict(r) if r else None

    # ── video date/location browse (DLNA sub-containers for the LG) ──
    # `created` is an ISO timestamp string, so substr() gives the year
    # (1,4) and month (1,7) buckets directly. Videos without a created
    # date are absent from the date tree (still reachable via location
    # + the flat list).

    def video_years(self, udn: str) -> list:
        """[{year, count}] newest first."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT substr(created,1,4) AS year, COUNT(*) AS count "
                "FROM videos WHERE udn=? AND created != '' "
                "GROUP BY year ORDER BY year DESC", (udn,)).fetchall()
        return [dict(r) for r in rows]

    def video_months(self, udn: str, year: str) -> list:
        """[{month: 'YYYY-MM', count}] newest first, within one year."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT substr(created,1,7) AS month, COUNT(*) AS count "
                "FROM videos WHERE udn=? AND substr(created,1,4)=? "
                "GROUP BY month ORDER BY month DESC", (udn, year)).fetchall()
        return [dict(r) for r in rows]

    def videos_by_month(self, udn: str, month: str) -> list:
        """One month's videos ('YYYY-MM'), newest capture first."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE udn=? AND substr(created,1,7)=? "
                "ORDER BY created DESC, title COLLATE NOCASE",
                (udn, month)).fetchall()
        return [dict(r) for r in rows]

    def video_countries(self, udn: str) -> list:
        """[{country, count}] A-Z by ISO code; '' (located, country unknown)
        counts only located videos. A country-only video (country set,
        location empty — Plan A inference) counts under its country; videos
        with NEITHER belong to the top-level "(no location)" bucket."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT COALESCE(country, '') AS country, COUNT(*) AS count "
                "FROM videos WHERE udn=? "
                "AND (COALESCE(location_name, '') != '' "
                "     OR COALESCE(country, '') != '') "
                "GROUP BY COALESCE(country, '') "
                "ORDER BY (COALESCE(country, '') = ''), country",
                (udn,)).fetchall()
        return [dict(r) for r in rows]

    def video_locations_for_country(self, udn: str, country: str) -> list:
        """One country's locations ('' = located, country unknown), A-Z.
        For a real country a trailing {location_name: '', count} row is the
        "(no city)" bucket — country-only videos (Plan A inference)."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT location_name, COUNT(*) AS count FROM videos "
                "WHERE udn=? AND COALESCE(country, '')=? "
                "AND COALESCE(location_name, '') != '' "
                "GROUP BY location_name "
                "ORDER BY location_name COLLATE NOCASE",
                (udn, country)).fetchall()
            out = [dict(r) for r in rows]
            if country:
                n = conn.execute(
                    "SELECT COUNT(*) FROM videos WHERE udn=? AND country=? "
                    "AND COALESCE(location_name, '')=''",
                    (udn, country)).fetchone()[0]
                if n:
                    out.append({"location_name": "", "count": n})
        return out

    def videos_by_country_location(self, udn: str, country: str,
                                   location_name: str) -> list:
        """One (country, location)'s videos, newest capture first.
        location_name '' = the "(no city)" bucket (matches NULL too)."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT * FROM videos WHERE udn=? "
                "AND COALESCE(country, '')=? "
                "AND COALESCE(location_name, '')=? "
                "ORDER BY created DESC, title COLLATE NOCASE",
                (udn, country, location_name)).fetchall()
        return [dict(r) for r in rows]

    def video_locations(self, udn: str) -> list:
        """[{location_name, count}] A-Z case-insensitive; the no-location
        bucket sorts LAST when present. Un-geocoded videos carry NULL in
        live data ('' in some tests) — COALESCE folds both into one ''
        bucket (a bare `= ''` comparison is NULL for NULL rows, which made
        the bucket sort FIRST and resolve empty; live bug 2026-07-06).
        Country-only videos (Plan A inference) live under their country's
        "(no city)" bucket, so the '' bucket here means NEITHER."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT COALESCE(location_name, '') AS location_name, "
                "COUNT(*) AS count FROM videos "
                "WHERE udn=? AND NOT (COALESCE(location_name, '')='' "
                "                     AND COALESCE(country, '') != '') "
                "GROUP BY COALESCE(location_name, '') "
                "ORDER BY (COALESCE(location_name, '') = ''), "
                "location_name COLLATE NOCASE",
                (udn,)).fetchall()
        return [dict(r) for r in rows]

    def videos_by_location(self, udn: str, location_name: str) -> list:
        """One location's videos (''=no location, matches NULL too; excludes
        country-only videos — those belong to their country's "(no city)"
        bucket), newest capture first."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT * FROM videos "
                "WHERE udn=? AND COALESCE(location_name, '')=? "
                "AND NOT (COALESCE(location_name, '')='' "
                "         AND COALESCE(country, '') != '') "
                "ORDER BY created DESC, title COLLATE NOCASE",
                (udn, location_name)).fetchall()
        return [dict(r) for r in rows]

    def clear_videos(self, udn: str) -> int:
        """Wipe the video index for this udn (force-rescan). Returns rows removed."""
        with self._pool.write() as conn:
            cur = conn.execute("DELETE FROM videos WHERE udn=?", (udn,))
        log.info(f"Video index cleared for {udn} ({cur.rowcount} rows)")
        return cur.rowcount

    def prune_videos(self, udn: str, keep_ids) -> int:
        """Delete this udn's video rows whose id is NOT in keep_ids — drops
        rows for files removed from disk after an incremental scan. Returns
        the number removed."""
        keep = set(keep_ids)
        with self._pool.write() as conn:
            rows = conn.execute(
                "SELECT id FROM videos WHERE udn=?", (udn,)).fetchall()
            gone = [r["id"] for r in rows if r["id"] not in keep]
            for vid in gone:
                conn.execute("DELETE FROM videos WHERE id=?", (vid,))
        return len(gone)

    # ── video location overrides (Plan A — inferred/manual locations
    #    for GPS-less videos; see the table comment in _init_schema) ──

    def video_loc_override_set(self, video_id: str, location_name,
                               country, source: str) -> bool:
        """Upsert an override. 'manual' always wins: an inferred write onto
        an existing manual row is refused (returns False)."""
        with self._pool.write() as conn:
            if source != "manual":
                row = conn.execute(
                    "SELECT source FROM video_location_overrides "
                    "WHERE video_id=?", (video_id,)).fetchone()
                if row and row["source"] == "manual":
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO video_location_overrides "
                "(video_id, location_name, country, source, updated_at) "
                "VALUES (?,?,?,?, strftime('%s','now'))",
                (video_id, location_name or "", country or "", source))
        return True

    def video_loc_override_remove(self, video_id: str) -> bool:
        with self._pool.write() as conn:
            cur = conn.execute(
                "DELETE FROM video_location_overrides WHERE video_id=?",
                (video_id,))
        return cur.rowcount > 0

    def video_loc_override_list(self) -> list:
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT * FROM video_location_overrides "
                "ORDER BY video_id").fetchall()
        return [dict(r) for r in rows]

    def update_video_location(self, video_id: str, location_name,
                              country, title: str) -> None:
        """Write an applied override onto the videos row (location fields +
        rebuilt title). Caller (dlna_video_index.apply_location_overrides)
        owns the never-touch-a-real-GPS-row / title rules."""
        with self._pool.write() as conn:
            conn.execute(
                "UPDATE videos SET location_name=?, country=?, title=? "
                "WHERE id=?",
                (location_name or None, country or None, title, video_id))

    # ── video people (Plan B — Immich person sync; see the table
    #    comment in _init_schema) ────────────────────────────────────

    def video_people_replace(self, person: str, person_id: str,
                             video_ids) -> int:
        """SYNC semantics: replace this person's whole row set (a re-sync
        drops videos Immich no longer lists). Returns rows inserted."""
        ids = list(video_ids)
        with self._pool.write() as conn:
            conn.execute("DELETE FROM video_people WHERE person=?",
                         (person,))
            for vid in ids:
                conn.execute(
                    "INSERT OR REPLACE INTO video_people "
                    "(video_id, person, person_id, updated_at) "
                    "VALUES (?,?,?, strftime('%s','now'))",
                    (vid, person, person_id or ""))
        return len(ids)

    def video_people_list(self, udn: str) -> list:
        """[{person, count}] A-Z — only counting videos that exist for
        the udn (a stale person→video link is invisible, not an error)."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT p.person AS person, COUNT(*) AS count "
                "FROM video_people p JOIN videos v ON v.id = p.video_id "
                "WHERE v.udn=? GROUP BY p.person "
                "ORDER BY p.person COLLATE NOCASE", (udn,)).fetchall()
        return [dict(r) for r in rows]

    def videos_by_person(self, udn: str, person: str) -> list:
        """One person's videos, newest capture first."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT v.* FROM videos v "
                "JOIN video_people p ON p.video_id = v.id "
                "WHERE v.udn=? AND p.person=? "
                "ORDER BY v.created DESC, v.title COLLATE NOCASE",
                (udn, person)).fetchall()
        return [dict(r) for r in rows]

    def video_people_map(self, udn: str) -> dict:
        """{video_id: [person, …]} (A-Z within a video) — one query for
        the PWA's /api/videos payload."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT p.video_id AS video_id, p.person AS person "
                "FROM video_people p JOIN videos v ON v.id = p.video_id "
                "WHERE v.udn=? "
                "ORDER BY p.video_id, p.person COLLATE NOCASE",
                (udn,)).fetchall()
        out = {}
        for r in rows:
            out.setdefault(r["video_id"], []).append(r["person"])
        return out

    # ── Reverse-geocode cache (V1) ────────────────────────────────
    @staticmethod
    def _geo_key(lat, lon):
        return (round(float(lat), 3), round(float(lon), 3))   # ~111 m at 3 dp

    def geocode_get(self, lat, lon):
        """(place, country, True) if cached (place/country may be '' =
        looked-up-no-value); country is None on a pre-country legacy row
        (the geocoder upgrades it with one re-fetch). (None, None, False)
        on a miss."""
        la, lo = self._geo_key(lat, lon)
        with self._pool.read() as conn:
            r = conn.execute(
                "SELECT place, country FROM geocode_cache "
                "WHERE lat_key=? AND lon_key=?", (la, lo)).fetchone()
        if r is None:
            return (None, None, False)
        return (r["place"] or "", r["country"], True)

    def geocode_put(self, lat, lon, place, country=None):
        la, lo = self._geo_key(lat, lon)
        with self._pool.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO geocode_cache"
                "(lat_key, lon_key, place, country, fetched_at) "
                "VALUES (?, ?, ?, ?, strftime('%s','now'))",
                (la, lo, place or "", country))

    # ── FTS5 search ───────────────────────────────────────────────

    def search(self, udn: str, query: str, limit: int = 300) -> dict:
        """
        Full-text search returning tracks, distinct albums, distinct artists.
        Browse-side dedup is applied: lower-quality 16-bit duplicates of
        a 24-bit track are hidden. See `_dedup_clause` docstring.
        """
        # Type-ahead semantics (2026-07-03): each whitespace-separated
        # term must match (FTS5 implicit AND) and the LAST term matches
        # as a prefix — "essential chil" finds "Essential Classical
        # Chillout". The old single-quoted-phrase form made any partial
        # final word match NOTHING, which read as missing content in
        # clients that search per keystroke (Amperfy, the PWA box).
        # Punctuation-only tokens ("-", "&", "/") tokenize to nothing in
        # FTS5 and would AND-blank the whole query — drop them.
        terms = [t.replace('"', '""') for t in query.split()
                 if any(c.isalnum() for c in t)]
        if not terms:
            return {"tracks": [], "albums": [], "artists": []}
        fts_q = " ".join(f'"{t}"' for t in terms[:-1]) + \
                (" " if len(terms) > 1 else "") + f'"{terms[-1]}"*'
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:

            tracks = conn.execute(
                f"""SELECT t.obj_id as id, t.url, t.title, t.artist, t.album,
                          t.album_key, t.duration, t.art, t.mime, 'audio' as type
                   FROM tracks_fts f
                   JOIN tracks t ON t.id = f.rowid
                   WHERE tracks_fts MATCH ? AND t.udn = ?
                     AND {dedup}
                   ORDER BY t.artist, t.album, t.title
                   LIMIT ?""",
                (fts_q, udn, limit)).fetchall()

            if _is_localfs(udn):
                albums = conn.execute(
                    f"""SELECT t.album_key,
                              {_localfs_album_name("t")} as album,
                              {_localfs_album_artist("t")} as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks_fts f
                       JOIN tracks t ON t.id = f.rowid
                       WHERE tracks_fts MATCH ? AND t.udn = ?
                         AND t.album_key != ''
                         AND {dedup}
                       GROUP BY t.album_key
                       ORDER BY album
                       LIMIT 100""",
                    (fts_q, udn)).fetchall()
            else:
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

    def primary_udn(self) -> str:
        """The udn of the library to expose as 'the' gateway MediaServer —
        the server owning the most tracks (in this single-library deployment,
        the LocalFs backend). Used by the gateway-as-MediaServer UPnP browse
        (api_upnp._gw_browse) to back the Artists/Albums/Genres tree. Returns
        '' when no library is indexed yet."""
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT udn FROM tracks GROUP BY udn "
                "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        return row["udn"] if row else ""

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

    def album_tracks(self, udn: str, artist: str, album: str,
                     album_key: str = "") -> list:
        """Return all tracks for an album, with lower-quality 16/24-bit
        duplicates hidden from the browse view (see `_dedup_clause`).

        Two addressing modes:
          * `album_key` set → folder-based identity (LocalFs). Returns
            every track in that folder regardless of per-track artist/album
            tags, ordered by `file_path` so disc/track order is preserved.
            This is what makes a Various-Artists compilation open as one
            album.
          * otherwise → the legacy `(artist, album)` pair (UPnP and any
            caller that hasn't moved to album_key — favourites, UPnP,
            Subsonic). Unchanged behaviour."""
        dedup = _dedup_clause("t")
        cols = ("t.obj_id as id, t.url, t.title, t.artist, t.album, "
                "t.album_key, t.duration, t.art, t.mime, t.genre, "
                "'audio' as type")
        with self._pool.read() as conn:
            if album_key:
                rows = conn.execute(
                    f"""SELECT {cols} FROM tracks t
                       WHERE t.udn=? AND t.album_key=?
                         AND {dedup}
                       ORDER BY t.file_path COLLATE NOCASE, t.title""",
                    (udn, album_key)).fetchall()
            else:
                rows = conn.execute(
                    f"""SELECT {cols} FROM tracks t
                       WHERE t.udn=? AND t.album=?
                         AND (? = '' OR t.artist=?)
                         AND {dedup}
                       ORDER BY t.title""",
                    (udn, album, artist, artist)).fetchall()
        return [dict(r) for r in rows]

    def all_albums(self, udn: str) -> list:
        """All distinct albums, grouping compilations under 'Various Artists'.
        Track count reflects browse-visible (deduped) tracks only.
        LocalFs sources group by FOLDER (album_key) and carry it as the
        album identity; other sources keep (artist, album) grouping."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            if _is_localfs(udn):
                rows = conn.execute(
                    f"""SELECT t.album_key,
                              {_localfs_album_name("t")} as album,
                              {_localfs_album_artist("t")} as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks t
                       WHERE t.udn=? AND t.album_key != ''
                         AND {dedup}
                       GROUP BY t.album_key
                       ORDER BY album COLLATE NOCASE""",
                    (udn,)).fetchall()
            else:
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
        browse-visible (deduped) count. LocalFs groups by FOLDER: the
        albums are the folders that contain a track by this artist
        (so opening a performer on a compilation lands on the whole
        comp folder); other sources keep (artist, album) grouping."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            if _is_localfs(udn):
                rows = conn.execute(
                    f"""SELECT t.album_key,
                              {_localfs_album_name("t")} as album,
                              {_localfs_album_artist("t")} as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks t
                       WHERE t.udn=? AND t.album_key != ''
                         AND t.album_key IN (
                             SELECT album_key FROM tracks
                              WHERE udn=? AND artist=? AND album_key != '')
                         AND {dedup}
                       GROUP BY t.album_key
                       ORDER BY album COLLATE NOCASE""",
                    (udn, udn, artist)).fetchall()
            else:
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
            elif mode == "albums" and _is_localfs(udn):
                # Folder-based grouping: one album = one folder. The
                # letter filter applies to the DISPLAY name, which is an
                # aggregate, so it moves from WHERE to HAVING.
                name        = _localfs_album_name("t")
                artist_expr = _localfs_album_artist("t")
                dedup       = _dedup_clause("t")
                having      = where_extra.format(col=name)
                params      = [udn] + ([like] if like else [])
                base = (f"FROM tracks t WHERE t.udn=? AND t.album_key!='' "
                        f"AND {dedup} GROUP BY t.album_key HAVING 1=1 {having}")
                total = conn.execute(
                    f"SELECT COUNT(*) FROM (SELECT t.album_key {base})",
                    params).fetchone()[0]
                rows = conn.execute(
                    f"""SELECT t.album_key,
                              {name} as album,
                              {artist_expr} as artist,
                              COUNT(*) as track_count, MAX(t.art) as art
                       {base}
                       ORDER BY album COLLATE NOCASE
                       LIMIT ? OFFSET ?""",
                    params + [limit, offset]).fetchall()
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
        Track count is browse-visible (deduped). LocalFs groups by FOLDER:
        the albums are the folders that contain a track of this genre."""
        dedup = _dedup_clause("t")
        with self._pool.read() as conn:
            if _is_localfs(udn):
                rows = conn.execute(
                    f"""SELECT t.album_key,
                              {_localfs_album_name("t")} as album,
                              {_localfs_album_artist("t")} as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks t
                       WHERE t.udn=? AND t.album_key != ''
                         AND t.album_key IN (
                             SELECT album_key FROM tracks
                              WHERE udn=? AND genre=? AND album_key != '')
                         AND {dedup}
                       GROUP BY t.album_key
                       ORDER BY album COLLATE NOCASE""",
                    (udn, udn, genre)).fetchall()
            else:
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
        distinct artists collapse to 'Various Artists'. LocalFs groups by
        FOLDER: the albums are the folders that contain a track in the
        decade."""
        dedup = _dedup_clause("t")
        eff = self._EFFECTIVE_YEAR
        with self._pool.read() as conn:
            if _is_localfs(udn):
                # Effective-year expression for the inner per-track filter
                # (tracks aliased t2 there; metadata_overrides stays m).
                eff2 = eff.replace("t.year", "t2.year")
                rows = conn.execute(
                    f"""SELECT t.album_key,
                              {_localfs_album_name("t")} as album,
                              {_localfs_album_artist("t")} as artist,
                              COUNT(*) as track_count,
                              MAX(t.art) as art
                       FROM tracks t
                       WHERE t.udn = ? AND t.album_key != ''
                         AND t.album_key IN (
                             SELECT t2.album_key FROM tracks t2
                        LEFT JOIN metadata_overrides m ON m.url = t2.url
                             WHERE t2.udn = ? AND t2.album_key != ''
                               AND ({eff2} / 10) * 10 = ?)
                         AND {dedup}
                       GROUP BY t.album_key
                       ORDER BY album COLLATE NOCASE""",
                    (udn, udn, decade)).fetchall()
            else:
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

        This method is for positive metadata; a 'notfound' sticky-negative
        row is written directly (INSERT OR IGNORE) where needed.

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
                # Push the override onto the live tracks row so browse / search /
                # Subsonic see it immediately. (`update_tracks=False` is a
                # vestigial opt-out from the removed AcoustID bulk worker — no
                # current caller passes it.)
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
                   ORDER BY added_at, id""", (pl_id,)).fetchall()
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
        ``tracks_ad`` / ``tracks_au`` triggers reference ``tracks_fts``
        by name and keep working after the recreate.
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

    def run_with_fts_heal(self, body_fn, *args, **kwargs):
        """Run ``body_fn(*args, **kwargs)``; if it raises a
        ``sqlite3.DatabaseError`` whose message contains "malformed" (the
        FTS5 shadow-table corruption symptom — see ``repair_fts``), repair
        the FTS index and retry the body ONCE. Any other exception, or a
        malformed error on the retry, is re-raised — never loops.

        This is the single heal implementation: ``clear`` and
        ``upsert_tracks`` route their bodies through it (mass DELETEs /
        INSERTs fire the FTS triggers straight into the corrupt index —
        the 5th and 6th occurrences, 2026-07-03, were exactly that), the
        Indexer wraps its crawl with it, and the LocalFs provider wraps
        its rescan write-transaction. The body must be idempotent /
        retry-safe (all current callers are: DELETE, INSERT OR IGNORE,
        INSERT OR REPLACE).
        """
        for attempt in (1, 2):
            try:
                return body_fn(*args, **kwargs)
            except sqlite3.DatabaseError as e:
                if attempt == 1 and "malformed" in str(e).lower():
                    log.warning(f"LibraryDB: FTS index malformed ({e}) — "
                                f"rebuilding tracks_fts and retrying once")
                    self.repair_fts()
                    continue
                raise

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

    def position_get(self, album_key: str) -> Optional[dict]:
        if not album_key:
            return None
        with self._pool.read() as conn:
            r = conn.execute(
                "SELECT album_key, url, position_sec, duration_sec, "
                "       finished, updated_at "
                "FROM playback_positions WHERE album_key=?",
                (album_key,)).fetchone()
        return dict(r) if r else None

    def position_clear(self, album_key: str) -> bool:
        with self._pool.write() as conn:
            cur = conn.execute(
                "DELETE FROM playback_positions WHERE album_key=?",
                (album_key,))
        return cur.rowcount > 0

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

DEVICE_ROLES     = DeviceRoleCache(DB)
INDEXER          = Indexer(DB)
ART_FETCHER      = AlbumArtFetcher(DB)


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