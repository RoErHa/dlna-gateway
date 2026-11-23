#!/usr/bin/env python3
"""
dlna_library_playlists.py — `PlaylistsMixin`: track playlists, including the
⭐ Favourites one.

Split out of dlna_library_collections.py (2026-11-23), which sat at 394 of
its 400 lines when the four editing calls arrived (rename, add several,
remove several, reorder — docs/gateway-contract-playlists.md). It keeps that
module's invariant: `clear(udn)` never touches any of this.

ORDER. A row's place is `position`; a row without one (written by code that
does not know the column: an older gateway after a downgrade, the
playlists.json import) comes after the numbered ones, oldest first. Adding a
track first gives such rows the number they are already shown at —
otherwise "highest + 1" would put the new track BEFORE them.

Every write that looks before it writes opens with BEGIN IMMEDIATE.
`_pool.write()` only serialises THIS process; the tools write the same file
from another one.
"""
from __future__ import annotations

import random
import sqlite3
import uuid

from dlna_library_sql import FAVOURITES_ID, UNKNOWN_ARTISTS_PLAYLIST

PL_NAME_MAX = 200

# The one definition of a playlist's order.
_PL_ORDER = "position IS NULL, position, added_at, id"


def _exists(conn: sqlite3.Connection, pl_id: str) -> bool:
    return conn.execute("SELECT 1 FROM playlists WHERE id=?",
                        (pl_id,)).fetchone() is not None


def _urls(conn: sqlite3.Connection, pl_id: str) -> set:
    return {r["url"] for r in conn.execute(
        "SELECT url FROM playlist_tracks WHERE pl_id=?", (pl_id,))}


def _next_position(conn: sqlite3.Connection, pl_id: str) -> int:
    """The position for a track added at the end. Numbers any unnumbered
    rows first (see the module docstring), without moving them."""
    top = conn.execute("SELECT MAX(position) FROM playlist_tracks "
                       "WHERE pl_id=?", (pl_id,)).fetchone()[0] or 0
    loose = conn.execute(
        "SELECT id FROM playlist_tracks WHERE pl_id=? AND position IS NULL "
        "ORDER BY added_at, id", (pl_id,)).fetchall()
    for r in loose:
        top += 1
        conn.execute("UPDATE playlist_tracks SET position=? WHERE id=?",
                     (top, r["id"]))
    return top + 1


def _insert(conn: sqlite3.Connection, pl_id: str, track: dict,
            position: int) -> None:
    conn.execute(
        "INSERT INTO playlist_tracks "
        "(pl_id, url, title, artist, album, duration, art, position) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (pl_id, track.get("url", ""), track.get("title", ""),
         track.get("artist", ""), track.get("album", ""),
         track.get("duration", ""), track.get("art", ""), position))


class PlaylistsMixin:
    """See module docstring. Mixed into `LibraryDB` via `CollectionsMixin`;
    relies on `self._pool` from the host class."""

    def pl_list(self) -> list:
        with self._pool.read() as conn:
            rows = conn.execute(
                """SELECT p.id, p.name, COUNT(pt.id) as count
                   FROM playlists p
                   LEFT JOIN playlist_tracks pt ON pt.pl_id = p.id
                   GROUP BY p.id
                   ORDER BY p.sort_order, p.name""").fetchall()
        return [dict(r) for r in rows]

    def pl_get(self, pl_id: str) -> dict | None:
        with self._pool.read() as conn:
            pl = conn.execute(
                "SELECT id, name FROM playlists WHERE id=?",
                (pl_id,)).fetchone()
            if not pl:
                return None
            tracks = conn.execute(
                f"SELECT url, title, artist, album, duration, art "
                f"FROM playlist_tracks WHERE pl_id=? ORDER BY {_PL_ORDER}",
                (pl_id,)).fetchall()
        return {"id": pl["id"], "name": pl["name"],
                "tracks": [dict(t) for t in tracks]}

    def pl_create(self, name: str) -> str:
        pl_id = str(uuid.uuid4())[:8]
        with self._pool.write() as conn:
            conn.execute("INSERT INTO playlists (id, name) VALUES (?,?)",
                         (pl_id, name))
        return pl_id

    def pl_rename(self, pl_id: str, name: str) -> str:
        """Returns 'renamed', 'bad_name', 'not_found' or 'protected'. The
        ONE rule for every surface (`/api/playlist/rename` and Subsonic).

        Protected: Favourites, and the generated worklist — which its sync
        finds BY NAME. That cuts both ways: renaming it away makes the next
        scan create a second one, and renaming something else TO it hands a
        person's own playlist to a sync that deletes rows from it."""
        name = name.strip()
        # Control characters are not legal XML, and this name goes into the
        # Naim's DIDL and Subsonic's XML: one would break the whole listing.
        # (Not str.isprintable: it refuses the joiners inside emoji.)
        if not name or len(name) > PL_NAME_MAX \
                or any(ord(c) < 32 or ord(c) == 127 for c in name):
            return "bad_name"
        with self._pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT name FROM playlists WHERE id=?",
                               (pl_id,)).fetchone()
            if not row:
                return "not_found"
            if pl_id == FAVOURITES_ID \
                    or UNKNOWN_ARTISTS_PLAYLIST in (row["name"], name):
                return "protected"
            conn.execute("UPDATE playlists SET name=? WHERE id=?",
                         (name, pl_id))
        return "renamed"

    def pl_delete(self, pl_id: str) -> bool:
        if pl_id == FAVOURITES_ID:
            return False
        with self._pool.write() as conn:
            cur = conn.execute("DELETE FROM playlists WHERE id=?", (pl_id,))
        return cur.rowcount > 0

    def pl_add_track(self, pl_id: str, track: dict) -> str:
        """Returns 'added', 'duplicate', or 'not_found'."""
        with self._pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not _exists(conn, pl_id):
                return "not_found"
            try:
                _insert(conn, pl_id, track, _next_position(conn, pl_id))
                return "added"
            except sqlite3.IntegrityError:
                return "duplicate"

    def pl_add_tracks(self, pl_id: str, tracks: list) -> dict | None:
        """Add several at the end, in the order given, all or none. A url
        already present (or repeated in `tracks`) is counted, and the
        present row stays where it is. None → no such playlist."""
        with self._pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not _exists(conn, pl_id):
                return None
            have = _urls(conn, pl_id)
            position = _next_position(conn, pl_id)
            added = 0
            for t in tracks:
                if t["url"] in have:
                    continue
                have.add(t["url"])
                _insert(conn, pl_id, t, position + added)
                added += 1
        return {"added": added, "duplicates": len(tracks) - added,
                "count": len(have)}

    def pl_remove_track(self, pl_id: str, url: str) -> bool:
        with self._pool.write() as conn:
            cur = conn.execute(
                "DELETE FROM playlist_tracks WHERE pl_id=? AND url=?",
                (pl_id, url))
        return cur.rowcount > 0

    def pl_remove_tracks(self, pl_id: str, urls: list) -> dict | None:
        """Remove several, all or none. A url that is not in the playlist is
        counted as missing, so a repeated request is harmless. None → no
        such playlist."""
        with self._pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not _exists(conn, pl_id):
                return None
            have, wanted = _urls(conn, pl_id), set(urls)
            gone = have & wanted
            conn.executemany(
                "DELETE FROM playlist_tracks WHERE pl_id=? AND url=?",
                [(pl_id, u) for u in gone])
        return {"removed": len(gone), "missing": len(wanted - have),
                "count": len(have - gone)}

    def pl_reorder(self, pl_id: str, urls: list) -> str:
        """Returns 'reordered', 'not_found', or 'changed' — `urls` is not
        exactly the playlist's present urls, each once, and nothing was
        written."""
        with self._pool.write() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not _exists(conn, pl_id):
                return "not_found"
            have = _urls(conn, pl_id)
            if len(urls) != len(have) or set(urls) != have:
                return "changed"
            conn.executemany(
                "UPDATE playlist_tracks SET position=? "
                "WHERE pl_id=? AND url=?",
                [(i, pl_id, u) for i, u in enumerate(urls, 1)])
        return "reordered"

    def pl_to_m3u(self, pl_id: str, shuffle: bool = False,
                  output_path: str = "/tmp/dlna-gw-pl.m3u") -> str | None:
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
