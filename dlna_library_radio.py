#!/usr/bin/env python3
"""
dlna_library_radio.py — `RadioFavouritesMixin`: the user's saved
internet-radio stations (`radio_favourites`).

Split out of dlna_library_collections.py (2026-08-20), which had reached
458 lines. `CollectionsMixin` INHERITS this mixin, so `LibraryDB`'s
composition and the public `DB.<method>` surface are unchanged.

Two contracts worth keeping in view:
  * The 25-station cap is enforced HERE, server-side, never trusted to
    the client — `radio_fav_add` returns 'full' rather than evicting
    anything, and re-adding an existing station is idempotent and does
    not count against the cap. The cap is what makes the favourites
    behave like physical preset buttons in the now-playing prev/next.
  * Like `album_favourites` / `play_counts` / `lyrics`, this table is
    independent of `tracks` and is NOT touched by `clear(udn)`.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("dlna.library")


class RadioFavouritesMixin:
    """See module docstring. Mixed into `LibraryDB` via `CollectionsMixin`;
    never instantiated on its own — it relies on `self._pool` from the
    host class."""

    # Hard cap on saved stations — see the module docstring.
    # Read by callers as `DB.RADIO_FAV_MAX`.
    RADIO_FAV_MAX = 25

    def radio_fav_count(self) -> int:
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM radio_favourites").fetchone()
        return row["n"] if row else 0
    def radio_fav_is(self, station_uuid: str) -> bool:
        if not station_uuid:
            return False
        with self._pool.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM radio_favourites WHERE station_uuid=? LIMIT 1",
                (station_uuid,)).fetchone()
        return row is not None
    def radio_fav_add(self, station: dict) -> str:
        """Add a station to favourites. Returns one of:
          'ok'     — new row created
          'exists' — already favourited (idempotent no-op)
          'full'   — at RADIO_FAV_MAX and this is a NEW station
          'bad'    — missing station_uuid / name / stream_url

        The 25-cap is enforced HERE, server-side — never trust the
        client. Re-adding an existing favourite is always allowed and
        never counts against the cap.
        """
        uuid   = (station.get("station_uuid") or "").strip()
        name   = (station.get("name") or "").strip()
        stream = (station.get("stream_url") or "").strip()
        if not uuid or not name or not stream:
            return "bad"
        if self.radio_fav_is(uuid):
            return "exists"
        if self.radio_fav_count() >= self.RADIO_FAV_MAX:
            return "full"
        try:
            bitrate = int(station.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        with self._pool.write() as conn:
            # New favourites land last in the preset order.
            row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 "
                               "AS nxt FROM radio_favourites").fetchone()
            conn.execute(
                "INSERT OR IGNORE INTO radio_favourites "
                "(station_uuid, name, stream_url, homepage, favicon, "
                " codec, bitrate, country, tags, added_at, sort_order) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (uuid, name, stream,
                 station.get("homepage") or "",
                 station.get("favicon")  or "",
                 station.get("codec")    or "",
                 bitrate,
                 station.get("country")  or "",
                 station.get("tags")     or "",
                 int(time.time()), row["nxt"] if row else 0))
        return "ok"
    def radio_fav_remove(self, station_uuid: str) -> bool:
        with self._pool.write() as conn:
            cur = conn.execute(
                "DELETE FROM radio_favourites WHERE station_uuid=?",
                (station_uuid,))
        return cur.rowcount > 0
    def radio_fav_list(self) -> list:
        """All favourited stations, ordered by sort_order then added_at."""
        with self._pool.read() as conn:
            rows = conn.execute(
                "SELECT station_uuid, name, stream_url, homepage, favicon, "
                "       codec, bitrate, country, tags, added_at, sort_order "
                "FROM radio_favourites "
                "ORDER BY sort_order ASC, added_at ASC").fetchall()
        return [dict(r) for r in rows]
    def radio_fav_reorder(self, uuid_list: list) -> bool:
        """Persist a new preset ordering: each UUID's sort_order is set
        to its index in uuid_list. UUIDs not in the favourites table are
        silently ignored; favourites not named keep their old order
        value (so they sort after the listed ones if those start at 0)."""
        if not uuid_list:
            return False
        with self._pool.write() as conn:
            for i, uuid in enumerate(uuid_list):
                conn.execute(
                    "UPDATE radio_favourites SET sort_order=? "
                    "WHERE station_uuid=?", (i, uuid))
        return True
    def radio_fav_update(self, station_uuid: str, *, name: str = None,
                         stream_url: str = None,
                         homepage: str = None) -> bool:
        """Update an existing favourite's editable fields — backs
        Subsonic's updateInternetRadioStation. Only non-None arguments
        are written. Returns True if a row was changed."""
        sets, vals = [], []
        if name is not None:
            sets.append("name=?");       vals.append(name)
        if stream_url is not None:
            sets.append("stream_url=?"); vals.append(stream_url)
        if homepage is not None:
            sets.append("homepage=?");   vals.append(homepage)
        if not sets:
            return False
        vals.append(station_uuid)
        with self._pool.write() as conn:
            cur = conn.execute(
                f"UPDATE radio_favourites SET {', '.join(sets)} "
                f"WHERE station_uuid=?", vals)
        return cur.rowcount > 0
