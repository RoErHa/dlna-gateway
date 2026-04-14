#!/usr/bin/env python3
"""
dlna_cast.py — Chromecast discovery and queue playback.

Discovers Chromecast-capable devices on the LAN via pychromecast (mDNS/zeroconf)
and provides a queue player that casts audio URLs via the Default Media Receiver.

The gateway's /stream proxy provides the HTTP URL that Chromecast fetches directly.

Standalone test:
    python dlna_cast.py              # discover + list devices
    python dlna_cast.py <device>     # play a test tone on <device>
"""
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("dlna.cast")

# ── Lazy import — pychromecast is optional ────────────────────────
_pychromecast = None
_zeroconf_inst = None
_browser = None
_available = None  # None = not checked, True/False after check


def _ensure_pychromecast():
    """Import pychromecast on first use. Returns True if available."""
    global _pychromecast, _available
    if _available is not None:
        return _available
    try:
        import pychromecast
        _pychromecast = pychromecast
        _available = True
        log.info("pychromecast loaded — Chromecast support enabled")
    except ImportError:
        _available = False
        log.info("pychromecast not installed — Chromecast support disabled. "
                 "Install with: pip install pychromecast")
    return _available


# ── Device model ──────────────────────────────────────────────────

@dataclass
class CastDevice:
    """A discovered Chromecast-capable device."""
    uuid: str           # unique device id (string form of UUID)
    name: str           # friendly name ("4K TV Box", "Uniti Nova")
    model: str          # model name
    cast_type: str      # "cast", "group", "audio"
    host: str           # IP address
    port: int           # cast port
    _cc: object = field(default=None, repr=False)  # pychromecast Chromecast obj
    last_seen: float = field(default_factory=time.time)

    def to_dict(self):
        return {
            "uuid": self.uuid,
            "name": self.name,
            "model": self.model,
            "cast_type": self.cast_type,
            "host": self.host,
        }


# ── Registry ──────────────────────────────────────────────────────

class CastRegistry:
    def __init__(self):
        self._d: Dict[str, CastDevice] = {}
        self._lock = threading.Lock()

    def add(self, dev: CastDevice):
        with self._lock:
            if dev.uuid not in self._d:
                log.info(f"[CAST +] {dev.name!r}  ({dev.model})  @ {dev.host}")
            dev.last_seen = time.time()
            self._d[dev.uuid] = dev

    def remove(self, uuid: str):
        with self._lock:
            d = self._d.pop(uuid, None)
            if d:
                log.info(f"[CAST -] {d.name!r}")

    def get(self, uuid: str) -> Optional[CastDevice]:
        with self._lock:
            return self._d.get(uuid)

    def all(self) -> List[CastDevice]:
        with self._lock:
            return list(self._d.values())

    @property
    def available(self) -> bool:
        return _available is True


CAST_DEVICES = CastRegistry()


# ── Discovery ─────────────────────────────────────────────────────

def _on_cast_added(uuid, service_name):
    """Called by SimpleCastListener when a new device is found."""
    # Delay slightly — device may not be in browser.devices yet
    threading.Timer(0.5, _register_device, args=(uuid,)).start()


def _register_device(uuid):
    """Register a discovered device from the browser's device list."""
    try:
        if _browser is None:
            log.debug("_register_device: _browser is None")
            return
        info = _browser.devices.get(uuid)
        if info is None:
            log.debug(f"_register_device: uuid {uuid} not in browser.devices")
            return
        # browser.devices values are CastInfo objects (not Chromecast wrappers)
        # CastInfo has: friendly_name, host, port, model_name, cast_type, uuid, etc.
        dev = CastDevice(
            uuid=str(uuid),
            name=info.friendly_name or info.host or str(uuid),
            model=info.model_name or "Chromecast",
            cast_type=info.cast_type or "cast",
            host=info.host,
            port=info.port,
            _cc=None,  # we'll connect on demand when casting
        )
        CAST_DEVICES.add(dev)
    except Exception as e:
        log.warning(f"Error registering cast device {uuid}: {e}")


def _on_cast_removed(uuid, service_name, service):
    """Called by SimpleCastListener when a device disappears."""
    CAST_DEVICES.remove(str(uuid))


def _on_cast_updated(uuid, service_name):
    """Called by SimpleCastListener when a device updates."""
    _register_device(uuid)


def _sweep_devices():
    """Sweep all devices in browser.devices — catches anything
    the callbacks missed during the initial discovery burst."""
    try:
        if _browser is None:
            log.warning("Chromecast sweep: _browser is None")
            return
        browser_count = len(_browser.devices)
        log.info(f"Chromecast sweep: browser has {browser_count} device(s)")
        for uuid, info in list(_browser.devices.items()):
            name = getattr(info, 'friendly_name', None) or str(uuid)
            log.info(f"  Registering {name} ({uuid})")
            _register_device(uuid)
        reg_count = len(CAST_DEVICES.all())
        log.info(f"Chromecast sweep done: {reg_count} device(s) in registry")
    except Exception as e:
        log.warning(f"Chromecast sweep error: {e}")
        import traceback
        traceback.print_exc()


def start_discovery():
    """Start background Chromecast discovery. Call once at startup."""
    global _browser, _zeroconf_inst
    if not _ensure_pychromecast():
        return

    try:
        import zeroconf as _zc
        _zeroconf_inst = _zc.Zeroconf()

        listener = _pychromecast.SimpleCastListener(
            _on_cast_added,
            _on_cast_removed,
            _on_cast_updated,
        )
        _browser = _pychromecast.CastBrowser(listener, _zeroconf_inst)
        _browser.start_discovery()

        log.info("Chromecast discovery started (CastBrowser + mDNS)")

        # Delayed sweep to catch devices found during initial burst
        threading.Timer(10.0, _sweep_devices).start()
    except Exception as e:
        log.warning(f"Chromecast discovery failed: {e}")
        import traceback
        traceback.print_exc()


def stop_discovery():
    """Stop Chromecast discovery (call on shutdown)."""
    global _browser, _zeroconf_inst
    try:
        if _browser and _pychromecast:
            _pychromecast.discovery.stop_discovery(_browser)
    except Exception:
        pass
    try:
        if _zeroconf_inst:
            _zeroconf_inst.close()
    except Exception:
        pass


# ── Queue player ──────────────────────────────────────────────────

class CastQueue:
    """
    Manages a play queue on a Chromecast device.
    Similar to RENDERER_QUEUE but uses the Cast Default Media Receiver.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tracks: List[dict] = []
        self._idx: int = 0
        self._device: Optional[CastDevice] = None
        self._mc = None  # media controller
        self._stop_flag = False

    def start(self, uuid: str, tracks: List[dict], stream_base: str):
        """
        Start playing a queue on the given Chromecast.
        stream_base: e.g. "http://192.168.1.52:8765" — used to build stream URLs.
        """
        dev = CAST_DEVICES.get(uuid)
        if not dev:
            log.error(f"Cast device {uuid} not found")
            return

        # Connect to the device on demand
        cc = dev._cc
        if cc is None:
            log.info(f"Connecting to {dev.name} @ {dev.host}:{dev.port}…")
            try:
                chromecasts, _ = _pychromecast.get_listed_chromecasts(
                    friendly_names=[dev.name])
                if chromecasts:
                    cc = chromecasts[0]
                else:
                    # Fallback: connect by host
                    cc = _pychromecast.Chromecast(
                        dev.host, port=dev.port)
                dev._cc = cc
            except Exception as e:
                log.error(f"Failed to connect to {dev.name}: {e}")
                return

        with self._lock:
            self._tracks = tracks
            self._idx = 0
            self._device = dev
            self._stop_flag = False

        cc.wait()
        self._mc = cc.media_controller

        log.info(f"Cast queue: {len(tracks)} tracks → {dev.name}")
        self._play_current(stream_base)

        # Poll for track completion
        threading.Thread(
            target=self._poll_loop,
            args=(stream_base,),
            daemon=True,
            name=f"cast-poll-{uuid[:8]}"
        ).start()

    def _play_current(self, stream_base: str):
        with self._lock:
            if self._idx >= len(self._tracks) or self._stop_flag:
                return
            t = self._tracks[self._idx]

        url = t.get("url", "")
        if not url:
            log.warning("Cast: track has no URL, skipping")
            self._next(stream_base)
            return

        # Build the stream proxy URL so Chromecast fetches from our gateway
        from urllib.parse import quote
        stream_url = f"{stream_base}/stream?url={quote(url, safe='')}"

        title = t.get("title", "Unknown")
        artist = t.get("artist", "")
        album = t.get("album", "")
        art = t.get("art", "")

        log.info(f"Cast ▶ [{self._idx+1}/{len(self._tracks)}] {title}")

        try:
            metadata = {
                "metadataType": 3,  # MusicTrackMediaMetadata
                "title": title,
                "albumName": album,
                "artist": artist,
            }
            if art:
                metadata["images"] = [{"url": art}]

            self._mc.play_media(
                stream_url,
                "audio/flac",  # gateway stream proxy handles format
                title=title,
                metadata=metadata,
            )
            self._mc.block_until_active(timeout=10)
        except Exception as e:
            log.warning(f"Cast play failed: {e}")

    def _poll_loop(self, stream_base: str):
        """Poll media status; advance queue when track ends."""
        import time
        while not self._stop_flag:
            time.sleep(2)
            try:
                if not self._mc or self._stop_flag:
                    break
                status = self._mc.status
                if status and status.player_is_idle and status.idle_reason == "FINISHED":
                    self._next(stream_base)
            except Exception:
                pass

    def _next(self, stream_base: str):
        with self._lock:
            if self._idx < len(self._tracks) - 1:
                self._idx += 1
            else:
                log.info("Cast queue: finished")
                return
        self._play_current(stream_base)

    def pause(self):
        try:
            if self._mc:
                status = self._mc.status
                if status and status.player_is_paused:
                    self._mc.play()
                else:
                    self._mc.pause()
        except Exception as e:
            log.warning(f"Cast pause: {e}")

    def stop(self):
        self._stop_flag = True
        try:
            if self._mc:
                self._mc.stop()
            if self._device and self._device._cc:
                self._device._cc.quit_app()
        except Exception as e:
            log.warning(f"Cast stop: {e}")

    def next_track(self, stream_base: str = ""):
        if stream_base:
            self._next(stream_base)
        else:
            # Try to get stream_base from current context
            self._stop_flag = False
            with self._lock:
                if self._idx < len(self._tracks) - 1:
                    self._idx += 1

    def prev_track(self, stream_base: str = ""):
        with self._lock:
            if self._idx > 0:
                self._idx -= 1
        if stream_base:
            self._play_current(stream_base)

    def snapshot(self) -> dict:
        """Return current state for the UI status poll."""
        try:
            if not self._mc or not self._device:
                return {"state": "stopped", "alive": False}

            status = self._mc.status
            if not status:
                return {"state": "stopped", "alive": False}

            with self._lock:
                t = self._tracks[self._idx] if self._idx < len(self._tracks) else {}

            state = "playing"
            if status.player_is_paused:
                state = "paused"
            elif status.player_is_idle:
                state = "stopped"

            return {
                "state": state,
                "alive": not status.player_is_idle,
                "paused": status.player_is_paused,
                "media_title": t.get("title", ""),
                "artist": t.get("artist", ""),
                "album": t.get("album", ""),
                "position": status.current_time or 0,
                "duration": status.duration or 0,
                "queue_pos": self._idx + 1,
                "queue_len": len(self._tracks),
                "device": self._device.name if self._device else "",
            }
        except Exception:
            return {"state": "stopped", "alive": False}


CAST_QUEUE = CastQueue()


# ── Standalone test ───────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s %(levelname)-5s %(message)s")

    if not _ensure_pychromecast():
        print("\npychromecast not installed. Install with:")
        print("  pip install pychromecast")
        sys.exit(1)

    print("\nScanning for Chromecast devices (10 s)…\n")
    start_discovery()
    time.sleep(10)

    devices = CAST_DEVICES.all()
    if not devices:
        print("No Chromecast devices found.")
    else:
        print(f"Found {len(devices)} device(s):\n")
        for d in devices:
            print(f"  {d.name:<30} {d.model:<20} {d.host}  ({d.cast_type})")

    stop_discovery()
    print()