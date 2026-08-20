#!/usr/bin/env python3
"""Live Subsonic API verification: performance, content, completeness.

Motivated by "Amperfy is flaky — art, response times, and I seem to see
only a selection of the library" (2026-07-03). Measures instead of
guessing, against the live gateway + library.db as ground truth:

  COMPLETENESS  getArtists / getAlbumList2 pagination / getAlbum song
                counts vs the SAME LibraryDB methods the API wraps —
                any gap means Subsonic clients see less than the PWA.
                Also verifies the served UDN is the MUSIC LocalFs (the
                movies server registering first would silently swap the
                whole library — _default_udn takes the first online).
  PERFORMANCE   p50/p95/max per endpoint over --reps runs (browse,
                search, album fetch, art, stream first-256KB).
  COVER ART     getCoverArt over a random album sample: status, MIME,
                byte size, latency; distinguishes "no art in DB"
                (expected miss) from "art in DB but getCoverArt failed"
                (real defect).

Live-gateway + OPT-IN (like chaos.py / load_stream.py — NOT in
run_all.py). Credentials come from .env (SUBSONIC_USER/PASSWORD).

    python3 tests/subsonic_verify.py
    python3 tests/subsonic_verify.py --gateway https://127.0.0.1:8443 \
        --reps 5 --art-sample 40 --album-sample 25

Exit 0 = all checks pass; 1 = discrepancies (printed).
"""
import argparse
import base64
import json
import os
import random
import ssl
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)


def load_env(path):
    creds = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    creds[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return creds


class Client:
    def __init__(self, base, user, password, insecure=True):
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.ctx = ssl._create_unverified_context() if insecure else None

    def call(self, method, timeout=30, **params):
        """GET /rest/<method>; returns (elapsed_sec, status, body_bytes,
        content_type)."""
        q = {"u": self.user, "p": self.password, "v": "1.16.1",
             "c": "subsonic-verify", "f": "json", **params}
        url = f"{self.base}/rest/{method}?{urllib.parse.urlencode(q)}"
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=timeout,
                                        context=self.ctx) as r:
                body = r.read()
                return time.monotonic() - t0, r.status, body, \
                    r.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            return time.monotonic() - t0, e.code, e.read(), ""

    def js(self, method, **params):
        el, status, body, _ = self.call(method, **params)
        data = json.loads(body)["subsonic-response"]
        if data.get("status") != "ok":
            raise RuntimeError(f"{method}: {data.get('error')}")
        return el, data


def album_id(artist, album, album_key=""):
    payload = artist + "\x00" + album
    if album_key:
        payload += "\x00" + album_key
    return "al:" + base64.urlsafe_b64encode(payload.encode()).decode()


def pctl(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="https://127.0.0.1:8443")
    ap.add_argument("--db", default=os.path.join(PROJECT, "library.db"))
    ap.add_argument("--env", default=os.path.join(PROJECT, ".env"))
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--album-sample", type=int, default=25)
    ap.add_argument("--art-sample", type=int, default=40)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    env = load_env(args.env)
    user = os.environ.get("SUBSONIC_USER", env.get("SUBSONIC_USER", "user"))
    pw = os.environ.get("SUBSONIC_PASSWORD", env.get("SUBSONIC_PASSWORD"))
    if not pw:
        print("FATAL: SUBSONIC_PASSWORD not in .env / env"); return 2
    cli = Client(args.gateway, user, pw)

    # ── ground truth: same LibraryDB code path the API wraps ──────────
    os.environ.setdefault("GATEWAY_NO_SERVICES", "1")
    from dlna_library import LibraryDB
    db = LibraryDB(db_file=args.db)
    music_udn = db.primary_udn()
    truth_artists = db.all_artists(music_udn)
    truth_albums = db.all_albums(music_udn)
    print(f"ground truth [{music_udn}]: {len(truth_artists)} artists · "
          f"{len(truth_albums)} albums")

    problems = []
    timings = {}

    def timed(label, fn, reps=args.reps):
        xs = []
        out = None
        for _ in range(reps):
            el, out = fn()
            xs.append(el)
        timings[label] = xs
        return out

    # ── completeness: artists ──────────────────────────────────────────
    data = timed("getArtists", lambda: cli.js("getArtists"))
    api_artists = [a for ix in data["artists"].get("index", [])
                   for a in ix.get("artist", [])]
    print(f"getArtists: {len(api_artists)} (truth {len(truth_artists)})")
    if len(api_artists) != len(truth_artists):
        problems.append(
            f"ARTIST COUNT: getArtists={len(api_artists)} vs "
            f"DB={len(truth_artists)} — wrong UDN or filtering gap")

    # ── completeness: full album pagination (what Amperfy syncs) ──────
    got_albums, offset = [], 0
    t0 = time.monotonic()
    while True:
        _, data = cli.js("getAlbumList2", type="alphabeticalByName",
                         size=500, offset=offset)
        page = data.get("albumList2", {}).get("album", [])
        got_albums.extend(page)
        if len(page) < 500:
            break
        offset += 500
    el_total = time.monotonic() - t0
    print(f"getAlbumList2 paginated: {len(got_albums)} albums in "
          f"{el_total:.1f}s (truth {len(truth_albums)})")
    if len(got_albums) != len(truth_albums):
        problems.append(
            f"ALBUM COUNT: paginated getAlbumList2={len(got_albums)} vs "
            f"DB={len(truth_albums)}")
    ids_seen = [a.get("id") for a in got_albums]
    if len(set(ids_seen)) != len(ids_seen):
        problems.append(
            f"ALBUM PAGINATION: {len(ids_seen) - len(set(ids_seen))} "
            f"duplicate ids across pages (unstable sort?)")

    # ── completeness: per-album song counts + search ──────────────────
    sample = rng.sample(truth_albums, min(args.album_sample,
                                          len(truth_albums)))
    bad_counts = search_misses = display_only = 0
    el_album, el_search = [], []
    for a in sample:
        aid = album_id(a.get("artist", ""), a.get("album", ""),
                       a.get("album_key", "") or "")
        el, data = cli.js("getAlbum", id=aid)
        el_album.append(el)
        api_n = data["album"].get("songCount", 0)
        truth_n = len(db.album_tracks(music_udn, a.get("artist", ""),
                                      a.get("album", ""),
                                      album_key=a.get("album_key", "") or ""))
        if api_n != truth_n or api_n == 0:
            bad_counts += 1
            problems.append(
                f"ALBUM TRACKS: {a.get('artist','?')!r}/{a.get('album')!r} "
                f"api={api_n} truth={truth_n}")
        # Search by the browse display name; if that misses AND the raw
        # tag name differs, retry with the raw name — folder-derived
        # display names are NOT in FTS (known limitation, counted
        # separately, not a failure).
        truth_tracks = db.album_tracks(
            music_udn, a.get("artist", ""), a.get("album", ""),
            album_key=a.get("album_key", "") or "")
        raw_name = (truth_tracks[0].get("album") if truth_tracks else "") or ""
        q = (a.get("album") or "")[:25].strip()
        if len(q) >= 4:
            el, data = cli.js("search3", query=q)
            el_search.append(el)
            albs = data.get("searchResult3", {}).get("album", [])
            # Match by folder identity (album_key inside the id) when set —
            # search results carry the raw tag name while browse uses the
            # folder-derived display name, so name equality is too strict.
            ak = a.get("album_key") or ""

            def _hit(x, ak=ak, a=a):
                if ak:
                    try:
                        b = (x.get("id") or "")[3:]
                        b += "=" * (-len(b) % 4)      # API strips padding
                        raw = base64.urlsafe_b64decode(b.encode()).decode()
                        return raw.split("\x00")[2:3] == [ak]
                    except Exception:                     # noqa: BLE001
                        return False
                return (x.get("name") or "").lower() == \
                    (a.get("album") or "").lower()

            if not any(_hit(x) for x in albs):
                q2 = raw_name[:25].strip()
                if q2 and q2.lower() != q.lower():
                    _, data = cli.js("search3", query=q2)
                    albs = data.get("searchResult3", {}).get("album", [])
                    if any(_hit(x) for x in albs):
                        display_only += 1
                        continue
                search_misses += 1
                problems.append(f"SEARCH MISS: {q!r} did not return "
                                f"{a.get('album')!r}")
    timings["getAlbum"] = el_album
    timings["search3"] = el_search
    print(f"album sample ({len(sample)}): {bad_counts} bad track counts, "
          f"{search_misses} search misses, {display_only} findable only by "
          f"raw tag (folder display name not in FTS — known limitation)")

    # ── cover art health ───────────────────────────────────────────────
    with_art = [a for a in truth_albums if a.get("art")]
    art_sample = rng.sample(with_art, min(args.art_sample, len(with_art)))
    art_ok = art_fail = 0
    el_art = []
    for a in art_sample:
        aid = album_id(a.get("artist", ""), a.get("album", ""),
                       a.get("album_key", "") or "")
        el, status, body, ctype = cli.call("getCoverArt", id=aid,
                                           timeout=30)
        el_art.append(el)
        if status == 200 and ctype.startswith("image/") and len(body) > 1024:
            art_ok += 1
        else:
            art_fail += 1
            problems.append(
                f"ART FAIL: {a.get('artist','?')!r}/{a.get('album')!r} "
                f"status={status} ctype={ctype} bytes={len(body)}")
    timings["getCoverArt"] = el_art
    no_art = len(truth_albums) - len(with_art)
    print(f"cover art sample ({len(art_sample)} of {len(with_art)} "
          f"albums with art): ok={art_ok} fail={art_fail} · "
          f"{no_art} albums have NO art in the DB (expected misses)")

    # ── stream first-bytes ─────────────────────────────────────────────
    el_stream = []
    for a in rng.sample(truth_albums, min(5, len(truth_albums))):
        tracks = db.album_tracks(music_udn, a.get("artist", ""),
                                 a.get("album", ""),
                                 album_key=a.get("album_key", "") or "")
        if not tracks:
            continue
        tid = "tr:" + base64.urlsafe_b64encode(
            tracks[0]["url"].encode()).decode()
        q = urllib.parse.urlencode({"u": user, "p": pw, "v": "1.16.1",
                                    "c": "subsonic-verify", "id": tid})
        req = urllib.request.Request(
            f"{cli.base}/rest/stream?{q}", headers={"Range": "bytes=0-262143"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=30,
                                        context=cli.ctx) as r:
                r.read()
                el_stream.append(time.monotonic() - t0)
        except Exception as e:                                # noqa: BLE001
            problems.append(f"STREAM FAIL: {tracks[0]['title']!r}: {e}")
    timings["stream 256KB"] = el_stream

    # ── ping baseline ──────────────────────────────────────────────────
    timed("ping", lambda: cli.js("ping"))

    # ── report ─────────────────────────────────────────────────────────
    print("\n── latency (seconds) ──")
    print(f"{'endpoint':<16}{'n':>4}{'p50':>9}{'p95':>9}{'max':>9}")
    for label, xs in timings.items():
        if xs:
            print(f"{label:<16}{len(xs):>4}{pctl(xs,50):>9.3f}"
                  f"{pctl(xs,95):>9.3f}{max(xs):>9.3f}")

    print(f"\n{'FAIL' if problems else 'PASS'}: "
          f"{len(problems)} problem(s)")
    for p in problems[:40]:
        print(f"  ✗ {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
