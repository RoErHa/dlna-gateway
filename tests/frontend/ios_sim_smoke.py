#!/usr/bin/env python3
"""iOS-Simulator Service-Worker smoke layer (Appium + Mobile Safari).

The highest-fidelity *automated* iOS coverage available: drives real Mobile
Safari inside an iOS Simulator via Appium/XCUITest. Its Service-Worker
lifecycle is the genuine iOS one — the class that caused the 2026-06-27
"full UI, no content" outage — which even desktop Safari (safari_smoke.py)
only approximates.

Still NOT covered here (real device only — see CLAUDE.md "Mobile / PWA testing
checklist"): standalone home-screen PWA mode (Add to Home Screen), iOS
autoplay/audio-session policy, and WKWebView-vs-Safari networking. This covers
Mobile Safari + its SW lifecycle in the Simulator.

Like safari_smoke.py this is OPT-IN and NOT wired into run_all.py (needs a
booted Simulator + a running Appium server; slow first-run WDA build).

PREREQUISITES (one-time)
  • Xcode + an iOS Simulator RUNTIME:  xcodebuild -downloadPlatform iOS
  • npm i -g appium ; appium driver install xcuitest
  • .venv/bin/pip install Appium-Python-Client
  • an Appium server running:  appium        (default http://127.0.0.1:4723)

RUN
  appium >/tmp/appium.log 2>&1 &                 # or a separate terminal
  .venv/bin/python tests/frontend/ios_sim_smoke.py
  (exit 0 = all checks passed; first run builds WebDriverAgent — minutes)

Env overrides: APPIUM_URL, IOS_DEVICE (default "iPhone 15"), IOS_VERSION (e.g. "26.5").
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APPIUM_URL = os.environ.get("APPIUM_URL", "http://127.0.0.1:4723")


def _fail(msg: str) -> NoReturn:  # noqa: F821
    print(f"✗ {msg}")
    sys.exit(2)


try:
    from appium import webdriver as appium_webdriver
    from appium.options.ios import XCUITestOptions
    from selenium.webdriver.support.ui import WebDriverWait
except ModuleNotFoundError as e:
    _fail(f"missing dep ({e.name}) — run: "
          ".venv/bin/pip install Appium-Python-Client")

from tests.frontend.stub_gateway import StubServer  # noqa: E402

# Same checks/JS as safari_smoke.py so the two layers assert identical outcomes.
_SW_ACTIVE = """
const cb = arguments[arguments.length - 1];
if (!('serviceWorker' in navigator)) return cb(false);
navigator.serviceWorker.getRegistration()
  .then(r => cb(!!(r && r.active && r.active.state === 'activated')))
  .catch(() => cb(false));
"""
_POISON = """
const cb = arguments[arguments.length - 1];
caches.keys()
  .then(ks => ks.find(n => n.startsWith('dlna-gw-app-')))
  .then(name => caches.open(name))
  .then(c => Promise.all([
    c.put('/', new Response('<html><body>POISON-DOC</body></html>',
          {headers: {'Content-Type': 'text/html'}})),
    c.put('/static/app.js', new Response('throw new Error("POISON-JS")',
          {headers: {'Content-Type': 'application/javascript'}})),
  ]))
  .then(() => cb('ok')).catch(e => cb('err:' + e));
"""
_BOOTED = (
    "const el = document.getElementById('source-sel');"
    "return !!(el && !el.textContent.includes('Scanning'));"
)


def _appium_up() -> bool:
    try:
        with urllib.request.urlopen(APPIUM_URL + "/status", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _detect_ios_version() -> str | None:
    if os.environ.get("IOS_VERSION"):
        return os.environ["IOS_VERSION"]
    try:
        out = subprocess.check_output(
            ["xcrun", "simctl", "list", "runtimes", "-j"], text=True)
        for rt in json.loads(out).get("runtimes", []):
            if rt.get("isAvailable") and "iOS" in rt.get("name", ""):
                return rt.get("version")
    except Exception:
        pass
    return None


def run() -> int:
    if not _appium_up():
        _fail(f"no Appium server at {APPIUM_URL} — start one:  "
              "appium >/tmp/appium.log 2>&1 &")
    version = _detect_ios_version()
    if not version:
        _fail("no iOS Simulator runtime found — run: "
              "xcodebuild -downloadPlatform iOS  (then retry)")
    device = os.environ.get("IOS_DEVICE", "iPhone 15")

    stub = StubServer()
    stub.start()
    driver = None
    passed, failed = [], []

    def check(name: str, ok: bool):
        (passed if ok else failed).append(name)
        print(f"{'✓' if ok else '✗'} {name}")

    try:
        opts = XCUITestOptions()
        opts.platform_name = "iOS"
        opts.automation_name = "XCUITest"
        opts.browser_name = "Safari"
        opts.device_name = device
        opts.platform_version = version
        # First run compiles + launches WebDriverAgent into the sim — be patient.
        opts.set_capability("wdaLaunchTimeout", 300_000)
        opts.set_capability("wdaConnectionTimeout", 300_000)
        opts.set_capability("newCommandTimeout", 180)
        # A freshly-booted sim's Safari Web Inspector isn't ready within the
        # default 5s — the remote debugger returns no web apps and the session
        # fails. Give it room (esp. on a cold first boot).
        opts.set_capability("webviewConnectTimeout", 60_000)
        print(f"… connecting Appium → iOS {version} / {device} "
              "(first run builds WebDriverAgent, several minutes)")
        try:
            driver = appium_webdriver.Remote(APPIUM_URL, options=opts)
        except Exception as e:
            _fail(f"could not start the iOS Simulator session: {e}")
        # Async scripts (execute_async_script) need a non-zero script timeout,
        # else they fail instantly with 'Timed out ... after 0 ms'.
        driver.set_script_timeout(30)
        wait = WebDriverWait(driver, 30)

        # The Simulator shares the host loopback, so 127.0.0.1:<port> reaches
        # the stub on this Mac. localhost/127.0.0.1 is a secure context, so
        # Mobile Safari permits Service Workers over plain HTTP.
        driver.get(stub.base_url + "/")

        try:
            wait.until(lambda d: d.execute_script(_BOOTED))
            check("app boots + content renders (source picker populated)", True)
        except Exception:
            check("app boots + content renders (source picker populated)", False)

        try:
            wait.until(lambda d: d.execute_async_script(_SW_ACTIVE))
            check("service worker reaches 'activated'", True)
        except Exception:
            check("service worker reaches 'activated'", False)

        res = driver.execute_async_script(_POISON)
        if not str(res).startswith("ok"):
            check("poison-recovery: cache poisoned", False)
        else:
            driver.refresh()
            try:
                wait.until(lambda d: d.execute_script(_BOOTED))
                body = driver.execute_script("return document.body.innerText;")
                check("poison-recovery: app still renders after reload "
                      "(network-first, Mobile Safari)", "POISON" not in body)
            except Exception:
                check("poison-recovery: app still renders after reload "
                      "(network-first, Mobile Safari)", False)

        print(f"\n{'PASS' if not failed else 'FAIL'} — "
              f"{len(passed)} passed, {len(failed)} failed")
        return 0 if not failed else 1
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        stub.stop()


if __name__ == "__main__":
    sys.exit(run())
