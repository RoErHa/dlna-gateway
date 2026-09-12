"""
dlna_localfs_watch.py — a configured root that is not there YET.

`_resolve_root` answers "is this volume present?" once, at boot. That
is the wrong number of times to ask, and 2026-09-11 showed why: the
machine rebooted, launchd started the gateway at 20:06:36, and at
20:06:50 macOS had not finished mounting the external music drive. The
independent-root wiring did exactly what it promises — music and video
disabled themselves, audiobooks came up — and then the drive mounted a
few seconds later and NOTHING re-asked. The gateway ran for seventeen
hours serving a file server whose `allowed_roots` held only the books,
while `tracks` still listed all 26k music rows: every album browsed
fine and every play came back 403.

So a missing root is now a WAITING root. This module owns three things
the wiring should not have to:

  1. **Re-checking.** One daemon thread re-resolves every waiting root
     every `LOCALFS_ROOT_RECHECK_SEC` (default 30 s) and brings up the
     ones that have appeared, via the callback the wiring registered.
     It stops itself once nothing is waiting, so a fully-mounted
     machine carries no idle thread.

  2. **Saying so.** A volume nobody mounts is a person's job, not a
     retry loop's, and the only evidence used to be one WARNING at
     boot — which is exactly what went unread. `snapshot()` backs
     `GET /api/libraries` so the PWA can say it in the window the
     person is actually looking at, and every state change publishes
     an SSE event so it appears without waiting for a poll.

  3. **Activating exactly once.** The callback starts a file server,
     binds a provider and registers a device; running it twice would
     double-register the library.

Deliberately generic — it knows "a labelled path that may appear", not
music/video/audiobooks. What to DO when one appears lives in
`dlna_localfs_wiring`, which is where the rest of that knowledge is.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from collections.abc import Callable

log = logging.getLogger("dlna.localfs.watch")

# How often a waiting root is re-resolved. 30 s is chosen against the
# thing being waited for: a volume appears when a person plugs it in or
# macOS finishes mounting it, so the cost of noticing late is seconds
# of a person's patience, and the cost of asking often is one `stat`.
_DEFAULT_RECHECK_SEC = 30

WAITING = "waiting"
ACTIVE = "active"


def _recheck_sec() -> int:
    try:
        return max(5, int(os.environ.get("LOCALFS_ROOT_RECHECK_SEC", "")
                          or _DEFAULT_RECHECK_SEC))
    except ValueError:
        return _DEFAULT_RECHECK_SEC


def resolve_root(label: str, root: str) -> Path | None:
    """A configured root that is actually THERE, or None.

    THE shared answer to "is this volume present?", asked at boot by
    the wiring and again on every re-check here. Each root is optional
    and independent: an unmounted volume — or a locked APFS one, which
    macOS declines to mount at all — disables ONLY its own library.

    Returns the path so the caller cannot re-derive it differently, and
    swallows `OSError` because a volume caught mid-unmount raises from
    `exists()` rather than answering False.
    """
    if not root:
        return None
    path = Path(root).expanduser()
    try:
        present = path.exists()
    except OSError as e:
        log.debug(f"{label}: stat of {root} failed: {e}")
        return None
    return path if present else None


def _publish(event: dict) -> None:
    """Best-effort SSE. The bus is a no-op until the ASGI lifespan binds
    its loop, and boot-time registration runs BEFORE that — which is
    precisely why `snapshot()` exists rather than events alone."""
    try:
        from dlna_events import EVENTS
        EVENTS.publish(event)
    except Exception as e:                                    # noqa: BLE001
        log.debug(f"event publish failed: {e}")


class RootWatch:
    """The configured roots and whether each one is actually there."""

    def __init__(self):
        self._lock = threading.Lock()
        self._roots: dict[str, dict] = {}
        self._thread: threading.Thread | None = None

    # ── registration ────────────────────────────────────────────────
    def register(self, label: str, root: str,
                 activate: Callable[[Path], None]) -> bool:
        """Record a configured root and bring it up if it is present.

        Returns True when it activated. An unconfigured root (empty
        string) is not tracked at all — it is off, not waiting, and
        showing "waiting for Video" to someone who never wanted video
        would be noise.
        """
        if not root:
            return False
        with self._lock:
            self._roots[label] = {"label": label, "root": root,
                                  "state": WAITING, "activate": activate,
                                  "since": time.time()}
        if self._activate(label):
            return True
        log.warning(f"{label} root not found: {root} — waiting for it. "
                    "Mount/unlock the volume and it will be picked up "
                    f"within {_recheck_sec()}s; no restart needed.")
        _publish({"type": "libraries", "label": label, "state": WAITING})
        return False

    def _activate(self, label: str) -> bool:
        """Resolve `label`'s root and, if it is there now, run its
        callback exactly once."""
        with self._lock:
            entry = self._roots.get(label)
            if not entry or entry["state"] == ACTIVE:
                return False
            root = entry["root"]
        path = resolve_root(label, root)
        if path is None:
            return False
        # Marked ACTIVE before the callback runs: the callback starts a
        # server and scans a library, and a re-check firing meanwhile
        # must not start a second one.
        with self._lock:
            entry = self._roots.get(label)
            if not entry or entry["state"] == ACTIVE:
                return False
            entry["state"] = ACTIVE
            entry["since"] = time.time()
        try:
            entry["activate"](path)
        except Exception as e:                                # noqa: BLE001
            # Left ACTIVE on purpose. The root IS there; what failed is
            # our handling of it, and retrying that every 30 s would
            # repeat the failure forever instead of reporting it once.
            log.exception(f"{label}: root appeared but wiring it failed: {e}")
            with self._lock:
                self._roots[label]["error"] = str(e)
            return True
        log.info(f"{label} root is available: {root} — library enabled")
        _publish({"type": "libraries", "label": label, "state": ACTIVE})
        _publish({"type": "devices"})     # the source list just changed
        return True

    # ── the re-check loop ───────────────────────────────────────────
    def start(self) -> None:
        """Start the re-check thread if anything is waiting (and one
        isn't already running)."""
        with self._lock:
            waiting = [k for k, v in self._roots.items()
                       if v["state"] == WAITING]
            if not waiting or (self._thread and self._thread.is_alive()):
                return
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name="localfs-root-watch")
            t = self._thread
        log.info(f"Watching for {', '.join(sorted(waiting))} "
                 f"(re-checking every {_recheck_sec()}s)")
        t.start()

    def _loop(self) -> None:
        while True:
            time.sleep(_recheck_sec())
            for label in self.waiting():
                self._activate(label)
            if not self.waiting():
                log.debug("every configured root is up — watch thread exiting")
                return

    def waiting(self) -> list[str]:
        with self._lock:
            return [k for k, v in self._roots.items()
                    if v["state"] == WAITING]

    # ── what the UI is told ─────────────────────────────────────────
    def snapshot(self) -> list[dict]:
        """`GET /api/libraries`. One entry per CONFIGURED root, so the
        PWA can name the volume a person has to go and mount."""
        with self._lock:
            entries = [dict(v) for v in self._roots.values()]
        out = []
        for e in entries:
            e.pop("activate", None)
            if e["state"] == WAITING:
                e["message"] = (f"{e['label']} library unavailable — "
                                f"mount {e['root']}. It will be picked up "
                                "automatically, no restart needed.")
            out.append(e)
        return sorted(out, key=lambda e: e["label"])

    def reset(self) -> None:
        """Tests only — the app never un-registers a root."""
        with self._lock:
            self._roots.clear()
            self._thread = None


# Module singleton — the gateway's one set of watched roots.
WATCH = RootWatch()

__all__ = ["RootWatch", "WATCH", "WAITING", "ACTIVE", "resolve_root"]
