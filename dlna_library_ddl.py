#!/usr/bin/env python3
"""
dlna_library_ddl.py — the literal schema DDL for every table LibraryDB
owns, plus the idempotent ADD COLUMN list applied to pre-existing DBs.

Split out of dlna_library_schema.py (2026-08-20): that module was 505
lines, of which ~340 were this data. Keeping the DDL as data rather than
embedded in `SchemaMixin._init_schema` leaves that method as readable
control flow (create → alter → migrate → seed) and lets the schema be
diffed and regenerated without scrolling past it.

This module holds NO logic and imports nothing — `SchemaMixin` executes
`SCHEMA_DDL` in one `execute_script`, then applies `ADD_COLUMN_SQL` one
statement at a time (each wrapped so a duplicate-column error is the
expected no-op).

`schema.sql` is the committed dump of what this produces — after ANY
change here run `python3 tools/regen_schema.py`
(`tests/test_schema_sync.py` fails the suite otherwise).
"""
from __future__ import annotations

# ── CREATE TABLE / INDEX / TRIGGER ────────────────────────────────
# Composed from three domain modules (split 2026-09-20). The seam is
# the schema's central invariant: what a rebuild RECREATES
# (`_index`) versus what it must never touch (`_user`), with video
# (`_video`) separate again because a different client browses it and
# a different worker fills it. `SCHEMA_DDL` stays the single name every
# caller imports, so nothing outside this file changed.
# Order matters once: the FTS triggers reference `tracks`, and both
# live in _index, which is concatenated first.
# Executed as a single script at startup; every statement is
# IF NOT EXISTS so this is a no-op on an existing DB.
from dlna_library_ddl_index import SCHEMA_INDEX
from dlna_library_ddl_user import SCHEMA_USER
from dlna_library_ddl_video import SCHEMA_VIDEO

SCHEMA_DDL = SCHEMA_INDEX + SCHEMA_USER + SCHEMA_VIDEO



# ── ADD COLUMN migrations ─────────────────────────────────────────
# Applied one at a time to existing DBs. "duplicate column name" is the
# expected outcome on an up-to-date DB and is swallowed by the caller;
# any OTHER OperationalError is logged as a real schema problem.
ADD_COLUMN_SQL = [
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
    # 2026-09-20: songwriting credits, read straight from the tags
    # already on disk (composer ~32%, lyricist ~18% of this library) —
    # no network. mutagen's easy interface needs a one-time EasyMP4
    # key registration to see them on MP4; see dlna_providers/
    # localfs_tags.py. Blank-safe on refresh like genre/art: a file
    # that lost its tag never erases a stored credit.
    "ALTER TABLE tracks ADD COLUMN composer TEXT DEFAULT ''",
    "ALTER TABLE tracks ADD COLUMN lyricist TEXT DEFAULT ''",
    # 2026-09-20 (step 3): the artist facts the MBID unlocks. All
    # DISPLAY-layer — nothing here is ever written back to a file, and
    # `artist_meta.source` continues to describe how the MBID was
    # resolved, not where these came from.
    #   born/died/birth_place  MusicBrainz life-span + begin-area
    #   mb_type/gender         Person vs Group decides which fields the
    #                          UI can even show — a band has no DOB.
    #   genres                 MB's curated genre list, comma-joined
    #   bio/bio_url            Wikipedia extract; CC BY-SA, so bio_url
    #                          is NOT optional — it is the attribution.
    #   image_url              Wikimedia Commons (via Wikidata P18)
    #   notable                Wikidata P800 "notable work"
    #   top_tracks             ListenBrainz popularity (no API key)
    "ALTER TABLE artist_meta ADD COLUMN mb_type TEXT",
    "ALTER TABLE artist_meta ADD COLUMN gender TEXT",
    "ALTER TABLE artist_meta ADD COLUMN born TEXT",
    "ALTER TABLE artist_meta ADD COLUMN died TEXT",
    "ALTER TABLE artist_meta ADD COLUMN birth_place TEXT",
    "ALTER TABLE artist_meta ADD COLUMN country TEXT",
    "ALTER TABLE artist_meta ADD COLUMN genres TEXT",
    "ALTER TABLE artist_meta ADD COLUMN disambiguation TEXT",
    "ALTER TABLE artist_meta ADD COLUMN bio TEXT",
    "ALTER TABLE artist_meta ADD COLUMN bio_url TEXT",
    "ALTER TABLE artist_meta ADD COLUMN image_url TEXT",
    "ALTER TABLE artist_meta ADD COLUMN notable TEXT",
    "ALTER TABLE artist_meta ADD COLUMN top_tracks TEXT",
    "ALTER TABLE artist_meta ADD COLUMN meta_fetched_at INTEGER",
]
