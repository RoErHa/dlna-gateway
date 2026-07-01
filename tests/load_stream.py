#!/usr/bin/env python3
"""/stream concurrency load test — guards the threadpool-starvation regression.

The 2.0 origin bug: the ASGI app ran EVERY blocking op (browse/DB, /art, the
legacy bridge, AND every byte-relay read of every audio stream) through one
shared 40-token threadpool. Under a few concurrent iOS streams + browsing the
40 slots saturated, stream starts stalled for seconds, and Mobile Safari
aborted the load ("stops after one track" / code-4). Fixed by raising the
limiter to 256 + 256 KB relay reads (dlna_asgi.py). Nothing guarded it — this
does: fire N concurrent full /stream pulls at the LIVE gateway and assert zero
failures + p95 under a threshold.

This is a LIVE-gateway, OPT-IN load test (like tests/chaos.py) — NOT part of
run_all.py. It needs a running gateway with an indexed library.

RUN
  # gateway must be running; uses real track URLs from library.db
  python3 tests/load_stream.py                                  # defaults: c=40 n=80
  python3 tests/load_stream.py --concurrency 60 --count 120
  python3 tests/load_stream.py --gateway https://127.0.0.1:8443 --insecure
  python3 tests/load_stream.py --max-p95 6.0                    # fail if p95 exceeds

Exit 0 = all requests succeeded AND p95 <= --max-p95 (if set). Non-zero = a
failure/timeout occurred, or p95 exceeded the threshold. Prints full stats so
before/after runs are directly comparable.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _track_urls(db_path: str, limit: int) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT url FROM tracks WHERE url LIKE '%/localfs/stream/%' "
            "ORDER BY RANDOM() LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _pull(stream_url: str, timeout: float, ctx) -> tuple[int, float, int]:
    """Full-body GET of one /stream URL. Returns (status, seconds, bytes)."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(stream_url)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            n = 0
            while True:
                chunk = r.read(262_144)
                if not chunk:
                    break
                n += len(chunk)
            return (r.status, time.monotonic() - t0, n)
    except Exception as e:  # noqa: BLE001
        # 0 status = failure (timeout, refused, 5xx via HTTPError, ...).
        code = getattr(e, "code", 0) or 0
        return (int(code) if code >= 400 else 0, time.monotonic() - t0, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gateway", default="http://127.0.0.1:8765")
    ap.add_argument("--db", default=str(ROOT / "library.db"))
    ap.add_argument("--concurrency", type=int, default=40)
    ap.add_argument("--count", type=int, default=80)
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="per-request timeout (s); a stalled stream = failure")
    ap.add_argument("--max-p95", type=float, default=None,
                    help="fail if p95 latency exceeds this many seconds")
    ap.add_argument("--insecure", action="store_true",
                    help="skip TLS verify (for the self-signed :8443 bind)")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"✗ no library.db at {args.db}")
        return 2
    urls = _track_urls(args.db, args.count)
    if not urls:
        print("✗ no /localfs/stream/ track URLs in the DB — nothing to load-test")
        return 2

    ctx = None
    if args.gateway.startswith("https") and args.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    targets = [
        f"{args.gateway}/stream?url={urllib.parse.quote(u, safe='')}"
        for u in (urls * ((args.count // len(urls)) + 1))[:args.count]
    ]
    print(f"load: {args.count} full /stream pulls @ concurrency "
          f"{args.concurrency} → {args.gateway}")
    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        results = list(ex.map(lambda u: _pull(u, args.timeout, ctx), targets))
    wall = time.monotonic() - t0

    ok = [r for r in results if r[0] in (200, 206)]
    bad = [r for r in results if r[0] not in (200, 206)]
    times = sorted(r[1] for r in ok)
    def pct(p): return times[min(int(len(times) * p), len(times) - 1)] if times else 0.0
    p50, p95, mx = pct(.5), pct(.95), (times[-1] if times else 0.0)
    total_mb = sum(r[2] for r in ok) / 1e6

    print(f"  wall={wall:.1f}s  ok={len(ok)}  failed={len(bad)}  "
          f"throughput={total_mb/wall:.0f} MB/s")
    print(f"  latency  p50={p50:.2f}s  p95={p95:.2f}s  max={mx:.2f}s")
    if bad:
        from collections import Counter
        print(f"  failure codes: {dict(Counter(r[0] for r in bad))} "
              "(0 = timeout/connection error)")

    failed = False
    if bad:
        print(f"✗ {len(bad)} request(s) failed under load")
        failed = True
    if args.max_p95 is not None and p95 > args.max_p95:
        print(f"✗ p95 {p95:.2f}s exceeds --max-p95 {args.max_p95:.2f}s")
        failed = True
    print("FAIL" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
