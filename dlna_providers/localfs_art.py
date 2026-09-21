#!/usr/bin/env python3
"""
dlna_providers/localfs_art.py — album art for a local file: the picture
EMBEDDED in it, and the one sitting BESIDE it in the folder.

Split out of `localfs_tags.py` on 2026-09-21, which crossed its 400-line
budget when folder-cover support landed. The seam is real: everything
left there answers "what does this file SAY about itself" from its tags,
while this answers "what does this album LOOK like" — a different
question, with different failure modes, and the only half that touches
sibling files.

`_extract_art_bytes` is the single point BOTH the scanner and the file
server call, which is why the folder fallback lives inside it: the
indexer gets a marker and `/localfs/art/<id>` serves the bytes, with no
new route, no schema change and no URL change.

⚠ `_sniff_image_mime` ignores the DECLARED mime and reads magic bytes,
which is load-bearing security rather than tidiness — see its docstring
and tests/test_art_safety.py. Do not "simplify" it to trust the
container, and do not add image/svg+xml.
"""
from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from dlna_providers.localfs_tags import _DISC_SUBDIR_RE

log = logging.getLogger("dlna.provider.localfs")


def _sniff_image_mime(data: bytes) -> str:
    """Best-effort image MIME from magic bytes. Embedded-art metadata
    sometimes lies about (or omits) its MIME, so sniff the bytes rather
    than trust the container. Falls back to image/jpeg.

    SECURITY — this is load-bearing, do not "improve" it into trusting the
    declared MIME, and do not add image/svg+xml. A media file is untrusted
    input, and its embedded cover is the one part of it we hand to a browser:

      * ID3 `APIC` has a documented phone-home form — MIME `-->` means the
        picture payload is a URL, not image bytes. Because we sniff instead
        of trusting, that payload is served as opaque bytes and NEVER
        dereferenced, so a crafted file cannot make the gateway (or a
        viewer) fetch an attacker's URL.
      * An SVG "cover" would be script-capable markup. Falling back to
        image/jpeg means the browser refuses to parse it as SVG, so a cover
        cannot become an XSS or a tracking beacon.

    The allowlist below is deliberately raster-only for that reason: anything
    unrecognised is labelled image/jpeg and renders as a broken image, which
    is the correct failure. Guarded by tests/test_art_safety.py."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _extract_art_bytes(path: Path) -> tuple[bytes, str] | None:
    """Return `(picture_bytes, mime)` for the first embedded cover, or
    None if the file has no embedded art / can't be read. Single source
    of cover bytes: used by `_extract_art_hash` (stable marker at scan
    time) AND the file server's `/localfs/art/<id>` route (serve on
    demand). All mutagen access is funnelled here so tests can mock it."""
    import mutagen
    try:
        audio = mutagen.File(str(path))
    except Exception as e:                                    # mutagen raises broadly
        # A file mutagen cannot open is skipped from the index entirely, so
        # "my album is missing" traces back to here. 163 audiobook files hit
        # this on the first AUDIOBOOKS_ROOT scan.
        log.debug(f"mutagen could not read {path}: {type(e).__name__}: {e}")
        # A file mutagen cannot open has no embedded art BY DEFINITION,
        # which is precisely when the folder cover is the only one
        # there — so fall through rather than giving up.
        return _read_folder_art(path)
    if audio is None:
        return _read_folder_art(path)
    art_bytes: bytes | None = None
    # FLAC: .pictures
    pics = getattr(audio, "pictures", None)
    if pics:
        art_bytes = pics[0].data
    # ID3 (MP3): tags.getall('APIC')
    if not art_bytes and getattr(audio, "tags", None):
        try:
            apics = audio.tags.getall("APIC")
            if apics:
                art_bytes = apics[0].data
        except Exception as e:                                # tag shapes vary wildly
            log.debug(f"ID3 APIC art unreadable in {path}: {e}")
        # M4A / MP4: 'covr' atom
        try:
            covr = audio.tags.get("covr") if hasattr(audio.tags, "get") else None
            if covr:
                art_bytes = bytes(covr[0])
        except Exception as e:                                # tag shapes vary wildly
            log.debug(f"MP4 covr art unreadable in {path}: {e}")
    if not art_bytes:
        # Nothing embedded — fall back to the cover sitting BESIDE the
        # music. Here, rather than in the caller, because this function
        # is the single point both the scanner and /localfs/art/<id>
        # call: one change indexes it AND serves it.
        return _read_folder_art(path)
    return (art_bytes, _sniff_image_mime(art_bytes))


# An embedded picture is bounded by the audio file; a folder image is
# not. 12 MB matches the cap the byte routes already enforce — a folder
# of 3 MB sleeve scans should not become a per-request memory spike.
_MAX_FOLDER_ART_BYTES = 12 * 1024 * 1024


def _read_folder_art(path: Path) -> tuple[bytes, str] | None:
    img = _folder_art_path(path)
    if img is None:
        return None
    try:
        if img.stat().st_size > _MAX_FOLDER_ART_BYTES:
            log.debug(f"folder art too large, skipped: {img}")
            return None
        data = img.read_bytes()
    except OSError as e:
        log.debug(f"folder art unreadable {img}: {e}")
        return None
    if not data:
        return None
    # Sniffed, never trusted from the extension — same rule as embedded
    # art, and for the same reasons (see _sniff_image_mime).
    return (data, _sniff_image_mime(data))


# ── the cover that sits BESIDE the music ─────────────────────────
# Only embedded pictures were ever read, so an album whose cover is a
# separate file showed none. Measured live: of 506 album-folders with no
# art, 99 have an image file right there.
#
# Choosing WHICH image is the whole problem. In those same folders:
#     front.jpg  2927px 2032KB   cover.jpg  300px 36KB
#     back.jpg   2900px 3015KB   albumartsmall.jpg  75px 3KB
# "First image wins" picks a 75px thumbnail; "biggest wins" picks the
# BACK of the sleeve. Only the NAME tells you which one is the cover.

_IMAGE_EXTS = frozenset((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"))

# Whole WORDS that mean "this is not the front cover". Matched with
# boundaries, never as substrings — 'cd' inside 'ACDC - cover.jpg' is
# not disc art, the same trap `_is_junk_name` documents.
_NOT_COVER = re.compile(
    r"(?:^|[\s._\-\[(])(back|rear|inlay|inside|booklet|tray|label|disc|"
    r"disk|cd\d*|matrix|obi|spine|sticker)(?:$|[\s._\-\])])", re.I)

# Windows Media Player leaves 75x75 thumbnails everywhere.
_SMALL_THUMB = re.compile(r"small\s*$|_small\b|thumb", re.I)

# Exact stems worth trusting outright, best first.
_COVER_STEMS = ("cover", "folder", "front", "albumart", "album", "art")

# A fallback image has to be big enough to be a cover at all: the WMP
# thumbnails are ~3 KB, the smallest real cover measured was 36 KB.
_MIN_ART_BYTES = 12_000


def _folder_art_candidates(directory: Path) -> list[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return [e for e in entries
            if e.suffix.lower() in _IMAGE_EXTS and e.is_file()]


def _rank_folder_image(path: Path) -> tuple[int, int] | None:
    """(tier, -size) for sorting, or None when the image must not be
    used as a cover at all."""
    stem = path.stem
    if _NOT_COVER.search(stem) or _SMALL_THUMB.search(stem):
        return None
    low = stem.lower().strip()
    if low in _COVER_STEMS:
        return (0, _COVER_STEMS.index(low))
    if any(w in low for w in ("cover", "front")):
        return (1, 0)
    # Windows Media Player's LARGE cached cover. Measured here at 6-14 KB
    # (~200px) — modest but real, and it sat just under the byte floor
    # below. The floor is the wrong instrument for a file whose name
    # already declares which variant it is; `_small` is rejected above.
    if low.startswith("albumart") and low.endswith("large"):
        return (1, 1)
    try:
        if path.stat().st_size < _MIN_ART_BYTES:
            return None
    except OSError:
        return None
    return (2, 0)


def _folder_art_path(track_path: Path) -> Path | None:
    """The album cover sitting next to a track, or None.

    Looks in the track's own folder first. If that folder is a DISC
    subfolder (`CD1`, `Disc 2`), it also looks in the parent — a box
    set's single cover normally sits beside the disc folders rather
    than inside each of them. An ordinary folder never climbs, or every
    album under a genre folder would inherit a stray image from it."""
    try:
        directory = Path(track_path).parent
    except (TypeError, ValueError):
        return None
    search = [directory]
    if _DISC_SUBDIR_RE.match(directory.name):
        search.append(directory.parent)
    for d in search:
        ranked = []
        for img in _folder_art_candidates(d):
            r = _rank_folder_image(img)
            if r is not None:
                ranked.append((r, img.name.lower(), img))
        if ranked:
            ranked.sort(key=lambda x: (x[0], x[1]))
            return ranked[0][2]
    return None


def _extract_art_hash(path: Path) -> str | None:
    """Return sha1(first-embedded-cover-bytes) or None. The marker lets
    the existing `_backfill_album_art` propagate a consistent value
    across the album's siblings; the actual bytes are served on demand
    by the file server's `/localfs/art/<id>` route, and `rescan()` heals
    the marker into a real `<base_url>/localfs/art/<id>` URL when
    base_url is set."""
    got = _extract_art_bytes(path)
    if not got:
        return None
    return hashlib.sha1(got[0]).hexdigest()[:24]
