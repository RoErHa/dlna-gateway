#!/usr/bin/env python3
"""iOS permission-dialog CAPABILITY DEMO (Appium + applesimutils + Mobile Safari).

⚠️  THIS IS A CAPABILITY / SCAFFOLD DEMO, NOT AN APP REGRESSION TEST.
The DLNA-Gateway PWA requests NO device permissions (no geolocation, camera,
mic, notifications/Web-Push, motion). So this doesn't guard any real gateway
behaviour — it exists to prove the permission-automation toolchain works
(applesimutils-backed `mobile: setPermission`) and to serve as a template for
if the app ever grows a permission feature (e.g. Web-Push "now playing"). Also
opt-in, NOT in run_all.py.

WHAT IT DEMONSTRATES (against real Mobile Safari in the iOS Simulator):
  • setPermission location=yes → navigator.geolocation.getCurrentPosition
    RESOLVES with coordinates. Safari still shows a per-site location prompt,
    which the `autoAcceptAlerts` capability auto-accepts (app-level TCC and the
    per-site web prompt are two separate gates — applesimutils controls the
    former, autoAcceptAlerts handles the latter).
  • setPermission location=no  → getCurrentPosition FAILS with
    PERMISSION_DENIED (error code 1), deterministically (no prompt).

Verified on iOS 26.5 / iPhone 15. NB `mobile: setPermission` location values
are 'yes' | 'no' | 'unset' (NOT 'inuse'/'never').

PREREQUISITES (see also ios_sim_smoke.py + CLAUDE.md)
  • Xcode iOS Simulator runtime, appium + xcuitest driver, Appium-Python-Client
  • applesimutils:  brew install wix/brew/applesimutils
  • an Appium server running:  appium >/tmp/appium.log 2>&1 &

RUN
  .venv/bin/python tests/frontend/ios_permission_smoke.py
  (exit 0 = both grant + deny behaved as expected)

Env overrides: APPIUM_URL, IOS_DEVICE (default "iPhone 15"), IOS_VERSION.
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
SAFARI_BUNDLE = "com.apple.mobilesafari"


def _fail(msg: str) -> NoReturn:  # noqa: F821
    print(f"✗ {msg}")
    sys.exit(2)


try:
    from appium import webdriver as appium_webdriver
    from appium.options.ios import XCUITestOptions
except ModuleNotFoundError as e:
    _fail(f"missing dep ({e.name}) — run: "
          ".venv/bin/pip install Appium-Python-Client")

from tests.frontend.stub_gateway import StubServer  # noqa: E402

# getCurrentPosition needs a secure context — 127.0.0.1 (the stub) qualifies.
# Returns 'ok:<lat>,<lon>' on success or 'err:<code>:<msg>' (code 1 == denied).
_GEO = """
const cb = arguments[arguments.length - 1];
if (!navigator.geolocation) return cb('no-geo-api');
navigator.geolocation.getCurrentPosition(
  p => cb('ok:' + p.coords.latitude.toFixed(2) + ',' + p.coords.longitude.toFixed(2)),
  e => cb('err:' + e.code + ':' + e.message),
  {timeout: 8000, maximumAge: 0});
"""


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
    if not (subprocess.run(["which", "applesimutils"],
            capture_output=True).returncode == 0):
        _fail("applesimutils not installed — run: "
              "brew install wix/brew/applesimutils")
    version = _detect_ios_version()
    if not version:
        _fail("no iOS Simulator runtime — run: xcodebuild -downloadPlatform iOS")
    device = os.environ.get("IOS_DEVICE", "iPhone 15")

    # Best-effort: pre-boot the sim + give it a location so a GRANT actually
    # resolves (a sim with no location would time out even when permitted).
    subprocess.run(["xcrun", "simctl", "boot", device], capture_output=True)
    subprocess.run(["xcrun", "simctl", "location", device, "set",
                    "52.0907,5.1214"], capture_output=True)

    stub = StubServer()
    stub.start()
    driver = None
    passed, failed = [], []

    def check(name: str, ok: bool, detail: str = ""):
        (passed if ok else failed).append(name)
        print(f"{'✓' if ok else '✗'} {name}" + (f"  ({detail})" if detail else ""))

    def set_location_permission(value: str):
        # applesimutils-backed. Values: 'yes' | 'no' | 'unset'.
        driver.execute_script("mobile: setPermission", {
            "bundleId": SAFARI_BUNDLE, "access": {"location": value}})

    try:
        opts = XCUITestOptions()
        opts.platform_name = "iOS"
        opts.automation_name = "XCUITest"
        opts.browser_name = "Safari"
        opts.device_name = device
        opts.platform_version = version
        opts.set_capability("wdaLaunchTimeout", 300_000)
        opts.set_capability("wdaConnectionTimeout", 300_000)
        opts.set_capability("newCommandTimeout", 180)
        opts.set_capability("webviewConnectTimeout", 60_000)
        # Safari's per-site location prompt is a modal alert; auto-accept it so
        # a GRANTED getCurrentPosition can proceed (DENY needs no prompt).
        opts.set_capability("autoAcceptAlerts", True)
        print(f"… connecting Appium → iOS {version} / {device}")
        try:
            driver = appium_webdriver.Remote(APPIUM_URL, options=opts)
        except Exception as e:
            _fail(f"could not start the iOS Simulator session: {e}")
        driver.set_script_timeout(30)

        # ── GRANT: getCurrentPosition must resolve with coordinates ──
        set_location_permission("yes")
        driver.get(stub.base_url + "/")
        res = driver.execute_async_script(_GEO)
        check("setPermission location=yes → geolocation RESOLVES",
              str(res).startswith("ok:"), str(res))

        # ── DENY: getCurrentPosition must fail PERMISSION_DENIED (code 1) ──
        set_location_permission("no")
        driver.get(stub.base_url + "/")
        res = driver.execute_async_script(_GEO)
        check("setPermission location=no → geolocation PERMISSION_DENIED",
              str(res).startswith("err:1"), str(res))

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
