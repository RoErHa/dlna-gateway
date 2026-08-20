#!/usr/bin/env python3
"""
dlna_library.py — SQLite music library index, FTS5 search, playlists.

`LibraryDB` is the single DB handle the whole gateway shares. It is
assembled from one mixin per responsibility rather than defined inline:
until 2026-08-20 this file was 2,912 lines and `LibraryDB` a 95-method
God Object spanning tracks, browse, videos, geocoding, playlists,
lyrics, favourites, radio, audiobook positions, book metadata, device
roles, schema DDL and migrations.

    dlna_library_sql.py           pure helpers (no I/O, no DB)
    dlna_library_schema.py        SchemaMixin       CREATE TABLE/INDEX
    dlna_library_migrations.py    MigrationsMixin   in-place migrations + FTS heal
    dlna_library_tracks.py        TracksMixin       tracks + metadata_overrides
    dlna_library_browse.py        BrowseMixin       artists/albums/genres/search/radio
    dlna_library_videos.py        VideosMixin       GWMovies index + geocode cache
    dlna_library_collections.py   CollectionsMixin  playlists/favs/lyrics/positions

MIXINS, NOT COLLABORATORS — deliberately. The composed class keeps the
exact same public surface, so every `DB.<method>` call site (~240 of
them across the app, the tools and the test suite) and every
`db._pool` reach-in is unchanged by the split. Splitting into
collaborator objects would have meant `DB.tracks.upsert(...)` and a
rewrite of all of them.

MRO note: the mixins are siblings, none of them inherits another, and
no method name is defined twice — so resolution order is irrelevant to
behaviour and cross-mixin calls (`BrowseMixin` → `run_with_fts_heal` on
`MigrationsMixin`, say) simply resolve on the composed class.
`tests/test_library_composition.py` asserts both properties.

⚠ `DB = LibraryDB()` is constructed at MODULE IMPORT, so importing this
module runs `_init_schema` and every pending migration against the real
`library.db` — even while the gateway is live.

Class Indexer crawls a MediaServer and populates the DB.

Standalone test:
    python dlna_library.py
"""
import logging
import os

from dlna_config import DB_FILE
from db_pool import Pool

from dlna_library_browse import BrowseMixin
from dlna_library_collections import CollectionsMixin
from dlna_library_migrations import MigrationsMixin
from dlna_library_schema import SchemaMixin
from dlna_library_tracks import TracksMixin
from dlna_library_videos import VideosMixin

# Re-exported so the historical `from dlna_library import _dedup_clause`
# / `_parse_audio_params` / `FAVOURITES_ID` import form keeps working.
from dlna_library_sql import (  # noqa: F401  (re-export)
    FAVOURITES_ID,
    _d_id,
    _dedup_clause,
    _dur_to_secs,
    _is_localfs,
    _localfs_album_artist,
    _localfs_album_name,
    _norm_title,
    _parse_audio_params,
)

log = logging.getLogger("dlna.library")


# ── LibraryDB ─────────────────────────────────────────────────────

class LibraryDB(SchemaMixin, MigrationsMixin, TracksMixin,
                BrowseMixin, VideosMixin, CollectionsMixin):
    """
    Thread-safe SQLite wrapper for:
      - Track index (tracks + FTS5)          → TracksMixin / BrowseMixin
      - Playlists (playlists + playlist_tracks) → CollectionsMixin
      - Videos, favourites, lyrics, positions   → see module docstring

    Uses db_pool.Pool for connection management:
      - Reads are concurrent (WAL mode)
      - Writes are serialized (write lock)
    """

    def __init__(self, db_file: str = DB_FILE):
        self._pool = Pool(db_file)
        self._init_schema()


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
    """Standalone smoke test — `python dlna_library.py`.

    Was broken from the db_pool migration until 2026-08-20: it still
    reached for `db._db_file`, `db._lock` and `db._connect()`, none of
    which have existed since Pool took over connection management (the
    T3.2 checks in tests/run_all.py assert their ABSENCE). It now uses
    the public Pool surface, so the command CLAUDE.md documents works."""
    from dlna_config import setup_logging
    setup_logging(debug=True)
    log.info("=== dlna_library self-test ===")

    db = LibraryDB()
    db_file = db._pool.db_file
    log.info(f"DB file : {db_file}  exists={os.path.exists(db_file)}")
    log.info(f"Composed from: "
             f"{', '.join(c.__name__ for c in LibraryDB.__mro__ if c.__name__.endswith('Mixin'))}")

    pls = db.pl_list()
    log.info(f"Playlists ({len(pls)}):")
    for p in pls:
        log.info(f"  {p['name']}  ({p['count']} tracks)")

    with db._pool.read() as conn:
        udns = conn.execute(
            "SELECT udn, COUNT(*) as n FROM tracks GROUP BY udn").fetchall()

    if udns:
        log.info("Track index:")
        for row in udns:
            log.info(f"  {row['udn'][:40]}  → {row['n']} tracks")
            log.info(f"    Albums: {db.album_count(row['udn'])}")
    else:
        log.info("Track index: empty (not yet indexed)")

    log.info("PASS — dlna_library OK")


if __name__ == "__main__":
    _test()
