#!/usr/bin/env python3
"""
dlna_library_ddl_video.py — The video half of the schema (GWMovies) — the scanned video index
plus the three things that must OUTLIVE a rescan: manual/inferred
location overrides, Immich person tags, and the Nominatim geocode
cache (which is also a politeness measure — it is what keeps the
gateway to ~1 req/s against a free public service).

Separated from the music schema because it is browsed by a different
client (the LG WebOS TV), scanned by a different worker
(`dlna_video_index.scan_videos`) and carries the project's only
privacy-relevant outbound call. See docs/VIDEO_SUPPORT.md.

Split out of `dlna_library_ddl.py` on 2026-09-20, which had
reached 399 of its 400-line budget. No logic, no imports —
literal DDL as DATA, concatenated by `dlna_library_ddl.SCHEMA_DDL`.
After ANY change here run `python3 tools/regen_schema.py`.
"""
from __future__ import annotations

SCHEMA_VIDEO = """

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
"""
