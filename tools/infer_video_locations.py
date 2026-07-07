#!/usr/bin/env python3
"""Infer locations for GPS-less videos from their temporal neighbors.

Phone videos carry GPS; videos received via WhatsApp/AirDrop or shot on
older devices don't, so they sit in the "(no location)" bucket. But a
GPS-less clip shot the same day (or within a few days) as located clips
was almost certainly shot in the same place. Tiers, strongest first:

  same_day  all located same-calendar-day neighbors agree on (place, country)
  day1      all located neighbors within ±1 day agree
  window    all located neighbors within ±`--window` days (default 3) agree
  country   neighbors within the window agree on COUNTRY only (cities
            differ) → country-level location, browsable via the
            "(no city)" bucket inside that country

Safety rules:
  * Evidence = rows located by REAL GPS only — never by a previous
    inference (no chaining) and never by a geocode-less GPS row.
  * DEVICE CHECK (default on): the video and its neighbors must carry the
    same make/model tags (com.apple.quicktime.make/model via ffprobe).
    A clip with no device tags (typically WhatsApp-received) is never
    inferred. `--no-device-check` relaxes both.
  * Results are written to `video_location_overrides` (source =
    inferred_*), NOT onto the scanner-owned `videos` rows directly — the
    end-of-scan hook re-applies them after every (force) rescan. A
    'manual' override is never overwritten.
  * DRY-RUN by default; --apply writes and immediately re-applies.

`--retry-geocode` additionally handles the other unlocated class: rows
that HAVE GPS but got an empty geocode result (sticky-cached '') — it
evicts the cached miss and asks Nominatim again (network; only acts
with --apply).

    python3 tools/infer_video_locations.py                # preview
    python3 tools/infer_video_locations.py --apply        # write overrides
    python3 tools/infer_video_locations.py --window 5 --no-device-check
    python3 tools/infer_video_locations.py --retry-geocode --apply

Undo a single video:   DELETE FROM video_location_overrides WHERE video_id='…';
(the next 5-minute scan pass won't re-add it unless the tool runs again;
clear the videos row's fields via a force rescan or the tool)
"""
import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

VIDEO_UDN = "uuid:localfs-movies"
DEFAULT_DB = os.path.join(PROJECT, "library.db")

TIER_SOURCE = {"same_day": "inferred_same_day", "day1": "inferred_window",
               "window": "inferred_window", "country": "inferred_country"}

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _day(created):
    """ISO-ish timestamp → datetime.date, or None."""
    m = _DATE_RE.match(str(created or ""))
    if not m:
        return None
    try:
        return _dt.date(*map(int, m.groups()))
    except ValueError:
        return None


# ── device tags (the WhatsApp guard) ──────────────────────────────

def probe_device(path, ffprobe="ffprobe"):
    """(make, model) from the file's format tags, or None when either is
    absent — a tag-less clip (WhatsApp strips them) is never inferred."""
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        fmt = (json.loads(r.stdout).get("format") or {})
        tags = {str(k).lower(): v for k, v in (fmt.get("tags") or {}).items()}
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    make = tags.get("com.apple.quicktime.make") or tags.get("make") or ""
    model = tags.get("com.apple.quicktime.model") or tags.get("model") or ""
    make, model = str(make).strip(), str(model).strip()
    if not (make and model):
        return None
    return (make, model)


# ── the pure inference core (unit-tested) ─────────────────────────

def _decide(v, located, window, device_check, device_of):
    base = {"id": v["id"], "tier": None, "source": "", "location_name": "",
            "country": "", "neighbors": 0, "reason": ""}
    d = _day(v.get("created"))
    if d is None:
        base["reason"] = "no_date"
        return base
    cands = []
    for n in located:
        nd = _day(n.get("created"))
        if nd is None:
            continue
        dd = abs((nd - d).days)
        if dd <= window:
            cands.append((n, dd))
    if device_check:
        dev = device_of(v) if device_of else None
        if not dev:
            base["reason"] = "no_device"
            return base
        cands = [(n, dd) for n, dd in cands
                 if (device_of(n) if device_of else None) == dev]
    if not cands:
        base["reason"] = "no_neighbors"
        return base
    for tier, dist in (("same_day", 0), ("day1", 1), ("window", window)):
        sel = [n for n, dd in cands if dd <= dist]
        keys = {((n.get("location_name") or ""), (n.get("country") or ""))
                for n in sel}
        if sel and len(keys) == 1:
            loc, cc = keys.pop()
            return {**base, "tier": tier, "source": TIER_SOURCE[tier],
                    "location_name": loc, "country": cc,
                    "neighbors": len(sel)}
    ccs = {(n.get("country") or "") for n, _dd in cands}
    if len(ccs) == 1 and "" not in ccs:
        return {**base, "tier": "country", "source": "inferred_country",
                "country": ccs.pop(), "neighbors": len(cands)}
    base["reason"] = "disagree"
    return base


def infer_all(videos, *, window=3, device_check=True, device_of=None):
    """Decide every GPS-less video against the real-GPS-located ones.
    Returns one decision dict per target (tier=None when undecidable)."""
    located = [v for v in videos
               if (v.get("location") or "").strip()
               and (v.get("location_name") or "").strip()]
    targets = [v for v in videos if not (v.get("location") or "").strip()]
    return [_decide(v, located, window, device_check, device_of)
            for v in targets]


def write_overrides(db, decisions) -> int:
    """Persist the decided inferences (manual rows are never overwritten —
    video_loc_override_set refuses those). Returns rows written."""
    n = 0
    for d in decisions:
        if not d.get("tier"):
            continue
        if db.video_loc_override_set(d["id"], d["location_name"],
                                     d["country"], d["source"]):
            n += 1
    return n


# ── --retry-geocode (the GPS-but-cached-empty class) ──────────────

def retry_geocode(db, videos, *, apply=False, verbose=False):
    """Rows with GPS but an empty geocode result: evict the sticky cache
    miss and re-ask Nominatim (network). Only acts with apply=True."""
    import dlna_ffmpeg as ff
    import dlna_geocode
    targets = [v for v in videos
               if (v.get("location") or "").strip()
               and not (v.get("location_name") or "").strip()]
    print(f"\n--retry-geocode: {len(targets)} GPS-but-unnamed video(s)")
    if not targets or not apply:
        return 0
    fixed = 0
    for v in targets:
        coords = ff.parse_iso6709(v.get("location"))
        if not coords:
            continue
        lat, lon = coords
        la, lo = db._geo_key(lat, lon)
        with db._pool.write() as conn:
            conn.execute("DELETE FROM geocode_cache "
                         "WHERE lat_key=? AND lon_key=?", (la, lo))
        got = dlna_geocode.place_for(db, lat, lon)
        if not got or not (got[0] or got[1]):
            if verbose:
                print(f"  ✗ {v['id']}  still no result for "
                      f"{lat:.4f},{lon:.4f}")
            continue
        name, cc = got
        ext = os.path.splitext(v.get("file_path") or "")[1]
        coords_str = f"{lat:.4f},{lon:.4f}"
        title = v.get("title") or ""
        # rebuild only a constructed title (with or without the coords
        # fallback the scanner may have used) — never an embedded one
        for old_loc in (coords_str, None):
            if title == ff.build_display_title(
                    None, v.get("created"), None, old_loc, ext,
                    country=v.get("country") or ""):
                title = ff.build_display_title(
                    None, v.get("created"), name or None, None, ext,
                    country=cc or "")
                break
        db.update_video_location(v["id"], name or None, cc or None, title)
        fixed += 1
        print(f"  ✓ {v['id']}  → {name or '(no name)'}, {cc or '??'}")
    print(f"--retry-geocode: resolved {fixed}/{len(targets)}")
    return fixed


# ── CLI ───────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Infer locations for GPS-less videos from temporal "
                    "neighbors (dry-run by default).")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--udn", default=VIDEO_UDN)
    ap.add_argument("--window", type=int, default=3,
                    help="max ± days for neighbor evidence (default 3)")
    ap.add_argument("--no-device-check", action="store_true",
                    help="don't require matching make/model tags")
    ap.add_argument("--retry-geocode", action="store_true",
                    help="also re-geocode GPS-but-unnamed rows "
                         "(network; acts only with --apply)")
    ap.add_argument("--apply", action="store_true",
                    help="write the overrides (default: preview only)")
    ap.add_argument("-y", "--yes", action="store_true",
                    help="skip the confirmation prompt")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f"FATAL: DB not found: {args.db}")
        return 2

    from dlna_library import LibraryDB
    import dlna_ffmpeg as ff
    import dlna_video_index
    db = LibraryDB(args.db)

    videos = db.all_videos(args.udn)
    if not videos:
        print(f"No videos for udn {args.udn} — nothing to do.")
        return 0

    if args.retry_geocode:
        if retry_geocode(db, videos, apply=args.apply,
                         verbose=args.verbose):
            videos = db.all_videos(args.udn)   # fresh rows = fresh evidence

    device_of = None
    if not args.no_device_check:
        ffprobe = ff.find_ffprobe()
        if not ffprobe:
            print("FATAL: ffprobe not found — the device check needs it "
                  "(brew install ffmpeg), or pass --no-device-check.")
            return 2
        memo = {}

        def device_of(v):                                  # noqa: F811
            vid = v["id"]
            if vid not in memo:
                memo[vid] = probe_device(v.get("file_path") or "",
                                         ffprobe=ffprobe)
            return memo[vid]

    decisions = infer_all(videos, window=args.window,
                          device_check=not args.no_device_check,
                          device_of=device_of)

    by_id = {v["id"]: v for v in videos}
    decided = [d for d in decisions if d["tier"]]
    blocked = [d for d in decisions if not d["tier"]]
    tiers = {}
    for d in decided:
        tiers[d["tier"]] = tiers.get(d["tier"], 0) + 1
    reasons = {}
    for d in blocked:
        reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1

    print(f"\n{len(videos)} videos · {len(decisions)} GPS-less targets · "
          f"window ±{args.window}d · device check "
          f"{'OFF' if args.no_device_check else 'on'}")
    print("inferred: " + (", ".join(
        f"{t}={tiers[t]}" for t in ("same_day", "day1", "window", "country")
        if t in tiers) or "none"))
    print("blocked:  " + (", ".join(
        f"{r}={n}" for r, n in sorted(reasons.items())) or "none"))

    for d in decided:
        v = by_id.get(d["id"], {})
        where = (f"{d['country']}/{d['location_name']}" if d["location_name"]
                 else f"{d['country']} (country only)")
        print(f"  {d['id']}  {str(v.get('created') or '')[:10]}  "
              f"→ {where}   [{d['tier']}, {d['neighbors']} nb]  "
              f"{v.get('title') or ''}")
    if args.verbose:
        for d in blocked:
            v = by_id.get(d["id"], {})
            print(f"  – {d['id']}  {str(v.get('created') or '')[:10]}  "
                  f"blocked: {d['reason']}   {v.get('title') or ''}")

    if not args.apply:
        if decided:
            print("\nDRY-RUN — re-run with --apply to write the overrides.")
        return 0
    if not decided:
        print("\nNothing to apply.")
        return 0
    if not args.yes:
        ans = input(f"\nWrite {len(decided)} override(s) and re-apply "
                    f"onto the video index? [Y/n] ").strip().lower()
        if ans not in ("", "y", "yes"):
            print("Aborted.")
            return 1
    n = write_overrides(db, decisions)
    applied = dlna_video_index.apply_location_overrides(db, args.udn)
    print(f"overrides written: {n} · applied onto videos: {applied}")
    print("(the periodic video scan re-applies these after every rescan)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
