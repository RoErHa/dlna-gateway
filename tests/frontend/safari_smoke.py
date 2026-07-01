#!/usr/bin/env python3
"""Real-Safari smoke layer (safaridriver / Selenium).

WHY THIS EXISTS, AND WHAT IT IS NOT
-----------------------------------
The main frontend suite runs on Playwright (Chromium; optionally its bundled
WebKit). Neither is *real Safari*, and neither models the iOS platform layer
where the 2026-06-27 Service-Worker outage lived. This script drives the
**actual Safari on this Mac** via safaridriver, so its real WebKit Service-
Worker lifecycle (closer to iOS than Chromium's) exercises the exact
behaviours that bit us: SW register → activate, and — the important one —
NETWORK-FIRST recovery from a poisoned app-shell cache.

It is a *smoke* layer, deliberately tiny and OPT-IN:
  • not collected by pytest (filename isn't test_*.py) and NOT in run_all.py
  • safaridriver has no headless mode — it opens a real Safari window
  • only one Safari WebDriver session can exist at a time
  • it is NOT iOS. Desktop Safari ≠ iOS Safari: no standalone-PWA mode, no
    iOS autoplay/audio-session policy, no WKWebView networking. For those,
    the real-device checklist in CLAUDE.md ("Mobile / PWA testing checklist")
    remains the gate. This catches the Safari *engine + SW* class only.

ONE-TIME SETUP
--------------
  1. pip install selenium          (into the gateway venv: .venv/bin/pip …)
  2. safaridriver --enable          (may prompt for admin)
  3. Safari → Settings → Advanced → "Show features for web developers",
     then Develop menu → "Allow Remote Automation"

RUN
---
  .venv/bin/python tests/frontend/safari_smoke.py
  (exit 0 = all smoke checks passed; non-zero = a failure, printed per-check)
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `tests.frontend.stub_gateway` importable when run from the repo root.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    print(f"✗ {msg}")
    sys.exit(1)


try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.support.ui import WebDriverWait
except ModuleNotFoundError:
    _fail("selenium not installed — run:  .venv/bin/pip install selenium")

from tests.frontend.stub_gateway import StubServer  # noqa: E402

# ── JS snippets (execute_async_script passes a callback as the last arg) ──
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


def _booted(driver) -> bool:
    return bool(driver.execute_script(_BOOTED))


def run() -> int:
    stub = StubServer()
    stub.start()
    driver = None
    passed, failed = [], []

    def check(name: str, ok: bool):
        (passed if ok else failed).append(name)
        print(f"{'✓' if ok else '✗'} {name}")

    try:
        try:
            driver = webdriver.Safari()
        except WebDriverException as e:
            print("✗ could not start Safari WebDriver.\n"
                  "  Enable it once:\n"
                  "    safaridriver --enable\n"
                  "    Safari → Settings → Advanced → 'Show features for web "
                  "developers'\n"
                  "    Develop → 'Allow Remote Automation'\n"
                  f"  ({e.msg or e})")
            return 2
        driver.set_script_timeout(15)
        wait = WebDriverWait(driver, 15)

        # 1) App boots with content (real Safari renders the library).
        driver.get(stub.base_url + "/")
        try:
            wait.until(_booted)
            check("app boots + content renders (source picker populated)", True)
        except Exception:
            check("app boots + content renders (source picker populated)", False)

        # 2) Service Worker registers + activates on real Safari.
        try:
            wait.until(lambda d: d.execute_async_script(_SW_ACTIVE))
            check("service worker reaches 'activated'", True)
        except Exception:
            check("service worker reaches 'activated'", False)

        # 3) THE one that matters: poison the app-shell cache, reload, and the
        #    app must STILL render — proving network-first recovery on real
        #    WebKit (the 2026-06-27 outage condition).
        res = driver.execute_async_script(_POISON)
        if not str(res).startswith("ok"):
            check("poison-recovery: cache poisoned", False)
        else:
            driver.refresh()
            try:
                wait.until(_booted)
                body = driver.execute_script("return document.body.innerText;")
                check("poison-recovery: app still renders after reload "
                      "(network-first)", "POISON" not in body)
            except Exception:
                check("poison-recovery: app still renders after reload "
                      "(network-first)", False)

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
