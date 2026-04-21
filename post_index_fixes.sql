-- Post-index metadata fixes.
-- Run after every library rebuild:
--   sqlite3 library.db < post_index_fixes.sql
--
-- Rule: when both a lossy and a lossless copy exist, keep lossless.
--       When equal quality, keep higher id (more recently indexed).


-- ── Rolling Stones ────────────────────────────────────────────────
-- AssetUPnP tags some albums as "Rolling Stones", others as "The Rolling Stones".
-- 1. Drop inferior duplicates (MP3 where a FLAC already exists under the correct name).
DELETE FROM tracks
WHERE artist = 'Rolling Stones'
  AND id IN (
    SELECT r.id
    FROM tracks r
    JOIN tracks t
      ON  t.udn   = r.udn
      AND t.album = r.album
      AND t.title = r.title
    WHERE r.artist = 'Rolling Stones'
      AND t.artist = 'The Rolling Stones'
);

-- 2. Rename whatever remains.
UPDATE tracks SET artist = 'The Rolling Stones' WHERE artist = 'Rolling Stones';

-- ── Rebuild FTS index ─────────────────────────────────────────────
INSERT INTO tracks_fts(tracks_fts) VALUES('rebuild');
