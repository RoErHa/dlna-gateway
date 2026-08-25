#!/usr/bin/env python3
"""
api_upnp_browse.py — ContentDirectory Browse: the music, audiobook,
playlist and favourite-album handlers, plus the dispatch tables and
`_gw_browse` itself.

Split out of api_upnp.py on 2026-08-20, when that module reached
1,349 lines. The family is:

    api_upnp_ids.py          identity, id codecs, junk filter, library reads
    api_upnp_didl.py         DIDL-Lite renderers + the _Browse request context
    api_upnp_browse.py       music/books/playlists/favourites handlers + dispatch
    api_upnp_browse_video.py the GWMovies video tree handlers
    api_upnp_descriptors.py  device.xml + the two service SCPDs
    api_upnp_ssdp.py         SSDP announce/M-SEARCH + GENA eventing
    api_upnp.py              SOAP control endpoints + the public re-exports

api_upnp re-exports every public name, so `import api_upnp` and
`api_upnp.<anything>` keep working for callers and tests.

Browse is a TABLE, not a chain of `if obj_id ==` branches (it was 26 of them
in a single 491-line function until 2026-08-20). Adding a container is one
`_br_*` handler plus one entry in `_BROWSE_EXACT` or `_BROWSE_PREFIX`.

The two tables are disjoint by construction — every prefix ends in ':' and no
exact id contains one — which is what stops "vidlocs" being swallowed by
"vidloc:" and "favalbums" by "favalbum:".
`tests/test_upnp_browse_dispatch.py` enforces that, plus reachability of
every handler.
"""
import logging

import api_upnp_ids as _ids
from api_upnp_browse_video import (
    _br_vid,
    _br_vidall,
    _br_vidcloc,
    _br_vidcountry,
    _br_viddate,
    _br_viddates,
    _br_videos,
    _br_vidloc,
    _br_vidlocs,
    _br_vidpeople,
    _br_vidperson,
)
from api_upnp_didl import _Browse, _didl_album, _didl_container, _didl_track
from api_upnp_ids import (
    GW_NAME,
    _VIDEO_UDN,
    _ab_udn,
    _album_letters,
    _b64d,
    _b64e,
    _decode_ab_book_id,
    _decode_album_id,
    _decode_lib_album_id,
    _encode_ab_book_id,
    _encode_album_id,
    _is_junk_name,
    _letter_of,
    _lib_albums,
    _lib_artists,
    _lib_genres,
)

log = logging.getLogger("dlna.api.upnp")


# ── Handlers: root ────────────────────────────────────────────────

def _br_root(ctx: _Browse) -> tuple:
    n_videos = len(_ids.DB.all_videos(_VIDEO_UDN))
    ab_udn   = _ab_udn()
    n_books  = len(_lib_artists(ab_udn)) if ab_udn else 0
    if ctx.is_meta:
        return ctx.meta("-1", GW_NAME,
                        5 + (1 if n_videos else 0) + (1 if n_books else 0))
    udn       = _ids.DB.primary_udn()
    n_artists = len(_lib_artists(udn))   if udn else 0
    n_albums  = len(_album_letters(udn)) if udn else 0   # # of letter buckets
    n_genres  = len(_lib_genres(udn))    if udn else 0
    items = [
        _didl_container("artists",   "0", "Artists",            n_artists),
        _didl_container("albums",    "0", "Albums",             n_albums),
        _didl_container("genres",    "0", "Genres",             n_genres),
        _didl_container("favalbums", "0", "⭐ Favourite Albums", len(_ids.DB.album_fav_list())),
        _didl_container("playlists", "0", "Playlists",          len(_ids.DB.pl_list())),
    ]
    # Videos folder — only when there ARE videos (so it never clutters the
    # Naim's view unless GWMovies is enabled + populated).
    if n_videos:
        items.append(_didl_container("videos", "0", "\U0001F4F9 Videos", n_videos))
    # Audiobooks — only when the audiobooks source exists + has authors
    # (P5). Authors → books → chapters, resolved via the AB udn.
    if n_books:
        items.append(_didl_container("abooks", "0", "\U0001F4D6 Audiobooks", n_books))
    return ctx.listing(items, len(items))


# ── Handlers: audiobooks tree (P5) — abooks → authors → books → chapters ──

def _br_abooks(ctx: _Browse) -> tuple:
    ab_udn = _ab_udn()
    rows   = _lib_artists(ab_udn) if ab_udn else []
    if ctx.is_meta:
        return ctx.meta("0", "\U0001F4D6 Audiobooks", len(rows))
    items = [_didl_container("abauthor:" + _b64e(r["artist"]), "abooks",
                             r["artist"], r.get("album_count", 0))
             for r in ctx.page(rows)]
    return ctx.listing(items, len(rows))


def _br_abauthor(ctx: _Browse) -> tuple:
    author = _b64d(ctx.obj_id[len("abauthor:"):])
    ab_udn = _ab_udn()
    rows   = [r for r in _ids.DB.artist_albums(ab_udn, author)
              if not _is_junk_name(r.get("album"))] if ab_udn else []
    if ctx.is_meta:
        return ctx.meta("abooks", author or "(author)", len(rows))
    items = []
    for r in ctx.page(rows):
        cid   = _encode_ab_book_id(r.get("artist", ""), r.get("album", ""),
                                   r.get("album_key", ""))
        title = r.get("album", "") or "(book)"
        # Series overlay when OpenLibrary knows the book.
        meta = _ids.DB.book_meta_get(r.get("album_key", "")) \
            if r.get("album_key") else None
        if meta and meta.get("series"):
            seq = meta.get("series_seq")
            seq_s = f" #{seq:g}" if seq is not None else ""
            title = f"{title}  \U0001F4DA {meta['series']}{seq_s}"
        items.append(_didl_container(cid, ctx.obj_id, title,
                                     r.get("track_count", 0)))
    return ctx.listing(items, len(rows))


def _br_abbook(ctx: _Browse) -> tuple:
    artist, album, album_key = _decode_ab_book_id(ctx.obj_id)
    ab_udn = _ab_udn()
    tracks = _ids.DB.album_tracks(ab_udn, artist, album, album_key=album_key) \
        if ab_udn and (artist or album or album_key) else []
    if ctx.is_meta:
        return ctx.meta("abooks", album or "(book)", len(tracks))
    items = [_didl_track(t, ctx.obj_id) for t in ctx.page(tracks)]
    return ctx.listing(items, len(tracks))


# ── Handlers: full-library tree (Artists / Albums / Genres) ────────
# Backed by LibraryDB on the primary library udn (the LocalFs backend).
# Each list paginates via StartingIndex/RequestedCount; album rows carry
# album_key so a LocalFs folder-album (incl. Various-Artists comps)
# resolves correctly through album_tracks.

def _br_artists(ctx: _Browse) -> tuple:
    udn  = _ids.DB.primary_udn()
    rows = _lib_artists(udn) if udn else []
    if ctx.is_meta:
        return ctx.meta("0", "Artists", len(rows))
    items = [_didl_container("gartist:" + _b64e(r["artist"]), "artists",
                             r["artist"], r.get("album_count", 0))
             for r in ctx.page(rows)]
    return ctx.listing(items, len(rows))


def _artist_rows(artist: str):
    """`(their own records, the compilations they appear on)`."""
    udn = _ids.DB.primary_udn()
    rows = [r for r in _ids.DB.artist_albums(udn, artist)
            if not _is_junk_name(r.get("album"))] if udn else []
    return ([r for r in rows if r.get("own", True)],
            [r for r in rows if not r.get("own", True)])


def _br_gartist(ctx: _Browse) -> tuple:
    """Their own records, plus ONE container for everything they merely
    appear on — UPnP has no dividers, so a sub-container is the honest
    equivalent of the PWA's fold. See CLAUDE.md, "Appears on"."""
    artist = _b64d(ctx.obj_id[len("gartist:"):])
    own, appears = _artist_rows(artist)
    if not own:
        # Nothing to bury: an artist you own only via compilations would
        # get a page holding one container, a tap from their only music.
        own, appears = appears, []
    items = list(own)
    if appears:
        items.append({"__appears__": True, "artist": artist,
                      "count": len(appears)})
    if ctx.is_meta:
        return ctx.meta("artists", artist or "(artist)", len(items))
    didl = [_didl_container("gappears:" + _b64e(r["artist"]), ctx.obj_id,
                            f"Appears on ({r['count']})", r["count"])
            if r.get("__appears__") else _didl_album(r, ctx.obj_id)
            for r in ctx.page(items)]
    return ctx.listing(didl, len(items))


def _br_gappears(ctx: _Browse) -> tuple:
    """The compilations an artist appears on. Each still resolves to only
    their tracks — the album id carries the performer."""
    artist = _b64d(ctx.obj_id[len("gappears:"):])
    _, appears = _artist_rows(artist)
    if ctx.is_meta:
        return ctx.meta("gartist:" + _b64e(artist),
                        f"Appears on ({len(appears)})", len(appears))
    items = [_didl_album(r, ctx.obj_id) for r in ctx.page(appears)]
    return ctx.listing(items, len(appears))


def _br_albums(ctx: _Browse) -> tuple:
    """"Albums" is a #-0-A..Z letter index (not one flat 2,000-entry list)."""
    udn     = _ids.DB.primary_udn()
    letters = _album_letters(udn) if udn else []
    if ctx.is_meta:
        return ctx.meta("0", "Albums", len(letters))
    items = [_didl_container("albumltr:" + L, "albums", L, cnt)
             for L, cnt in ctx.page(letters)]
    return ctx.listing(items, len(letters))


def _br_albumltr(ctx: _Browse) -> tuple:
    letter = ctx.obj_id[len("albumltr:"):]
    udn    = _ids.DB.primary_udn()
    rows   = [r for r in _lib_albums(udn)
              if _letter_of(r.get("album")) == letter] if udn else []
    if ctx.is_meta:
        return ctx.meta("albums", letter, len(rows))
    items = [_didl_album(r, ctx.obj_id) for r in ctx.page(rows)]
    return ctx.listing(items, len(rows))


def _br_galbum(ctx: _Browse) -> tuple:
    artist, album, album_key = _decode_lib_album_id(ctx.obj_id)
    udn    = _ids.DB.primary_udn()
    tracks = _ids.DB.album_tracks(udn, artist, album, album_key=album_key) if udn else []
    if ctx.is_meta:
        return ctx.meta("albums", album or "(album)", len(tracks))
    items = [_didl_track(t, ctx.obj_id) for t in ctx.page(tracks)]
    return ctx.listing(items, len(tracks))


def _br_genres(ctx: _Browse) -> tuple:
    udn  = _ids.DB.primary_udn()
    rows = _lib_genres(udn) if udn else []
    if ctx.is_meta:
        return ctx.meta("0", "Genres", len(rows))
    items = [_didl_container("ggenre:" + _b64e(r["genre"]), "genres",
                             r["genre"], r.get("album_count", 0))
             for r in ctx.page(rows)]
    return ctx.listing(items, len(rows))


def _br_ggenre(ctx: _Browse) -> tuple:
    genre = _b64d(ctx.obj_id[len("ggenre:"):])
    udn   = _ids.DB.primary_udn()
    rows  = [r for r in _ids.DB.genre_albums(udn, genre)
             if not _is_junk_name(r.get("album"))] if udn else []
    if ctx.is_meta:
        return ctx.meta("genres", genre or "(genre)", len(rows))
    items = [_didl_album(r, ctx.obj_id) for r in ctx.page(rows)]
    return ctx.listing(items, len(rows))


# ── Handlers: playlists + favourite albums ────────────────────────

def _br_playlists(ctx: _Browse) -> tuple:
    pls = _ids.DB.pl_list()
    if ctx.is_meta:
        return ctx.meta("0", "Playlists", len(pls))
    items = [_didl_container(f"pl:{p['id']}", "playlists", p["name"], p["count"])
             for p in ctx.page(pls)]
    return ctx.listing(items, len(pls))


def _br_playlist(ctx: _Browse) -> tuple:
    pl = _ids.DB.pl_get(ctx.obj_id[3:])
    if not pl:
        return ctx.empty()
    tracks = pl["tracks"]
    if ctx.is_meta:
        return ctx.meta("playlists", pl["name"], len(tracks))
    items = [_didl_track(t, ctx.obj_id) for t in ctx.page(tracks)]
    return ctx.listing(items, len(tracks))


def _br_favalbums(ctx: _Browse) -> tuple:
    favs = _ids.DB.album_fav_list()
    if ctx.is_meta:
        return ctx.meta("0", "⭐ Favourite Albums", len(favs))
    items = [_didl_container(_encode_album_id(f["artist"], f["album"],
                                              f.get("album_key", "")),
                             "favalbums",
                             f"{f['album']} — {f['artist']}" if f["artist"]
                                                             else f["album"],
                             f["track_count"])
             for f in ctx.page(favs)]
    return ctx.listing(items, len(favs))


def _br_favalbum(ctx: _Browse) -> tuple:
    artist, album, album_key = _decode_album_id(ctx.obj_id)
    # Resolve the udn lazily — the favourite is keyed by album_key
    # (LocalFs folder) when present, else (artist, album), not by
    # server. If the album isn't in any indexed library we silently
    # return an empty container rather than 500 — a Naim control point
    # handles "0 results" gracefully.
    if album_key:
        fav = next((f for f in _ids.DB.album_fav_list()
                    if f.get("album_key") == album_key), None)
    else:
        fav = next((f for f in _ids.DB.album_fav_list()
                    if f["artist"] == artist and f["album"] == album
                    and not f.get("album_key")), None)
    if not fav or not fav["udn"]:
        return ctx.empty()
    tracks = _ids.DB.album_tracks(fav["udn"], artist, album, album_key=album_key)
    if ctx.is_meta:
        return ctx.meta("favalbums", album or "(album)", len(tracks))
    items = [_didl_track(t, ctx.obj_id) for t in ctx.page(tracks)]
    return ctx.listing(items, len(tracks))


# ── Dispatch ──────────────────────────────────────────────────────
# Exact ids are matched first, then prefixes. The two sets are disjoint
# by construction — every prefix ends in ':' and no exact id contains one
# — so "vidlocs" can never be captured by "vidloc:", and "favalbums"
# can never be captured by "favalbum:".

_BROWSE_EXACT = {
    "0":               _br_root,
    "abooks":          _br_abooks,
    "artists":         _br_artists,
    "albums":          _br_albums,
    "genres":          _br_genres,
    "videos":          _br_videos,
    "vidall":          _br_vidall,

    "viddates":        _br_viddates,
    "vidlocs":         _br_vidlocs,
    "vidpeople":       _br_vidpeople,
    "vidcountry-none": _br_vidcountry,
    "vidloc-none":     _br_vidloc,
    "playlists":       _br_playlists,
    "favalbums":       _br_favalbums,
}


_BROWSE_PREFIX = (
    ("abauthor:",   _br_abauthor),
    ("abbook:",     _br_abbook),
    ("gartist:",    _br_gartist),
    ("gappears:",   _br_gappears),
    ("albumltr:",   _br_albumltr),
    ("galbum:",     _br_galbum),
    ("ggenre:",     _br_ggenre),
    ("viddate:",    _br_viddate),
    ("vidcountry:", _br_vidcountry),
    ("vidcloc:",    _br_vidcloc),
    ("vidperson:",  _br_vidperson),
    ("vidloc:",     _br_vidloc),
    ("vid:",        _br_vid),
    ("pl:",         _br_playlist),
    ("favalbum:",   _br_favalbum),
)


def _gw_browse(obj_id: str, browse_flag: str,
               start: int, count: int) -> tuple:
    """Returns (DIDL-Lite XML, number_returned, total_matches)."""
    ctx = _Browse(obj_id, browse_flag, start, count)
    handler = _BROWSE_EXACT.get(obj_id)
    if handler is None:
        handler = next((h for p, h in _BROWSE_PREFIX if obj_id.startswith(p)),
                       None)
    if handler is None:
        return ctx.empty()
    return handler(ctx)


def _gw_browse_response(result_xml: str, n_returned: int,
                        total: int, update_id: int = 1) -> bytes:
    escaped = (result_xml.replace("&", "&amp;")
                         .replace("<", "&lt;")
                         .replace(">", "&gt;"))
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
        ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        '<u:BrowseResponse xmlns:u="urn:schemas-upnp-org:service:ContentDirectory:1">'
        f'<Result>{escaped}</Result>'
        f'<NumberReturned>{n_returned}</NumberReturned>'
        f'<TotalMatches>{total}</TotalMatches>'
        f'<UpdateID>{update_id}</UpdateID>'
        '</u:BrowseResponse>'
        '</s:Body></s:Envelope>'
    )
    return body.encode("utf-8")
