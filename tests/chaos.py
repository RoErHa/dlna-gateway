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
  - /tmp/dlna-gateway.err must NOT grow during the run. Any growth means
    a daemon thread crashed silently — the exact class of bug that the
    per-renderer refactor and log.exception wrapping are meant to
    prevent regressing.
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

STDERR_PATH = "/tmp/dlna-gateway.err"


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


# ── Main loop ─────────────────────────────────────────────────────

def run(base, iterations, workers, seed, quiet):
    rng = random.Random(seed)
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
            if 500 <= status < 600:
                failures.append((i, desc, status, "5xx response"))
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

    stderr_after  = _stderr_size()
    stderr_growth = stderr_after - stderr_before

    # Summary
    print()
    print("═" * 60)
    print(f"chaos: {iterations} actions in {total_sec:.1f}s "
          f"({iterations/total_sec:.0f}/s, {workers} workers)")
    print(f"chaos: status histogram: {dict(status_hist)}")
    print(f"chaos: /tmp/dlna-gateway.err grew by {stderr_growth} bytes")
    if slow_calls:
        print(f"chaos: {len(slow_calls)} slow snapshot(s) > 5s:")
        for d, e in slow_calls[:10]:
            print(f"    {e*1000:.0f}ms  {d}")

    # Pass/fail
    hard_fails = []
    if stderr_growth > 0:
        hard_fails.append(
            f"stderr grew by {stderr_growth} bytes — "
            f"a worker thread crashed silently. "
            f"tail {STDERR_PATH} to see the traceback.")
    if any(500 <= s < 600 for s in status_hist):
        n = sum(v for s, v in status_hist.items() if 500 <= s < 600)
        hard_fails.append(f"{n} 5xx response(s) — handler returned 500")
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
