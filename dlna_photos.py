#!/usr/bin/env python3
"""
dlna_photos.py — the Immich photo library for the TV's media player
(2026-11-09 single album; 2026-11-10 the full tree).

The LG TV (OLED55C9, webOS 4.x) cannot run Immich's web app: its browser is
too old and shows a blank page. Its DLNA photo viewer works, so the gateway
offers Immich as a 📷 Photos folder. This module is the data side; the tree
is api_upnp_browse_photo.py, the bytes dlna_asgi_photos.py.

  * WHAT: every image Immich shows on its timeline, for every
    `IMMICH_API_KEY*` in `.env` (favourites and named people are PER USER in
    Immich, so each key is scanned and the answers merged), plus every album
    either user owns or was shared. Videos are in 📹 Videos. Trashed, locked
    and hidden assets never appear; archived ones only inside an album.
  * FROM WHERE: Immich's own API on THIS Mac (`IMMICH_URL` must be a
    loopback address). Immich's files are never opened.
  * WHAT THE TV GETS: Immich's existing 1440 px JPEG preview. The TV shows
    neither HEIC/NEF nor WebP, and nothing is converted or written to disk.
  * SAFETY: a preview is fetched only for an asset in the index (a LAN peer
    cannot walk Immich by id), only with a key that listed it, and only JPEG
    bytes go out. Keys never leave this process.

Size, measured 2026-11-10: 48,958 images, one full scan ~28 s per key. The
index is ~20 MB of RAM. It is REBUILT off to the side every
`PHOTOS_REFRESH_SEC` (900 s) and swapped in whole, so a Browse never waits
on Immich and never sees a half-built library.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import NamedTuple

from dlna_photos_immich import UUID, Immich, loopback_url

log = logging.getLogger("dlna.photos")

JPEG_MAGIC = b"\xff\xd8\xff"
CACHE_ITEMS = 48            # previews kept in RAM: the TV asks for each twice (art + res)
TIMELINE = {"timeline"}
IN_ALBUMS = {"timeline", "archive"}
_FETCH_ERRORS = (urllib.error.URLError, OSError, ValueError, KeyError, TypeError)


class Photo(NamedTuple):
    id: str
    date: str       # local capture time, YYYY-MM-DDTHH:MM:SS
    title: str
    city: str
    country: str


@dataclass
class Album:
    id: str
    name: str
    photos: list = field(default_factory=list)     # Photo, chronological


@dataclass
class Library:
    """One complete, immutable snapshot of what the TV can browse."""
    photos: dict = field(default_factory=dict)      # id -> Photo
    owner: dict = field(default_factory=dict)       # id -> the key that listed it
    months: dict = field(default_factory=dict)      # 'YYYY-MM' -> [Photo]
    albums: list = field(default_factory=list)      # [Album], newest first
    people: list = field(default_factory=list)      # [(name, [Photo])], A-Z
    places: list = field(default_factory=list)      # [(country, [(city, [Photo])])]
    favourites: list = field(default_factory=list)  # [Photo]


def keys_from_env(env=None) -> list:
    env = os.environ if env is None else env
    return [v for k, v in sorted(env.items()) if k.startswith("IMMICH_API_KEY") and v.strip()]


def _title(item: dict) -> str:
    when = (item.get("localDateTime") or "")[:16].replace("T", " ")
    city = ((item.get("exifInfo") or {}).get("city") or "").strip()
    return " · ".join(x for x in (when, city) if x) or item.get("originalFileName", "photo")


def _photo(item: dict) -> Photo:
    exif = item.get("exifInfo") or {}
    return Photo(item["id"], (item.get("localDateTime") or "")[:19], _title(item),
                 (exif.get("city") or "").strip(), (exif.get("country") or "").strip())


def _shown(item: dict, allowed: set) -> bool:
    return (not item.get("isTrashed") and UUID.fullmatch(item.get("id", "")) is not None
            and item.get("visibility", "timeline") in allowed)


def _chrono(photos) -> list:
    return sorted(photos, key=lambda p: (p.date, p.id))


def build(scans, albums) -> Library:
    """The pure half of a refresh.
    scans:  [(key, timeline items as Immich returns them)], one per key.
    albums: [(key, {id, name}, that album's items)]."""
    lib, people, favs = Library(), defaultdict(dict), {}
    for key, items in scans:
        for it in items:
            if not _shown(it, TIMELINE):
                continue
            p = lib.photos.setdefault(it["id"], _photo(it))
            lib.owner.setdefault(p.id, key)
            if it.get("isFavorite"):
                favs[p.id] = p
            for person in it.get("people") or []:
                name = (person.get("name") or "").strip()
                if name and not person.get("isHidden"):
                    people[name][p.id] = p          # one name, merged across users
    timeline = list(lib.photos.values())
    by_month = defaultdict(list)
    places = defaultdict(lambda: defaultdict(list))
    for p in timeline:
        if p.date:
            by_month[p.date[:7]].append(p)
        if p.country:
            places[p.country][p.city].append(p)
    lib.months = {m: _chrono(ps) for m, ps in by_month.items()}
    seen = set()
    for key, meta, items in albums:
        if meta["id"] in seen:
            continue
        seen.add(meta["id"])
        ps = []
        for it in items:
            if _shown(it, IN_ALBUMS):
                ps.append(lib.photos.setdefault(it["id"], _photo(it)))
                lib.owner.setdefault(it["id"], key)
        if ps:
            lib.albums.append(Album(meta["id"], meta["name"], _chrono(ps)))
    lib.albums.sort(key=lambda a: (a.photos[-1].date, a.name), reverse=True)
    lib.people = sorted(((n, _chrono(ps.values())) for n, ps in people.items()),
                        key=lambda np: np[0].casefold())
    # Places: biggest country first; inside one, biggest city first and the
    # photos with a country but no city last.
    lib.places = sorted(
        ((c, sorted(((city, _chrono(ps)) for city, ps in cities.items()),
                    key=lambda cp: (cp[0] == "", -len(cp[1]), cp[0])))
         for c, cities in places.items()),
        key=lambda cc: (-sum(len(ps) for _, ps in cc[1]), cc[0]))
    lib.favourites = _chrono(favs.values())
    return lib


class PhotoIndex:
    def __init__(self, url: str = "", keys=(), enabled: bool = False, client=Immich):
        self.url, self.keys, self.on, self.client = url, list(keys), enabled, client
        self.lib = Library()
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> PhotoIndex:
        url = loopback_url(os.environ.get("IMMICH_URL", ""))
        if os.environ.get("IMMICH_URL") and not url:
            log.warning("photos: IMMICH_URL is not a loopback address; the 📷 Photos folder stays off")
        on = os.environ.get("PHOTOS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
        return cls(url, keys_from_env(), on)

    @property
    def enabled(self) -> bool:
        return bool(self.on and self.url and self.keys)

    def refresh(self) -> None:
        t0 = time.monotonic()
        scans, albums = [], []
        for key in self.keys:
            api = self.client(self.url, key)
            try:
                scans.append((key, list(api.search())))
                for meta in api.albums():
                    albums.append((key, meta, list(api.search(albumIds=[meta["id"]]))))
            except _FETCH_ERRORS as e:
                log.warning("photos: an IMMICH_API_KEY* could not read Immich: %s", e)
        if not scans:
            log.warning("photos: no key could read Immich; keeping the previous index")
            return
        lib = build(scans, albums)
        with self._lock:
            self.lib = lib
            self._cache = OrderedDict((k, v) for k, v in self._cache.items() if k in lib.owner)
        log.info("photos: %d photos, %d months, %d albums, %d people, %d countries, "
                 "%d favourites (%.0fs)", len(lib.photos), len(lib.months), len(lib.albums),
                 len(lib.people), len(lib.places), len(lib.favourites), time.monotonic() - t0)

    def preview(self, asset_id: str):
        """JPEG bytes for an indexed asset, else None (the caller answers an opaque 502)."""
        with self._lock:
            key = self.lib.owner.get(asset_id)
            if key is None:
                return None
            if asset_id in self._cache:
                self._cache.move_to_end(asset_id)
                return self._cache[asset_id]
        try:
            data = self.client(self.url, key).preview(asset_id)
        except _FETCH_ERRORS as e:
            log.warning("photos: preview of %s failed: %s", asset_id, e)
            return None
        if len(data) > 25 * 1024 * 1024 or not data.startswith(JPEG_MAGIC):
            log.warning("photos: preview of %s is not a JPEG (%d bytes); not served", asset_id, len(data))
            return None
        with self._lock:
            self._cache[asset_id] = data
            while len(self._cache) > CACHE_ITEMS:
                self._cache.popitem(last=False)
        return data


INDEX = PhotoIndex()


def start() -> None:
    """Configure INDEX from .env and keep it fresh. Does nothing when photos are off.
    Configures INDEX IN PLACE: every reader holds that one object, so rebinding
    the name would leave them reading an empty index forever."""
    fresh = PhotoIndex.from_env()
    INDEX.url, INDEX.keys, INDEX.on = fresh.url, fresh.keys, fresh.on
    if not INDEX.enabled:
        log.info("photos: off (needs PHOTOS_ENABLED=1, IMMICH_URL and an IMMICH_API_KEY*)")
        return
    every = max(300, int(os.environ.get("PHOTOS_REFRESH_SEC", "900") or 900))

    def _loop():
        while True:
            try:
                INDEX.refresh()
            except Exception as e:      # noqa: BLE001 — a refresh must never kill the thread
                log.warning("photos: refresh failed: %s", e)
            time.sleep(every)
    threading.Thread(target=_loop, daemon=True, name="photos-index").start()
