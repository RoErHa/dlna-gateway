#!/usr/bin/env python3
"""
dlna_asgi_books.py — the 🌐 language view of the audiobooks source.

  GET /api/book_languages?udn=U                  → {languages: [{language, books}]}
  GET /api/book_language_albums?udn=U&language=L → {albums: [...]}
  GET  /api/book_language?url=…   → the edit form's view of one book
  POST /api/book_language {url, language}  → set by hand ('' = Auto)

`language` is an ISO 639-1 code; an EMPTY `language=` asks for the books
nothing could place (Unknown), which is a real bucket rather than an
error. Album rows have `browse_letter`'s Albums shape, so the PWA renders
them with the same code as every other album list.

Its own module because `dlna_asgi_browse.py` sits at its line budget,
and because this is a feature seam: anything else the PWA learns to ask
about books (narrator, series) belongs beside it, not in browse.

Not on the Service Worker's CACHEABLE_API allowlist: a language set by
`tools/book_language.py` should show on the next tap, not the one after.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

import dlna_asgi_state as _st
import dlna_localfs_wiring

router = APIRouter()


@router.get("/api/book_languages", include_in_schema=False)
async def book_languages_route(request: Request):
    udn = request.query_params.get("udn", "")
    if not udn:
        return JSONResponse({"error": "missing udn"}, status_code=400)
    facets = await run_in_threadpool(_st.DB.book_language_facets, udn)
    return JSONResponse({"languages": facets})


@router.get("/api/book_language_albums", include_in_schema=False)
async def book_language_albums_route(request: Request):
    q = request.query_params
    udn = q.get("udn", "")
    if not udn or "language" not in q:
        return JSONResponse({"error": "missing udn or language"},
                            status_code=400)
    albums = await run_in_threadpool(
        _st.DB.book_language_albums, udn, q.get("language", "").strip().lower())
    return JSONResponse({"albums": albums})


def _books_udn() -> str:
    # Read at CALL time: the wiring sets it when the audiobooks root
    # activates, which may be minutes after boot.
    return dlna_localfs_wiring.AUDIOBOOKS_UDN


@router.get("/api/book_language", include_in_schema=False)
async def book_language_get_route(request: Request):
    """{book: false} for anything that is not a chapter of a folder book,
    so the edit form hides the field rather than offering a choice the
    server would refuse."""
    url = request.query_params.get("url", "")
    if not url:
        return JSONResponse({"error": "missing url"}, status_code=400)
    info = await run_in_threadpool(_st.DB.book_language_of, url, _books_udn())
    return JSONResponse({"book": True, **info} if info else {"book": False})


@router.post("/api/book_language", include_in_schema=False)
async def book_language_set_route(request: Request):
    """Set a book's language by hand (`manual`, which always wins), or
    hand it back to detection with an empty `language`. The book is
    resolved from the chapter URL server-side; a client never names the
    album_key it may write."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or not body.get("url") \
            or "language" not in body:
        return JSONResponse({"error": "missing url or language"},
                            status_code=400)
    url, lang = body["url"], str(body.get("language") or "").strip()
    db, udn = _st.DB, _books_udn()

    def _apply():
        info = db.book_language_of(url, udn)
        if not info:
            return None, "not a book"
        ok = (db.book_language_clear(info["album_key"]) or True) if not lang \
            else db.book_language_set(info["album_key"], lang)
        return (db.book_language_of(url, udn), None) if ok \
            else (None, "unknown language")

    info, err = await run_in_threadpool(_apply)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    return JSONResponse({"ok": True, **info})
