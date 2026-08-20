#!/usr/bin/env python3
"""
dlna_asgi_video.py — the video routes the PWA uses (same-origin so iOS
will play them), including on-demand transcoding.

Split out of dlna_asgi.py on 2026-08-20, when that module reached 1,156
lines holding every route in the gateway. Each group is now an APIRouter that
dlna_asgi includes:

    dlna_asgi_state.py     the shared runtime handles every router binds against
    dlna_asgi_browse.py    the JSON read API + SSE
    dlna_asgi_video.py     /video/* (PWA, same-origin)
    dlna_asgi_media.py     /art, /stream, /radio_stream byte relays
    dlna_asgi_upnp.py      /gw/* — the Naim-facing UPnP surface
    dlna_asgi_subsonic.py  /rest/* — the CarPlay surface
    dlna_asgi_static.py    /, /sw.js, /manifest.json, generated icons
    dlna_asgi.py           lifespan, the app, legacy-bridge wiring, includes

Route ORDER across these routers is not load-bearing: no two routes in the
app can match the same request (asserted by tests/test_asgi.py), so grouping
is free. dlna_asgi re-exports every handler, so the ~58 tests that call
`dlna_asgi.<route>()` directly keep working.

`/video_hls/{vid}/{seg}` exists because progressive `/video_transcode`
cannot seek: the HLS variant computes a VOD playlist from the duration
instantly and transcodes only the ~6s segment the player asks for.

⚠ `_isfile()` wraps `os.path.isfile` in the threadpool ON PURPOSE. These
paths live on an EXTERNAL volume (SAMDATA); a stat() against a spun-down USB
disk blocks for seconds, and inside an `async def` that stalls the entire
gateway, not just this request. Never call os.path.* directly here.
"""
import asyncio
import logging
import os

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response, StreamingResponse

import dlna_asgi_state as _st
from dlna_asgi_state import _VIDEO_UDN

router = APIRouter()

log = logging.getLogger("dlna.asgi")


async def _isfile(path: str) -> bool:
    """`os.path.isfile` off the event loop.

    The video/poster roots live on an EXTERNAL volume (SAMDATA). A stat()
    against a spun-down USB disk blocks for seconds, and inside an
    `async def` that stalls the whole gateway — every other request plus
    the SSE stream — not just this one. Ruff ASYNC240 guards this."""
    return await run_in_threadpool(os.path.isfile, path)


def _video_payload(v: dict, people=None) -> dict:
    return {
        "id": v["id"], "title": v.get("title"), "folder": v.get("folder"),
        "duration": v.get("duration"), "width": v.get("width"),
        "height": v.get("height"), "vcodec": v.get("vcodec"),
        "acodec": v.get("acodec"), "container": v.get("container"),
        "mime": v.get("mime"), "created": v.get("created"),
        "location_name": v.get("location_name"),
        "country": v.get("country"),
        "people": list(people or []),          # Immich person tags (Plan B)
        "playUrl": f"/video/{v['id']}",
        "transcodeUrl": f"/video_transcode/{v['id']}",
        "hlsUrl": f"/video_hls/{v['id']}/index.m3u8",
        "posterUrl": (f"/video_poster?id={v['id']}" if v.get("poster") else ""),
    }


@router.get("/api/videos")
async def videos() -> list:
    rows = await run_in_threadpool(_st.DB.all_videos, _VIDEO_UDN)
    people = await run_in_threadpool(_st.DB.video_people_map, _VIDEO_UDN)
    return [_video_payload(v, people.get(v["id"])) for v in rows]


@router.get("/api/video_meta")
async def video_meta(id: str = ""):
    v = await run_in_threadpool(_st.DB.video_by_id, id) if id else None
    if not v:
        return JSONResponse({"error": "not found"}, status_code=404)
    people = await run_in_threadpool(_st.DB.video_people_map, _VIDEO_UDN)
    return _video_payload(v, people.get(v["id"]))


@router.get("/video/{vid}", include_in_schema=False)
async def video_file(vid: str):
    v = await run_in_threadpool(_st.DB.video_by_id, vid)
    if not v or not v.get("file_path") or not await _isfile(v["file_path"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(v["file_path"], media_type=(v.get("mime") or "video/mp4"))


@router.get("/video_poster", include_in_schema=False)
async def video_poster(id: str = ""):
    import dlna_ffmpeg
    p = os.path.join(dlna_ffmpeg.POSTER_DIR, f"{os.path.basename(id)}.jpg")
    if not id or not await _isfile(p):
        return JSONResponse({"error": "no poster"}, status_code=404)
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@router.get("/video_transcode/{vid}", include_in_schema=False)
async def video_transcode(vid: str):
    """On-demand transcode → H.264/AAC fragmented MP4, streamed (V3). The PWA
    falls back here for clips the browser can't decode natively (HEVC / MKV /
    E-AC3). ffmpeg absent → 503 (native-only still works). Progressive (no
    Range/seek yet — HLS is the future upgrade)."""
    import dlna_ffmpeg
    v = await run_in_threadpool(_st.DB.video_by_id, vid)
    if not v or not v.get("file_path") or not await _isfile(v["file_path"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not dlna_ffmpeg.find_ffmpeg():
        return JSONResponse({"error": "ffmpeg not available"}, status_code=503)
    cmd = dlna_ffmpeg.transcode_cmd(v["file_path"])
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def _pump():
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:        # client disconnected / done
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

    return StreamingResponse(_pump(), media_type="video/mp4",
                             headers={"Cache-Control": "no-store",
                                      "Connection": "close"})


@router.get("/video_hls/{vid}/{seg}", include_in_schema=False)
async def video_hls(vid: str, seg: str):
    """SEEKABLE transcode via on-demand HLS (V3+). `index.m3u8` = a VOD playlist
    computed from the duration (instant, no transcode); `segN.ts` = that ~6s
    segment transcoded to H.264/AAC MPEG-TS on demand → the player fetches only
    the segment for the seek target. ffmpeg absent → 503."""
    import dlna_ffmpeg
    import re as _re
    v = await run_in_threadpool(_st.DB.video_by_id, vid)
    if not v or not v.get("file_path") or not await _isfile(v["file_path"]):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not dlna_ffmpeg.find_ffmpeg():
        return JSONResponse({"error": "ffmpeg not available"}, status_code=503)

    if seg == "index.m3u8":
        pl = dlna_ffmpeg.hls_playlist(v.get("duration") or 0)
        return Response(pl, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})

    m = _re.fullmatch(r"seg(\d+)\.ts", seg)
    if not m:
        return JSONResponse({"error": "bad segment"}, status_code=404)
    start = int(m.group(1)) * dlna_ffmpeg.HLS_SEG
    cmd = dlna_ffmpeg.hls_segment_cmd(v["file_path"], start)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

    async def _pump():
        try:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

    return StreamingResponse(_pump(), media_type="video/mp2t",
                             headers={"Cache-Control": "no-store"})
