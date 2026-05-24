CREATE TABLE tracks (
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
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE metadata_overrides (
                    url       TEXT PRIMARY KEY,
                    artist    TEXT,
                    album     TEXT,
                    title     TEXT,
                    genre     TEXT,
                    updated_at TEXT DEFAULT (datetime('now'))
                );
CREATE TABLE index_meta (
                    udn        TEXT PRIMARY KEY,
                    indexed_at TEXT
                );
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
CREATE TABLE track_loudness (
                    url        TEXT PRIMARY KEY,
                    lufs       REAL,
                    gain_db    REAL DEFAULT 0.0,
                    scanned_at INTEGER NOT NULL
                , peak_db REAL);
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
                    added_at   INTEGER NOT NULL,
                    PRIMARY KEY (artist, album)
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
CREATE VIRTUAL TABLE tracks_fts USING fts5(title, artist, album, content=tracks, content_rowid=id, tokenize='unicode61 remove_diacritics 1')
/* tracks_fts(title,artist,album) */;
CREATE TABLE IF NOT EXISTS 'tracks_fts_data'(id INTEGER PRIMARY KEY, block BLOB);
CREATE TABLE IF NOT EXISTS 'tracks_fts_idx'(segid, term, pgno, PRIMARY KEY(segid, term)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS 'tracks_fts_docsize'(id INTEGER PRIMARY KEY, sz BLOB);
CREATE TABLE IF NOT EXISTS 'tracks_fts_config'(k PRIMARY KEY, v) WITHOUT ROWID;
