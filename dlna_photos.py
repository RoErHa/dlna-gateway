#!/usr/bin/env python3
"""
dlna_photos.py — Immich photos for the TV's media player (2026-11-09).

The LG TV (OLED55C9, webOS 4.x) cannot run Immich's web app: its browser is
too old and shows a blank page. Its DLNA photo viewer works, so the gateway
offers Immich photos as a 📷 Photos folder. This module is the data side:

  * WHAT: the albums named in `PHOTOS_ALBUMS` (comma-separated Immich album
    ids; a first, single-album test). Images only (videos are in 📹 Videos),
    never trashed/locked/hidden ones.
  * FROM WHERE: Immich's own API on THIS Mac only (`IMMICH_URL` must be a
    loopback address), with every `IMMICH_API_KEY*` in `.env`; each album is
    read with the first key that may see it. Immich's files are never opened.
  * WHAT THE TV GETS: Immich's JPEG preview (1440 px). The TV shows neither
    HEIC/NEF nor WebP (Immich's small thumbnails are WebP), so the preview is
    also used as the item's art.
  * SAFETY: a preview is fetched only for an asset in the index (so a LAN peer
    cannot walk Immich by id), only by the key that listed it, and only JPEG
    bytes go out (magic-byte check). Keys never leave this process.

The index refreshes in a daemon thread (`start()`) every
`PHOTOS_REFRESH_SEC`, so a Browse never waits on Immich.
Wiring: api_upnp_browse_photo.py (the tree), dlna_asgi_photos.py (the bytes).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

log = logging.getLogger("dlna.photos")

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
JPEG_MAGIC = b"\xff\xd8\xff"
MAX_PREVIEW = 25 * 1024 * 1024
CACHE_ITEMS = 48            # previews kept in memory: the TV asks for each twice (art + res)
TIMEOUT = 10.0
SHOWN = {"timeline", "archive"}


@dataclass
class Album:
    id: str
    name: str
    photos: list = field(default_factory=list)   # dicts: id, title, date


def loopback_url(url: str) -> str:
    """IMMICH_URL if it points at this Mac, else '' (photos stay off)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme in ("http", "https") and parts.hostname in LOOPBACK:
        return url.rstrip("/")
    return ""


def keys_from_env(env=None) -> list:
    env = os.environ if env is None else env
    return [v for k, v in sorted(env.items()) if k.startswith("IMMICH_API_KEY") and v.strip()]


def album_ids_from_env(env=None) -> list:
    env = os.environ if env is None else env
    return [a for a in (x.strip().lower() for x in env.get("PHOTOS_ALBUMS", "").split(",")) if UUID.fullmatch(a)]


def _title(item: dict) -> str:
    when = (item.get("localDateTime") or "")[:16].replace("T", " ")
    city = ((item.get("exifInfo") or {}).get("city") or "").strip()
    return " · ".join(x for x in (when, city) if x) or item.get("originalFileName", "photo")


class Immich:
    """The few Immich calls this needs; urllib, short timeouts."""

    def __init__(self, url: str, key: str):
        self.url, self.key = url, key

    def _open(self, method: str, path: str, body=None):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"x-api-key": self.key, "Accept": "application/json",
                                              **({"Content-Type": "application/json"} if body is not None else {})})
        return urllib.request.urlopen(req, timeout=TIMEOUT)

    def json(self, method: str, path: str, body=None):
        with self._open(method, path, body) as r:
            return json.loads(r.read(4 * 1024 * 1024) or b"null")

    def album(self, album_id: str) -> Album:
        name = self.json("GET", f"/api/albums/{album_id}")["albumName"]
        photos, page = [], 1
        while page:
            data = self.json("POST", "/api/search/metadata",
                             {"albumIds": [album_id], "type": "IMAGE", "withExif": True,
                              "size": 1000, "page": page})["assets"]
            for it in data.get("items", []):
                if it.get("isTrashed") or it.get("visibility", "timeline") not in SHOWN or not UUID.fullmatch(it.get("id", "")):
                    continue
                photos.append({"id": it["id"], "title": _title(it), "date": (it.get("localDateTime") or "")[:19]})
            nxt = data.get("nextPage")
            page = int(nxt) if nxt and str(nxt).isdigit() and int(nxt) > page else 0
        photos.sort(key=lambda p: (p["date"], p["id"]))
        return Album(album_id, name, photos)

    def preview(self, asset_id: str) -> bytes:
        with self._open("GET", f"/api/assets/{asset_id}/thumbnail?size=preview") as r:
            return r.read(MAX_PREVIEW + 1)


class PhotoIndex:
    def __init__(self, url: str = "", keys=(), album_ids=(), client=Immich):
        self.url, self.keys, self.album_ids, self.client = url, list(keys), list(album_ids), client
        self.albums: list = []
        self._owner: dict = {}          # asset id -> key that listed it
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> PhotoIndex:
        url = loopback_url(os.environ.get("IMMICH_URL", ""))
        if os.environ.get("IMMICH_URL") and not url:
            log.warning("photos: IMMICH_URL is not a loopback address; the 📷 Photos folder stays off")
        return cls(url, keys_from_env(), album_ids_from_env())

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.keys and self.album_ids)

    def refresh(self) -> None:
        albums, owner = [], {}
        for aid in self.album_ids:
            for key in self.keys:
                try:
                    album = self.client(self.url, key).album(aid)
                except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
                    log.debug("photos: album %s not readable with this key: %s", aid, e)
                    continue
                albums.append(album)
                owner.update({p["id"]: key for p in album.photos})
                break
            else:
                log.warning("photos: album %s could not be read with any IMMICH_API_KEY*", aid)
        with self._lock:
            self.albums, self._owner = albums, owner
            self._cache = OrderedDict((k, v) for k, v in self._cache.items() if k in owner)
        log.info("photos: %d album(s), %d photos", len(albums), len(owner))

    def album(self, album_id: str):
        return next((a for a in self.albums if a.id == album_id), None)

    def photo(self, asset_id: str):
        return next(((a, p) for a in self.albums for p in a.photos if p["id"] == asset_id), (None, None))

    def preview(self, asset_id: str):
        """JPEG bytes for an indexed asset, else None (the caller answers an opaque 502)."""
        with self._lock:
            key = self._owner.get(asset_id)
            if key is None:
                return None
            if asset_id in self._cache:
                self._cache.move_to_end(asset_id)
                return self._cache[asset_id]
        try:
            data = self.client(self.url, key).preview(asset_id)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("photos: preview of %s failed: %s", asset_id, e)
            return None
        if len(data) > MAX_PREVIEW or not data.startswith(JPEG_MAGIC):
            log.warning("photos: preview of %s is not a JPEG (%d bytes); not served", asset_id, len(data))
            return None
        with self._lock:
            self._cache[asset_id] = data
            while len(self._cache) > CACHE_ITEMS:
                self._cache.popitem(last=False)
        return data


INDEX = PhotoIndex()


def start() -> None:
    """Build the index from .env and keep it fresh. Does nothing when photos are not configured.
    Configures INDEX IN PLACE: every reader holds that one object, so rebinding
    the name would leave them reading an empty index forever."""
    fresh = PhotoIndex.from_env()
    INDEX.url, INDEX.keys, INDEX.album_ids = fresh.url, fresh.keys, fresh.album_ids
    if not INDEX.enabled:
        log.info("photos: off (needs IMMICH_URL, an IMMICH_API_KEY* and PHOTOS_ALBUMS)")
        return
    every = max(60, int(os.environ.get("PHOTOS_REFRESH_SEC", "300") or 300))

    def _loop():
        while True:
            try:
                INDEX.refresh()
            except Exception as e:      # noqa: BLE001 — a refresh must never kill the thread
                log.warning("photos: refresh failed: %s", e)
            time.sleep(every)
    threading.Thread(target=_loop, daemon=True, name="photos-index").start()
