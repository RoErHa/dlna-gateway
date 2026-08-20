"""
dlna_video_index.py — scan LOCALFS_VIDEO_ROOT (GWMovies) into the `videos`
table (Phase V1c). Fully independent of the audio LocalFs scan.

Per video file: stable id = sha1(rel_path)[:16]; metadata via dlna_ffmpeg.probe
(graceful when ffprobe absent → filename/mtime fallback); place name via
dlna_geocode.place_for (always-on when online, cached); display title via
dlna_ffmpeg.build_display_title; a poster frame into dlna_ffmpeg.POSTER_DIR.
Incremental (skip files whose (mtime,size) is unchanged) and prunes rows whose
file is gone. `force=True` clears first for a full rebuild.
"""
import datetime as _dt
import hashlib
import logging
import os
from pathlib import Path

import dlna_ffmpeg as ff
import dlna_geocode

log = logging.getLogger("dlna.video.index")

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".3gp",
              ".m2ts", ".mts"}

_MIME = {
    "mp4": "video/mp4", "m4v": "video/mp4", "mov": "video/quicktime",
    "mkv": "video/x-matroska", "webm": "video/webm", "avi": "video/x-msvideo",
    "3gp": "video/3gpp", "m2ts": "video/mp2t", "mts": "video/mp2t",
}


def video_id(rel_path: str) -> str:
    """Stable id = sha1(rel_path)[:16] — mirrors the LocalFs track-id scheme."""
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]


def _walk(root: Path):
    for dirpath, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            if os.path.splitext(name)[1].lower() in VIDEO_EXTS:
                yield Path(dirpath) / name


def _mtime_iso(mtime: float) -> str:
    return _dt.datetime.fromtimestamp(mtime, _dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def build_row(path: Path, rel: str, vid: str, udn: str, base_url: str, st,
              db, *, poster_dir: str = None, geocode: bool = True,
              ffprobe: str = None, ffmpeg: str = None) -> dict:
    """Assemble a `videos` row for one file (probe + geocode + title + poster)."""
    ext = path.suffix.lstrip(".").lower()
    meta = ff.probe(str(path), ffprobe=ffprobe) or {}
    created = meta.get("created") or _mtime_iso(st.st_mtime)

    # Reverse-geocode the GPS, if present + online.
    coords = ff.parse_iso6709(meta.get("location"))
    location_name = None
    country = ""
    if coords and geocode:
        try:
            got = dlna_geocode.place_for(db, coords[0], coords[1])
            if got is not None:
                location_name, country = got
        except Exception as e:                       # noqa: BLE001
            log.debug("geocode error for %s: %s", rel, e)
    coords_str = (f"{coords[0]:.4f},{coords[1]:.4f}" if coords else None)

    title = ff.build_display_title(
        meta.get("title"), created, location_name or None, coords_str, ext,
        country=country)

    # Poster frame (best-effort). Seek a few seconds in, but not past the end
    # of short clips (a 1.4s clip seeking to 3s yields no frame).
    dur = meta.get("duration") or 0
    when = "0" if dur < 1.5 else ("1" if dur < 6 else "3")
    poster = None
    pdir = poster_dir or ff.POSTER_DIR
    if pdir:
        try:
            os.makedirs(pdir, exist_ok=True)
            out = os.path.join(pdir, f"{vid}.jpg")
            if os.path.isfile(out) or ff.extract_poster(str(path), out,
                                                         when=when, ffmpeg=ffmpeg):
                poster = vid
        except OSError as e:
            log.debug("poster error for %s: %s", rel, e)

    return {
        "id": vid, "udn": udn,
        "url": f"{base_url.rstrip('/')}/localfs/video/{vid}",
        "title": title, "file_path": str(path),
        "folder": os.path.dirname(rel),
        "duration": meta.get("duration"),
        "width": meta.get("width"), "height": meta.get("height"),
        "vcodec": meta.get("vcodec"), "acodec": meta.get("acodec"),
        "container": meta.get("container") or ext,
        "mime": _MIME.get(ext, "application/octet-stream"),
        "size": st.st_size, "mtime": st.st_mtime,
        "created": created,
        "location": meta.get("location"),
        "location_name": (location_name or None),
        "country": (country or None),
        "poster": poster,
    }


def apply_location_overrides(db, udn: str) -> int:
    """Lay `video_location_overrides` rows back onto `videos` (Plan A).

    The scanner derives rows from file metadata, and overridden files have
    NO GPS — so a force rescan regenerates them bare. Called at the end of
    every scan_videos pass. Rules:
      * a row with real GPS (`location` set) is NEVER touched — the
        override only fills where the file itself is silent;
      * a constructed title is rebuilt with the override; an embedded
        title (anything that doesn't match the constructed form) is kept;
      * idempotent — returns the number of rows actually changed.
    """
    changed = 0
    for ovr in db.video_loc_override_list():
        v = db.video_by_id(ovr["video_id"])
        if not v or v.get("udn") != udn:
            continue
        if (v.get("location") or "").strip():
            continue                       # real GPS wins, always
        new_loc = ovr.get("location_name") or None
        new_cc = ovr.get("country") or None
        cur_loc = v.get("location_name") or None
        cur_cc = v.get("country") or None
        if (cur_loc, cur_cc) == (new_loc, new_cc):
            continue
        ext = os.path.splitext(v.get("file_path") or "")[1]
        # The title the row would have if constructed from its CURRENT
        # fields; a match means it's not an embedded title → safe to rebuild.
        expected = ff.build_display_title(
            None, v.get("created"), cur_loc, None, ext,
            country=cur_cc or "")
        title = v.get("title") or ""
        if title == expected:
            title = ff.build_display_title(
                None, v.get("created"), new_loc, None, ext,
                country=new_cc or "")
        db.update_video_location(ovr["video_id"], new_loc, new_cc, title)
        changed += 1
    if changed:
        log.info("video location overrides re-applied: %d row(s)", changed)
    return changed


def scan_videos(root, udn: str, db, base_url: str, *, force: bool = False,
                poster_dir: str = None, geocode: bool = True,
                ffprobe: str = None, ffmpeg: str = None) -> dict:
    """Scan `root` into `videos` for `udn`. Incremental by (mtime,size); prunes
    rows whose file is gone; `force` clears first. Returns a stats dict."""
    root = Path(root).expanduser()
    if not root.exists():
        log.warning("video root not found: %s — skipping", root)
        return {"scanned": 0, "added": 0, "skipped": 0, "pruned": 0,
                "missing_root": True}
    if force:
        db.clear_videos(udn)

    existing = {v["id"]: (v.get("mtime"), v.get("size"))
                for v in db.all_videos(udn)}
    seen, batch = set(), []
    scanned = added = skipped = 0
    for path in _walk(root):
        try:
            st = path.stat()
            rel = str(path.relative_to(root))
        except OSError:
            continue
        scanned += 1
        vid = video_id(rel)
        seen.add(vid)
        if existing.get(vid) == (st.st_mtime, st.st_size):
            skipped += 1
            continue
        batch.append(build_row(path, rel, vid, udn, base_url, st, db,
                               poster_dir=poster_dir, geocode=geocode,
                               ffprobe=ffprobe, ffmpeg=ffmpeg))
        if len(batch) >= 50:
            db.upsert_videos(udn, batch); added += len(batch); batch = []
    if batch:
        db.upsert_videos(udn, batch); added += len(batch)
    pruned = db.prune_videos(udn, seen)
    applied = apply_location_overrides(db, udn)
    log.info("video scan %s: %d files, +%d, skip %d, prune %d, overrides %d",
             root, scanned, added, skipped, pruned, applied)
    return {"scanned": scanned, "added": added, "skipped": skipped,
            "pruned": pruned, "overrides_applied": applied,
            "missing_root": False}
