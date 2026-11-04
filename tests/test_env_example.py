"""`.env.example` lists every setting the code reads, and nothing it doesn't (2026-11-04).

`.env.example` is the only documentation a fresh install has for its
configuration, and nothing kept it current: lockfiles, the schema, the badges
and module sizes each have a gate, this file had none. Compared on 2026-11-04
it was missing eight keys the code reads (a custom certificate path, the root
re-check interval, four renew-cert.sh knobs) and documented five keys twice,
while the owner's `.env` held a token nothing in the repo reads any more.

What counts as "read": a string literal passed to `os.environ.get` /
`os.getenv` / `os.environ[...]` / an `env.get(...)` / the `_env` and
`_truthy` helpers, found with `ast` in every tracked .py outside tests/, plus
every `env_get NAME` in renew-cert.sh (the one script that reads `.env`
itself). A commented-out `#KEY=` line in the example counts as documented:
that is how an optional setting with a built-in default is shown.

The `.env` half SKIPS on a clone without one.
"""
import ast
import os
import re
import subprocess
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(PROJECT, ".env.example")
DOTENV = os.path.join(PROJECT, ".env")

# Read by the code but deliberately NOT .env settings.
NOT_ENV_SETTINGS = {
    "ALERT_STATE_DIR": "test isolation only: points alert state at a temp dir",
    "GATEWAY_NO_SERVICES": "a process flag tools set for themselves; in .env "
                           "it would start the gateway with no background services",
    "BACKUP_DIR": "read by backup-library.sh and tools/upgrade.py from their own "
                  "environment, which does not load .env",
}
# The header of the example's block for keys nothing here reads on purpose.
UNREAD_BLOCK = "Not read by the gateway"

_KEY = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_ENV_LINE = re.compile(r"^\s*#?\s?([A-Z][A-Z0-9_]+)=")
_READERS = ("getenv", "_env", "_truthy")


def _tracked(pattern):
    out = subprocess.run(["git", "ls-files", pattern], cwd=PROJECT,
                         capture_output=True, text=True, check=True).stdout
    return [f for f in out.split() if not f.startswith("tests/")]


def _reads(node):
    """The env key this AST node reads, or None."""
    if isinstance(node, ast.Call) and node.args:
        arg = node.args[0]
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            return None
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in ("get", "getenv", "setdefault"):
            if f.attr == "getenv" or "env" in ast.unparse(f.value).lower():
                return arg.value
        if isinstance(f, ast.Name) and f.id in _READERS:
            return arg.value
    if isinstance(node, ast.Subscript) and "environ" in ast.unparse(node.value):
        s = node.slice
        if isinstance(s, ast.Constant) and isinstance(s.value, str):
            return s.value
    return None


def code_keys():
    """{key: [files]} for every env key the code reads."""
    found = {}
    for rel in _tracked("*.py"):
        with open(os.path.join(PROJECT, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), rel)
        for node in ast.walk(tree):
            key = _reads(node)
            if key and _KEY.match(key):
                found.setdefault(key, []).append(rel)
    with open(os.path.join(PROJECT, "renew-cert.sh"), encoding="utf-8") as fh:
        for key in re.findall(r"env_get\s+([A-Z][A-Z0-9_]+)", fh.read()):
            found.setdefault(key, []).append("renew-cert.sh")
    return found


def env_file_keys(path):
    """[(line number, key, is_commented)] for every KEY= line of an env file."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            m = _ENV_LINE.match(line)
            if m:
                out.append((n, m.group(1), line.lstrip().startswith("#")))
    return out


def unread_block_keys():
    """Keys listed under the example's "Not read by the gateway" header."""
    keys, inside = set(), False
    with open(EXAMPLE, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("# ──"):
                inside = UNREAD_BLOCK in line
                continue
            m = _ENV_LINE.match(line)
            if inside and m:
                keys.add(m.group(1))
    return keys


class TestScanner(unittest.TestCase):
    """The comparison is only as good as the scan, so pin what it must see."""

    def test_sees_every_reading_style(self):
        keys = code_keys()
        for key in ("LOCALFS_BASE_URL",      # os.environ.get across lines
                    "CSP_MODE",              # (os.environ if env is None else env).get
                    "ALERT_PUSH_URL",        # the _env helper
                    "GATEWAY_DEBUG",         # the _truthy helper
                    "GATEWAY_CERTFILE",      # _os.environ.get (aliased import)
                    "RENEW_DAYS"):           # renew-cert.sh env_get
            self.assertIn(key, keys)

    def test_reads_only_settings(self):
        self.assertTrue(all(_KEY.match(k) for k in code_keys()))


class TestExample(unittest.TestCase):
    def setUp(self):
        self.example = env_file_keys(EXAMPLE)
        self.documented = {k for _, k, _ in self.example}

    def test_every_key_the_code_reads_is_documented(self):
        missing = {k: v for k, v in code_keys().items()
                   if k not in self.documented and k not in NOT_ENV_SETTINGS}
        self.assertEqual(missing, {}, "read by the code but not in .env.example "
                         "(document it, commented out if it has a default)")

    def test_every_documented_key_is_read(self):
        unread = self.documented - set(code_keys()) - unread_block_keys()
        self.assertEqual(unread, set(), "in .env.example but read by nothing: "
                         f"remove it, or move it under '{UNREAD_BLOCK}'")

    def test_no_key_is_documented_twice(self):
        seen, twice = {}, []
        for n, k, _ in self.example:
            if k in seen:
                twice.append(f"{k} (lines {seen[k]} and {n})")
            seen.setdefault(k, n)
        self.assertEqual(twice, [])

    def test_exemptions_are_still_real(self):
        """An exemption for a key the code no longer reads is a stale excuse."""
        self.assertEqual(set(NOT_ENV_SETTINGS) - set(code_keys()), set())
        self.assertEqual(set(NOT_ENV_SETTINGS) & self.documented, set())


@unittest.skipUnless(os.path.isfile(DOTENV), "no .env (a fresh clone)")
class TestYourDotenv(unittest.TestCase):
    """The owner's real .env — read for its key NAMES only, never a value."""

    def setUp(self):
        self.active = [(n, k) for n, k, commented in env_file_keys(DOTENV)
                       if not commented]

    def test_every_key_is_known(self):
        known = set(code_keys()) | {k for _, k, _ in env_file_keys(EXAMPLE)}
        stray = [f"{k} (line {n})" for n, k in self.active if k not in known]
        self.assertEqual(stray, [], ".env holds a key nothing reads — a typo, "
                         "or a leftover; delete it, or document it in "
                         f".env.example under '{UNREAD_BLOCK}'")

    def test_no_key_is_set_twice(self):
        """Set twice, WHICH line wins depends on the reader: python-dotenv and
        renew-cert.sh take the last, dlna_config's fallback parser the first.
        Found on 2026-11-04 (GATEWAY_CONTACT_EMAIL, same value both times)."""
        seen, twice = {}, []
        for n, k in self.active:
            if k in seen:
                twice.append(f"{k} (lines {seen[k]} and {n})")
            seen.setdefault(k, n)
        self.assertEqual(twice, [])


if __name__ == "__main__":
    unittest.main()
