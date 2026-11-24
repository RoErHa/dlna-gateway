"""The repo is public: nothing about the owner's own setup may be in it (2026-11-24).

The docs, code defaults and fixtures used to name the owner's drives, tailnet
host, addresses, a MAC address and family members. They were replaced by
placeholders; this keeps them out.

Two halves:

  * SHAPES, always on: a tailnet hostname, a MAC address or a home folder must
    be one of the placeholders below.
  * VALUES, only where a `.private-terms` file exists (gitignored, so this test
    publishes nothing): every private value in `.env` (hosts, addresses, drive
    names, logins, secrets) plus every line of that file. It is opt-in because
    another install may really live at a placeholder's address.

A failure names the file, the line and where the term came from, NEVER the term.
"""
import os
import re
import subprocess
import unittest
from urllib.parse import urlsplit

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TERMS_FILE = os.path.join(PROJECT, ".private-terms")
DOTENV = os.path.join(PROJECT, ".env")

# The author's own copyright line is meant to name them.
EXEMPT = {"LICENSE"}

SHAPES = {
    "tailnet hostname": (
        re.compile(r"[A-Za-z0-9<>-]+\.([A-Za-z0-9<>-]+)\.ts\.net"),
        {"tailxxxxx", "tail0000", "tailtest", "<tailnet>"}),
    "MAC address": (
        re.compile(r"(?<![0-9A-Fa-f:])((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f:])"),
        {"aa:bb:cc:dd:ee:ff"}),
    "home folder": (
        re.compile(r"/Users/([A-Za-z0-9_<>.-]+)"),
        {"<name>", "<you>", "x"}),
}
_SECRET = re.compile(r"PASSWORD|API_KEY|TOKEN|SMS_TO|EMAIL|PUSH_URL")
_LOCAL = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def shape_problems(text):
    """[(line number, what)] for every non-placeholder host, MAC or home folder."""
    return [(n, what)
            for n, line in enumerate(text.splitlines(), 1)
            for what, (rx, allowed) in SHAPES.items()
            for m in rx.finditer(line) if m.group(1).lower() not in allowed]


def private_values(env):
    """{term: the .env key it came from}: what `.env` holds that is personal."""
    out = {}

    def add(key, term, shortest=4):
        term = term.strip()
        if len(term) >= shortest and term.lower() not in _LOCAL:
            out.setdefault(term, key)

    for key, raw in env.items():
        if _SECRET.search(key):
            add(key, raw, 6)
        elif key == "SUBSONIC_USER":
            add(key, raw, 5)
        elif key == "TAILSCALE_CERT_HOST":
            add(key, raw)
            labels = raw.split(".")
            add(key, labels[0])
            if len(labels) > 2:
                add(key, labels[1])
        elif key.startswith("GATEWAY_BIND") or key == "LOCALFS_BIND":
            for part in raw.split(","):
                add(key, urlsplit("//" + part.strip()).hostname or "")
        elif key == "GATEWAY_ALLOWED_HOSTS":
            for part in raw.split(","):
                add(key, part)
        for vol in re.findall(r"/Volumes/([^/:]+)", raw):
            add(key, vol)
    return out


def term_lines(text, term):
    """Line numbers holding `term` as a whole word, in any case."""
    rx = re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.I)
    return [n for n, line in enumerate(text.splitlines(), 1) if rx.search(line)]


def read_env(path):
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip().strip("\"'")
    return env


def repo_texts():
    """(path, text) of every text file that is, or is about to be, committed."""
    out = subprocess.run(["git", "ls-files", "-z", "-co", "--exclude-standard"],
                         cwd=PROJECT, capture_output=True, text=True, check=True).stdout
    for rel in filter(None, out.split("\0")):
        if rel in EXEMPT or ".min." in rel:
            continue
        try:
            with open(os.path.join(PROJECT, rel), encoding="utf-8") as fh:
                yield rel, f"{rel}\n{fh.read()}"     # line 1 = the file's own name
        except (UnicodeDecodeError, FileNotFoundError):
            continue                                 # binary, or deleted


class TestTheRepoNamesNobody(unittest.TestCase):
    def test_hosts_macs_and_home_folders_are_placeholders(self):
        bad = [f"{rel}:{n - 1} {what}" for rel, text in repo_texts()
               if rel != "tests/test_anonymised.py"
               for n, what in shape_problems(text)]
        self.assertFalse(bad, "use a placeholder from this file:\n" + "\n".join(bad))

    def test_no_private_value_of_this_install(self):
        if not os.path.exists(TERMS_FILE):
            self.skipTest("no .private-terms here")
        terms = private_values(read_env(DOTENV)) if os.path.exists(DOTENV) else {}
        with open(TERMS_FILE, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.setdefault(line, f".private-terms line {n}")
        bad = [f"{rel}:{n - 1} holds the value of {source}"
               for rel, text in repo_texts()
               for term, source in terms.items()
               for n in term_lines(text, term)]
        self.assertFalse(bad, "\n" + "\n".join(bad))

    def test_the_terms_file_is_never_committed(self):
        done = subprocess.run(["git", "check-ignore", "-q", ".private-terms"], cwd=PROJECT)
        self.assertEqual(done.returncode, 0)


class TestTheRules(unittest.TestCase):
    ENV = {"TAILSCALE_CERT_HOST": "jukebox.tailabc.ts.net",
           "GATEWAY_BIND_TLS": "10.1.2.3:8443,127.0.0.1:8443,[fd7a::1]:8443",
           "LOCALFS_BIND": "10.1.2.3", "SUBSONIC_USER": "somebody",
           "SUBSONIC_PASSWORD": "s3cretword", "GW_NAME": "DLNA Gateway",
           "LOCALFS_MUSIC_ROOT": "/Volumes/BIGDISK/Music",
           "LOCALFS_VIDEO_LINK_ROOTS": "/Volumes/PICS/a:/Volumes/PICS-2/b"}

    def test_env_values(self):
        self.assertEqual(set(private_values(self.ENV)), {
            "jukebox.tailabc.ts.net", "jukebox", "tailabc", "10.1.2.3", "fd7a::1",
            "somebody", "s3cretword", "BIGDISK", "PICS", "PICS-2"})

    def test_loopback_and_empty_values_are_not_private(self):
        self.assertEqual(private_values({"GATEWAY_BIND_PLAIN": "127.0.0.1:8765",
                                         "LASTFM_API_KEY": "",
                                         "IMMICH_URL": "http://localhost:2283"}), {})

    def test_terms_match_whole_words_in_any_case(self):
        self.assertEqual(term_lines("on bigdisk\nBIGDISK-1TB\nBIGDISKS", "BIGDISK"), [1, 2])
        self.assertEqual(term_lines("10.1.2.30\nhttp://10.1.2.3:80", "10.1.2.3"), [2])

    def test_shapes(self):
        for text, bad in (("https://jukebox.tailabc.ts.net:8443", 1),
                          ("https://mymachine.tailXXXXX.ts.net:8443", 0),
                          ("<host>.<tailnet>.ts.net", 0),
                          ("at 00:1b:63:84:45:e6", 1), ("at aa:bb:cc:dd:ee:ff", 0),
                          ("/Users/somebody/x", 1), ("/Users/<you>/x", 0),
                          ("12:30:45 and 2026-11-24 10:38:24", 0)):
            with self.subTest(text):
                self.assertEqual(len(shape_problems(text)), bad)
