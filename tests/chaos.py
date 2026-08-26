#!/usr/bin/env python3
"""
tests/chaos.py — Chaotic-user simulator against a live DLNA Gateway.

Hammers the gateway's HTTP API with a weighted random pool of realistic
user actions AND injected edge cases the UI technically allows but
normal users wouldn't try. Monitors health so a silent thread death or
a 5xx spike fails the run.

Usage:
    python3 tests/chaos.py                                  # 200 iterations against https://localhost:8443
    python3 tests/chaos.py --iterations 1000 --workers 4
    python3 tests/chaos.py --base https://192.168.1.125:8443
    python3 tests/chaos.py --seed 1234                      # reproduce a failure
    python3 tests/chaos.py --quiet                          # only summary

Pass criteria:
  - No 5xx responses at all.
  - Snapshot endpoint responds in < 1s throughout the run.
  - No CRASH SIGNATURE appended to /tmp/dlna-gateway.err during the run.
    A silently-dead daemon thread is the exact class of bug the
    per-renderer refactor and the log.exception wrapping exist to stop
    regressing, and that file is where its traceback lands.

    The criterion used to be "the file must not grow by a single byte",
    which is not the same thing: that path is the launchd STDERR SINK,
    and hypercorn logs its `[INFO] Running on …` banner there on every
    boot, as does any Python warning. A restart overlapping a run — or
    the SIGKILLed old process flushing `resource_tracker: leaked
    semaphore objects` on its way out, which `kickstart -k` produces
    every single time — therefore failed the run while nothing had
    crashed. We now read WHAT was appended and judge that.
"""
import argparse
import json
import os
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

# ── SSL: gateway uses a self-signed cert on 8443 ──────────────────
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode   = ssl.CERT_NONE
_opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ctx))

# Default is 1.x's launchd stderr; override for the 2.x side-by-side rig
# (run-2.0-asgi.sh writes elsewhere) so the silent-thread-death check watches
# the gateway actually under test.
STDERR_PATH = os.environ.get("CHAOS_STDERR_PATH", "/tmp/dlna-gateway.err")


# ── HTTP helpers ──────────────────────────────────────────────────

def _http(base, method, path, body=None, timeout=10):
    """Single request. Returns (status, text, elapsed_sec). Network
    errors become status=0."""
    url = base.rstrip("/") + path
    data = body.encode("utf-8") if isinstance(body, str) else body
    req  = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    t0 = time.monotonic()
    try:
        resp = _opener.open(req, timeout=timeout)
        return resp.status, resp.read().decode("utf-8", "replace"), time.monotonic() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.monotonic() - t0
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", time.monotonic() - t0


def _get_json(base, path, timeout=5):
    status, text, _ = _http(base, "GET", path, timeout=timeout)
    if status != 200:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


# ── Discovery: what's on this gateway right now ───────────────────

class Target:
    """Everything the chaos script needs to know about the live gateway
    before it starts randomising. Learned once at start so each iteration
    doesn't re-probe."""
    def __init__(self, base):
        self.base       = base
        self.renderers  = _get_json(base, "/api/renderers") or []
        self.servers    = _get_json(base, "/api/servers")   or []
        self.playlists  = _get_json(base, "/api/playlists") or []
        # We need at least one server and one renderer to do anything useful.
        # Without them the chaos script can still exercise error paths,
        # but most meaningful actions will no-op.
        self.udns = [r["udn"] for r in self.renderers]
        self.server_udn = self.servers[0]["udn"] if self.servers else None
        self.artists = []
        if self.server_udn:
            self.artists = _get_json(
                base, f"/api/artists?udn={urllib.parse.quote(self.server_udn)}"
            ) or []

    def describe(self):
        return (f"renderers={len(self.renderers)}  servers={len(self.servers)}  "
                f"playlists={len(self.playlists)}  artists={len(self.artists)}")


# ── Action pool ───────────────────────────────────────────────────
# Each action is (weight, fn). fn takes (rng, target, base) and returns
# a short description + an (status, elapsed) tuple. status < 0 means the
# action was skipped (e.g. no renderer to post to).

def _rand_playlist_tracks(rng, target, min_n=1, max_n=15):
    """Pick a random playlist and return its tracks (possibly truncated)."""
    if not target.playlists:
        return []
    pl = rng.choice(target.playlists)
    data = _get_json(target.base, f"/api/playlist?id={pl['id']}")
    tracks = (data or {}).get("tracks", [])
    if not tracks:
        return []
    n = rng.randint(min_n, min(max_n, len(tracks)))
    start = rng.randint(0, max(0, len(tracks) - n))
    return tracks[start:start + n]


# --- Normal-user actions ---

def act_list_renderers(rng, target, base):
    s, _, t = _http(base, "GET", "/api/renderers")
    return "list renderers", (s, t)

def act_list_playlists(rng, target, base):
    s, _, t = _http(base, "GET", "/api/playlists")
    return "list playlists", (s, t)

def act_snapshot_no_udn(rng, target, base):
    s, _, t = _http(base, "GET", "/api/renderer_state")
    return "snapshot (no udn)", (s, t)

def act_snapshot_with_udn(rng, target, base):
    if not target.udns: return "snapshot (no udn available)", (-1, 0)
    u = rng.choice(target.udns)
    s, _, t = _http(base, "GET",
                    f"/api/renderer_state?udn={urllib.parse.quote(u)}")
    return f"snapshot udn={u[:16]}…", (s, t)

def act_search(rng, target, base):
    if not target.server_udn: return "search (no server)", (-1, 0)
    q = rng.choice(["love", "night", "blue", "home", "sun", "×", "日本", "🎵"])
    path = f"/api/search?udn={urllib.parse.quote(target.server_udn)}&q={urllib.parse.quote(q)}"
    s, _, t = _http(base, "GET", path)
    return f"search q={q!r}", (s, t)

def act_browse_artists(rng, target, base):
    if not target.server_udn: return "browse artists (no server)", (-1, 0)
    s, _, t = _http(base, "GET",
                    f"/api/artists?udn={urllib.parse.quote(target.server_udn)}")
    return "browse artists", (s, t)

def act_post_queue(rng, target, base):
    if not target.udns:      return "post queue (no renderer)", (-1, 0)
    tracks = _rand_playlist_tracks(rng, target)
    if not tracks:           return "post queue (no tracks)",   (-1, 0)
    u = rng.choice(target.udns)
    body = json.dumps({"udn": u, "tracks": tracks, "force": True})
    s, _, t = _http(base, "POST", "/api/render_queue", body)
    return f"post queue ({len(tracks)} tracks, force)", (s, t)

def act_post_queue_no_force(rng, target, base):
    """Same as above but NO force — exercises the 409 busy path."""
    if not target.udns:      return "post queue (no renderer)", (-1, 0)
    tracks = _rand_playlist_tracks(rng, target, max_n=5)
    if not tracks:           return "post queue (no tracks)",   (-1, 0)
    u = rng.choice(target.udns)
    body = json.dumps({"udn": u, "tracks": tracks})
    s, _, t = _http(base, "POST", "/api/render_queue", body)
    # 409 is a LEGITIMATE response here, not a failure
    return f"post queue (no force) → {s}", (s, t)

def act_control(rng, target, base):
    if not target.udns: return "control (no renderer)", (-1, 0)
    u = rng.choice(target.udns)
    action = rng.choice(["pause", "stop", "next", "prev"])
    body = json.dumps({"device": f"upnp:{u}", "action": action})
    s, _, t = _http(base, "POST", "/api/control", body)
    return f"control {action}", (s, t)


# --- Edge-case / injected-chaos actions ---

def act_queue_unknown_udn(rng, target, base):
    """UI should never allow this but bugs do slip in."""
    body = json.dumps({
        "udn": f"uuid:ghost-{rng.randint(0,9999)}",
        "tracks": [{"url": "x", "title": "t"}],
    })
    s, _, t = _http(base, "POST", "/api/render_queue", body)
    return f"queue unknown udn → {s} (expect 404)", (s, t)

def act_queue_empty_tracks(rng, target, base):
    if not target.udns: return "queue empty (no renderer)", (-1, 0)
    body = json.dumps({"udn": rng.choice(target.udns), "tracks": []})
    s, _, t = _http(base, "POST", "/api/render_queue", body)
    return f"queue empty tracks → {s} (expect 400)", (s, t)

def act_queue_malformed_json(rng, target, base):
    body = rng.choice([
        "",
        "{not valid json",
        "[]",
        '{"udn": null, "tracks": "not-a-list"}',
        '\x00\xff\xfe',
    ])
    s, _, t = _http(base, "POST", "/api/render_queue", body)
    # Any response that ISN'T a 5xx is acceptable — the handler must
    # reject gracefully, not crash the process
    return f"queue malformed → {s}", (s, t)

def act_queue_huge(rng, target, base):
    """10k-track queue. Exercises memory handling on both sides."""
    if not target.udns: return "queue huge (no renderer)", (-1, 0)
    tracks = [{"url": f"http://127.0.0.1:1/t{i}",
               "title": f"Track {i}", "artist": "Chaos",
               "album": "StressTest",
               "duration": "0:03:30.000",
               "mime": "audio/flac"} for i in range(10_000)]
    body = json.dumps({"udn": rng.choice(target.udns),
                       "tracks": tracks, "force": True})
    s, _, t = _http(base, "POST", "/api/render_queue", body, timeout=30)
    return f"queue huge (10k tracks) → {s}", (s, t)

def act_queue_hms_duration(rng, target, base):
    """Direct regression for today's bug — queue with an HH:MM:SS string."""
    if not target.udns: return "queue hms (no renderer)", (-1, 0)
    body = json.dumps({
        "udn": rng.choice(target.udns),
        "force": True,
        "tracks": [{
            "url":      "http://127.0.0.1:1/fake.flac",
            "title":    "Regression Track",
            "artist":   "DurationBug",
            "album":    "HMS-Format",
            "duration": rng.choice(["0:04:51.000", "0:07:07.000",
                                     "3:45", "0:00:00", "malformed"]),
            "mime":     "audio/flac",
        }],
    })
    s, _, t = _http(base, "POST", "/api/render_queue", body)
    return f"queue hms-duration → {s}", (s, t)

def act_control_unknown_udn(rng, target, base):
    body = json.dumps({"device": f"upnp:uuid:ghost-{rng.randint(0,999)}",
                       "action": rng.choice(["pause","stop","next","prev"])})
    s, _, t = _http(base, "POST", "/api/control", body)
    return f"control unknown udn → {s} (expect 404)", (s, t)

def act_control_bad_action(rng, target, base):
    if not target.udns: return "bad action (no renderer)", (-1, 0)
    body = json.dumps({"device": f"upnp:{rng.choice(target.udns)}",
                       "action": rng.choice(["delete-everything",
                                              "🔥", "", "stop; rm -rf /"])})
    s, _, t = _http(base, "POST", "/api/control", body)
    return f"control bad action → {s}", (s, t)


# --- Browser-mode / PWA actions ---
#
# These exercise the proxy endpoints that serve the in-browser audio
# player and its lock-screen / Service Worker dependencies. The goal
# is NOT to test the browser's audio stack (we can't do that from
# Python) but to confirm the gateway-side proxies stay stable under
# the kind of load a flaky mobile browser produces: rapid Range
# fetches, abrupt disconnects (laptop close / tab backgrounded on
# iOS), many concurrent sessions (tab duplication, PWA reload loops).

def _sample_track_url(rng, target):
    """Grab a real content URL from a playlist so /stream proxies
    something that actually exists. Falls back to a bogus local URL
    so adversarial scenarios still exercise the error paths."""
    if not target.playlists:
        return None
    pl = rng.choice(target.playlists)
    data = _get_json(target.base, f"/api/playlist?id={pl['id']}")
    tracks = (data or {}).get("tracks", [])
    return rng.choice(tracks).get("url") if tracks else None

def act_stream_abrupt_disconnect(rng, target, base):
    """Open /stream, read a few chunks, then close without reading the
    rest. This is the exact pattern of 'laptop lid closed while music
    is playing'. The proxy must clean up the upstream without leaking
    a file descriptor or raising a handled exception into stderr."""
    url = _sample_track_url(rng, target)
    if not url: return "stream disconnect (no playlist)", (-1, 0)
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{base.rstrip('/')}/stream?url={urllib.parse.quote(url)}")
        with _opener.open(req, timeout=10) as r:
            # Read just enough to get the proxy streaming, then bail
            r.read(rng.randint(1024, 65536))
        # urllib closes on context exit — abrupt from server's POV
        return "stream abrupt disconnect", (200, time.monotonic() - t0)
    except Exception as e:
        return f"stream disconnect: {type(e).__name__}", (0, time.monotonic() - t0)

def act_stream_range_request(rng, target, base):
    """Simulate a browser seeking: HEAD or GET with a Range header for
    a middle segment. The proxy forwards Range upstream; upstream may
    or may not honour it. Either way, no 5xx."""
    url = _sample_track_url(rng, target)
    if not url: return "stream range (no playlist)", (-1, 0)
    t0 = time.monotonic()
    start = rng.randint(0, 1_000_000)
    end = start + rng.randint(1024, 262_144)
    try:
        req = urllib.request.Request(
            f"{base.rstrip('/')}/stream?url={urllib.parse.quote(url)}")
        req.add_header("Range", f"bytes={start}-{end}")
        with _opener.open(req, timeout=10) as r:
            r.read(rng.randint(256, 8192))
            s = r.status
        return f"stream range {start}- → {s}", (s, time.monotonic() - t0)
    except urllib.error.HTTPError as e:
        return f"stream range → {e.code}", (e.code, time.monotonic() - t0)
    except Exception as e:
        return f"stream range: {type(e).__name__}", (0, time.monotonic() - t0)

def act_stream_bogus_url(rng, target, base):
    """URL param points at a port nothing listens on. The proxy should
    emit a clean 502 (or similar), NOT a 5xx from unhandled exception."""
    bogus = rng.choice([
        "http://127.0.0.1:1/nope.flac",
        "http://240.0.0.1/unreachable.flac",
        "http://does-not-exist.local:9999/x.flac",
    ])
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(
            f"{base.rstrip('/')}/stream?url={urllib.parse.quote(bogus)}")
        with _opener.open(req, timeout=8) as r:
            s = r.status
            r.read(1024)
        return f"stream bogus → {s}", (s, time.monotonic() - t0)
    except urllib.error.HTTPError as e:
        return f"stream bogus → {e.code}", (e.code, time.monotonic() - t0)
    except Exception as e:
        return f"stream bogus: {type(e).__name__}", (0, time.monotonic() - t0)

def act_art_fetch(rng, target, base):
    """The lock-screen artwork proxy. Must return 200 for a valid image
    URL, 4xx/502 otherwise — never a 5xx."""
    url = rng.choice([
        "http://127.0.0.1:1/nope.jpg",            # unreachable → 502
        "http://240.0.0.1/unroutable.jpg",        # unroutable → 502
        "https://example.com/robots.txt",         # non-image → 502
        "",                                        # missing → 400
    ])
    t0 = time.monotonic()
    try:
        if url:
            req = f"{base.rstrip('/')}/art?url={urllib.parse.quote(url)}"
        else:
            req = f"{base.rstrip('/')}/art"
        resp = _opener.open(urllib.request.Request(req), timeout=8)
        s = resp.status
        resp.read(1024)
        return f"art → {s}", (s, time.monotonic() - t0)
    except urllib.error.HTTPError as e:
        return f"art → {e.code}", (e.code, time.monotonic() - t0)
    except Exception as e:
        return f"art: {type(e).__name__}", (0, time.monotonic() - t0)

def act_client_log_flood(rng, target, base):
    """A broken PWA could spam this endpoint. Should always 200 or 400,
    never crash."""
    kinds = ["audio_error", "play_rejected", "unknown_chaos"]
    body = json.dumps({
        "kind":    rng.choice(kinds),
        "code":    rng.randint(0, 5),
        "message": "chaos fuzz " + "x" * rng.randint(0, 500),
        "ua":      "ChaosUA/" + str(rng.randint(0, 99)),
    })
    s, _, t = _http(base, "POST", "/api/client_log", body)
    return f"client_log → {s}", (s, t)

def act_client_log_malformed(rng, target, base):
    """Must 400, not 500, on garbage input — verified path still holds
    after the 2026-04-23 JSON-shape fix."""
    body = rng.choice([
        "[]",
        "42",
        '"just-a-string"',
        "{not json",
        "",
    ])
    s, _, t = _http(base, "POST", "/api/client_log", body)
    return f"client_log malformed → {s}", (s, t)


ACTIONS = [
    # weight, fn
    (10, act_list_renderers),
    (10, act_list_playlists),
    (20, act_snapshot_no_udn),
    (20, act_snapshot_with_udn),
    ( 8, act_search),
    ( 8, act_browse_artists),
    (12, act_post_queue),
    ( 6, act_post_queue_no_force),
    (15, act_control),
    # Edge cases — less frequent but present every run
    ( 4, act_queue_unknown_udn),
    ( 4, act_queue_empty_tracks),
    ( 3, act_queue_malformed_json),
    ( 1, act_queue_huge),
    ( 5, act_queue_hms_duration),
    ( 3, act_control_unknown_udn),
    ( 3, act_control_bad_action),
    # Browser / PWA / mobile proxy endpoints
    ( 8, act_stream_abrupt_disconnect),
    ( 5, act_stream_range_request),
    ( 3, act_stream_bogus_url),
    ( 5, act_art_fetch),
    ( 3, act_client_log_flood),
    ( 2, act_client_log_malformed),
]


def _weighted_choice(rng, actions):
    total = sum(w for w, _ in actions)
    pick  = rng.random() * total
    s = 0.0
    for w, fn in actions:
        s += w
        if pick < s:
            return fn
    return actions[-1][1]


# ── Health monitoring ─────────────────────────────────────────────

def _stderr_size():
    try:
        return os.path.getsize(STDERR_PATH)
    except OSError:
        return 0


# Lines that mean a thread died. Deliberately a short, specific list:
# this canary only earns its keep if a hit is worth waking up for, and
# the 2026-08-21 incident in this very file opened with "Exception in
# thread renderer-queue:".
_CRASH_MARKERS = (
    "Traceback (most recent call last):",
    "Exception in thread",
    "Fatal Python error",
    "Segmentation fault",
)


def classify_stderr(text: str):
    """Split appended stderr into (crash lines, benign line count).

    Pure, so the hostile shapes are tests rather than things discovered
    during a 500-iteration run against the live gateway."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    crash = [ln for ln in lines
             if any(marker in ln for marker in _CRASH_MARKERS)]
    return crash, len(lines) - len(crash)


def _stderr_since(offset: int):
    """The text appended to STDERR_PATH since `offset` bytes.

    Returns (text, size_now, rotated). `rotated` is True when the file is
    SHORTER than the offset — launchd recreated or truncated it mid-run,
    which makes the offset meaningless; we then read the whole file and
    say so rather than compare against a stale position and report
    nonsense."""
    try:
        size = os.path.getsize(STDERR_PATH)
    except OSError:
        return "", 0, False
    rotated = size < offset
    try:
        with open(STDERR_PATH, "rb") as fh:
            fh.seek(0 if rotated else offset)
            raw = fh.read()
    except OSError as e:
        print(f"chaos: could not read {STDERR_PATH}: {e}")
        return "", size, rotated
    return raw.decode("utf-8", "replace"), size, rotated


# ── Main loop ─────────────────────────────────────────────────────

def run(base, iterations, workers, seed, quiet):
    _rng = random.Random(seed)
    target = Target(base)
    print(f"chaos: {target.describe()}")
    print(f"chaos: base={base}  iterations={iterations}  workers={workers}  seed={seed}")

    stderr_before = _stderr_size()
    print(f"chaos: /tmp/dlna-gateway.err starts at {stderr_before} bytes")

    # Pre-check: snapshot must respond. If the gateway is down, bail
    # rather than flood it with failed requests.
    s, _, _ = _http(base, "GET", "/api/renderer_state", timeout=3)
    if s != 200:
        print(f"chaos: FATAL — gateway not responding on {base} (status={s})")
        sys.exit(2)

    status_hist = Counter()
    slow_calls  = []   # (action_name, elapsed)
    failures    = []   # (iteration, action_name, status, detail)
    lock        = threading.Lock()

    def iteration(i):
        # Each worker uses a seeded Random derived from the master seed
        # + its own id, so the action sequence is deterministic per
        # (seed, i) but workers don't all do the same thing.
        local_rng = random.Random(seed * 997 + i)
        fn = _weighted_choice(local_rng, ACTIONS)
        desc, (status, elapsed) = fn(local_rng, target, base)

        with lock:
            status_hist[status] += 1
            # 500 = internal server error (gateway bug) → always a failure.
            # 503 = service unavailable → gateway refusing work → failure.
            # 502/504 = bad gateway / upstream timeout → LEGITIMATE when
            # a chaos action deliberately points at an unreachable URL.
            # Counting those as failures gives false positives.
            if status in (500, 503):
                failures.append((i, desc, status, "gateway 5xx"))
            # 5s is the "something is actually broken" threshold. An
            # overloaded UPnP renderer can take 2-4s to answer
            # GetTransportInfo when hammered — that's upstream slowness,
            # not a gateway bug. Cache coalescing means only the ~1 in
            # TTL-window fetcher eats the latency; all other callers
            # get the stale cache in milliseconds.
            if elapsed > 5.0 and desc.startswith("snapshot"):
                slow_calls.append((desc, elapsed))
        if not quiet:
            print(f"  [{i:4d}] {status:>4}  {elapsed*1000:6.0f}ms  {desc}")

    t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(iteration, range(iterations)))
    total_sec = time.monotonic() - t0

    stderr_text, stderr_after, stderr_rotated = _stderr_since(stderr_before)
    stderr_growth = len(stderr_text)
    crash_lines, benign_lines = classify_stderr(stderr_text)

    # Summary
    print()
    print("═" * 60)
    print(f"chaos: {iterations} actions in {total_sec:.1f}s "
          f"({iterations/total_sec:.0f}/s, {workers} workers)")
    print(f"chaos: status histogram: {dict(status_hist)}")
    if stderr_rotated:
        print(f"chaos: {STDERR_PATH} was truncated/recreated during the run "
              f"— read from the start ({stderr_after} bytes)")
    if not stderr_growth:
        print(f"chaos: {STDERR_PATH} did not grow")
    elif crash_lines:
        print(f"chaos: {STDERR_PATH} grew by {stderr_growth} bytes — "
              f"{len(crash_lines)} CRASH line(s):")
        for ln in crash_lines[:5]:
            print(f"    {ln.strip()[:120]}")
    else:
        print(f"chaos: {STDERR_PATH} grew by {stderr_growth} bytes "
              f"({benign_lines} line(s)) — no crash signature, benign")
    if slow_calls:
        print(f"chaos: {len(slow_calls)} slow snapshot(s) > 5s:")
        for d, e in slow_calls[:10]:
            print(f"    {e*1000:.0f}ms  {d}")

    # Pass/fail
    hard_fails = []
    if crash_lines:
        hard_fails.append(
            f"{len(crash_lines)} crash line(s) appended to {STDERR_PATH} — "
            f"a worker thread died silently: "
            f"{crash_lines[0].strip()[:100]}. "
            f"tail {STDERR_PATH} to see the traceback.")
    bad_5xx = {500, 503}
    if any(s in bad_5xx for s in status_hist):
        n = sum(v for s, v in status_hist.items() if s in bad_5xx)
        hard_fails.append(f"{n} 500/503 response(s) — handler itself broke")
    if slow_calls:
        hard_fails.append(
            f"{len(slow_calls)} snapshot(s) took > 5s — "
            f"gateway is wedged or renderer is unreachable")

    if hard_fails:
        print()
        print("FAIL:")
        for f in hard_fails:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("PASS")
    sys.exit(0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--base",       default="https://localhost:8443")
    ap.add_argument("--iterations", type=int, default=200)
    ap.add_argument("--workers",    type=int, default=3)
    ap.add_argument("--seed",       type=int, default=None,
                    help="seed for reproducibility (default: random)")
    ap.add_argument("--quiet",      action="store_true")
    args = ap.parse_args()
    if args.seed is None:
        args.seed = random.randint(1, 10**6)
    run(args.base, args.iterations, args.workers, args.seed, args.quiet)
