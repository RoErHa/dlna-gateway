#!/usr/bin/env python3
"""
dlna_loudness.py — per-track integrated-loudness scanner.

Walks tracks with a known local file_path and computes the integrated
loudness via `ffmpeg -af ebur128`. Stores the measured LUFS value plus a
per-track `gain_db = TARGET_LUFS - measured` (clamped ±20 dB) into the
`track_loudness` cache.

The cache is independent of `tracks` (keyed by URL, no FK), so it
survives `clear(udn)` — same persistence pattern as `album_art` and
`play_counts`. Failed scans get a sticky negative-cache row
(`lufs IS NULL`) so we don't re-attempt every restart.

The `LOUDNESS_SCANNER` singleton is created in `dlna_library` (the
composition root) and re-exported from there for backward compat.

Lifecycle hooks (mirror `AlbumArtFetcher`):
  - `start_initial_scan(delay=120)` — one-shot startup mop-up.
  - `trigger()` from `Indexer._run()` tail when new tracks are indexed.
"""
import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional

log = logging.getLogger("dlna.library")


# Reference loudness target. -18 LUFS = audiophile / max-headroom: quiet
# classical stays present, loud rock gets attenuated rather than chasing
# the user's amp into clipping.
TARGET_LUFS: float = -18.0

# Bound the per-track gain. Tracks measured at -70 LUFS (effectively
# silence) would otherwise produce +52 dB and blow the renderer.
_MAX_ABS_GAIN_DB: float = 20.0

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
    r"Integrated loudness:\s*\n\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS",
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


class LoudnessScanner:
    """Background worker that analyses tracks with `ffmpeg -af ebur128`
    and stores the per-track gain in `track_loudness`. Mirrors
    `AlbumArtFetcher` (`dlna_art_fetcher.py:98-212`)."""

    def __init__(self, db):
        self._db     = db
        self._stop   = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── Public API ─────────────────────────────────────────────────

    def bare_tracks(self) -> list:
        """Tracks with a known local file_path that haven't been analysed
        yet. The negative-cache rows (`lufs IS NULL`) count as "scanned"
        — they're already present in `track_loudness` so they don't
        appear here."""
        with self._db._pool.read() as conn:
            rows = conn.execute("""
                SELECT t.url, t.file_path
                  FROM tracks t
                 WHERE t.file_path != ''
                   AND NOT EXISTS (
                       SELECT 1 FROM track_loudness l WHERE l.url = t.url)
                 GROUP BY t.url
                 ORDER BY t.id
            """).fetchall()
        return [(r["url"], r["file_path"]) for r in rows]

    def run_once(self) -> dict:
        """Process bare tracks until none remain. Re-queries between
        batches so triggers arriving mid-run are absorbed."""
        stats = {"total": 0, "ok": 0, "failed": 0}
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
                     f"(target={TARGET_LUFS} LUFS)")
            for url, file_path in tracks[:_BATCH_SIZE]:
                if self._stop.is_set():
                    log.info("LoudnessScanner: stop requested — exiting early")
                    break
                lufs = self._analyze(file_path)
                self._persist(url, lufs)
                if lufs is None:
                    stats["failed"] += 1
                    log.info(f"LoudnessScanner ✗ {file_path} — cached "
                             f"as negative (won't retry)")
                else:
                    stats["ok"] += 1
                    gain = self._compute_gain(lufs)
                    log.debug(f"LoudnessScanner ✓ {file_path} → "
                              f"{lufs:+.1f} LUFS, gain {gain:+.1f} dB")
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

    def _analyze(self, file_path: str) -> Optional[float]:
        """Shell out to ffmpeg, return the parsed integrated LUFS or None."""
        if not file_path:
            return None
        try:
            proc = subprocess.run(
                ["ffmpeg", "-nostats", "-hide_banner", "-i", file_path,
                 "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
                capture_output=True, text=True,
                timeout=_FFMPEG_TIMEOUT_SEC,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.warning(f"LoudnessScanner: ffmpeg failed for {file_path}: {e}")
            return None
        # ebur128 writes the summary to stderr regardless of exit code.
        return _parse_ebur128(proc.stderr or "")

    def _compute_gain(self, lufs: float) -> float:
        """gain = TARGET - measured, clamped ±_MAX_ABS_GAIN_DB."""
        gain = TARGET_LUFS - lufs
        if gain >  _MAX_ABS_GAIN_DB: gain =  _MAX_ABS_GAIN_DB
        if gain < -_MAX_ABS_GAIN_DB: gain = -_MAX_ABS_GAIN_DB
        return gain

    def _persist(self, url: str, lufs: Optional[float]):
        """Store the row. lufs=None → sticky negative cache (gain_db=0)."""
        gain = self._compute_gain(lufs) if lufs is not None else 0.0
        with self._db._pool.write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO track_loudness "
                "(url, lufs, gain_db, scanned_at) VALUES (?,?,?,?)",
                (url, lufs, gain, int(time.time())))


if __name__ == "__main__":
    # Manual smoke: scan whatever's in the live library.
    logging.basicConfig(level=logging.INFO)
    from dlna_library import DB
    s = LoudnessScanner(DB)
    s.run_once()
