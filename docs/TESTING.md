# Testing — opt-in browser layers and the real-device checklist

> Moved verbatim out of `CLAUDE.md` on 2026-10-23 to keep that file under its
> size budget. `CLAUDE.md` keeps the load-bearing rules and points here; the
> text below is the full reasoning, measurements and history. Section
> references elsewhere ("see CLAUDE.md → X") resolve via CLAUDE.md's pointer.

### Real-Safari smoke layer (`tests/frontend/safari_smoke.py`)

The Playwright suite runs **Chromium** by default (and optionally its bundled
**WebKit** via `--browser webkit` — which is desktop WebCore/JSC, *not* real
Safari and *not* iOS: it caught the video `native` vs `native-hls` codec
divergence, but not the SW/PWA/autoplay class). `safari_smoke.py` fills the next
rung: an **opt-in Selenium/safaridriver script that drives the actual Safari on
this Mac**, whose real WebKit Service-Worker lifecycle is closer to iOS than
Chromium's. It boots the same `StubServer` and runs three checks — app boots +
renders, SW reaches `activated`, and **poison-recovery** (poison the app-shell
cache → reload → the app must still render, i.e. network-first on real WebKit —
the exact 2026-06-27 outage condition; see the SW cache-tiers note above).

Deliberately **NOT** in `run_all.py`: safaridriver has no headless mode (a real
Safari window opens), allows only one session, and needs one-time enablement.
Setup: `.venv/bin/pip install selenium` (optional dev dep, not in
`requirements.txt` — same as pytest/playwright) · `safaridriver --enable` ·
Safari → Settings → Advanced → "Show features for web developers" → Develop →
"Allow Remote Automation". Run: `.venv/bin/python tests/frontend/safari_smoke.py`
(exit 0 = pass). **Honest scope:** desktop Safari ≠ iOS Safari — no
standalone-PWA mode, autoplay/audio-session policy, or WKWebView networking, so
the "Mobile / PWA testing checklist" (real device) stays the iOS gate; this
covers the Safari engine + SW class only.

### iOS-Simulator smoke layer (`tests/frontend/ios_sim_smoke.py`)

The highest-fidelity **automated** iOS rung: drives real **Mobile Safari in an
iOS Simulator** via Appium/XCUITest, so it exercises the genuine iOS
Service-Worker lifecycle (the class behind the 2026-06-27 outage) that even
desktop Safari only approximates. Same three checks as `safari_smoke.py` (boot +
render, SW `activated`, and **poison-recovery** — poison the app-shell cache →
reload → app must still render, network-first on Mobile Safari). Verified
passing on iOS 26.5 / iPhone 15. Also opt-in, NOT in `run_all.py` (needs a
booted Simulator + a running Appium server; slow first-run WebDriverAgent
build). Default device `iPhone 15`; override with `IOS_DEVICE` / `IOS_VERSION` /
`APPIUM_URL`. **Same honest scope caveat as above** — even the Simulator can't
script standalone home-screen PWA mode, autoplay/audio-session, or WKWebView
networking; the real-device checklist stays the final iOS gate.

One-time setup:
```bash
xcodebuild -downloadPlatform iOS                 # iOS Simulator runtime (~8.5 GB)
npm install -g appium && appium driver install xcuitest
.venv/bin/pip install Appium-Python-Client        # optional dev dep, not in requirements.txt
```

Appium server (start / stop):
```bash
appium >/tmp/appium.log 2>&1 &                    # start (listens on :4723)
curl -s http://127.0.0.1:4723/status              # health check ({"ready":true})
pkill -f "node.*appium"                           # stop
```

Simulator devices (create-once already done for 15/16/17; boot / shutdown):
```bash
# create (only if a device is missing):
xcrun simctl create "iPhone 16" \
  com.apple.CoreSimulator.SimDeviceType.iPhone-16 \
  com.apple.CoreSimulator.SimRuntime.iOS-26-5
xcrun simctl boot "iPhone 15"                     # boot (also "iPhone 16" / "iPhone 17")
xcrun simctl boot "iPhone 16"
xcrun simctl boot "iPhone 17"
xcrun simctl list devices | grep Booted           # what's running
xcrun simctl shutdown "iPhone 15"                 # stop one (or: shutdown all)
xcrun simctl shutdown all
open -a Simulator                                 # optional: show the sim window
```

`chaos.py` hard-fails if it sees any 5xx, a **crash signature** is appended
to `/tmp/dlna-gateway.err` (= silent thread death), or a snapshot takes >5s.
Its first real-world find was the `playlist_tracks.duration` HH:MM:SS-string
`ValueError` that was killing the renderer-queue daemon thread invisibly.

**The stderr criterion reads CONTENT, not byte count (2026-08-26).** It used
to fail on any growth at all, which asks the wrong question: that path is the
launchd **stderr sink**, so hypercorn's `[INFO] Running on …` banner lands
there on every boot — and `launchctl kickstart -k` SIGKILLs the old process,
which flushes `resource_tracker: leaked semaphore objects` on the way out
*every single time* (18 of them had accumulated in the live file, one per
restart). A restart overlapping a run therefore failed it while nothing had
crashed. `classify_stderr` (pure, tested) now looks for `Traceback`,
`Exception in thread`, `Fatal Python error` and `Segmentation fault`; benign
growth is reported with a line count and passes. The marker list is
deliberately short — this canary only earns its keep if a hit is worth waking
up for, so an `[ERROR]` line or a 500 the run provoked on purpose must not
trip it. A file truncated mid-run is detected (size < offset) and re-read
from the start rather than compared against a stale position.
`tests/test_chaos_stderr.py` (10) covers both directions, including a real
crash buried inside restart noise.

> ⚠️ **Cold-start caveat, unrelated and NOT a wedge:** the first
> `snapshot (no udn)` after a restart pays one full SOAP timeout for any
> switched-off renderer (measured 6.1 s with the LG TV off), because the
> `_UNREACHABLE_BACKOFF_SEC` backoff and the per-queue snapshot cache are
> both empty. It trips the >5 s hard-fail on a first run and passes on the
> next (6.2 s total, PASS). Give a freshly-restarted gateway one warm-up run
> before believing a slow-snapshot failure.

Each core module also has a standalone self-test:

```bash
python dlna_config.py              # config/logging
python dlna_discovery.py           # SSDP discovery (20s live scan)
python dlna_content.py <control-url>  # UPnP SOAP
python dlna_library.py             # DB operations (composition + pool smoke test)
python db_pool.py                  # concurrent DB stress test
python dlna_player.py              # QueueRegistry + duration-parser self-test
```

### Mobile / PWA testing checklist

Because there's no automated test coverage for on-device behavior (iOS Safari, Android Chrome, PWA standalone mode), before believing a browser-mode change is shipped, manually verify on an actual mobile device over Tailscale:

1. **Fresh PWA install**: Safari → Share → Add to Home Screen. Icon + title use manifest values.
2. **First-play autoplay**: Tap a track. Audio starts. (If it doesn't, and a toast says "Browser blocked playback", that's the correct new behavior — the `catch` surfaces it.)
3. **Lock screen artwork**: Lock the phone during playback. Album art shown on lock screen. Previous/Next/Play/Pause work.
4. **Tab backgrounded**: Switch to another app. Audio continues. (If it pauses, that's iOS's audio-session policy, not a bug.)
5. **Laptop/phone suspend during stream**: Close lid (or long-press sleep). Wait 5+ min. Check `gateway.log` shows `proxy_stream ■ END ... reason=client_idle_timeout` within 6 minutes.
6. **Error reporting**: After playback fails (e.g., play an unsupported-format track), `grep dlna.client gateway.log` should show the event with `codeName=unsupported` etc.
7. **Rebuild / force-refresh**: Pull-to-refresh in PWA. Service Worker updates (check DevTools Application tab → SW version).

When something goes wrong, the diagnostic order is (1) `gateway.log` `dlna.client` entries for browser-side events, (2) `/tmp/dlna-gateway.err` for server-thread crashes, (3) Safari DevTools Web Inspector for pre-report JS errors.
