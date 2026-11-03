"""Forgetting a renderer — POST /api/renderers/forget (2026-11-03).

Built so `tests/chaos.py --fake-renderer` can take its fake renderer away
again: the gateway otherwise never forgets one. The two things that would
make it dangerous are tested hardest — it must not strand a LIVE device
(the seen-location is cleared so discovery can bring it back), and a LAN
peer must not be able to call it (loopback only).
"""
import asyncio
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from starlette.requests import Request  # noqa: E402

import dlna_asgi  # noqa: E402,F401 — the router must be mounted
import dlna_asgi_devices  # noqa: E402
import dlna_discovery  # noqa: E402
import dlna_player  # noqa: E402
from dlna_devices import DeviceRoleCache  # noqa: E402
from dlna_player_registry import QueueRegistry  # noqa: E402
from dlna_registry import MediaRenderer, RendererRegistry  # noqa: E402

FAKE = "uuid:fake-renderer"
LOC = "http://127.0.0.1:5555/desc.xml"


class _RolesDB:
    """The two calls DeviceRoleCache makes, recorded."""
    def __init__(self, rows=None):
        self.rows = dict(rows or {})

    def roles_load(self):
        return dict(self.rows)

    def role_set(self, udn, name, location="", host="", is_server=False,
                 is_renderer=False):
        self.rows[udn] = {"name": name, "location": location, "host": host,
                          "is_server": is_server, "is_renderer": is_renderer}

    def roles_delete(self, udn):
        return 1 if self.rows.pop(udn, None) is not None else 0


def _renderer(udn=FAKE, loc=LOC):
    return MediaRenderer(udn=udn, name="Fake", location=loc,
                         av_url="http://127.0.0.1:5555/av", base_url="")


class TestRendererRegistryRemove(unittest.TestCase):
    def test_remove_returns_it_and_it_is_gone(self):
        reg = RendererRegistry()
        reg.add(_renderer())
        self.assertEqual(reg.remove(FAKE).udn, FAKE)
        self.assertIsNone(reg.get(FAKE))
        self.assertIsNone(reg.remove(FAKE))


class TestRoleCacheForget(unittest.TestCase):
    def test_forget_drops_cache_and_row(self):
        cache = DeviceRoleCache(_RolesDB())
        cache.mark(FAKE, "Fake", location=LOC, host="127.0.0.1",
                   is_renderer=True)
        self.assertTrue(cache.forget(FAKE))
        self.assertIsNone(cache.get(FAKE))
        self.assertEqual(cache._db.rows, {})
        self.assertFalse(cache.forget(FAKE))

    def test_host_index_is_rebuilt_not_left_stale(self):
        """A renderer on a server's host hides that server from the boot
        pre-probe. Once the renderer is forgotten the server must be
        pre-probed again — the index has to be rebuilt, not just pruned."""
        cache = DeviceRoleCache(_RolesDB())
        cache.mark("uuid:srv", "Server", location="http://127.0.0.1:9/d.xml",
                   host="127.0.0.1", is_server=True)
        cache.mark(FAKE, "Fake", location=LOC, host="127.0.0.1",
                   is_renderer=True)
        self.assertEqual(cache.known_servers(), [])
        cache.forget(FAKE)
        self.assertEqual([s["udn"] for s in cache.known_servers()],
                         ["uuid:srv"])


class _FakeQueue:
    def __init__(self, alive):
        self.alive = alive
        self.stopped = False

    def snapshot(self):
        return {"alive": self.alive}

    def stop(self):
        self.stopped = True


class TestQueueDiscard(unittest.TestCase):
    def test_playing_queue_is_stopped_then_dropped(self):
        reg = QueueRegistry()
        q = _FakeQueue(alive=True)
        reg._queues[FAKE] = q
        self.assertTrue(reg.discard(FAKE))
        self.assertTrue(q.stopped)
        self.assertIsNone(reg.peek(FAKE))

    def test_idle_queue_is_not_sent_a_stop(self):
        reg = QueueRegistry()
        q = _FakeQueue(alive=False)
        reg._queues[FAKE] = q
        reg.discard(FAKE)
        self.assertFalse(q.stopped)

    def test_unknown_udn(self):
        self.assertFalse(QueueRegistry().discard(FAKE))


class TestForgetRenderer(unittest.TestCase):
    def setUp(self):
        self.reg = RendererRegistry()
        self.roles = DeviceRoleCache(_RolesDB())
        self.queues = QueueRegistry()
        for target, new in ((dlna_discovery, "RENDERERS"),
                            (dlna_discovery, "DEVICE_ROLES"),
                            (dlna_player, "QUEUES")):
            value = {"RENDERERS": self.reg, "DEVICE_ROLES": self.roles,
                     "QUEUES": self.queues}[new]
            p = mock.patch.object(target, new, value)
            p.start()
            self.addCleanup(p.stop)
        seen = mock.patch.object(dlna_discovery, "_seen_locations", set())
        self.seen = seen.start()
        self.addCleanup(seen.stop)

    def _register(self):
        self.reg.add(_renderer())
        self.roles.mark(FAKE, "Fake", location=LOC, host="127.0.0.1",
                        is_renderer=True)
        self.seen.add(LOC)

    def test_forgets_everywhere(self):
        self._register()
        q = _FakeQueue(alive=True)
        self.queues._queues[FAKE] = q
        self.assertTrue(dlna_discovery.forget_renderer(FAKE))
        self.assertIsNone(self.reg.get(FAKE))
        self.assertIsNone(self.roles.get(FAKE))
        self.assertTrue(q.stopped)

    def test_location_is_released_so_a_live_device_can_come_back(self):
        """Without this, forgetting the Naim by mistake would hide it until
        a restart: `_register_location` skips every location it has seen."""
        self._register()
        dlna_discovery.forget_renderer(FAKE)
        self.assertNotIn(LOC, self.seen)

    def test_unknown_udn_is_refused(self):
        self.assertFalse(dlna_discovery.forget_renderer("uuid:nobody"))

    def test_row_left_by_a_crashed_run_can_still_be_forgotten(self):
        """After a restart the renderer is not live, but its row remains."""
        self.roles.mark(FAKE, "Fake", location=LOC, host="127.0.0.1",
                        is_renderer=True)
        self.assertTrue(dlna_discovery.forget_renderer(FAKE))
        self.assertIsNone(self.roles.get(FAKE))

    def test_a_media_server_is_not_forgotten(self):
        self.roles.mark("uuid:srv", "Server", location="http://x/d.xml",
                        host="10.0.0.2", is_server=True)
        self.assertFalse(dlna_discovery.forget_renderer("uuid:srv"))
        self.assertIsNotNone(self.roles.get("uuid:srv"))


def _request(body, host="127.0.0.1"):
    scope = {"type": "http", "method": "POST",
             "path": "/api/renderers/forget", "headers": [],
             "query_string": b"", "client": (host, 50000)}
    data = body if isinstance(body, bytes) else json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    return Request(scope, receive)


def _call(body, host="127.0.0.1"):
    resp = asyncio.run(dlna_asgi_devices.forget_renderer_route(
        _request(body, host)))
    return resp.status_code, json.loads(resp.body)


class TestRoute(unittest.TestCase):
    def test_is_mounted(self):
        from tests.test_asgi import _flatten
        paths = {getattr(r, "path", "") for r in _flatten(dlna_asgi.app.routes)}
        self.assertIn("/api/renderers/forget", paths)

    def test_lan_peer_is_refused_before_anything_happens(self):
        with mock.patch.object(dlna_discovery, "forget_renderer") as f:
            for host in ("192.168.1.50", "100.64.0.7", "testclient"):
                self.assertEqual(_call({"udn": FAKE}, host)[0], 403, host)
            f.assert_not_called()

    def test_ipv6_loopback_is_allowed(self):
        with mock.patch.object(dlna_discovery, "forget_renderer",
                               return_value=True):
            self.assertEqual(_call({"udn": FAKE}, "::1"),
                             (200, {"forgotten": FAKE}))

    def test_missing_or_garbled_udn_is_400(self):
        with mock.patch.object(dlna_discovery, "forget_renderer") as f:
            for body in ({}, {"udn": ""}, {"udn": 5}, [FAKE], b"{not json",
                         b"\xff\xfe", b""):
                self.assertEqual(_call(body)[0], 400, body)
            f.assert_not_called()

    def test_unknown_is_404(self):
        with mock.patch.object(dlna_discovery, "forget_renderer",
                               return_value=False):
            self.assertEqual(_call({"udn": FAKE})[0], 404)


if __name__ == "__main__":
    unittest.main()
