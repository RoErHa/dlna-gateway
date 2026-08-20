#!/usr/bin/env python3
"""
api_upnp_ids.py — gateway UPnP identity, ObjectID codecs, the
junk-name display filter, and the LibraryDB reads the browse tree needs.

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

⚠ `DB` IS BOUND HERE, ONCE, FOR THE WHOLE api_upnp FAMILY.
Sibling modules reach it as `_ids.DB` — an attribute lookup resolved at CALL
time — rather than `from api_upnp_ids import DB`, which would snapshot the
object at import and make the binding un-patchable. Tests therefore inject a
temp library with:

    patch.object(api_upnp_ids, "DB", my_test_db)

and that one patch covers browse, video browse and the SOAP layer. Before the
split the target was `api_upnp.DB`; binding it in more than one module would
have left the others silently pointed at the REAL library.db, which is a
false pass rather than a failure — much worse than a crash.

The ObjectID codecs all base64-urlsafe a NUL-joined tuple, which is what lets
arbitrary unicode (and '&', '/', non-ASCII) round-trip through SOAP/XML
unharmed. Three distinct namespaces share the scheme deliberately —
`galbum:` (library album), `favalbum:` (favourite) and `abbook:` (audiobook)
must not be interchangeable. A garbled id decodes to empty strings rather
than raising, so a stale bookmark on the Naim yields an empty container
instead of a SOAP fault that aborts the whole browse.
"""
import base64
import logging
import os
import re
import socket

# ⚠ THE single DB binding for the whole api_upnp family — see the module
# docstring. Siblings use `_ids.DB` (resolved at call time) so this one
# object is what a test patch actually reaches.
from dlna_library import DB

log = logging.getLogger("dlna.api.upnp")


# ── Gateway UPnP identity ─────────────────────────────────────────
# Env-overridable so a side-by-side 2.0 instance announces a DISTINCT
# MediaServer (different UDN + friendly name) on the LAN — otherwise the
# Naim sees two servers with the same identity. Defaults keep 1.x behaviour.
GW_UDN  = os.environ.get("GW_UDN",  "uuid:dlna-gateway-iina-8765")


GW_NAME = os.environ.get("GW_NAME", "DLNA Gateway (IINA)")


def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError as e:
        # Falling back here means we may advertise a loopback/stale IP over
        # SSDP, which no renderer can reach — never fail this silently.
        log.warning(f"LAN IP probe failed ({e}) — falling back to hostname "
                    f"resolution; SSDP may advertise an unreachable address")
        return socket.gethostbyname(socket.gethostname())


def _xml_esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;") \
                    .replace(">", "&gt;").replace('"', "&quot;")


# Video library udn (mirrors dlna_localfs_wiring.VIDEO_UDN). Videos are exposed
# as a "📹 Videos" folder so a TV (LG) can browse + play them; the Naim sees the
# folder but is audio-only, so the user just doesn't open it.
_VIDEO_UDN = "uuid:localfs-movies"


def _fmt_duration(sec) -> str:
    """Seconds → UPnP res 'H:MM:SS' (empty when unknown)."""
    try:
        sec = int(float(sec))
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(max(sec, 0), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


# ── Album-favourite ObjectID encoding ────────────────────────────
# UPnP ObjectIDs travel through SOAP XML and back; an artist or album
# can contain any unicode character (incl. quotes, ampersands, NULs).
# Base64-urlsafe of "artist\x00album" is unambiguous and round-trips
# cleanly through XML escaping.

def _b64e(s: str) -> str:
    """URL-safe base64 of a single string (artist / genre names) for use in a
    UPnP ObjectID — round-trips arbitrary unicode through SOAP/XML."""
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(s: str) -> str:
    s += "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _encode_lib_album_id(artist: str, album: str, album_key: str = "") -> str:
    """galbum:* ObjectID for a full-library album (distinct from favalbum:* —
    a galbum resolves via the primary library udn, not the favourites row)."""
    raw = f"{artist}\x00{album}\x00{album_key}".encode()
    return "galbum:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _ab_udn() -> str:
    """The audiobooks LocalFs UDN ('' when the feature is off). Read
    dynamically — dlna_localfs_wiring sets it during boot."""
    try:
        import dlna_localfs_wiring
        return dlna_localfs_wiring.AUDIOBOOKS_UDN
    except ImportError:
        return ""


def _encode_ab_book_id(artist: str, album: str, album_key: str = "") -> str:
    """abbook:* ObjectID — same 3-field payload as galbum:*, but resolves
    against the AUDIOBOOKS udn, not the music library."""
    raw = f"{artist}\x00{album}\x00{album_key}".encode()
    return "abbook:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_ab_book_id(obj_id: str) -> tuple:
    payload = obj_id[len("abbook:"):]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ("", "", "")
    parts = raw.split("\x00")
    return (parts[0] if len(parts) > 0 else "",
            parts[1] if len(parts) > 1 else "",
            parts[2] if len(parts) > 2 else "")


def _decode_lib_album_id(obj_id: str) -> tuple:
    """Return (artist, album, album_key) from a galbum:* id; ('', '', '') on junk."""
    payload = obj_id[len("galbum:"):]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ("", "", "")
    parts = raw.split("\x00")
    return (parts[0] if len(parts) > 0 else "",
            parts[1] if len(parts) > 1 else "",
            parts[2] if len(parts) > 2 else "")


# ── Junk-name filter (untagged / track-number-as-name) ───────────
# The library carries filename-derived metadata gaps (artist "07", album
# "10. Some Title …", or a blank album name). These clutter the Naim browse,
# so the gateway-as-MediaServer tree hides them (the PWA still shows the raw
# data; beets enrichment fixes the tags over time). DISPLAY-only filter.
_JUNK_PREFIX_RE = re.compile(r'^\d+\s*[.):\-]')   # "10.", "1)", "07 -", "3:"


def _is_junk_name(s: str) -> bool:
    s = (s or "").strip()
    if not s:                       # blank → "(album)" / unnamed
        return True
    if _JUNK_PREFIX_RE.match(s):    # track-number-as-name
        return True
    if re.fullmatch(r'\d{1,2}', s): # bare 1–2 digit number ("07")
        return True
    return False


def _lib_artists(udn: str) -> list:
    return [r for r in DB.all_artists(udn) if not _is_junk_name(r.get("artist"))]


def _lib_albums(udn: str) -> list:
    return [r for r in DB.all_albums(udn) if not _is_junk_name(r.get("album"))]


def _lib_genres(udn: str) -> list:
    return [r for r in DB.all_genres(udn) if not _is_junk_name(r.get("genre"))]


def _letter_of(name: str) -> str:
    """First-letter bucket matching LibraryDB.browse_letter / the PWA letter
    bar: 'A'..'Z', '0' for a leading digit, '#' for anything else."""
    s = (name or "").strip()
    if not s:
        return "#"
    c = s[0].upper()
    if "A" <= c <= "Z":
        return c
    if "0" <= c <= "9":
        return "0"
    return "#"


_LETTER_ORDER = ["#", "0"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]


def _album_letters(udn: str) -> list:
    """Ordered (letter, count) for the non-empty album-letter buckets
    (junk-filtered), so the Naim's 'Albums' folder is a #-0-A..Z index
    rather than one flat 2,000-entry list."""
    buckets: dict = {}
    for r in _lib_albums(udn):
        L = _letter_of(r.get("album"))
        buckets[L] = buckets.get(L, 0) + 1
    return [(L, buckets[L]) for L in _LETTER_ORDER if L in buckets]


def _encode_album_id(artist: str, album: str, album_key: str = "") -> str:
    # NUL-delimited (artist, album, album_key). album_key (LocalFs folder
    # identity) lets a Various-Artists compilation round-trip as one album;
    # empty for (artist, album)-keyed favourites.
    raw = f"{artist}\x00{album}\x00{album_key}".encode()
    return "favalbum:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_album_id(obj_id: str) -> tuple:
    """Return (artist, album, album_key). Tolerates legacy 2-field ids
    (no album_key) by defaulting album_key to ''."""
    if not obj_id.startswith("favalbum:"):
        return ("", "", "")
    payload = obj_id[len("favalbum:"):]
    payload += "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ("", "", "")
    parts = raw.split("\x00")
    artist    = parts[0] if len(parts) > 0 else ""
    album     = parts[1] if len(parts) > 1 else ""
    album_key = parts[2] if len(parts) > 2 else ""
    return (artist, album, album_key)
