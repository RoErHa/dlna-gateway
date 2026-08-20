#!/usr/bin/env python3
"""
dlna_library_videos.py — `VideosMixin`: the GWMovies video index —
the `videos` table, the date/location/person browse queries behind the
LG TV's DLNA tree, the manual + inferred location overrides, the Immich
person tags, and the Nominatim reverse-geocode cache.

Split out of dlna_library.py (2026-08-20). See dlna_library_schema.py
for why these are mixins rather than collaborators.

`video_location_overrides` and `video_people` deliberately SURVIVE
`clear_videos` — the 5-minute rescan would otherwise wipe work that
took Nominatim rate-limited hours (and Immich face-tagging) to produce.
Overrides are re-applied at the end of every scan by
`dlna_video_index.apply_location_overrides`. Full feature docs:
`docs/VIDEO_SUPPORT.md`.
"""
from __future__ import annotations

import logging

log = logging.getLogger("dlna.library")


class VideosMixin:
    """See module docstring. Mixed into `LibraryDB`; never instantiated
    on its own — it relies on `self._pool` from the host class."""

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
