#!/usr/bin/env python3
"""
tests/test_resource_limits.py — what one peer is allowed to consume.

Track B2 of the public-release plan. The question is not "can it be broken"
but "is anything unbounded", because every unbounded resource here is reached
without authentication, and the way this gateway dies of exhaustion is
especially unhelpful: SQLite reports `unable to open database file`, which
reads like corruption rather than like a flood.

Three things were unbounded, and all three were measured on the running
gateway before being fixed:

  * **Reads from devices.** Anything that answers an SSDP packet becomes a
    "renderer" or "server" the gateway then talks SOAP to, and every SOAP
    response was read with a bare `resp.read()`. `safe_fromstring`'s 4 MB cap
    cannot help: it only sees bytes already in memory.
  * **Audio relays.** 40 stalled `/stream` requests cost 120 file
    descriptors — client socket, upstream socket, and the LocalFs server's
    own accepted socket — and 80 survived the client disconnecting, draining
    over about a minute. Nothing limited how many could exist.
  * **SSE subscribers.** Each connection is a socket, a task and a queue,
    with no ceiling on how many.

Worth recording what turned out NOT to be a problem, so nobody re-fixes it:
half-open requests (classic slowloris) are reaped by hypercorn's
`keep_alive_timeout` in about five seconds, the shared threadpool ceiling of
256 absorbed 150 concurrent full pulls with zero failures, and `/art` already
capped its read at 12 MB *during* the read, which is the pattern the rest now
follow.
"""
import io
import os
import sys
import time
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import dlna_asgi_state as _st          # noqa: E402
import dlna_xml                        # noqa: E402

# Fetched through the MODULE, not `from dlna_xml import read_capped`. The
# difference matters for the standard this project holds itself to: a
# regression test has to be verified failing on the UNFIXED code, and a
# missing name in a module-level `from ... import` is an ImportError at
# collection time — one error for the whole file, proving nothing about
# behaviour. Resolved per-test instead, each assertion fails on its own and
# says what is missing.
MAX_XML_BYTES = dlna_xml.MAX_XML_BYTES


def read_capped(*a, **kw):
    fn = getattr(dlna_xml, "read_capped", None)
    if fn is None:
        raise AssertionError(
            "dlna_xml.read_capped is missing — reads from network peers are "
            "unbounded")
    return fn(*a, **kw)


def _cap(limit, what):
    cls = getattr(_st, "ConcurrencyCap", None)
    if cls is None:
        raise AssertionError(
            "dlna_asgi_state.ConcurrencyCap is missing — long-lived "
            "connections are uncapped")
    return cls(limit, what)


class _Endless(io.RawIOBase):
    """A response body that never ends — what a hostile or broken device
    hands back. `read()` with no argument would never return."""

    def __init__(self):
        self.asked = []

    def read(self, n=-1):
        self.asked.append(n)
        if n is None or n < 0:
            raise AssertionError("read() with no limit would never return")
        return b"x" * n


class TestReadCapped(unittest.TestCase):
    def test_it_never_reads_without_a_limit(self):
        body = _Endless()
        read_capped(body, what="test")
        self.assertTrue(body.asked)
        for n in body.asked:
            self.assertGreater(n, 0)

    def test_an_oversized_body_yields_nothing(self):
        """Empty, not an exception: every caller already treats an empty body
        as 'the device said nothing useful', so a hostile response takes a
        path that already exists."""
        self.assertEqual(read_capped(_Endless(), what="test"), b"")

    def test_a_normal_body_is_returned_whole(self):
        self.assertEqual(read_capped(io.BytesIO(b"<root/>"), what="test"),
                         b"<root/>")

    def test_a_body_exactly_at_the_cap_is_kept(self):
        big = b"y" * 64
        self.assertEqual(read_capped(io.BytesIO(big), what="test", max_bytes=64),
                         big)

    def test_one_byte_over_the_cap_is_refused(self):
        self.assertEqual(
            read_capped(io.BytesIO(b"y" * 65), what="test", max_bytes=64), b"")

    def test_a_read_error_is_not_raised_at_the_caller(self):
        class _Broken:
            def read(self, n=-1):
                raise OSError("connection reset")
        self.assertEqual(read_capped(_Broken(), what="test"), b"")

    def test_the_default_cap_matches_the_parser(self):
        """One number, so a body that would be refused by the parser is not
        read into memory first."""
        big = io.BytesIO(b"z" * (MAX_XML_BYTES + 1))
        self.assertEqual(read_capped(big, what="test"), b"")


class TestEverySoapPathIsCapped(unittest.TestCase):
    """The fix is only worth anything if no call site was missed."""

    def test_no_bare_read_in_the_device_facing_modules(self):
        import re
        offenders = []
        for name in ("dlna_content.py", "dlna_avtransport.py",
                     "dlna_rendering_control.py", "dlna_discovery.py",
                     "dlna_art_fetcher.py", "dlna_lyrics.py",
                     "api_radio.py", "dlna_geocode.py"):
            with open(os.path.join(PROJECT, name), encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if line.lstrip().startswith(("#", "*")):
                        continue
                    if re.search(r"\bresp\.read\(\)|\br\.read\(\)", line):
                        offenders.append(f"{name}:{n}")
        self.assertEqual(offenders, [],
                         "unbounded read from a network peer")


class TestConcurrencyCap(unittest.TestCase):
    def setUp(self):
        self.cap = _cap(3, "test")

    def test_it_admits_up_to_the_limit(self):
        self.assertEqual([self.cap.acquire() for _ in range(3)],
                         [True, True, True])

    def test_it_refuses_past_the_limit(self):
        for _ in range(3):
            self.cap.acquire()
        self.assertFalse(self.cap.acquire())
        self.assertEqual(self.cap.in_flight, 3)

    def test_releasing_frees_a_slot(self):
        for _ in range(3):
            self.cap.acquire()
        self.cap.release()
        self.assertTrue(self.cap.acquire())

    def test_a_double_release_cannot_invent_capacity(self):
        """A leaked release would silently raise the real ceiling."""
        self.cap.acquire()
        self.cap.release()
        self.cap.release()
        self.cap.release()
        self.assertEqual(self.cap.in_flight, 0)
        self.assertEqual([self.cap.acquire() for _ in range(4)],
                         [True, True, True, False])

    def test_the_shipped_caps_exist_and_are_sane(self):
        caps = [getattr(_st, n, None) for n in ("AUDIO_RELAYS", "SSE_STREAMS")]
        self.assertNotIn(None, caps, "a shipped concurrency cap is missing")
        for cap in caps:
            with self.subTest(cap=cap.what):
                self.assertGreaterEqual(cap.limit, 16,
                                        "would break real household use")
                self.assertLessEqual(cap.limit, 256,
                                     "too high to bound anything")


class TestRelayRefusesPastTheCap(unittest.TestCase):
    """End to end through the real handler: at the cap it must 503 without
    even opening an upstream connection."""

    def test_503_and_no_upstream_dialled(self):
        import asyncio
        from unittest import mock

        import dlna_asgi_media as media

        opened = []

        def _never(*a, **k):
            opened.append(a)
            return (None, None)

        taken = 0
        try:
            while _st.AUDIO_RELAYS.acquire():
                taken += 1
                if taken > _st.AUDIO_RELAYS.limit:
                    self.fail("cap is not enforced")
            with mock.patch.object(media.dlna_stream_proxy,
                                   "open_stream_upstream", _never):
                r = asyncio.run(media._audio_relay_response(
                    "http://192.168.1.5:8200/localfs/stream/x", ""))
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.headers.get("Retry-After"), "5")
            self.assertEqual(opened, [], "dialled upstream while at the cap")
        finally:
            for _ in range(taken):
                _st.AUDIO_RELAYS.release()

    def test_a_failed_upstream_does_not_leak_a_slot(self):
        """The 502 path returns early — if it forgot to release, the cap
        would ratchet down to zero over a few unreachable streams."""
        import asyncio
        from unittest import mock

        import dlna_asgi_media as media

        before = _st.AUDIO_RELAYS.in_flight
        with mock.patch.object(media.dlna_stream_proxy,
                               "open_stream_upstream",
                               lambda *a, **k: (None, None)):
            for _ in range(5):
                r = asyncio.run(media._audio_relay_response("http://x/y", ""))
                self.assertEqual(r.status_code, 502)
        self.assertEqual(_st.AUDIO_RELAYS.in_flight, before)




class TestSnapshotAllFansOutInParallel(unittest.TestCase):
    """The B4 finding. Queues are never evicted, so this walks every renderer
    ever seen, including switched-off ones. Sequentially, the response time
    was the SUM of every dead renderer's SOAP timeout.

    Measured live with the LG TV off: /api/renderer_state (no udn) 6.011 s,
    the reachable Naim 2 ms. The PWA polls it every second while playing.
    """

    def _registry(self, n: int, delay: float):
        import dlna_player_registry as reg

        class _SlowQueue:
            _snap_cache = {"state": "stopped", "alive": False}

            def __init__(self, udn):
                self.udn = udn

            def snapshot(self):
                time.sleep(delay)            # stands in for a SOAP timeout
                return {"state": "stopped", "alive": False,
                        "renderer": self.udn}

        r = reg.QueueRegistry()
        r._queues = {f"uuid:{i}": _SlowQueue(f"uuid:{i}") for i in range(n)}
        return r

    def test_four_slow_renderers_cost_one_delay_not_four(self):
        r = self._registry(4, 0.4)
        t0 = time.monotonic()
        snaps = r.snapshot_all()
        elapsed = time.monotonic() - t0
        self.assertEqual(len(snaps), 4)
        # Sequential would be ~1.6 s. Allow generous slack for a loaded CI box
        # while still being nowhere near the sequential cost.
        self.assertLess(elapsed, 1.0,
                        f"snapshot_all took {elapsed:.2f}s — still sequential?")

    def test_every_queue_is_still_reported(self):
        r = self._registry(5, 0.0)
        self.assertEqual(sorted(r.snapshot_all()), [f"uuid:{i}" for i in range(5)])

    def test_a_queue_that_never_answers_is_served_from_cache(self):
        """A wedged renderer must not remove its own key or hold up the rest."""
        import dlna_player_registry as reg

        class _Wedged:
            _snap_cache = {"state": "stopped", "alive": False,
                           "renderer": "wedged"}

            def snapshot(self):
                time.sleep(30)

        class _Fast:
            _snap_cache = {}

            def snapshot(self):
                return {"state": "playing", "alive": True, "renderer": "fast"}

        r = reg.QueueRegistry()
        r._queues = {"uuid:wedged": _Wedged(), "uuid:fast": _Fast()}
        r._ALL_TIMEOUT_SEC = 0.5
        t0 = time.monotonic()
        snaps = r.snapshot_all()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 5.0, "a wedged renderer held up the response")
        self.assertEqual(sorted(snaps), ["uuid:fast", "uuid:wedged"])
        self.assertEqual(snaps["uuid:fast"]["renderer"], "fast")
        self.assertEqual(snaps["uuid:wedged"]["renderer"], "wedged")

    def test_the_single_queue_case_is_unchanged(self):
        r = self._registry(1, 0.0)
        self.assertEqual(len(r.snapshot_all()), 1)

    def test_no_queues_is_empty(self):
        import dlna_player_registry as reg
        self.assertEqual(reg.QueueRegistry().snapshot_all(), {})

class TestUnreachableRendererBackoff(unittest.TestCase):
    """The rest of the B4 finding. Fanning out in parallel stopped the cost
    being the SUM over dead renderers, but ONE dead renderer still cost a full
    ~6 s TCP connect timeout — and with a 500 ms snapshot cache, the next poll
    paid it again. Since queues are never evicted, that is the steady state
    for any renderer used once and then switched off, and the PWA polls this
    every second while audio plays.
    """

    def _queue(self, state):
        import dlna_player
        q = dlna_player.RendererQueue()
        q._av_url = "http://192.168.1.99:1/AVTransport"
        q._tracks = [{"title": "t", "url": "u"}]
        q._rnd_name = "Off TV"
        calls = []

        def _state(url):
            calls.append(url)
            return state

        return q, calls, _state

    def test_a_dead_renderer_is_dialled_once_not_every_poll(self):
        from unittest import mock

        import dlna_avtransport
        q, calls, _state = self._queue("UNREACHABLE")
        with mock.patch.object(dlna_avtransport, "avtransport_get_state", _state),              mock.patch.object(dlna_avtransport, "avtransport_get_position",
                               lambda u: None):
            q.snapshot()
            first = len(calls)
            for _ in range(5):
                q._snap_cache_at = 0.0        # expire the 500 ms cache
                q.snapshot()
        self.assertEqual(len(calls), first,
                         "re-dialled an unreachable renderer inside the backoff")

    def test_the_cached_state_is_still_served(self):
        from unittest import mock

        import dlna_avtransport
        q, _, _state = self._queue("UNREACHABLE")
        with mock.patch.object(dlna_avtransport, "avtransport_get_state", _state),              mock.patch.object(dlna_avtransport, "avtransport_get_position",
                               lambda u: None):
            q.snapshot()
            q._snap_cache_at = 0.0
            snap = q.snapshot()
        self.assertFalse(snap["alive"])
        self.assertEqual(snap["renderer"], "Off TV")

    def test_a_reachable_renderer_is_never_backed_off(self):
        from unittest import mock

        import dlna_avtransport
        q, calls, _state = self._queue("PLAYING")
        with mock.patch.object(dlna_avtransport, "avtransport_get_state", _state),              mock.patch.object(dlna_avtransport, "avtransport_get_position",
                               lambda u: {"position": "0:00:01",
                                          "duration": "0:03:00", "title": "t"}):
            for _ in range(4):
                q._snap_cache_at = 0.0
                q.snapshot()
        self.assertEqual(len(calls), 4, "a healthy renderer must be polled")
        self.assertEqual(q._unreachable_until, 0.0)

    def test_the_backoff_expires_so_a_renderer_can_come_back(self):
        from unittest import mock

        import dlna_avtransport
        q, calls, _state = self._queue("UNREACHABLE")
        with mock.patch.object(dlna_avtransport, "avtransport_get_state", _state),              mock.patch.object(dlna_avtransport, "avtransport_get_position",
                               lambda u: None):
            q.snapshot()
            q._snap_cache_at = 0.0
            q._unreachable_until = 0.0        # as if the window had passed
            q.snapshot()
        self.assertEqual(len(calls), 2)

    def test_the_backoff_is_bounded(self):
        import dlna_player
        self.assertLessEqual(dlna_player.RendererQueue._UNREACHABLE_BACKOFF_SEC, 120)


class TestPlayerModulesImportInEitherOrder(unittest.TestCase):
    """`dlna_player` and `dlna_player_registry` need each other: the registry
    builds queues, and dlna_player re-exports QUEUES from the registry because
    two production modules and a test spell it `from dlna_player import
    QUEUES`.

    While the registry's import sat at module level, that pair could only load
    in ONE order — `import dlna_player_registry` FIRST raised `ImportError:
    cannot import name 'QUEUES'`. Never a production fault (the app imports
    dlna_player first), but a trap for tests, tools and REPL sessions, which
    import whatever they actually care about. It cost real time during the
    2026-08-21 audit session.

    Subprocesses, because import order is process-global: once anything in
    this suite has imported dlna_player, the bad order is unreachable in-process.
    """

    def _run(self, code: str):
        import subprocess
        return subprocess.run([sys.executable, "-c", code], cwd=PROJECT,
                              capture_output=True, text=True, timeout=60)

    def test_registry_first(self):
        r = self._run("import dlna_player_registry as m; "
                      "assert m.QUEUES is not None; print('ok')")
        self.assertEqual(r.returncode, 0,
                         f"registry-first import failed:\n{r.stderr}")

    def test_player_first(self):
        r = self._run("from dlna_player import QUEUES, QueueRegistry; "
                      "assert QUEUES is not None; print('ok')")
        self.assertEqual(r.returncode, 0,
                         f"player-first import failed:\n{r.stderr}")

    def test_both_spellings_are_the_same_singleton(self):
        r = self._run("import dlna_player_registry as reg; "
                      "from dlna_player import QUEUES; "
                      "assert QUEUES is reg.QUEUES; print('ok')")
        self.assertEqual(r.returncode, 0,
                         f"the two spellings diverged:\n{r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
