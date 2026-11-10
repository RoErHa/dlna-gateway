#!/usr/bin/env python3
"""
dlna_photos_immich.py — the few Immich 3 API calls the 📷 Photos index
needs (2026-11-10). urllib only, short timeouts, loopback only.

Split from dlna_photos.py so the index logic can be tested with a fake
client and the HTTP stays in one small place. Immich 3 facts it depends on:

  * `GET /api/albums/{id}` no longer lists assets: an album's photos come
    from `POST /api/search/metadata {"albumIds": [id]}`, like everything else.
  * A search answers 1,000 items per page, with `nextPage` until the end.
  * `isFavorite` and the `people` on an item are the CALLER's: favourites
    and named people are per Immich user, so every key is scanned.
"""
from __future__ import annotations

import json
import re
import urllib.request
from urllib.parse import urlsplit

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
LOOPBACK = {"localhost", "127.0.0.1", "::1"}
MAX_PREVIEW = 25 * 1024 * 1024
MAX_JSON = 64 * 1024 * 1024        # a 1,000-item page with exif + people is ~2 MB
TIMEOUT = 30.0
PAGE = 1000


def loopback_url(url: str) -> str:
    """IMMICH_URL if it points at this Mac, else '' (photos stay off)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme in ("http", "https") and parts.hostname in LOOPBACK:
        return url.rstrip("/")
    return ""


class Immich:

    def __init__(self, url: str, key: str):
        self.url, self.key = url, key

    def _open(self, method: str, path: str, body=None):
        hdrs = {"x-api-key": self.key, "Accept": "application/json"}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.url + path, method=method, headers=hdrs,
            data=None if body is None else json.dumps(body).encode())
        return urllib.request.urlopen(req, timeout=TIMEOUT)

    def json(self, method: str, path: str, body=None):
        with self._open(method, path, body) as r:
            return json.loads(r.read(MAX_JSON) or b"null")

    def search(self, **filters):
        """Every IMAGE matching `filters`, with exif and the caller's people."""
        page = 1
        while page:
            data = self.json("POST", "/api/search/metadata", {
                "type": "IMAGE", "withExif": True, "withPeople": True,
                "size": PAGE, "page": page, **filters})["assets"]
            yield from data.get("items", [])
            nxt = data.get("nextPage")
            page = int(nxt) if nxt and str(nxt).isdigit() and int(nxt) > page else 0

    def albums(self) -> list:
        """[{id, name}] of every album this user owns or was shared."""
        seen = {}
        for q in ("", "?shared=true"):
            for a in self.json("GET", "/api/albums" + q) or []:
                if UUID.fullmatch(a.get("id", "")):
                    seen.setdefault(a["id"], (a.get("albumName") or "").strip() or "(album)")
        return [{"id": k, "name": v} for k, v in seen.items()]

    def preview(self, asset_id: str) -> bytes:
        with self._open("GET", f"/api/assets/{asset_id}/thumbnail?size=preview") as r:
            return r.read(MAX_PREVIEW + 1)
