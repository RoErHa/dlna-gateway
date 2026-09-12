"""
dlna_localfs_wiring.py — boot-time wiring of the LocalFs provider.

Phase 4 of the AssetUPnP migration. Kept in its own module to keep
`dlna_gateway.py` slim (the run_all.py lint enforces < 350 lines).

`maybe_start_localfs()` wires up to THREE independent libraries — music
(`LOCALFS_MUSIC_ROOT` / `localfs.root`), video (`LOCALFS_VIDEO_ROOT` /
`localfs.video_root`) and audiobooks (`AUDIOBOOKS_ROOT` /
`localfs.audiobooks_root`). Each is REGISTERED with
`dlna_localfs_watch.WATCH`, which brings it up the moment its volume is
present — at boot if it already is, otherwise on a later re-check, so a
drive that mounts after launchd starts the gateway costs no restart.
Whenever a root activates, it:

  1. Ensures the LocalFs HTTP file server is running on its own port
     (default 8200 / honors `$LOCALFS_PORT`). Binds `0.0.0.0` so the
     Naim can reach it on the LAN. ONE server serves all three roots:
     the first root to activate starts it, and every later one widens
     its `allowed_roots` via `add_allowed_root`.
  2. Computes the file server's base_url (`$LOCALFS_BASE_URL`
     overrides; otherwise auto-detects the LAN IP via the same
     `get_lan_ip` helper the gateway uses for SSDP).
  3. Constructs a `LocalFsProvider` against `library.db` with that
     base_url. Track URLs written into `tracks.url` at scan time are
     then Naim-fetchable directly — no translation layer needed
     downstream.
  4. Binds the provider to its synthesised UDN via
     `dlna_providers.bind_provider`, and inserts a matching
     `MediaServer` record into `SERVERS` so api_browse exposes it
     alongside any AssetUPnP / MinimServer entries.
  5. Kicks off a background initial scan. Subsequent runs are
     incremental thanks to the `localfs_files` (mtime, size) cache
     introduced in P2.

The fallback when no root is configured is the existing UPnP-only
behaviour — no functional change for users who haven't opted in.
"""
from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("dlna.localfs.wiring")

# Synthetic udn for the video library — kept distinct from the audio LocalFs
# source so videos never mix into the music browse / the Naim's UPnP tree.
VIDEO_UDN = "uuid:localfs-movies"

# UDN of the audiobooks LocalFs source, set when maybe_start_localfs()
# starts one ('' when the feature is off). api_browse.servers_payload
# reads this to tag the /api/servers entry `kind: "audiobooks"` so the
# PWA knows which source gets resume-position behaviour.
AUDIOBOOKS_UDN = ""


def music_root() -> str:
    """Configured MUSIC root: env `LOCALFS_MUSIC_ROOT`, else
    `localfs.root` in config.json. Returns '' when unset = music
    disabled. Independent of the video and audiobook roots — see
    `dlna_localfs_watch` for why that independence is load-bearing."""
    root = os.environ.get("LOCALFS_MUSIC_ROOT", "").strip()
    if not root:
        from dlna_config import load_config
        root = ((load_config().get("localfs") or {})
                .get("root", "") or "").strip()
    return root


def audiobooks_root() -> str:
    """Configured AUDIOBOOKS root: env `AUDIOBOOKS_ROOT`, else
    `localfs.audiobooks_root` in config.json. Returns '' when unset =
    audiobooks disabled. A separate root + UDN keeps books out of the
    music letter bar, 📻 radio shuffle, and music search (all per-UDN)."""
    root = os.environ.get("AUDIOBOOKS_ROOT", "").strip()
    if not root:
        from dlna_config import load_config
        root = ((load_config().get("localfs") or {})
                .get("audiobooks_root", "") or "").strip()
    return root


def video_root() -> str:
    """Configured VIDEO root for the video feature (Phase V1+): env
    `LOCALFS_VIDEO_ROOT`, else `localfs.video_root` in config.json. Returns ''
    when unset = video disabled. Separate from the music root
    (`LOCALFS_MUSIC_ROOT` / `localfs.root`); the two are fully independent."""
    root = os.environ.get("LOCALFS_VIDEO_ROOT", "").strip()
    if not root:
        from dlna_config import load_config
        root = ((load_config().get("localfs") or {})
                .get("video_root", "") or "").strip()
    return root


class _LocalFsWiring:
    """Brings ONE library up, whenever its volume turns out to be there.

    A class rather than three closures because all three activations
    share mutable state — the single file server — and "has it been
    started yet?" is the question that has to be answered identically
    from a boot thread and from the watch thread half an hour later.
    """

    def __init__(self, get_lan_ip):
        self.port = int(os.environ.get("LOCALFS_PORT", "8200"))
        self.base_url = os.environ.get(
            "LOCALFS_BASE_URL",
            f"http://{get_lan_ip()}:{self.port}").rstrip("/")
        self.server = None
        self._lock = threading.Lock()

    def _ensure_server(self, path):
        """Start the file server on the first root to arrive; widen it
        for every root after that. Returns False if it cannot serve —
        the caller must then not register a provider, because a
        `MediaServer` with no bytes behind it is what earns dead URLs
        the SSRF guard's known-device allowance."""
        from dlna_localfs_server import add_allowed_root, start_server
        root = str(path.resolve())
        with self._lock:
            if self.server is not None:
                add_allowed_root(self.server, root)
                return True
            # $LOCALFS_BIND narrows the listener to one address (audit
            # 2026-08-20). ONE only — this is a single
            # ThreadingHTTPServer socket, unlike hypercorn's multi-bind.
            # The LAN address is the right choice: the Naim and the TV
            # fetch bytes from here directly, and tailnet clients reach
            # audio through the gateway's own relay rather than this
            # port. Default stays 0.0.0.0 so a fresh clone works
            # unconfigured.
            bind = (os.environ.get("LOCALFS_BIND", "") or "0.0.0.0").strip()
            try:
                from dlna_config import DB_FILE
                self.server = start_server(DB_FILE, port=self.port,
                                           host=bind,
                                           allowed_roots=(root,))
            except OSError as e:
                log.error(f"LocalFs file server failed to bind "
                          f"{bind}:{self.port}: {e} — is the port in use, or "
                          "has the machine's address changed? Set "
                          "$LOCALFS_BIND / $LOCALFS_PORT.")
                return False
        log.info(f"LocalFs file server: port={self.port} "
                 f"base_url={self.base_url}")
        return True

    def _add_provider(self, path, *, name, namespace="",
                      collect_unknown_artists=True):
        """Construct + bind a LocalFsProvider, publish it as a
        `MediaServer`, and scan it in the background."""
        from dlna_library import DB
        from dlna_providers import bind_provider
        from dlna_providers.localfs import LocalFsProvider
        from dlna_registry import MediaServer
        import dlna_discovery as _disc

        prov = LocalFsProvider(DB, path, base_url=self.base_url,
                               id_namespace=namespace,
                               collect_unknown_artists=collect_unknown_artists)
        bind_provider(prov.udn, prov)
        _disc.SERVERS.add(MediaServer(
            udn=prov.udn, name=name, location=self.base_url,
            control_url=self.base_url, base_url=self.base_url))

        # Background initial scan — same lazy posture as the other
        # boot-time mop-ups, so the gateway never blocks on a big tree.
        # Guarded on its own: one unreadable library must not cost the
        # other its index.
        def _scan():
            try:
                log.info(f"{name} initial scan complete: {prov.rescan()}")
            except Exception as e:                            # noqa: BLE001
                log.exception(f"{name} initial scan failed: {e}")
        threading.Thread(target=_scan, daemon=True,
                         name="localfs-initial-scan").start()
        return prov

    # ── one activation per library ──────────────────────────────────
    def activate_music(self, path):
        if not self._ensure_server(path):
            return
        log.info(f"Music enabled: root={path}")
        self._add_provider(path, name="RoHaLocalFS")

    def activate_audiobooks(self, path):
        if not self._ensure_server(path):
            return
        log.info(f"Audiobooks enabled: root={path}")
        # id_namespace salts the track ids so a rel_path shared with the
        # music root can't collide on obj_id (the file server resolves
        # across all localfs UDNs).
        prov = self._add_provider(path, name="RoHaAudioBooks",
                                  namespace="audiobooks",
                                  collect_unknown_artists=False)
        global AUDIOBOOKS_UDN
        AUDIOBOOKS_UDN = prov.udn
        log.info(f"Audiobooks provider bound: udn={prov.udn}")

    def activate_video(self, path):
        """Video has no provider — its bytes come off the same :8200 and
        its index is the periodic GWMovies scan. PERIODIC + incremental
        so new clips appear without a restart: each pass skips unchanged
        files (mtime,size) and prunes removed ones, so a steady library
        is near-free and new clips are geocoded once (cached). Only logs
        at INFO when something changed."""
        if not self._ensure_server(path):
            return
        log.info(f"Video enabled: root={path} udn={VIDEO_UDN}")
        interval = max(30, int(os.environ.get("VIDEO_SCAN_INTERVAL_SEC",
                                              "300")))

        def _video_scan():
            import dlna_video_index
            from dlna_library import DB
            first = True
            while True:
                try:
                    stats = dlna_video_index.scan_videos(
                        str(path), VIDEO_UDN, DB, self.base_url)
                    if first or stats.get("added") or stats.get("pruned"):
                        log.info(f"Video scan: {stats}")
                    first = False
                except Exception as e:                        # noqa: BLE001
                    log.exception(f"Video scan failed: {e}")
                time.sleep(interval)

        threading.Thread(target=_video_scan, daemon=True,
                         name="video-scan").start()


def maybe_start_localfs(get_lan_ip):
    """Caller passes the gateway's own `get_lan_ip` function so this
    module doesn't need to re-implement LAN-IP detection.

    The three libraries are wired INDEPENDENTLY, and a root that is not
    present yet is WAITED FOR rather than written off: each one is
    registered with `dlna_localfs_watch.WATCH`, which activates it now
    if its volume is there and otherwise re-checks until it is. Nothing
    here is conditional on the music root any more — it has no special
    status, and a boot that races an unmounted drive costs a delay, not
    a restart."""
    mroot, vroot, abroot = music_root(), video_root(), audiobooks_root()
    if not (mroot or vroot or abroot):
        log.debug("LocalFs disabled (no music / video / audiobooks root set "
                  "in the environment or config.json)")
        return

    from dlna_localfs_watch import WATCH
    w = _LocalFsWiring(get_lan_ip)
    WATCH.register("Music", mroot, w.activate_music)
    WATCH.register("Audiobooks", abroot, w.activate_audiobooks)
    WATCH.register("Video", vroot, w.activate_video)
    WATCH.start()


__all__ = ["maybe_start_localfs", "music_root", "video_root", "VIDEO_UDN",
           "audiobooks_root", "AUDIOBOOKS_UDN"]
