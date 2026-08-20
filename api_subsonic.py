#!/usr/bin/env python3
"""
api_subsonic.py — Subsonic-compatible API (/rest/*): the method table,
parameter parsing, the single `handle()` entry point, and the public face
of the api_subsonic module family.

Lets any third-party Subsonic iOS client (Amperfy, substreamer, play:Sub)
browse and stream the gateway's library. THE PRIMARY MOTIVATOR IS CARPLAY:
those clients have polished CarPlay implementations that the PWA
fundamentally cannot match (CarPlay is a closed iOS-native framework).
Subsonic is also plain HTTP, so it traverses Tailscale cleanly — unlike
UPnP's SSDP multicast discovery.

── Module family ────────────────────────────────────────────────────
This file was 1,174 lines until 2026-08-20. It is now the dispatcher plus
re-exports; everything else moved to a sibling:

    api_subsonic_proto.py      auth + response wrapping + the XML serialiser
    api_subsonic_ids.py        id codecs, udn resolution, object builders
    api_subsonic_browse.py     ping/artists/albums/search/genres
    api_subsonic_playlists.py  playlists, starring, scrobble
    api_subsonic_media.py      stream + cover art (the byte endpoints)
    api_subsonic_extras.py     internet radio + audiobook bookmarks

Every public name is re-exported below, so `import api_subsonic` and
`api_subsonic.<anything>` behave exactly as before.

⚠ `DB`, `SERVERS` and `SUBSONIC_PASSWORD_OVERRIDE` are bound ONCE, in
api_subsonic_proto. Inject there — patching `api_subsonic.DB` only rebinds
this module's re-export and leaves the handlers on the real library.db.

Adding a method: write the handler in the matching sibling, then add one
line to `_METHODS`. Anything absent from that table answers with a proper
"not implemented" fault rather than a 500 — about 45 of the spec's 60+
endpoints are deliberately out of scope (multi-user, podcasts, jukebox,
transcoding, chat, shares).
"""
import logging
import urllib.parse
from collections.abc import Callable

# ── Re-exports: the family's public surface ──────────────────────────
from api_subsonic_browse import (  # noqa: F401
    _get_album,
    _get_album_list2,
    _get_artist,
    _get_artists,
    _get_genres,
    _get_indexes,
    _get_license,
    _get_music_folders,
    _get_open_subsonic_extensions,
    _get_scan_status,
    _get_starred,
    _get_user,
    _ping,
    _search3,
)
from api_subsonic_extras import (  # noqa: F401
    _create_bookmark,
    _create_internet_radio_station,
    _delete_bookmark,
    _delete_internet_radio_station,
    _get_bookmarks,
    _get_internet_radio_stations,
    _so_radio,
    _update_internet_radio_station,
)
from api_subsonic_ids import (  # noqa: F401
    _album_id,
    _album_id_decode,
    _artist_id,
    _artist_id_decode,
    _dec,
    _default_udn,
    _enc,
    _int_param,
    _iso,
    _radio_id,
    _radio_id_decode,
    _so_album,
    _so_artist,
    _so_song,
    _track_id,
    _track_id_decode,
)
from api_subsonic_media import (  # noqa: F401
    _cover_art_candidates,
    _cover_art_url,
    _get_cover_art,
    _resolve_cover,
    _stream,
)
from api_subsonic_playlists import (  # noqa: F401
    _create_playlist,
    _delete_playlist,
    _get_playlist,
    _get_playlists,
    _get_starred2,
    _scrobble,
    _star,
    _unstar,
    _update_playlist,
)
from api_subsonic_proto import (  # noqa: F401
    API_VERSION,
    DB,
    ERR_GENERIC,
    ERR_MISSING_PARAM,
    ERR_NOT_AUTHORIZED,
    ERR_NOT_FOUND,
    ERR_TOKEN_AUTH_NA,
    ERR_VERSION_INCOMPAT,
    ERR_WRONG_AUTH,
    SERVER_TYPE,
    SERVER_VERSION,
    SERVERS,
    SUBSONIC_USER_DEFAULT,
    _check_auth,
    _fail,
    _ok,
    _send_response,
    _subsonic_password,
    _subsonic_user,
    _to_xml,
    _to_xml_doc,
    _wrap,
    _wrap_error,
    _xml_escape,
    _xml_scalar,
)

log = logging.getLogger("dlna.api.subsonic")


def __getattr__(name):
    """`SUBSONIC_PASSWORD_OVERRIDE` lives in api_subsonic_proto, but reading
    it as `api_subsonic.SUBSONIC_PASSWORD_OVERRIDE` must still see the
    CURRENT value rather than an import-time snapshot. PEP 562 module
    __getattr__ forwards the read. (Writing it here would only shadow this
    module's attribute — set it on api_subsonic_proto.)"""
    if name == "SUBSONIC_PASSWORD_OVERRIDE":
        import api_subsonic_proto
        return api_subsonic_proto.SUBSONIC_PASSWORD_OVERRIDE
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── Dispatcher ───────────────────────────────────────────────────────

_METHODS: dict[str, Callable] = {
    "ping":             _ping,
    "getLicense":       _get_license,
    "getMusicFolders":  _get_music_folders,
    "getIndexes":       _get_indexes,
    "getArtists":       _get_artists,
    "getArtist":        _get_artist,
    "getAlbum":         _get_album,
    "getAlbumList2":    _get_album_list2,
    "search3":          _search3,
    "getPlaylists":     _get_playlists,
    "getPlaylist":      _get_playlist,
    "createPlaylist":   _create_playlist,
    "updatePlaylist":   _update_playlist,
    "deletePlaylist":   _delete_playlist,
    "star":             _star,
    "unstar":           _unstar,
    "getStarred":       _get_starred,
    "getStarred2":      _get_starred2,
    "stream":           _stream,
    "download":         _stream,   # treat download == stream
    "getCoverArt":      _get_cover_art,
    "scrobble":         _scrobble,
    "getUser":          _get_user,
    "getInternetRadioStations":   _get_internet_radio_stations,
    "createInternetRadioStation": _create_internet_radio_station,
    "updateInternetRadioStation": _update_internet_radio_station,
    "deleteInternetRadioStation": _delete_internet_radio_station,
    "getBookmarks":     _get_bookmarks,
    "createBookmark":   _create_bookmark,
    "deleteBookmark":   _delete_bookmark,
    "getGenres":        _get_genres,
    "getScanStatus":    _get_scan_status,
    "getOpenSubsonicExtensions": _get_open_subsonic_extensions,
}


def _method_from_path(path: str) -> str:
    """`/rest/ping` → 'ping'. `/rest/ping.view` → 'ping' (legacy form
    some old clients still send)."""
    tail = path[len("/rest/"):] if path.startswith("/rest/") else path
    if tail.endswith(".view"):
        tail = tail[:-5]
    return tail


def _parse_params(query, body: bytes = b"") -> dict:
    """Build the handler param dict from a raw query string (+ optional
    form-encoded POST body). Subsonic clients repeat params for
    multi-value fields (songId, id, …); parse_qsl into a dict keeps only
    the LAST value, so every key's full value list is also surfaced
    under a `<name>__all` key for handlers that need to iterate. Body
    params override / extend the query string.

    `query` may also be a dict — the test driver calls `handle()`
    directly with an already-built param dict; it's used as-is and only
    the `__all` mirror keys are (re)derived."""
    if isinstance(query, dict):
        pairs = [(k, v) for k, v in query.items() if not k.endswith("__all")]
    else:
        pairs = urllib.parse.parse_qsl(query)
        if body:
            try:
                pairs += urllib.parse.parse_qsl(
                    body.decode("utf-8", errors="replace"))
            except (ValueError, AttributeError) as e:
                log.debug(f"Subsonic: unparseable POST body ignored ({e})")
    params: dict = {}
    multi: dict[str, list[str]] = {}
    for k, v in pairs:
        params[k] = v
        multi.setdefault(k, []).append(v)
    for k, vs in multi.items():
        params[f"{k}__all"] = vs
    return params


def handle(h, http_method: str, path: str, query, body: bytes = b""):
    """Single entry point — called from the ASGI app (via the bridge) when
    the path starts with /rest/. Parses params, authenticates, dispatches to
    the per-method handler. `query` is the raw query string in production;
    tests may pass a pre-built param dict."""
    query_params = _parse_params(query, body)
    method = _method_from_path(path)
    client = query_params.get("c", "?")
    fmt_raw = (query_params.get("f", "") or "").lower()
    # Stash the format choice on the handler so _send_response can read
    # it from any deeper handler. Spec default is XML; clients (Amperfy)
    # really do rely on that default. JSON-aware clients send f=json.
    h._subsonic_format = "json" if fmt_raw in ("json", "jsonp") else "xml"
    log.debug(f"Subsonic {http_method} {method!r} client={client!r} "
              f"u={query_params.get('u', '')!r} f={fmt_raw or '<unset>'} "
              f"→ resp={h._subsonic_format}")
    if not method:
        _fail(h, ERR_NOT_FOUND, "missing method", http_code=404)
        return

    fn = _METHODS.get(method)
    if fn is None:
        log.debug(f"Subsonic: unimplemented method {method!r}")
        _fail(h, ERR_NOT_FOUND,
              f"Method not implemented: {method}", http_code=404)
        return

    if not _subsonic_password():
        # Distinct from auth-failure: deliberate refuse-all when env
        # not set so a misconfigured deploy doesn't accidentally
        # expose data with an empty-password match.
        log.warning("Subsonic call rejected — SUBSONIC_PASSWORD env not set")
        _fail(h, ERR_NOT_AUTHORIZED,
              "Server not configured (SUBSONIC_PASSWORD unset)",
              http_code=503)
        return

    if not _check_auth(query_params):
        log.info(f"Subsonic auth failed for user={query_params.get('u', '')!r} "
                 f"method={method}")
        _fail(h, ERR_WRONG_AUTH, "Wrong username or password")
        return

    try:
        fn(h, query_params)
    except (BrokenPipeError, ConnectionResetError) as e:
        # Client closed the connection mid-response (Amperfy and other
        # mobile clients do this aggressively — back-button, app
        # switcher, network handover). Not a server bug; just noise.
        log.debug(f"Subsonic {method}: client disconnected ({e})")
    except Exception as e:
        log.exception(f"Subsonic {method} crashed: {e}")
        _fail(h, ERR_GENERIC, f"Server error: {e}")
