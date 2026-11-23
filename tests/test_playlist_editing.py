#!/usr/bin/env python3
"""
tests/test_playlist_editing.py — rename, add several, remove several and
reorder (docs/gateway-contract-playlists.md), and the `position` migration.

The four endpoints are driven through the REAL ASGI app — route table, bridge
and request guard included — because half the contract (405, 403, the
`code`-less 404 of an older gateway) is about the route rather than the
handler. No network, no httpx: a minimal ASGI client below.

Run standalone:  python3 -m unittest tests.test_playlist_editing -v
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote, urlencode

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import api_playlists
import api_subsonic
import api_subsonic_proto
import api_upnp
import api_upnp_ids
import dlna_asgi
import dlna_asgi_state
from dlna_library import LibraryDB
from dlna_library_sql import FAVOURITES_ID, UNKNOWN_ARTISTS_PLAYLIST

BASE = "http://gw:8200/localfs/stream/"
WRITES = ("rename", "add_many", "remove_many", "reorder")


def _u(n):
    return f"{BASE}{n}"


def _t(n, **kw):
    return {"url": _u(n), "title": f"T{n}", **kw}


def _request(method, path, *, query="", body=b"", headers=()):
    """One request through dlna_asgi.app → (status, parsed body)."""
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    hdrs = [(b"host", b"127.0.0.1:8765"),
            *((k.encode(), v.encode()) for k, v in headers)]
    asyncio.run(dlna_asgi.app({
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "headers": hdrs, "client": ("127.0.0.1", 5),
        "server": ("127.0.0.1", 8765)}, receive, send))
    raw = b"".join(m.get("body", b"") for m in sent)
    try:
        return sent[0]["status"], json.loads(raw)
    except ValueError:
        return sent[0]["status"], raw


class _SubH:
    """Captures an api_subsonic reply."""
    def _json(self, code, payload):
        self.reply = payload["subsonic-response"]

    def _xml_response(self, code, body):
        raise AssertionError("expected JSON")


class _Base(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = LibraryDB(db_file=self.path)
        # DB is bound where each surface READS it; rebinding a re-export
        # would leave the handlers on another database.
        for mod in (api_playlists, dlna_asgi_state, api_subsonic_proto,
                    api_upnp_ids):
            p = mock.patch.object(mod, "DB", self.db)
            p.start()
            self.addCleanup(p.stop)
        self.pid = self.db.pl_create("Mix")
        for n in (1, 2, 3):
            self.db.pl_add_track(self.pid, _t(n))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self.path + suffix):
                os.unlink(self.path + suffix)

    def post(self, name, payload):
        return _request("POST", f"/api/playlist/{name}",
                        body=json.dumps(payload).encode())

    def rename(self, pl_id, name):
        return _request("POST", "/api/playlist/rename", query=urlencode(
            {"id": pl_id, "name": name}, quote_via=quote))

    def subsonic(self, method, **params):
        h = _SubH()
        with mock.patch.object(api_subsonic_proto,
                               "SUBSONIC_PASSWORD_OVERRIDE", "pw"):
            api_subsonic.handle(h, "GET", f"/rest/{method}", urlencode({
                "u": api_subsonic_proto._subsonic_user(), "p": "pw",
                "v": "1.16.1", "c": "t", "f": "json", **params}, doseq=True))
        return h.reply

    def order(self, pl_id=None):
        status, body = _request("GET", "/api/playlist",
                                query=f"id={pl_id or self.pid}")
        self.assertEqual(status, 200)
        return [t["url"].removeprefix(BASE) for t in body["tracks"]]

    def name(self, pl_id=None):
        return self.db.pl_get(pl_id or self.pid)["name"]

    def assert_refused(self, reply, status, code):
        self.assertEqual(reply[0], status, reply)
        self.assertIs(reply[1]["ok"], False)
        self.assertEqual(reply[1]["code"], code)
        self.assertTrue(reply[1]["error"])


class TestRename(_Base):
    def test_trims_and_replies(self):
        self.assertEqual(self.rename(self.pid, "  Road trip "), (
            200, {"ok": True, "id": self.pid, "name": "Road trip"}))
        self.assertEqual(self.name(), "Road trip")

    def test_empty_or_missing(self):
        for query in (f"id={self.pid}&name=%20%20", f"id={self.pid}&name=",
                      f"id={self.pid}", "name=x", "id=&name=x", ""):
            self.assert_refused(
                _request("POST", "/api/playlist/rename", query=query),
                400, "bad_request")
        self.assertEqual(self.name(), "Mix")

    def test_length_limit_is_after_trimming(self):
        self.assertEqual(self.rename(self.pid, f"  {'x' * 200}  ")[0], 200)
        self.assert_refused(self.rename(self.pid, "x" * 201),
                            400, "bad_request")

    def test_unknown(self):
        self.assert_refused(self.rename("nope", "x"),
                            404, "playlist_not_found")

    def test_awkward_names_survive_everywhere(self):
        name = 'R&B "best" <of> 👨‍👩‍👧 ⭐'
        self.assertEqual(self.rename(self.pid, name)[1]["name"], name)
        self.assertEqual(self.name(), name)
        got = self.subsonic("getPlaylist", id=f"pl:{self.pid}")
        self.assertEqual(got["playlist"]["name"], name)

    def test_control_characters_are_refused(self):
        """Not legal XML: one would break the Naim's whole Playlists list."""
        self.assert_refused(self.rename(self.pid, "a\x0bb"),
                            400, "bad_request")

    def test_same_name_and_shared_name(self):
        self.assertEqual(self.rename(self.pid, "Mix")[0], 200)
        other = self.db.pl_create("Other")
        self.assertEqual(self.rename(other, "Mix")[0], 200)

    def test_favourites_are_protected(self):
        before = self.name(FAVOURITES_ID)
        self.assert_refused(self.rename(FAVOURITES_ID, "x"), 400, "protected")
        self.assertEqual(self.name(FAVOURITES_ID), before)

    def test_the_worklist_is_protected_both_ways(self):
        """Its sync finds it by name AND deletes rows from what it finds."""
        work = self.db.pl_create(UNKNOWN_ARTISTS_PLAYLIST)
        self.assert_refused(self.rename(work, "x"), 400, "protected")
        self.assert_refused(self.rename(self.pid, UNKNOWN_ARTISTS_PLAYLIST),
                            400, "protected")
        self.assertEqual(self.name(), "Mix")

    def test_subsonic_follows_the_same_rule(self):
        before = self.name(FAVOURITES_ID)
        reply = self.subsonic(
            "updatePlaylist", playlistId=f"pl:{FAVOURITES_ID}", name="x",
            songIdToAdd=[api_subsonic._track_id(_u(9))])
        self.assertEqual(reply["status"], "failed")
        self.assertEqual(reply["error"]["code"], 50)
        self.assertEqual(self.name(FAVOURITES_ID), before)
        self.assertEqual(self.order(FAVOURITES_ID), [])   # nothing half-done
        for bad in ("   ", "x" * 201):
            self.assertEqual(self.subsonic(
                "updatePlaylist", playlistId=f"pl:{self.pid}",
                name=bad)["status"], "failed")
        self.assertEqual(self.subsonic(
            "updatePlaylist", playlistId=f"pl:{self.pid}",
            name=" New ")["status"], "ok")
        self.assertEqual(self.name(), "New")

    def test_subsonic_resending_the_present_name_is_not_a_rename(self):
        reply = self.subsonic(
            "updatePlaylist", playlistId=f"pl:{FAVOURITES_ID}",
            name=self.name(FAVOURITES_ID),
            songIdToAdd=[api_subsonic._track_id(_u(9))])
        self.assertEqual(reply["status"], "ok")
        self.assertEqual(self.order(FAVOURITES_ID), ["9"])


class TestAddMany(_Base):
    def test_order_and_duplicates(self):
        status, body = self.post("add_many", {"pl": self.pid, "tracks": [
            _t(9), _t(2), _t(7), _t(9), _t(8)]})
        self.assertEqual((status, body), (200, {
            "ok": True, "added": 3, "duplicates": 2, "count": 6}))
        self.assertEqual(self.order(), ["1", "2", "3", "9", "7", "8"])

    def test_fields_are_stored_and_may_be_missing(self):
        full = {"url": _u("f"), "title": "Ü", "artist": "A", "album": "B",
                "duration": "0:04:12.227", "art": "http://a/x.jpg"}
        self.post("add_many", {"pl": self.pid,
                               "tracks": [full, {"url": _u("bare")}]})
        rows = self.db.pl_get(self.pid)["tracks"]
        self.assertEqual(rows[-2], full)
        self.assertEqual(rows[-1], {**dict.fromkeys(full, ""),
                                    "url": _u("bare")})

    def test_favourites(self):
        self.assertEqual(self.post("add_many", {
            "pl": FAVOURITES_ID, "tracks": [_t(1)]})[0], 200)
        self.assertEqual(self.order(FAVOURITES_ID), ["1"])

    def test_one_bad_track_adds_nothing(self):
        for bad in ({"title": "no url"}, {"url": ""}, {"url": "  "},
                    {"url": 5}, {"url": None}, "str", None, [],
                    _t(8, title=3), _t(8, art=None),
                    {"url": "http://x/\ud800"}, _t(8, title="\ud800")):
            self.assert_refused(
                self.post("add_many", {"pl": self.pid, "tracks": [_t(7), bad]}),
                400, "bad_request")
        self.assertEqual(self.order(), ["1", "2", "3"])

    def test_a_failure_halfway_rolls_back(self):
        real, calls = dlna_insert(), []

        def boom(*a):
            calls.append(a)
            if len(calls) == 2:
                raise sqlite3.OperationalError("disk full")
            real(*a)
        with mock.patch("dlna_library_playlists._insert", boom), \
                self.assertRaises(sqlite3.OperationalError):
            self.db.pl_add_tracks(self.pid, [_t(7), _t(8)])
        self.assertEqual(self.order(), ["1", "2", "3"])

    def test_refusals(self):
        for payload in ({"pl": self.pid, "tracks": []}, {"pl": self.pid},
                        {"pl": self.pid, "tracks": {}}, {"tracks": [_t(7)]},
                        {"pl": "", "tracks": [_t(7)]},
                        {"pl": 4, "tracks": [_t(7)]}):
            self.assert_refused(self.post("add_many", payload),
                                400, "bad_request")
        self.assert_refused(
            self.post("add_many", {"pl": "nope", "tracks": [_t(7)]}),
            404, "playlist_not_found")

    def test_limit(self):
        tracks = [_t(f"n{i}") for i in range(2001)]
        self.assert_refused(
            self.post("add_many", {"pl": self.pid, "tracks": tracks}),
            400, "bad_request")
        self.assertEqual(self.post("add_many", {
            "pl": self.pid, "tracks": tracks[:2000]})[1]["count"], 2003)


def dlna_insert():
    import dlna_library_playlists
    return dlna_library_playlists._insert


class TestRemoveMany(_Base):
    def test_missing_are_counted_and_a_repeat_is_harmless(self):
        req = {"pl": self.pid, "urls": [_u(1), _u(3), _u("gone")]}
        self.assertEqual(self.post("remove_many", req), (200, {
            "ok": True, "removed": 2, "missing": 1, "count": 1}))
        self.assertEqual(self.post("remove_many", req), (200, {
            "ok": True, "removed": 0, "missing": 3, "count": 1}))
        self.assertEqual(self.order(), ["2"])

    def test_a_url_given_twice_counts_once(self):
        self.assertEqual(self.post("remove_many", {
            "pl": self.pid, "urls": [_u(1), _u(1)]})[1], {
                "ok": True, "removed": 1, "missing": 0, "count": 2})

    def test_empty_never_means_all(self):
        for payload in ({"pl": self.pid, "urls": []}, {"pl": self.pid},
                        {"pl": self.pid, "urls": None},
                        {"pl": self.pid, "urls": _u(1)},
                        {"pl": self.pid, "urls": [_u(1), ""]},
                        {"pl": self.pid, "urls": [_u(1), 2]},
                        {"pl": self.pid, "urls": ["x"] * 2001}):
            self.assert_refused(self.post("remove_many", payload),
                                400, "bad_request")
        self.assertEqual(self.order(), ["1", "2", "3"])

    def test_unknown_and_other_playlists(self):
        self.assert_refused(
            self.post("remove_many", {"pl": "nope", "urls": [_u(1)]}),
            404, "playlist_not_found")
        other = self.db.pl_create("Other")
        self.db.pl_add_track(other, _t(1))
        self.post("remove_many", {"pl": self.pid, "urls": [_u(1)]})
        self.assertEqual(self.order(other), ["1"])


class TestReorder(_Base):
    def reorder(self, *ns, pl_id=None):
        return self.post("reorder", {"id": pl_id or self.pid,
                                     "urls": [_u(n) for n in ns]})

    def test_every_surface_follows(self):
        self.assertEqual(self.reorder(3, 1, 2),
                         (200, {"ok": True, "count": 3}))
        self.assertEqual(self.order(), ["3", "1", "2"])
        got = self.subsonic("getPlaylist", id=f"pl:{self.pid}")
        self.assertEqual([e["title"] for e in got["playlist"]["entry"]],
                         ["T3", "T1", "T2"])
        xml = api_upnp._gw_browse(f"pl:{self.pid}", "BrowseDirectChildren",
                                  0, 0)[0]
        at = [xml.index(_u(n)) for n in (3, 1, 2)]
        self.assertEqual(at, sorted(at))

    def test_subsonic_index_removal_uses_the_new_order(self):
        self.reorder(3, 1, 2)
        self.subsonic("updatePlaylist", playlistId=f"pl:{self.pid}",
                      songIndexToRemove=["0"])
        self.assertEqual(self.order(), ["1", "2"])

    def test_a_different_list_changes_nothing(self):
        for ns in ((3, 1), (3, 1, 2, 9), (3, 1, 9)):
            self.assert_refused(self.reorder(*ns), 409, "playlist_changed")
        self.assert_refused(self.reorder(3, 1, 1), 400, "bad_request")
        self.assert_refused(self.reorder(3, 1, 2, 2), 400, "bad_request")
        self.assertEqual(self.order(), ["1", "2", "3"])

    def test_refusals(self):
        self.assert_refused(self.reorder(1, pl_id="nope"),
                            404, "playlist_not_found")
        for payload in ({"id": self.pid, "urls": []}, {"id": self.pid},
                        {"urls": [_u(1)]},
                        {"id": self.pid, "urls": [str(i) for i in range(2001)]}):
            self.assert_refused(self.post("reorder", payload),
                                400, "bad_request")

    def test_later_additions_come_last(self):
        self.reorder(3, 1, 2)
        self.db.pl_add_track(self.pid, _t(4))
        self.post("add_many", {"pl": self.pid, "tracks": [_t(6), _t(5)]})
        self.assertEqual(self.order(), ["3", "1", "2", "4", "6", "5"])

    def test_gaps_are_fine(self):
        self.reorder(3, 1, 2)
        self.post("remove_many", {"pl": self.pid, "urls": [_u(1)]})
        self.db.pl_add_track(self.pid, _t(1))
        self.assertEqual(self.order(), ["3", "2", "1"])

    def test_favourites(self):
        for n in (1, 2):
            self.db.pl_add_track(FAVOURITES_ID, _t(n))
        self.assertEqual(self.reorder(2, 1, pl_id=FAVOURITES_ID)[0], 200)
        self.assertEqual(self.order(FAVOURITES_ID), ["2", "1"])

    def test_subsonic_create_with_an_id_replaces_the_order_too(self):
        tid = api_subsonic._track_id
        self.subsonic("createPlaylist", playlistId=f"pl:{self.pid}",
                      songId=[tid(_u(n)) for n in (9, 3, 1, 3)])
        self.assertEqual(self.order(), ["9", "3", "1"])


class TestPositionColumn(_Base):
    def _make_old(self):
        """Put the table back to what the previous version wrote, with ids
        and added_at disagreeing so the order is not an accident."""
        with self.db._pool.write() as c:
            c.execute("ALTER TABLE playlist_tracks DROP COLUMN position")
            for n, day in ((1, 3), (2, 1), (3, 2)):
                c.execute("UPDATE playlist_tracks SET added_at=? "
                          "WHERE pl_id=? AND url=?",
                          (f"2026-01-0{day} 10:00:00", self.pid, _u(n)))
            for n in (5, 4):                       # same second → by id
                c.execute("INSERT INTO playlist_tracks (pl_id, url, added_at)"
                          " VALUES (?,?,'2026-01-09')", (FAVOURITES_ID, _u(n)))

    def _positions(self):
        with self.db._pool.read() as c:
            return [tuple(r) for r in c.execute(
                "SELECT id, position FROM playlist_tracks ORDER BY id")]

    def _loose(self, n, pl_id=None):
        """A row from code that does not know the column."""
        with self.db._pool.write() as c:
            c.execute("INSERT INTO playlist_tracks (pl_id, url, added_at) "
                      "VALUES (?,?,'2001-01-01')", (pl_id or self.pid, _u(n)))

    def test_an_old_database_keeps_every_order(self):
        self._make_old()
        self.db = LibraryDB(db_file=self.path)
        dlna_asgi_state.DB = self.db
        self.assertEqual(self.order(), ["2", "3", "1"])
        self.assertEqual(self.order(FAVOURITES_ID), ["5", "4"])
        self.assertNotIn(None, [p for _, p in self._positions()])
        self.db.pl_add_track(self.pid, _t(8))
        self.assertEqual(self.order(), ["2", "3", "1", "8"])

    def test_twice_changes_nothing(self):
        self._make_old()
        LibraryDB(db_file=self.path)
        self.db.pl_reorder(self.pid, [_u(1), _u(2), _u(3)])
        before = self._positions()
        LibraryDB(db_file=self.path)
        self.assertEqual(self._positions(), before)

    def test_column_and_values_land_together(self):
        """Died in between → no column, so the next start does it again. A
        column left unfilled would never be filled."""
        self._make_old()

        class Dies:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, sql, *a):
                if sql.startswith("UPDATE"):
                    raise sqlite3.OperationalError("killed")
                return self.conn.execute(sql, *a)
        with self.assertRaises(sqlite3.OperationalError), \
                self.db._pool.write() as c:
            self.db._migrate_playlist_position(Dies(c))
        with self.db._pool.read() as c:
            cols = {r[1] for r in c.execute(
                "PRAGMA table_info(playlist_tracks)")}
        self.assertNotIn("position", cols)

    def test_a_row_without_a_position_comes_last(self):
        self._loose(0)
        self.assertEqual(self.order(), ["1", "2", "3", "0"])

    def test_a_track_added_after_such_a_row_comes_after_it(self):
        self._loose(0)
        self.db.pl_add_track(self.pid, _t(4))
        self._loose("00")
        self.post("add_many", {"pl": self.pid, "tracks": [_t(5)]})
        self.assertEqual(self.order(), ["1", "2", "3", "0", "4", "00", "5"])

    def test_a_playlist_with_only_such_rows(self):
        pid = self.db.pl_create("Imported")
        self._loose("a", pid)
        self._loose("b", pid)
        self.db.pl_add_track(pid, _t("c"))
        self.assertEqual(self.order(pid), ["a", "b", "c"])


class TestTheRoutes(_Base):
    def test_get_is_405(self):
        for name in WRITES:
            self.assertEqual(
                _request("GET", f"/api/playlist/{name}",
                         query=f"id={self.pid}&name=x")[0], 405, name)
        self.assertEqual(self.name(), "Mix")

    def test_cross_site_is_403(self):
        for name in WRITES:
            for hdrs in ([("sec-fetch-site", "cross-site")],
                         [("origin", "https://evil.example")]):
                self.assertEqual(
                    _request("POST", f"/api/playlist/{name}",
                             query=f"id={self.pid}&name=x",
                             headers=hdrs)[0], 403, name)
        self.assertEqual(self.name(), "Mix")

    def test_a_broken_body_is_400(self):
        for name in WRITES[1:]:
            for body in (b"", b"{", b"null", b"[]", b'"x"', b"7", b"\xff\xfe",
                         b"[" * 200_000, b'{"pl": ' + b"9" * 5000 + b"}"):
                self.assert_refused(
                    _request("POST", f"/api/playlist/{name}", body=body),
                    400, "bad_request")

    def test_an_unknown_route_has_no_code(self):
        """How a client tells an older gateway from a missing playlist."""
        status, body = self.post("no_such_call", {})
        self.assertEqual(status, 404)
        self.assertNotIn("code", body)


if __name__ == "__main__":
    unittest.main()
