#!/usr/bin/env python3
"""
dlna_library_schema.py — `SchemaMixin`: the CREATE TABLE / CREATE
INDEX DDL for every table LibraryDB owns, plus the Phase-A album-art
sibling backfill that runs at the tail of each index pass.

Split out of dlna_library.py (2026-08-20), which had grown to 2,912
lines and 95 methods across 11 unrelated responsibilities. This is a
MIXIN, not a collaborator: `LibraryDB` inherits it, so `self._pool` and
every sibling method resolve normally through the MRO and the public
`DB.<method>` API is byte-for-byte unchanged at all ~240 call sites.

`schema.sql` is the committed dump of what this file produces — after
ANY change here run `python3 tools/regen_schema.py`
(`tests/test_schema_sync.py` fails the suite otherwise).
"""
from __future__ import annotations

import logging
import sqlite3

from dlna_library_sql import (
    FAVOURITES_ID,
)

log = logging.getLogger("dlna.library")


class SchemaMixin:
    """See module docstring. Mixed into `LibraryDB`; never instantiated
    on its own — it relies on `self._pool` from the host class."""

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
                -- Audiobook metadata overlay (2026-07-13). One row per
                -- book (album_key), filled by tools/openlibrary_books.py
                -- from the OpenLibrary API: canonical author/title plus
                -- series name + number-in-series (REAL — novellas can be
                -- #1.5). DISPLAY-layer only, never written into tracks
                -- or files. Survives clear(udn); sticky notfound like
                -- album_art/lyrics ('manual' always wins). Retry one:
                --   DELETE FROM book_meta WHERE source='notfound'
                --     AND album_key='…'
                CREATE TABLE IF NOT EXISTS book_meta (
                    album_key  TEXT PRIMARY KEY,
                    author     TEXT,
                    title      TEXT,
                    series     TEXT,
                    series_seq REAL,
                    source     TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL
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
