#!/usr/bin/env python3
"""
tests/test_hypercorn_conf.py — the listen addresses hypercorn actually gets.

WHY THIS FILE EXISTS. `hypercorn_conf.py` is not imported by the app; hypercorn
EXECUTES it and reads the leftover namespace, so nothing else in the suite ever
touches it — and a mistake there is invisible until a real boot. The
fresh-clone boot test on 2026-08-21 found exactly that: with no certificate the
file left `bind = []` and the addresses in `insecure_bind`, but hypercorn only
reads `insecure_bind` when TLS is enabled (`create_sockets`), so the gateway
came up with ZERO listening sockets while logging a perfectly healthy startup
and announcing itself on SSDP. Every request got connection-refused.

The tests execute the file the way hypercorn does (exec into a fresh namespace)
and assert on what survives.
"""
import os
import pickle
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

CONF = os.path.join(PROJECT, "hypercorn_conf.py")

# Every variable the conf file reads. Tests set ALL of them so the developer's
# real .env (loaded by dlna_config on import) can never change an outcome.
_KEYS = (
    "GATEWAY_BIND_TLS",
    "GATEWAY_BIND_PLAIN",
    "GATEWAY_CERTFILE",
    "GATEWAY_KEYFILE",
    "TAILSCALE_CERT_HOST",
)


def _run_conf(**env) -> dict:
    """Execute hypercorn_conf.py as hypercorn does; return its namespace."""
    full = dict.fromkeys(_KEYS, "")
    full.update(env)
    with open(CONF, encoding="utf-8") as f:
        src = f.read()
    ns: dict = {"__file__": CONF, "__name__": "module.name"}
    with patch.dict(os.environ, full, clear=False):
        exec(compile(src, CONF, "exec"), ns)  # noqa: S102 — that IS the contract
    return ns


class TestListeners(unittest.TestCase):
    def test_without_a_certificate_the_plain_addresses_are_served(self):
        """The regression. No cert → the plain binds must land in `bind`.

        Fails on the pre-2026-08-21 file, which left bind=[] and hypercorn
        listening on nothing at all.
        """
        ns = _run_conf(GATEWAY_BIND_PLAIN="127.0.0.1:18765")
        self.assertEqual(ns["bind"], ["127.0.0.1:18765"])
        self.assertEqual(ns["insecure_bind"], [])
        self.assertIsNone(ns["certfile"])

    def test_without_a_certificate_the_tls_addresses_are_dropped(self):
        """A TLS bind with no cert is not silently served in the clear."""
        ns = _run_conf(
            GATEWAY_BIND_TLS="127.0.0.1:18443",
            GATEWAY_BIND_PLAIN="127.0.0.1:18765",
        )
        self.assertEqual(ns["bind"], ["127.0.0.1:18765"])
        self.assertNotIn("127.0.0.1:18443", ns["bind"])

    def test_with_a_certificate_both_tiers_are_served(self):
        """The deployed shape: TLS on `bind`, plain on `insecure_bind`."""
        with tempfile.TemporaryDirectory() as d:
            crt = os.path.join(d, "x.crt")
            key = os.path.join(d, "x.key")
            for p in (crt, key):
                with open(p, "w", encoding="utf-8") as f:
                    f.write("not a real key")
            ns = _run_conf(
                GATEWAY_BIND_TLS="127.0.0.1:18443,100.0.0.1:18443",
                GATEWAY_BIND_PLAIN="127.0.0.1:18765",
                GATEWAY_CERTFILE=crt,
                GATEWAY_KEYFILE=key,
            )
        self.assertEqual(ns["bind"], ["127.0.0.1:18443", "100.0.0.1:18443"])
        self.assertEqual(ns["insecure_bind"], ["127.0.0.1:18765"])
        self.assertEqual(ns["certfile"], crt)
        self.assertEqual(ns["keyfile"], key)

    def test_unset_addresses_fall_back_to_all_interfaces(self):
        """A fresh clone with an unedited .env still boots (0.0.0.0)."""
        ns = _run_conf()
        self.assertEqual(ns["bind"], ["0.0.0.0:8765"])

    def test_no_usable_address_fails_loudly(self):
        """Zero listeners is never intended, and silence is unloggable.

        A stray comma is the realistic typo; it must stop the boot rather
        than start a server that answers nothing.
        """
        with self.assertRaises(RuntimeError) as cm:
            _run_conf(GATEWAY_BIND_PLAIN=" , ")
        self.assertIn("no listen address", str(cm.exception))


class TestNamespaceStaysPicklable(unittest.TestCase):
    """hypercorn pickles the config to hand to workers — see the docstring
    in hypercorn_conf.py. A `def`, class or module left at module scope kills
    the server with a confusing "Can't pickle …: No module named 'module'"."""

    def test_everything_hypercorn_keeps_can_be_pickled(self):
        ns = _run_conf(GATEWAY_BIND_PLAIN="127.0.0.1:18765")
        # from_object() copies exactly the non-underscore names.
        kept = {k: v for k, v in ns.items() if not k.startswith("_")}
        self.assertIn("bind", kept)
        for name, value in kept.items():
            with self.subTest(name=name):
                pickle.dumps(value)


class TestLaunchAgentTemplate(unittest.TestCase):
    """The committed plist is what a newcomer installs.

    It ran `python dlna_gateway.py --no-browser` until 2026-08-21 — an
    entrypoint that has not served anything since 2.0: it prints a hint and
    exits, so the job dies at login and nothing listens. Same class of bug as
    the Linux/Windows quick-starts corrected on 2026-08-20.
    """

    def setUp(self):
        p = os.path.join(PROJECT, "com.roha.dlna-gateway.plist")
        with open(p, encoding="utf-8") as f:
            self.plist = f.read()

    def test_the_job_launches_the_asgi_app(self):
        self.assertIn("<string>dlna_asgi:app</string>", self.plist)
        self.assertIn("/.venv/bin/hypercorn</string>", self.plist)
        self.assertIn("hypercorn_conf.py</string>", self.plist)

    def test_the_job_does_not_launch_the_retired_entrypoint(self):
        args = self.plist.split("<key>ProgramArguments</key>", 1)[1]
        args = args.split("</array>", 1)[0]
        self.assertNotIn("dlna_gateway.py", args)


if __name__ == "__main__":
    unittest.main(verbosity=2)
