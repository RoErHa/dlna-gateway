#!/usr/bin/env python3
"""
tests/test_no_silent_swallows.py — no exception may be caught BROADLY and
discarded SILENTLY in application code.

The 2026-08-20 audit found 50 handlers that caught `Exception` (or bare)
and neither logged, re-raised, nor recorded the failure. That combination
is how a defect hides for a year: the symptom ("my album is missing",
"renderers can't reach the gateway") appears far from the cause, and
`gateway.log` — the first place CLAUDE.md's diagnostic order sends you —
says nothing at all.

THE RULE ENFORCED HERE. A handler may be broad, or it may be silent, but
not both:

  * broad + observable  → fine. `except Exception as e: log.debug(...)`.
    Most swallows became this; the interesting ones became `log.warning`
    (a failed LAN-IP probe means the gateway advertises an address no
    renderer can reach — a total outage that used to be silent).
  * narrow + silent     → fine. `except ValueError: return 0` states a
    real contract and needs no log; `_dur_to_secs` is exactly this.
  * broad + silent      → REJECTED by this test.

Cleanup paths (`conn.close()` in a `finally`) are neither: they route
through `dlna_config.close_quietly`, which is broad ON PURPOSE — it runs
while another exception is propagating and must never replace it — and
logs at debug. One documented decision instead of ~18 anonymous `pass`es.

Scope is application code only. tests/ and tools/ are excluded: a test
asserting that something raises, and a one-shot maintenance script, have
different obligations than a daemon that must stay diagnosable.

Run standalone:  python3 -m unittest tests.test_no_silent_swallows -v
"""
import ast
import os
import pathlib
import sys
import unittest

PROJECT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

_LOG_METHODS = {"debug", "info", "warning", "error", "exception",
                "critical", "warn"}

# Directories whose obligations differ — see the module docstring.
_SKIP_TOP = {"tests", "tools", ".venv", "art_cache", "static", "docs"}


def _app_modules() -> list:
    out = []
    for p in sorted(PROJECT.rglob("*.py")):
        rel = p.relative_to(PROJECT)
        if rel.parts[0] in _SKIP_TOP:
            continue
        out.append(p)
    return out


def _is_broad(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:                       # bare `except:`
        return True
    return (isinstance(handler.type, ast.Name)
            and handler.type.id in ("Exception", "BaseException"))


def _is_observable(handler: ast.ExceptHandler) -> bool:
    """Does the handler leave ANY trace? A log call, a re-raise, or
    recording the failure somewhere a caller can see it."""
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _LOG_METHODS or node.func.attr == "append":
                return True
    return False


class TestNoSilentSwallows(unittest.TestCase):

    def test_no_broad_and_silent_handlers(self):
        offenders = []
        for path in _app_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) \
                        and _is_broad(node) and not _is_observable(node):
                    offenders.append(
                        f"{path.relative_to(PROJECT)}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "broad exception handlers that discard the failure silently — "
            "either log it (log.debug is enough) or narrow the except "
            "clause to the exception you actually expect:\n  "
            + "\n  ".join(offenders))

    def test_no_bare_except_anywhere(self):
        """`except:` also swallows KeyboardInterrupt and SystemExit, so a
        Ctrl-C or a shutdown can be silently ignored by a daemon thread."""
        offenders = []
        for path in _app_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler) and node.type is None:
                    offenders.append(
                        f"{path.relative_to(PROJECT)}:{node.lineno}")
        self.assertEqual(offenders, [], f"bare `except:` found: {offenders}")

    def test_close_quietly_exists_and_is_silent_by_design(self):
        """The one sanctioned broad-and-quiet path. If this helper is ever
        deleted, the ~18 cleanup sites will drift back to bare `pass`."""
        from dlna_config import close_quietly
        close_quietly(None)                        # None is a no-op

        class _Boom:
            def close(self):
                raise OSError("already closed")

        close_quietly(_Boom(), "test resource")    # must not propagate

    def test_cleanup_sites_use_the_helper(self):
        """A `try: x.close() / except …: pass` block anywhere in app code
        means someone re-introduced the anonymous pattern."""
        offenders = []
        for path in _app_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try) or len(node.body) != 1:
                    continue
                stmt = node.body[0]
                if not isinstance(stmt, ast.Expr):
                    continue
                call = stmt.value
                if (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "close"
                        and node.handlers
                        and all(isinstance(s, ast.Pass)
                                for h in node.handlers for s in h.body)):
                    offenders.append(
                        f"{path.relative_to(PROJECT)}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "use dlna_config.close_quietly() instead of an anonymous "
            f"try/close/except-pass: {offenders}")


if __name__ == "__main__":
    unittest.main()
