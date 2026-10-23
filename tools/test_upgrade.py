"""Tests for tools/upgrade.py — the pure decisions: which hls.js version to
move to, whether a downloaded tarball is the one the registry published, how
requirements-dev.txt is re-pinned, and how osv.dev answers are read.

    python3 -m unittest tools.test_upgrade -v
"""
import base64
import hashlib
import io
import os
import sys
import tarfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import upgrade as U


def _tgz(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class TestPickHls(unittest.TestCase):
    VERSIONS = ["1.5.17", "1.5.20", "1.6.0", "1.7.0-rc.3", "1.7.3", "2.0.0"]

    def test_stays_on_major_by_default(self):
        self.assertEqual(U.pick_hls_version(self.VERSIONS, "1.5.17", False), "1.7.3")

    def test_allow_major_takes_the_newest(self):
        self.assertEqual(U.pick_hls_version(self.VERSIONS, "1.5.17", True), "2.0.0")

    def test_prereleases_are_never_picked(self):
        self.assertEqual(U.pick_hls_version(["1.6.0", "1.7.0-rc.3"], "1.6.0", False), None)

    def test_current_is_none(self):
        self.assertIsNone(U.pick_hls_version(self.VERSIONS, "2.0.0", True))

    def test_unknown_current_does_nothing(self):
        # A build whose version we could not read must not be "upgraded" blind.
        self.assertIsNone(U.pick_hls_version(self.VERSIONS, "", True))

    def test_numeric_not_lexical_ordering(self):
        self.assertEqual(U.pick_hls_version(["1.9.0", "1.10.0"], "1.9.0", False), "1.10.0")


class TestHlsVersionIn(unittest.TestCase):
    def test_reads_the_1_5_build(self):
        js = '[{key:"version",get:function(){return"1.5.17"}},{key:"Events"'
        self.assertEqual(U.hls_version_in(js), "1.5.17")

    def test_reads_the_1_7_build(self):
        # Real 1.7.3 excerpt: the getter returns a minified variable, so only
        # the debug-log string names the version. The first --apply rolled
        # back on exactly this (2026-10-23).
        js = ('i.log(\'Debug logs enabled for "\'+t+\'" in hls.js version 1.7.3\')'
              '}catch(e){return fe()};var rs="1.7.3",is={}')
        self.assertEqual(U.hls_version_in(js), "1.7.3")

    def test_reads_the_real_vendored_file(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "static", "vendor", "hls.min.js")
        with open(path, encoding="utf-8", errors="replace") as fh:
            self.assertRegex(U.hls_version_in(fh.read()) or "", r"^\d+\.\d+\.\d+$")

    def test_none_when_absent(self):
        self.assertIsNone(U.hls_version_in("console.log(1)"))


class TestIntegrity(unittest.TestCase):
    def _sri(self, blob):
        return "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode()

    def test_accepts_matching_bytes(self):
        self.assertTrue(U.sri_ok(b"abc", self._sri(b"abc")))

    def test_rejects_tampered_bytes(self):
        self.assertFalse(U.sri_ok(b"abd", self._sri(b"abc")))

    def test_rejects_unknown_or_weak_algorithm(self):
        self.assertFalse(U.sri_ok(b"abc", "sha1-" + base64.b64encode(
            hashlib.sha1(b"abc").digest()).decode()))
        self.assertFalse(U.sri_ok(b"abc", ""))


class TestTarMember(unittest.TestCase):
    def test_reads_the_member(self):
        tgz = _tgz({"package/dist/hls.min.js": b"js", "package/x": b"y"})
        self.assertEqual(U.tar_member(tgz, U.HLS_MEMBER), b"js")

    def test_missing_member_raises(self):
        with self.assertRaises(KeyError):
            U.tar_member(_tgz({"package/x": b"y"}), U.HLS_MEMBER)


class TestDevPins(unittest.TestCase):
    TEXT = ("# header\n"
            "ruff==0.16.3\n"
            "\n"
            "playwright==1.62.0        # browsers: chromium r1234 (151), webkit r2336 (26.5)\n"
            "Appium-Python-Client==5.3.1\n"
            "pytest-base-url==2.1.0\n")

    def test_unpinned_names(self):
        self.assertEqual(U.unpinned(self.TEXT),
                         ["ruff", "playwright", "Appium-Python-Client", "pytest-base-url"])

    def test_repins_and_normalises_names(self):
        out = U.rewrite_pins(self.TEXT, {"ruff": "0.17.2", "appium-python-client": "6.0.8"})
        self.assertIn("ruff==0.17.2\n", out)
        self.assertIn("Appium-Python-Client==6.0.8\n", out)   # original spelling kept
        self.assertIn("pytest-base-url==2.1.0\n", out)        # not installed → untouched
        self.assertTrue(out.startswith("# header\n"))

    def test_stale_comment_replaced_when_version_moves(self):
        out = U.rewrite_pins(self.TEXT, {"playwright": "1.64.1"},
                             {"playwright": "browsers: chromium r1250 (153), webkit r2350 (26.6)"})
        self.assertIn("playwright==1.64.1        # browsers: chromium r1250 (153)", out)
        self.assertNotIn("r1234", out)

    def test_comment_kept_when_version_unchanged(self):
        out = U.rewrite_pins(self.TEXT, {"playwright": "1.62.0"}, {"playwright": "new"})
        self.assertEqual(out, self.TEXT)

    def test_browser_comment(self):
        dry = ("Chrome for Testing 151.0.7922.34 (playwright chromium v1234)\n"
               "WebKit 26.5 (playwright webkit v2336)\n")
        self.assertEqual(U.browser_comment(dry),
                         "browsers: chromium r1234 (151), webkit r2336 (26.5)")
        self.assertIsNone(U.browser_comment("nothing"))


class TestOsv(unittest.TestCase):
    def test_pairs_answers_with_questions(self):
        pkgs = [("PyPI", "a", "1"), ("PyPI", "b", "2"), ("npm", "hls.js", "1.5.17")]
        res = [{}, {"vulns": [{"id": "GHSA-2"}, {"id": "CVE-1"}]}, {"vulns": []}]
        self.assertEqual(U.osv_findings(pkgs, res), {"b 2 (PyPI)": ["CVE-1", "GHSA-2"]})


class TestBrewMoves(unittest.TestCase):
    def f(self, name, installed, current):
        return {"name": name, "installed_versions": installed, "current_version": current}

    def test_revision_bump_is_an_upgrade(self):
        up, held = U.brew_moves([self.f("ffmpeg", ["9.0.2"], "9.0.2_1")])
        self.assertEqual([n for n, _ in up], ["ffmpeg"])
        self.assertEqual(held, [])

    def test_repointed_formula_is_held_not_downgraded(self):
        # The real 2026-10-23 case: openssl@3 repointed to the 3.5 LTS line.
        up, held = U.brew_moves([self.f("openssl@3", ["3.6.5"], "3.5.9")])
        self.assertEqual(up, [])
        self.assertIn("DOWNGRADE", held[0])

    def test_compares_against_the_newest_installed_keg(self):
        up, _ = U.brew_moves([self.f("sqlite", ["3.53.4", "3.54.0"], "3.54.1")])
        self.assertEqual([n for n, _ in up], ["sqlite"])

    def test_date_versions(self):
        up, _ = U.brew_moves([self.f("ca-certificates", ["2026-09-25"], "2026-10-20")])
        self.assertEqual(len(up), 1)


class TestWheelFits(unittest.TestCase):
    M = (3, 15)

    def test_pure_wheel(self):
        self.assertTrue(U.wheel_fits("rich-15.0.0-py3-none-any.whl", self.M))
        self.assertTrue(U.wheel_fits("six-1.17.0-py2.py3-none-any.whl", self.M))

    def test_exact_cpython_arm64_and_universal2(self):
        self.assertTrue(U.wheel_fits("x-1-cp315-cp315-macosx_11_0_arm64.whl", self.M))
        self.assertTrue(U.wheel_fits("x-1-cp315-cp315-macosx_10_13_universal2.whl", self.M))

    def test_wrong_cpython_or_platform(self):
        self.assertFalse(U.wheel_fits("x-1-cp314-cp314-macosx_11_0_arm64.whl", self.M))
        self.assertFalse(U.wheel_fits("x-1-cp315-cp315-macosx_10_13_x86_64.whl", self.M))
        self.assertFalse(U.wheel_fits("x-1-cp315-cp315-manylinux_2_28_aarch64.whl", self.M))

    def test_free_threaded_abi_is_not_ours(self):
        self.assertFalse(U.wheel_fits("x-1-cp315-cp315t-macosx_11_0_arm64.whl", self.M))

    def test_stable_abi_from_an_older_minor(self):
        self.assertTrue(U.wheel_fits("x-1-cp39-abi3-macosx_11_0_arm64.whl", self.M))
        self.assertFalse(U.wheel_fits("x-1-cp316-abi3-macosx_11_0_arm64.whl", self.M))

    def test_sdist_is_not_a_wheel(self):
        self.assertFalse(U.wheel_fits("watchdog-6.0.0.tar.gz", self.M))


class TestReadiness(unittest.TestCase):
    def test_lists_only_blockers_and_says_why(self):
        files = {
            "rich": ("15.0.0", ["rich-15.0.0-py3-none-any.whl"]),
            "watchdog": ("6.0.0", ["watchdog-6.0.0-cp314-cp314-macosx_11_0_arm64.whl",
                                   "watchdog-6.0.0.tar.gz"]),
            "odd": ("1.0", []),
        }
        self.assertEqual(U.readiness(files, (3, 15)), [
            "odd 1.0 (nothing installable)",
            "watchdog 6.0.0 (source only — pip would have to COMPILE it)"])

    def test_ready_is_empty(self):
        self.assertEqual(U.readiness({"rich": ("1", ["rich-1-py3-none-any.whl"])}, (3, 15)), [])


class TestCaps(unittest.TestCase):
    def test_reads_the_deliberate_upper_bounds(self):
        text = ("# comment fastapi<9\n"
                "rich>=13.7.0\n"
                "fastapi>=0.110.0,<1.0\n"
                "hypercorn>=0.16.0,<1.0   # note\n"
                "starlette>=0.46,<2.0\n")
        self.assertEqual(U.caps(text), {"fastapi": "1.0", "hypercorn": "1.0", "starlette": "2.0"})


class TestNewAttention(unittest.TestCase):
    def test_only_items_not_seen_last_time(self):
        prev = ["WAIT Python 3.15: not yet — watchdog"]
        now = ["WAIT Python 3.15: not yet — watchdog", "DECIDE brew openssl@3 held"]
        self.assertEqual(U.new_attention(prev, now), ["DECIDE brew openssl@3 held"])

    def test_a_changed_verdict_is_new(self):
        # NOT READY → READY must ping, even though "Python 3.15" was there before.
        self.assertEqual(U.new_attention(["WAIT Python 3.15: not yet"],
                                         ["DECIDE Python 3.15: READY"]),
                         ["DECIDE Python 3.15: READY"])


class TestVtuple(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(U.vtuple("3.15"), (3, 15))
        self.assertIsNone(U.vtuple("1.7.0-rc.3"))
        self.assertGreater(U.vtuple("3.15"), U.vtuple("3.9"))


if __name__ == "__main__":
    unittest.main()
