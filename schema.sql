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
                    year        INTEGER, album_key TEXT DEFAULT '',    -- file-tag year (DIDL-Lite dc:date)
                    UNIQUE(udn, artist, album, title, bit_depth, sample_rate)
                );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE metadata_overrides (
                    url       TEXT PRIMARY KEY,
                    artist    TEXT,
                    album     TEXT,
                    title     TEXT,
                    genre     TEXT,
                    year      INTEGER,   -- original release year (MusicBrainz)
                    updated_at TEXT DEFAULT (datetime('now'))
                , source TEXT NOT NULL DEFAULT 'manual');
CREATE TABLE album_art (
                    artist     TEXT NOT NULL,
                    album      TEXT NOT NULL,
                    art_url    TEXT NOT NULL,
                    source     TEXT DEFAULT 'sibling',
                    updated_at TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (artist, album)
                );
CREATE TABLE play_counts (
                    url         TEXT PRIMARY KEY,
                    count       INTEGER NOT NULL DEFAULT 0,
                    last_played INTEGER
                );
CREATE TABLE lyrics (
                    url        TEXT PRIMARY KEY,
                    plain      TEXT,
                    synced     TEXT,
                    source     TEXT NOT NULL,
                    fetched_at INTEGER NOT NULL
                );
CREATE TABLE album_favourites (
                    artist     TEXT NOT NULL,
                    album      TEXT NOT NULL,
                    album_key  TEXT NOT NULL DEFAULT '',
                    added_at   INTEGER NOT NULL,
                    PRIMARY KEY (artist, album, album_key)
                );
CREATE TABLE radio_favourites (
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
CREATE TABLE index_meta (
                    udn        TEXT PRIMARY KEY,
                    indexed_at TEXT
                );
CREATE TABLE localfs_files (
                    path         TEXT PRIMARY KEY,
                    mtime        REAL    NOT NULL,
                    size         INTEGER NOT NULL,
                    track_id     TEXT    NOT NULL,
                    last_scanned INTEGER NOT NULL
                );
CREATE VIRTUAL TABLE tracks_fts USING fts5(
                    title, artist, album,
                    content=tracks, content_rowid=id,
                    tokenize='unicode61 remove_diacritics 1'
                )
/* tracks_fts(title,artist,album) */;
CREATE TABLE IF NOT EXISTS 'tracks_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE IF NOT EXISTS 'tracks_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS 'tracks_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE IF NOT EXISTS 'tracks_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
CREATE TRIGGER tracks_ai
                    AFTER INSERT ON tracks BEGIN
                        INSERT INTO tracks_fts(rowid, title, artist, album)
                        VALUES (new.id, new.title, new.artist, new.album);
                    END;
CREATE TRIGGER tracks_ad
                    AFTER DELETE ON tracks BEGIN
                        INSERT INTO tracks_fts(tracks_fts, rowid, title, artist, album)
                        VALUES ('delete', old.id, old.title, old.artist, old.album);
                    END;
CREATE TABLE playlists (
                    id         TEXT PRIMARY KEY,
                    name       TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    sort_order INTEGER DEFAULT 0
                );
CREATE TABLE playlist_tracks (
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
CREATE TABLE device_roles (
                    udn         TEXT PRIMARY KEY,
                    name        TEXT,
                    location    TEXT,
                    host        TEXT,
                    is_server   INTEGER NOT NULL DEFAULT 0,
                    is_renderer INTEGER NOT NULL DEFAULT 0,
                    first_seen  TEXT DEFAULT (datetime('now')),
                    last_seen   TEXT DEFAULT (datetime('now'))
                );
CREATE TABLE videos (
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
CREATE TABLE geocode_cache (
                    lat_key    REAL NOT NULL,
                    lon_key    REAL NOT NULL,
                    place      TEXT,
                    country    TEXT,
                    fetched_at INTEGER NOT NULL,
                    PRIMARY KEY (lat_key, lon_key)
                );
CREATE TABLE video_location_overrides (
                    video_id      TEXT PRIMARY KEY,
                    location_name TEXT,
                    country       TEXT,
                    source        TEXT NOT NULL,
                    updated_at    INTEGER NOT NULL
                );
CREATE UNIQUE INDEX idx_tracks_udn_url ON tracks(udn, url);
CREATE INDEX idx_tracks_udn_album_key ON tracks(udn, album_key);
