#!/usr/bin/env python3
"""
dlna_library_ddl_user.py — The half of the schema that SURVIVES a rebuild — the single most
load-bearing invariant in this database.

Everything here is either authored by a person (playlists, album
favourites, metadata overrides, radio stations, manual artist ids) or
earned over time at real cost (album art fetched from Cover Art
Archive, lyrics from lrclib, play counts, audiobook positions,
OpenLibrary book metadata, MusicBrainz artist ids at ~1.2 s each).

**`clear(udn)` must never touch any table in this file.** Re-indexing
rebuilds `tracks`; it cannot rebuild a playlist somebody made or an
hour of rate-limited lookups. If a table belongs here, adding it to a
clear path is a data-loss bug, not a tidy-up.

Split out of `dlna_library_ddl.py` on 2026-09-20, which had
reached 399 of its 400-line budget. No logic, no imports —
literal DDL as DATA, concatenated by `dlna_library_ddl.SCHEMA_DDL`.
After ANY change here run `python3 tools/regen_schema.py`.
"""
from __future__ import annotations

SCHEMA_USER = """
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
                -- artist_meta (2026-09-20): artist -> MusicBrainz id,
                -- keyed by dlna_mbid.norm_artist so spelling variants of
                -- one act share a row. NOT touched by clear(udn) — each
                -- row costs a rate-limited MB round-trip.
                -- source: 'tag'|'search'|'notfound'|'manual'; 'manual' is
                -- never overwritten by automation.
                CREATE TABLE IF NOT EXISTS artist_meta (
                    artist_key TEXT PRIMARY KEY,
                    artist     TEXT NOT NULL,
                    mbid       TEXT,
                    source     TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL
                );
"""
