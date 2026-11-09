#!/usr/bin/env python3
"""
dlna_asgi_photos.py — `/gw/photo/<asset id>.jpg`, the bytes behind the
📷 Photos tree (2026-11-09).

On the PLAIN :8765 bind with the rest of `/gw/*`: the LG TV fetches it
directly and does no TLS here. What it serves is decided by
`dlna_photos.INDEX.preview`:

  * only an asset id that is IN the index (a LAN peer cannot walk Immich by
    guessing ids — and Immich ids are random UUIDs anyway);
  * only JPEG bytes (magic-byte check), Immich's 1440 px preview;
  * every failure is the same opaque `502 {"error":"photo unavailable"}` —
    the reason goes to the log only, as for `/art` (Security posture 2).
"""
import re

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response

import dlna_photos
from api_upnp_browse_photo import PROTOCOL

router = APIRouter()

_NAME = re.compile(r"([0-9a-f-]{36})\.jpg")
_DLNA = {"contentFeatures.dlna.org": PROTOCOL.split(":", 3)[3],
         "transferMode.dlna.org": "Interactive",
         "Cache-Control": "private, max-age=3600"}


@router.api_route("/gw/photo/{name}", methods=["GET", "HEAD"],
                  include_in_schema=False)
async def gw_photo(name: str, request: Request):
    m = _NAME.fullmatch(name)
    data = (await run_in_threadpool(dlna_photos.INDEX.preview, m.group(1))
            if m else None)
    if data is None:
        return JSONResponse({"error": "photo unavailable"}, status_code=502)
    if request.method == "HEAD":
        return Response(b"", media_type="image/jpeg",
                        headers={**_DLNA, "Content-Length": str(len(data))})
    return Response(data, media_type="image/jpeg", headers=_DLNA)
