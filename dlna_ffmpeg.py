"""
dlna_ffmpeg.py — optional ffmpeg/ffprobe helpers for the video feature (V0).

ffmpeg + ffprobe are OPTIONAL external binaries (same posture as `fpcalc`):
  • probe()           — read a video's metadata (duration / dims / codecs /
                        container / capture time / GPS / title) via ffprobe.
  • extract_poster()  — grab a single poster frame via ffmpeg.
  • transcode_cmd()   — build the on-demand H.264/AAC transcode argv (Phase V3).
  • build_display_title() / parse_iso6709() — pure helpers for the
                        "<place>_YYYYMMDD_HHMM.ext" fallback title.

When the binaries are absent everything degrades gracefully (probe → None,
extract_poster → False) so the gateway stays audio-first and video simply
doesn't light up. launchd has a minimal PATH, so binary discovery also checks
the usual Homebrew locations explicitly.

See docs/VIDEO_SUPPORT.md (Phases V0/V1/V3).
"""
import json
import logging
import os
import re
import shutil
import subprocess

log = logging.getLogger("dlna.ffmpeg")

# Homebrew + common locations — launchd-spawned processes get a minimal PATH,
# so shutil.which alone isn't enough (same reason the enrichment tools do this).
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")

# On-disk poster-frame cache (mirrors dlna_art_cache). Posters are written in
# Phase V1; the dir is gitignored. Env-overridable.
POSTER_DIR = os.environ.get("VIDEO_POSTER_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "video_posters")


def _find(name: str):
    """Locate an executable by name → absolute path, or None."""
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_BIN_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def find_ffprobe():
    return _find("ffprobe")


def find_ffmpeg():
    return _find("ffmpeg")


# ── numeric / format helpers ──────────────────────────────────────

def _to_float(v):
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _container(format_name, path=""):
    """A short container token from ffprobe's format_name (e.g.
    'mov,mp4,m4a,3gp,3g2,mj2' → 'mov') or the file extension as fallback."""
    if format_name:
        return str(format_name).split(",")[0].strip() or None
    ext = os.path.splitext(path or "")[1].lstrip(".").lower()
    return ext or None


# ── GPS (ISO 6709) ────────────────────────────────────────────────
# Phone videos carry location as ISO 6709, e.g. '+52.3676+004.9041/' or
# '+52.3676-004.9041+012.3/' (lat, lon, optional altitude).
_ISO6709_RE = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def parse_iso6709(s):
    """ISO 6709 string → (lat, lon) floats, or None. Altitude is ignored."""
    if not s:
        return None
    m = _ISO6709_RE.search(str(s))
    if not m:
        return None
    try:
        return (float(m.group(1)), float(m.group(2)))
    except ValueError:
        return None


# ── ffprobe ───────────────────────────────────────────────────────

def _parse_probe(data: dict, path: str = "") -> dict:
    """Normalise a parsed ffprobe JSON document → our metadata dict. Pure."""
    fmt     = data.get("format") or {}
    streams = data.get("streams") or []
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    a = next((s for s in streams if s.get("codec_type") == "audio"), {})
    tags = {str(k).lower(): val for k, val in (fmt.get("tags") or {}).items()}
    return {
        "duration":  _to_float(fmt.get("duration") or v.get("duration")),
        "width":     _to_int(v.get("width")),
        "height":    _to_int(v.get("height")),
        "vcodec":    (v.get("codec_name") or "").lower() or None,
        "acodec":    (a.get("codec_name") or "").lower() or None,
        "container": _container(fmt.get("format_name"), path),
        "created":   tags.get("creation_time") or None,
        "location":  (tags.get("location")
                      or tags.get("com.apple.quicktime.location.iso6709")
                      or None),
        "title":     tags.get("title") or None,
    }


def probe(path: str, ffprobe: str = None):
    """Probe a video file → metadata dict, or None when ffprobe is unavailable
    or the probe fails (callers fall back to filename/mtime)."""
    exe = ffprobe or find_ffprobe()
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        return _parse_probe(json.loads(r.stdout), path)
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        log.debug("ffprobe failed for %s: %s", path, e)
        return None


def parse_chapters(data: dict) -> list:
    """ffprobe -show_chapters JSON → [{"start": sec, "end": sec|None,
    "title": str}], sorted by start. Untitled chapters get 'Chapter N'."""
    out = []
    for i, ch in enumerate(data.get("chapters") or []):
        start = _to_float(ch.get("start_time"))
        if start is None:
            continue
        title = ((ch.get("tags") or {}).get("title") or "").strip()
        out.append({"start": start,
                    "end": _to_float(ch.get("end_time")),
                    "title": title or f"Chapter {i + 1}"})
    out.sort(key=lambda c: c["start"])
    return out


def probe_chapters(path: str, ffprobe: str = None) -> list:
    """Chapter atoms of a media file (single-file m4b audiobooks carry
    them). [] when ffprobe is unavailable, the probe fails, or the file
    simply has no chapters — callers need no distinction."""
    exe = ffprobe or find_ffprobe()
    if not exe:
        return []
    try:
        r = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json",
             "-show_chapters", path],
            capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return []
        return parse_chapters(json.loads(r.stdout))
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        log.debug("ffprobe chapters failed for %s: %s", path, e)
        return []


# ── poster frame ──────────────────────────────────────────────────

def poster_cmd(path: str, out_path: str, when: str = "00:00:03",
               ffmpeg: str = "ffmpeg") -> list:
    """argv to grab one JPEG poster frame at `when` (seek before input = fast)."""
    return [ffmpeg, "-v", "error", "-ss", str(when), "-i", path,
            "-frames:v", "1", "-q:v", "3", "-y", out_path]


def extract_poster(path: str, out_path: str, when: str = "00:00:03",
                   ffmpeg: str = None) -> bool:
    """Extract a poster frame → True on success, False if ffmpeg missing/fails."""
    exe = ffmpeg or find_ffmpeg()
    if not exe:
        return False
    try:
        r = subprocess.run(poster_cmd(path, out_path, when, exe),
                           capture_output=True, timeout=30)
        return r.returncode == 0 and os.path.isfile(out_path)
    except (subprocess.SubprocessError, OSError) as e:
        log.debug("poster extract failed for %s: %s", path, e)
        return False


# ── on-demand transcode (Phase V3) ────────────────────────────────

HLS_SEG = 6.0    # segment length (s) — playlist + segment cmd must agree


def hls_playlist(duration, seg: float = HLS_SEG) -> str:
    """A VOD HLS playlist computed from the clip duration (no transcoding) —
    served instantly so the player knows the full timeline and can seek. Each
    segment is transcoded on demand when requested. Pure → unit-testable."""
    import math
    dur = max(float(duration or 0), 0.0)
    n = max(1, math.ceil(dur / seg)) if dur else 1
    out = ["#EXTM3U", "#EXT-X-VERSION:3",
           f"#EXT-X-TARGETDURATION:{int(math.ceil(seg))}",
           "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    for i in range(n):
        d = seg if (i < n - 1) else (round(dur - seg * (n - 1), 3) or seg)
        out.append(f"#EXTINF:{d:.3f},")
        out.append(f"seg{i}.ts")
    out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


def hls_segment_cmd(path: str, start: float, dur: float = HLS_SEG,
                    ffmpeg: str = None) -> list:
    """argv to transcode ONE segment [start, start+dur) → H.264/AAC MPEG-TS on
    stdout. `-output_ts_offset start` keeps each independently-encoded segment's
    timestamps on the global timeline so hls.js stitches + seeks cleanly."""
    exe = ffmpeg or find_ffmpeg() or "ffmpeg"
    return [
        exe, "-v", "error", "-ss", str(start), "-t", str(dur), "-i", path,
        # 8-bit 4:2:0 — source may be 10-bit HEVC (x265); H.264 High is 8-bit
        # only and 10-bit H.264 isn't broadly browser-playable anyway.
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
        "-level", "4.1", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2", "-ar", "48000", "-b:a", "192k",
        "-f", "mpegts", "-muxdelay", "0", "-output_ts_offset", str(start),
        "pipe:1",
    ]


def transcode_cmd(path: str, ffmpeg: str = None) -> list:
    """argv to transcode `path` → fragmented H.264/AAC MP4 on stdout (pipe:1) —
    the universal-playback target for the capability-aware fallback."""
    exe = ffmpeg or find_ffmpeg() or "ffmpeg"
    return [
        exe, "-v", "error", "-i", path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",     # 8-bit — source may be 10-bit HEVC
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
    ]


# ── display title ─────────────────────────────────────────────────

def _fmt_dt(created) -> str:
    """ISO-ish timestamp → 'YYYYMMDD_HHMM', or '' if unparseable."""
    if not created:
        return ""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", str(created))
    if not m:
        return ""
    y, mo, d, h, mi = m.groups()
    return f"{y}{mo}{d}_{h}{mi}"


def build_display_title(embedded_title, created, location_name, coords,
                        ext: str, country: str = "") -> str:
    """The video's display title: the embedded title if present, otherwise
    `<country>_<location>_<YYYYMMDD>_<HHMM>.<ext>` — country = ISO code
    (uppercase, 2026-07-06), location = geocoded place name, else raw
    coords, else omitted; date/time from capture time (caller passes mtime
    as a fallback `created`); falls back to 'video' when nothing is known."""
    if embedded_title and str(embedded_title).strip():
        return str(embedded_title).strip()
    cc  = (country or "").strip()
    loc = (location_name or coords or "").strip()
    dt  = _fmt_dt(created)
    stem = "_".join(p for p in (cc, loc, dt) if p) or "video"
    ext = (ext or "").lstrip(".").lower()
    return f"{stem}.{ext}" if ext else stem
