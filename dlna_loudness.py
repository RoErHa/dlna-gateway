#!/usr/bin/env python3
"""
dlna_loudness.py — per-track true-peak scanner (peak normalisation).

Walks tracks and measures their true peak (dBTP) via
`ffmpeg -af ebur128=peak=true`. Stores the measured peak plus a per-track
`gain_db = TARGET_PEAK_DBTP - peak_db` (clamped ±2 dB) into the
`track_loudness` cache. The integrated LUFS is captured from the same
ffmpeg run as informational metadata but does NOT drive the gain.

Why peak rather than LUFS: peak normalisation produces tiny per-track
adjustments (most modern masters peak near 0 dBFS, so all tracks land
within a fraction of a dB of each other) — minimal interference with
the user's chosen volume, low risk of clipping a renderer that has no
DSP headroom (the Naim is purely SetVolume-controlled). Trade-off:
peak-normalising does NOT equalise *perceived* loudness; loud rock
will still sound louder than quiet classical even after correction.

The cache is independent of `tracks` (keyed by URL, no FK), so it
survives `clear(udn)` — same persistence pattern as `album_art` and
`play_counts`. Failed scans get a sticky negative-cache row
(`peak_db IS NULL` AND `lufs IS NULL`) so we don't re-attempt every
restart.

The `LOUDNESS_SCANNER` singleton is created in `dlna_library` (the
composition root) and re-exported from there for backward compat.

Lifecycle hooks (mirror `AlbumArtFetcher`):
  - `start_initial_scan(delay=120)` — one-shot startup mop-up.
  - `trigger()` from `Indexer._run()` tail when new tracks are indexed.
"""
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Optional, Tuple

log = logging.getLogger("dlna.library")


# launchd-spawned processes have a minimal PATH (`/usr/bin:/bin:/usr/sbin:/sbin`)
# that excludes Homebrew. shutil.which honours that PATH; check the common
# install locations explicitly as a fallback so the gateway works under
# launchctl without forcing the user to edit the LaunchAgent .plist.
def _find_ffmpeg() -> Optional[str]:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/ffmpeg",   # Apple-Silicon Homebrew
                 "/usr/local/bin/ffmpeg",       # Intel-Mac Homebrew
                 "/usr/bin/ffmpeg"):            # system / Linux
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


_FFMPEG_PATH: Optional[str] = _find_ffmpeg()


# True-peak target. -1.0 dBTP is the typical audiophile choice — keeps
# 1 dB of safety headroom under 0 dBFS so inter-sample peaks can't clip
# the DAC or the renderer's downstream chain.
TARGET_PEAK_DBTP: float = -1.0

# Tight clamp on the per-track adjustment. Peak normalisation between
# modern masters typically lands within fractions of a dB; ±2 dB caps
# the worst-case correction (e.g. an unusually quiet vinyl rip) without
# letting the renderer volume swing audibly between tracks.
_MAX_ABS_GAIN_DB: float = 2.0

# ffmpeg per-call wall-clock cap. ebur128 on a typical FLAC takes ~1 sec;
# we leave generous headroom for high-bitrate / multi-channel files.
_FFMPEG_TIMEOUT_SEC: float = 60.0

# Batch size for the "drain → re-query" loop. Re-querying lets a trigger
# arriving mid-run get absorbed into the current pass rather than racing
# as a second scanner thread.
_BATCH_SIZE: int = 50


# Matches the ebur128 summary block:
#   Integrated loudness:
#     I:         -16.4 LUFS
_LUFS_RE = re.compile(
    r"Integrated loudness:\s*\n\s*I:\s*([+-]?\d+(?:\.\d+)?)\s*LUFS",
    re.MULTILINE)

# With `ebur128=peak=true` ffmpeg appends a True peak block to the same
# summary:
#   True peak:
#     Peak:       -0.4 dBFS
# (units are dBFS-equivalent; this is the inter-sample-reconstructed
# peak that a real DAC will produce, i.e. dBTP.)
_TRUE_PEAK_RE = re.compile(
    r"True peak:\s*\n\s*Peak:\s*([+-]?\d+(?:\.\d+)?)\s*dBFS",
    re.MULTILINE)


def _parse_ebur128(stderr: str) -> Optional[float]:
    """Extract the integrated-loudness LUFS value from ffmpeg's ebur128
    stderr output. Returns None if the LUFS line isn't present
    (NaN, ffmpeg crash, garbled output)."""
    m = _LUFS_RE.search(stderr or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _parse_true_peak(stderr: str) -> Optional[float]:
    """Extract the True peak (dBTP-equivalent) value from ffmpeg's
    ebur128 stderr output. Returns None if the True peak block is
    missing (peak=true wasn't passed, or ffmpeg crashed)."""
    m = _TRUE_PEAK_RE.search(stderr or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


class LoudnessScanner:
    """Background worker that analyses tracks with
    `ffmpeg -af ebur128=peak=true` and stores the per-track true-peak
    plus gain in `track_loudness`. Mirrors `AlbumArtFetcher`
    (`dlna_art_fetcher.py:98-212`)."""

    def __init__(self, db):
        self._db     = db
        self._stop   = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Public API ─────────────────────────────────────────────────

    def bare_tracks(self) -> list:
        """Tracks that haven't been analysed yet. The negative-cache rows
        (`lufs IS NULL`) count as "scanned" — they're already present in
        `track_loudness` so they don't appear here.

        ffmpeg accepts both local file paths and HTTP URLs as input;
        AssetUPnP exposes everything over HTTP (file_path stays empty
        for HTTP-only servers), so we feed `t.url` straight to ffmpeg.
        For servers that DO expose `file://` URIs (e.g. MinimServer),
        the URL still works — http or file: makes no difference here."""
        with self._db._pool.read() as conn:
            rows = conn.execute("""
                SELECT t.url
                  FROM tracks t
                 WHERE t.url != ''
                   AND NOT EXISTS (
                       SELECT 1 FROM track_loudness l WHERE l.url = t.url)
                 GROUP BY t.url
                 ORDER BY t.id
            """).fetchall()
        return [(r["url"],) for r in rows]

    def run_once(self) -> dict:
        """Process bare tracks until none remain. Re-queries between
        batches so triggers arriving mid-run are absorbed."""
        stats = {"total": 0, "ok": 0, "failed": 0}
        # Bail before iterating if ffmpeg isn't installed — otherwise every
        # track gets falsely sticky-cached as "failed" and the scan never
        # recovers when ffmpeg is later installed.
        if not _FFMPEG_PATH:
            log.warning("LoudnessScanner: ffmpeg not found in PATH or "
                        "common install locations — skipping scan. "
                        "Install ffmpeg (e.g. `brew install ffmpeg`) and "
                        "restart the gateway.")
            return stats
        # Be a polite background citizen — don't starve the renderer
        # heartbeat or the indexer of CPU.
        try:
            os.nice(10)
        except (AttributeError, PermissionError):
            pass
        while not self._stop.is_set():
            tracks = self.bare_tracks()
            if not tracks:
                break
            n = len(tracks)
            stats["total"] += n
            log.info(f"LoudnessScanner: analysing {n} track(s) "
                     f"(target={TARGET_PEAK_DBTP} dBTP)")
            for (url,) in tracks[:_BATCH_SIZE]:
                if self._stop.is_set():
                    log.info("LoudnessScanner: stop requested — exiting early")
                    break
                try:
                    lufs, peak_db = self._analyze(url)
                except FileNotFoundError as e:
                    # ffmpeg binary disappeared mid-scan (Homebrew update?).
                    # Don't poison the cache — bail and let the next trigger
                    # re-resolve the path.
                    log.warning(f"LoudnessScanner: ffmpeg binary missing "
                                f"({e}) — aborting scan without caching. "
                                f"Will retry on next trigger / restart.")
                    return stats
                self._persist(url, lufs, peak_db)
                if peak_db is None:
                    stats["failed"] += 1
                    log.info(f"LoudnessScanner ✗ {url[:80]} — cached "
                             f"as negative (won't retry)")
                else:
                    stats["ok"] += 1
                    gain = self._compute_gain(peak_db)
                    lufs_s = f"{lufs:+.1f} LUFS" if lufs is not None else "LUFS=?"
                    log.debug(f"LoudnessScanner ✓ {url[:80]} → "
                              f"peak {peak_db:+.1f} dBTP ({lufs_s}), "
                              f"gain {gain:+.1f} dB")
        if stats["total"]:
            log.info(f"LoudnessScanner: done — ok={stats['ok']}, "
                     f"failed={stats['failed']}")
        else:
            log.debug("LoudnessScanner: no bare tracks to analyse")
        return stats

    def trigger(self, delay: float = 0.0):
        """Fire `run_once()` in a background thread. If a scan is already
        in flight, this is a no-op — the ongoing run re-queries between
        batches and will pick up anything new."""
        if self._thread and self._thread.is_alive():
            log.debug("LoudnessScanner: trigger ignored — already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._delayed_run, args=(delay,),
            daemon=True, name="loudness-scan")
        self._thread.start()

    def _delayed_run(self, delay: float):
        if delay > 0 and self._stop.wait(delay):
            return
        try:
            self.run_once()
        except Exception as e:
            log.exception(f"LoudnessScanner: run_once error: {e}")

    def start_initial_scan(self, delay: float = 120.0):
        """Mop-up scan some time after boot — picks up tracks left bare
        by a previous interrupted run. Indexer-tail triggers handle
        steady-state additions."""
        log.info(f"LoudnessScanner: initial scan scheduled in {int(delay)}s")
        self.trigger(delay=delay)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    # ── Internal helpers ──────────────────────────────────────────

    def _analyze(self, audio_src: str) -> Tuple[Optional[float], Optional[float]]:
        """Shell out to ffmpeg with the source (HTTP URL or local path),
        return `(lufs, true_peak_dbtp)`. Either may be None if the
        corresponding ffmpeg summary block didn't parse (broken file,
        garbled output) — peak_db is the one that drives the gain.

        **Raises FileNotFoundError** if the ffmpeg binary itself can't
        be invoked. That's an environmental problem (e.g. Homebrew was
        updating /opt/homebrew/bin/ffmpeg mid-scan, leaving the symlink
        target briefly missing); the scan should bail without caching
        the track as a sticky negative — the next trigger will re-try."""
        if not audio_src or not _FFMPEG_PATH:
            return None, None
        try:
            proc = subprocess.run(
                [_FFMPEG_PATH, "-nostats", "-hide_banner", "-i", audio_src,
                 "-af", "ebur128=framelog=quiet:peak=true", "-f", "null", "-"],
                capture_output=True, text=True,
                # ffmpeg embeds the source file's metadata (artist / title)
                # verbatim in its stderr banner. Track tags can be Latin-1
                # / cp1252 / mojibake — strict UTF-8 decoding crashes the
                # scanner on the first such track and re-crashes on
                # restart since the same track surfaces again. errors=
                # "replace" substitutes U+FFFD for bad bytes; the
                # ebur128 summary block at the tail is pure ASCII so the
                # LUFS / peak regexes are unaffected.
                errors="replace",
                timeout=_FFMPEG_TIMEOUT_SEC,
            )
        except FileNotFoundError:
            # Propagate up so run_once() bails. Don't poison the cache.
            raise
        except subprocess.TimeoutExpired as e:
            log.warning(f"LoudnessScanner: ffmpeg timed out for {audio_src[:80]}: {e}")
            return None, None
        # ebur128 writes the summary to stderr regardless of exit code.
        stderr = proc.stderr or ""
        return _parse_ebur128(stderr), _parse_true_peak(stderr)

    def _compute_gain(self, peak_db: float) -> float:
        """gain = TARGET_PEAK_DBTP - measured peak, clamped ±_MAX_ABS_GAIN_DB."""
        gain = TARGET_PEAK_DBTP - peak_db
        if gain >  _MAX_ABS_GAIN_DB: gain =  _MAX_ABS_GAIN_DB
        if gain < -_MAX_ABS_GAIN_DB: gain = -_MAX_ABS_GAIN_DB
        return gain

    def _persist(self, url: str, lufs: Optional[float],
                 peak_db: Optional[float]):
        """Store the row. peak_db=None → sticky negative cache (gain_db=0).
        lufs is informational only and may be None even on a successful
        peak read (we don't fail the scan if only LUFS failed to parse)."""
        gain = self._compute_gain(peak_db) if peak_db is not None else 0.0
        with self._db._pool.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO track_loudness "
                "(url, lufs, peak_db, gain_db, scanned_at) VALUES (?,?,?,?,?)",
                (url, lufs, peak_db, gain, int(time.time())))


if __name__ == "__main__":
    # Manual smoke: scan whatever's in the live library.
    logging.basicConfig(level=logging.INFO)
    from dlna_library import DB
    s = LoudnessScanner(DB)
    s.run_once()
