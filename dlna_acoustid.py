#!/usr/bin/env python3
"""
dlna_acoustid.py — AcoustID/Chromaprint metadata enrichment background worker.

Walks tracks lacking any `metadata_overrides` row, fingerprints them via
the Chromaprint `fpcalc` CLI, resolves the fingerprint to MusicBrainz
metadata through the AcoustID API, and writes the result back into
`metadata_overrides` (source='acoustid' on a confident match,
source='notfound' as a sticky negative otherwise).

Why a background worker and not a playback-tied flow: the gateway is
not in the audio path for UPnP renderers (it just sends SetURI+Play;
AssetUPnP streams bytes straight to the Naim). So "recognise while
playing" buys nothing — we'd have to fetch the audio bytes ourselves
anyway. Decoupling recognition from playback also lets the user run
a batch over the entire library at once via the `trigger()` hook.

Why peak/confidence over LUFS-style averaging: AcoustID returns one or
more candidate matches each with a 0-1 score. False matches (covers,
live versions, remasters) are the single biggest correctness risk in
this feature, so we hard-threshold at ACOUSTID_CONFIDENCE_THRESHOLD
(0.85) — sub-threshold results are treated as 'notfound', not written.

Same persistence pattern as `album_art` / `lyrics`: writes are keyed
by track URL, no FK, untouched by `clear(udn)`. Sticky negative rows
prevent re-hitting AcoustID for known misses on every restart; to
retry one track after fixing source-side metadata:
    DELETE FROM metadata_overrides WHERE source='notfound' AND url='…';

The `ACOUSTID_FETCHER` singleton is created in `dlna_library` (the
composition root) and reads `ACOUSTID_API_KEY` from the environment.
If the key is unset the worker is dormant — every `run_once()` is a
no-op with a one-time WARN. Get a free key at acoustid.org and put
it in `.env`.

Lifecycle hooks (mirror `AlbumArtFetcher` / `LoudnessScanner`):
  - `start_initial_scan(delay=120)` — one-shot startup mop-up.
  - `trigger()` from `Indexer._run()` tail when new tracks are indexed.
"""
import http.client
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
from typing import Optional, Tuple

log = logging.getLogger("dlna.library")


# launchd-spawned processes have a minimal PATH that excludes Homebrew.
# Mirror `dlna_loudness._find_ffmpeg`'s fallback so the gateway works
# under launchctl without editing the LaunchAgent .plist.
def _find_fpcalc() -> Optional[str]:
    found = shutil.which("fpcalc")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/fpcalc",   # Apple-Silicon Homebrew
                 "/usr/local/bin/fpcalc",       # Intel-Mac Homebrew
                 "/usr/bin/fpcalc"):            # system / Linux
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


_FPCALC_PATH: Optional[str] = _find_fpcalc()


# Contact email for the User-Agent. AcoustID's ToS, like MusicBrainz,
# expects identifying contact info; reuse the same env var.
_CONTACT_EMAIL = os.environ.get(
    "GATEWAY_CONTACT_EMAIL", "you@example.com").strip() or "you@example.com"
_AC_USER_AGENT = f"DLNAGateway/1.0 ( {_CONTACT_EMAIL} )"

# AcoustID asks for ~3 req/sec max; 0.34s between calls is a small
# safety margin. Distinct from the MusicBrainz 1.1s limit (the AcoustID
# servers are independent of MB).
_AC_RATE_LIMIT_SEC: float = 0.34

# Per-connection timeout. AcoustID's hot path is fast (~100ms), but
# allow generous headroom for slow hops.
_AC_TIMEOUT: float = 10.0

# How long to give fpcalc per track. ~1s on a healthy LAN file; the
# cap is a circuit-breaker against a slow/dead AssetUPnP source.
_FPCALC_TIMEOUT_SEC: float = 30.0

# fpcalc -length flag — analyse only the first N seconds of audio.
# 30s is the AcoustID-recommended default; shorter saves bandwidth
# on slow networks at a small accuracy cost.
_FPCALC_ANALYSE_SEC: int = 30

# Batch size for the "drain → re-query" loop. Re-querying lets a
# trigger arriving mid-run get absorbed into the current pass
# rather than racing as a second worker thread.
_BATCH_SIZE: int = 50

# Confidence floor. AcoustID returns matches with score 0-1. Below
# this we treat the result as 'notfound' — covers, live versions, and
# remasters routinely score in 0.4–0.7 territory, and a wrong match
# is more damaging than no match. Tunable by ear once running.
ACOUSTID_CONFIDENCE_THRESHOLD: float = 0.85


# ── Pure parsing helpers (no I/O, easy to unit-test) ──────────────

def _parse_fpcalc_output(stdout: str) -> Tuple[Optional[str], Optional[int]]:
    """Parse `fpcalc -json -` output.

    fpcalc emits JSON like:
        {"duration": 240.0, "fingerprint": "AQADtE..."}

    Returns (fingerprint, duration_seconds) or (None, None) on any
    parse failure. Duration is rounded to int because that's what
    AcoustID's lookup endpoint expects."""
    if not stdout:
        return None, None
    try:
        data = json.loads(stdout)
    except (ValueError, TypeError):
        return None, None
    fp = data.get("fingerprint")
    dur = data.get("duration")
    if not isinstance(fp, str) or not fp:
        return None, None
    try:
        dur_i = int(round(float(dur)))
    except (TypeError, ValueError):
        return None, None
    if dur_i <= 0:
        return None, None
    return fp, dur_i


def _reconstruct_artist(artists: list) -> str:
    """Build the display artist string from AcoustID's `artists` array.
    Each entry has a `name` and optionally a `joinphrase` linking to
    the next artist (MB convention: ' & ', ' feat. ', ', ', etc.).
    Falls back to ' & ' between entries if joinphrase is missing."""
    if not artists:
        return ""
    parts = []
    for i, a in enumerate(artists):
        name = (a.get("name") or "").strip()
        if not name:
            continue
        parts.append(name)
        # The joinphrase is between this entry and the next, so we
        # apply it only if there's a next entry coming.
        if i < len(artists) - 1:
            jp = a.get("joinphrase")
            parts.append(jp if jp else " & ")
    return "".join(parts).strip()


def _extract_best_match(response: dict,
                        threshold: float = ACOUSTID_CONFIDENCE_THRESHOLD
                        ) -> Optional[dict]:
    """Pull the best (artist, album, title, score) match out of an
    AcoustID lookup response. Returns None if no result meets the
    confidence threshold or the response is shaped wrong.

    Selection logic:
      1. Top-level results sorted by score, take the highest above
         threshold.
      2. Within that result's `recordings`, prefer one whose
         release-group `type` is 'Album' over Single/Compilation.
      3. If multiple equally-good recordings, take the first.

    The returned dict always has keys: artist, album, title, score.
    Any of artist/album/title may be empty strings if AcoustID didn't
    return them (a partial match is still useful — title alone fixes
    "Track 03" entries)."""
    if not isinstance(response, dict):
        return None
    if response.get("status") != "ok":
        return None
    results = response.get("results") or []
    if not results:
        return None
    # Highest-scoring match first
    try:
        results = sorted(results,
                         key=lambda r: float(r.get("score", 0) or 0),
                         reverse=True)
    except (TypeError, ValueError):
        return None
    top = results[0]
    try:
        score = float(top.get("score", 0) or 0)
    except (TypeError, ValueError):
        return None
    if score < threshold:
        return None
    recordings = top.get("recordings") or []
    if not recordings:
        # We have a fingerprint match but no MB metadata attached —
        # treat as a miss; the user gets no useful enrichment.
        return None

    def _rank(rec):
        # Lower is better. Prefer recordings whose first releasegroup
        # is an Album; prefer ones with both artists and a title.
        rg_type = ""
        rgs = rec.get("releasegroups") or []
        if rgs:
            rg_type = (rgs[0].get("type") or "").lower()
        is_album = 0 if rg_type == "album" else 1
        has_artist = 0 if (rec.get("artists") or []) else 1
        has_title = 0 if rec.get("title") else 1
        return (is_album, has_artist, has_title)

    rec = sorted(recordings, key=_rank)[0]
    title = (rec.get("title") or "").strip()
    artist = _reconstruct_artist(rec.get("artists") or [])
    album = ""
    rgs = rec.get("releasegroups") or []
    if rgs:
        album = (rgs[0].get("title") or "").strip()
    if not any((title, artist, album)):
        return None
    return {"artist": artist, "album": album, "title": title,
            "score": score}


# ── Worker class ──────────────────────────────────────────────────

class AcoustIDFetcher:
    """Background worker that fingerprints tracks and enriches their
    metadata via AcoustID + MusicBrainz. Mirrors
    `LoudnessScanner` (dlna_loudness.py) line-for-line in lifecycle
    surface: trigger / stop / start_initial_scan / status.

    `api_key` may be None or empty — every run is then a no-op with a
    single WARN so an unconfigured deployment doesn't spew log lines
    on every Indexer-tail trigger."""

    def __init__(self, db, api_key: Optional[str] = None):
        self._db      = db
        self._api_key = (api_key or "").strip() or None
        self._stop    = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Status snapshot for /api/metadata/status.
        self._lock          = threading.Lock()
        self._in_progress   = False
        self._processed     = 0
        self._last_match    = ""   # "artist — title" of last positive
        self._last_url      = ""

    # ── Public surface ────────────────────────────────────────────

    def bare_tracks(self) -> list:
        """Delegates to LibraryDB so the SQL stays in one place."""
        return self._db.bare_metadata_tracks()

    def status(self) -> dict:
        with self._lock:
            return {
                "enabled":     self._api_key is not None,
                "fpcalc":      _FPCALC_PATH is not None,
                "in_progress": self._in_progress,
                "processed":   self._processed,
                "threshold":   ACOUSTID_CONFIDENCE_THRESHOLD,
                "last_match":  self._last_match,
                "last_url":    self._last_url,
            }

    def run_once(self) -> dict:
        """Process bare tracks until none remain. Re-queries between
        batches so triggers arriving mid-run are absorbed."""
        stats = {"total": 0, "hits": 0, "notfound": 0, "errors": 0}
        if self._api_key is None:
            log.warning("AcoustIDFetcher: ACOUSTID_API_KEY not set in env — "
                        "skipping. Get a free key at acoustid.org and add "
                        "it to .env.")
            return stats
        # Same defensive bail as LoudnessScanner: if fpcalc is missing,
        # don't iterate — otherwise every track would either error or
        # get sticky-cached as notfound and never recover when fpcalc
        # is later installed.
        if not _FPCALC_PATH:
            log.warning("AcoustIDFetcher: fpcalc not found in PATH or "
                        "common install locations — skipping. Install "
                        "Chromaprint (e.g. `brew install chromaprint`) "
                        "and restart the gateway.")
            return stats
        try:
            os.nice(10)
        except (AttributeError, PermissionError):
            pass
        with self._lock:
            self._in_progress = True
            self._processed   = 0
        try:
            while not self._stop.is_set():
                tracks = self.bare_tracks()
                if not tracks:
                    break
                n = len(tracks)
                stats["total"] += n
                eta_s = int(n * _AC_RATE_LIMIT_SEC) + n  # +1s/track for fpcalc
                log.info(f"AcoustIDFetcher: enriching {n} bare track(s) "
                         f"(~{eta_s}s eta)")
                for (url,) in tracks[:_BATCH_SIZE]:
                    if self._stop.is_set():
                        log.info("AcoustIDFetcher: stop requested — exiting early")
                        break
                    try:
                        fp, dur = self._fingerprint(url)
                    except FileNotFoundError as e:
                        # fpcalc binary disappeared mid-run (Homebrew updating
                        # /opt/homebrew/bin/fpcalc leaves the symlink target
                        # briefly missing). Bail without caching anything —
                        # next trigger will re-resolve the path.
                        log.warning(f"AcoustIDFetcher: fpcalc binary missing "
                                    f"({e}) — aborting scan without caching. "
                                    f"Will retry on next trigger / restart.")
                        return stats
                    if fp is None:
                        # Fingerprint failed (corrupt source, network 404,
                        # unsupported codec). Treat as notfound — same
                        # convention as AlbumArtFetcher caches MB misses.
                        self._db.metadata_override_mark_notfound(url)
                        stats["errors"] += 1
                        log.info(f"AcoustIDFetcher ✗ {url[:80]} — "
                                 f"fingerprint failed, cached as notfound")
                    else:
                        match = self._lookup(fp, dur)
                        if match:
                            self._db.metadata_override_set(
                                url, source="acoustid",
                                artist=match.get("artist") or None,
                                album=match.get("album") or None,
                                title=match.get("title") or None)
                            stats["hits"] += 1
                            label = (f"{match.get('artist','?')} — "
                                     f"{match.get('title','?')}").strip()
                            log.info(f"AcoustIDFetcher ✓ {url[:80]} → "
                                     f"{label!r} (score={match['score']:.2f})")
                            with self._lock:
                                self._last_match = label
                                self._last_url   = url
                        else:
                            self._db.metadata_override_mark_notfound(url)
                            stats["notfound"] += 1
                            log.info(f"AcoustIDFetcher ✗ {url[:80]} — "
                                     f"no confident match, cached as notfound")
                    with self._lock:
                        self._processed += 1
                    if self._stop.wait(_AC_RATE_LIMIT_SEC):
                        break
        finally:
            with self._lock:
                self._in_progress = False
        if stats["total"] == 0:
            log.debug("AcoustIDFetcher: no bare tracks to enrich")
        else:
            log.info(f"AcoustIDFetcher: done — hits={stats['hits']}, "
                     f"notfound={stats['notfound']}, errors={stats['errors']}")
        return stats

    def trigger(self, delay: float = 0.0):
        """Fire `run_once()` in a background thread. Idempotent — if a
        scan is already in flight, this is a no-op; the ongoing run
        re-queries between batches and picks up anything new."""
        if self._thread and self._thread.is_alive():
            log.debug("AcoustIDFetcher: trigger ignored — already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._delayed_run, args=(delay,),
            daemon=True, name="acoustid-scan")
        self._thread.start()

    def _delayed_run(self, delay: float):
        if delay > 0 and self._stop.wait(delay):
            return
        try:
            self.run_once()
        except Exception as e:
            log.exception(f"AcoustIDFetcher: run_once error: {e}")

    def start_initial_scan(self, delay: float = 120.0):
        """Mop-up scan after boot — picks up tracks left bare by a
        previous interrupted run. Indexer-tail triggers handle
        steady-state additions."""
        if self._api_key is None:
            log.info("AcoustIDFetcher: ACOUSTID_API_KEY not set — "
                     "initial scan disabled")
            return
        log.info(f"AcoustIDFetcher: initial scan scheduled in {int(delay)}s")
        self.trigger(delay=delay)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    # ── Internal helpers ──────────────────────────────────────────

    def _fingerprint(self, url: str) -> Tuple[Optional[str], Optional[int]]:
        """Shell out to fpcalc with an HTTP URL (or local path) and
        return `(fingerprint, duration_seconds)` or `(None, None)` on
        any failure. fpcalc uses ffmpeg under the hood for decoding,
        so it accepts every codec ffmpeg does — FLAC/MP3/AAC/OGG/
        DSF/DFF/WAV.

        **Raises FileNotFoundError** if the fpcalc binary itself can't
        be invoked. Caller bails without caching to avoid poisoning."""
        if not url or not _FPCALC_PATH:
            return None, None
        try:
            proc = subprocess.run(
                [_FPCALC_PATH, "-length", str(_FPCALC_ANALYSE_SEC),
                 "-json", url],
                capture_output=True, text=True,
                # Mirror dlna_loudness's lenient decoding: fpcalc echoes
                # ffmpeg banner lines that can contain Latin-1 metadata.
                errors="replace",
                timeout=_FPCALC_TIMEOUT_SEC,
            )
        except FileNotFoundError:
            raise
        except subprocess.TimeoutExpired as e:
            log.warning(f"AcoustIDFetcher: fpcalc timed out for "
                        f"{url[:80]}: {e}")
            return None, None
        except Exception as e:
            log.warning(f"AcoustIDFetcher: fpcalc error for {url[:80]}: {e}")
            return None, None
        if proc.returncode != 0:
            # Common: HTTP 404 from AssetUPnP (SAMDATA locked), corrupt
            # source file. fpcalc's stderr has the reason.
            err = (proc.stderr or "").strip().splitlines()[-1:] or [""]
            log.debug(f"AcoustIDFetcher: fpcalc rc={proc.returncode} for "
                      f"{url[:80]} — {err[0][:120]}")
            return None, None
        return _parse_fpcalc_output(proc.stdout or "")

    def _lookup(self, fingerprint: str, duration: int) -> Optional[dict]:
        """POST the fingerprint to AcoustID and return the best match
        above ACOUSTID_CONFIDENCE_THRESHOLD, or None.

        Uses POST (not GET) because fingerprints can run 1KB+ — well
        within typical GET-URL limits but POST is the AcoustID-
        recommended pattern."""
        if not self._api_key:
            return None
        body = urllib.parse.urlencode({
            "client":      self._api_key,
            # Space-separated list of fields. urlencode converts the
            # space to "+" which is what AcoustID expects; a literal
            # "+" here would become "%2B" and trigger HTTP 400.
            "meta":        "recordings releasegroups",
            "duration":    str(duration),
            "fingerprint": fingerprint,
        })
        try:
            conn = http.client.HTTPSConnection(
                "api.acoustid.org", timeout=_AC_TIMEOUT)
            try:
                conn.request("POST", "/v2/lookup", body=body,
                             headers={
                                 "User-Agent":   _AC_USER_AGENT,
                                 "Content-Type": "application/x-www-form-urlencoded",
                             })
                resp = conn.getresponse()
                raw  = resp.read()
                if resp.status != 200:
                    log.warning(f"AcoustIDFetcher: HTTP {resp.status} from "
                                f"AcoustID for fp[:16]={fingerprint[:16]}")
                    return None
                data = json.loads(raw)
            finally:
                conn.close()
        except Exception as e:
            log.warning(f"AcoustIDFetcher: lookup error: {e}")
            return None
        return _extract_best_match(data, ACOUSTID_CONFIDENCE_THRESHOLD)


if __name__ == "__main__":
    # Manual smoke: walk whatever's in the live library.
    logging.basicConfig(level=logging.INFO)
    from dlna_library import DB
    key = os.environ.get("ACOUSTID_API_KEY")
    f = AcoustIDFetcher(DB, api_key=key)
    f.run_once()
