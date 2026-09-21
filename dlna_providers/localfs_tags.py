#!/usr/bin/env python3
"""
localfs_tags.py — the pure, filesystem-level helpers behind
`LocalFsProvider`: deciding what counts as an audio file, deriving a
track's stable id and its album identity, reading tags via mutagen, and
pulling embedded cover art out of a file.

Split out of localfs.py (2026-08-20), which had reached 725 lines.
Everything here is a free function over a `Path` — no provider state, no
`library.db` — which is what makes it separable and directly testable.

Two identity rules live here and are load-bearing:
  * `_album_key_for` folds disc subfolders (`CD1`, `Disc 2`, …) into the
    parent, so a multi-disc album is ONE album. This folder identity is
    what makes a Various-Artists compilation group correctly, where the
    per-track `artist` tag would fragment it into one album per
    performer.
  * `_track_id_for` salts ids with a NAMESPACE for any root beyond the
    first. The file server resolves an obj_id across ALL localfs UDNs,
    so identical rel_paths under two roots (music + audiobooks) would
    otherwise collide. Music keeps `namespace=""` so its existing ids
    and URLs are unchanged.

`localfs.py` re-exports every name here, so `from
dlna_providers.localfs import _read_tags` (and the tests that patch it
there) keep working.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

log = logging.getLogger("dlna.provider.localfs")


# ── Audio file detection ────────────────────────────────────────
# Reuses the extension set from tools/find_corrupt_audio.py +
# tools/prune_empty_music_dirs.py. mp4 is deliberately excluded
# (music-video case — see "prune_empty_music_dirs.py" section in
# CLAUDE.md).
_AUDIO_EXTENSIONS = frozenset((
    ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac",
    ".wav", ".aiff", ".aif", ".alac",
    ".dsf", ".dff", ".ape", ".wma",
    ".m4b",   # audiobooks (MP4 container, same as .m4a)
))


def _is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in _AUDIO_EXTENSIONS


# A trailing disc subfolder (CD1 / "Disc 2" / Disk_3 / "Side A") is
# folded into its parent so a multi-disc release groups as ONE album.
_DISC_SUBDIR_RE = re.compile(r"^(cd|disc|disk|side)[\s._-]*\d+[a-z]?$", re.I)


def _album_key_for(rel_path: str) -> str:
    """Folder-based album identity for a track, relative to the music
    root. An album == its containing folder; a trailing disc subfolder
    is folded into the parent (multi-disc releases group as one album).

    This is the grouping key the browse layer uses instead of the
    per-track (artist, album): a compilation lives in ONE folder so it
    groups as a single album even though every track's `artist` is a
    different performer, while two distinct albums that merely share a
    name ("Greatest Hits") live in different folders and stay separate.

    Returns "" for a root-level loose file (no containing folder)."""
    parent = os.path.dirname(rel_path)
    if _DISC_SUBDIR_RE.match(os.path.basename(parent)):
        parent = os.path.dirname(parent)
    return parent


def _track_id_for(rel_path: str, namespace: str = "") -> str:
    """Stable per-file identifier — survives rescans. The lesson
    from AssetUPnP's `d-<id>` was: NEVER auto-increment. Use a
    content-derived hash of the relative path so renaming a file
    legitimately produces a new id while a re-walk produces the same
    id.

    `namespace` (2026-07-13, audiobooks): a SECOND provider root can
    contain the same rel_path as the music root, and the file server
    resolves `/localfs/stream/<id>` by obj_id across ALL localfs UDNs
    — so ids from secondary roots are salted with a namespace. The
    music root keeps namespace='' so every existing URL/playlist row
    stays byte-identical."""
    key = f"{namespace}\x00{rel_path}" if namespace else rel_path
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _udn_for_root(root: Path) -> str:
    """Synthesise a stable UDN per music root path so multiple
    LocalFs providers (different roots) coexist."""
    h = hashlib.sha1(str(root.resolve()).encode("utf-8")).hexdigest()
    return f"uuid:localfs-{h[:32]}"


# ── mutagen-backed tag reader ───────────────────────────────────
# All mutagen access lives behind these helpers so the rest of the
# module can be tested with mocks. Importing mutagen lazily lets
# `dlna_providers.localfs` be imported in environments where mutagen
# isn't installed (e.g. CI containers running the UPnP-only path) —
# only `rescan()` will then raise.

# ── EasyMP4 credit keys ─────────────────────────────────────────
# mutagen's `easy=True` interface is NOT uniform across containers.
# FLAC/Ogg expose `composer`/`lyricist` straight from the Vorbis
# comment, and EasyID3 maps both natively — but **EasyMP4 registers
# neither**, so an .m4a/.m4b carrying a real `\xa9wrt` credit reads
# back as blank. That failure is silent and indistinguishable from an
# untagged file, which is why it is registered here rather than worked
# around at the call site.
#
# Guarded so the module still imports without mutagen (see
# `_require_mutagen` below) — a UPnP-only setup never installs it.
try:
    from mutagen.easymp4 import EasyMP4Tags as _EasyMP4Tags

    _EasyMP4Tags.RegisterTextKey("composer", "\xa9wrt")
except Exception as _e:                                       # noqa: BLE001
    # No mutagen, or a version whose easymp4 shape differs. Credits
    # simply stay blank for MP4; every other format is unaffected —
    # so this must never be fatal, but it must not be silent either.
    log.debug(f"localfs_tags: EasyMP4 composer key not registered ({_e}); "
              f"MP4 credits will read blank")


def _require_mutagen():
    try:
        import mutagen          # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "LocalFsProvider needs `mutagen` — install with "
            "`pip install -r requirements.txt`. See CLAUDE.md → "
            "'Library backend migration' for the dependency story."
        ) from e


def _read_tags(path: Path) -> dict | None:
    """Read tags + technical info via mutagen, returning a dict in
    the shape `LibraryDB.upsert_tracks` expects. Returns None when
    mutagen can't open the file (corrupt / unrecognized format);
    the caller should log this as a malformed-file warning."""
    import mutagen
    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception as e:                                    # noqa: BLE001
        log.warning(f"LocalFs: mutagen failed to open {path!s}: {e}")
        return None
    if audio is None:
        log.warning(f"LocalFs: mutagen does not recognise {path!s}")
        return None

    def _first(key: str) -> str:
        vals = audio.get(key, [])
        return (vals[0] if vals else "").strip()

    duration_sec = getattr(audio.info, "length", 0.0) or 0.0
    info = audio.info
    bit_depth = (getattr(info, "bits_per_sample", None)
                 or getattr(info, "bits_per_raw_sample", None))
    sample_rate = getattr(info, "sample_rate", None)

    # Year: mutagen-easy gives a 'date' field, often 'YYYY' or
    # 'YYYY-MM-DD'. Take just the first 4 digits if plausible.
    raw_date = _first("date") or _first("year")
    year: int | None = None
    if raw_date:
        m = re.match(r"^(\d{4})", raw_date)
        if m:
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                year = y

    # Track number — strip the "/totaltracks" suffix some tags carry
    raw_tn = _first("tracknumber")
    tn_match = re.match(r"^(\d+)", raw_tn)
    track_number = int(tn_match.group(1)) if tn_match else 0

    return {
        "title":       _first("title")  or path.stem,
        "artist":      _first("artist") or _first("albumartist"),
        "album":       _first("album"),
        "genre":       _first("genre"),
        # Credits (2026-09-20). Present on ~32% (composer) / ~18%
        # (lyricist) of this library already, so they cost no network.
        "composer":    _first("composer"),
        "lyricist":    _first("lyricist"),
        "duration":    _format_duration(duration_sec),
        "bit_depth":   int(bit_depth) if bit_depth else None,
        "sample_rate": int(sample_rate) if sample_rate else None,
        "year":        year,
        "track_number": track_number,
        "mime":        _mime_for(path.suffix),
    }


def _format_duration(seconds: float) -> str:
    """Render seconds → AssetUPnP-shaped `H:MM:SS.fff` string so
    `dlna_player._dur_to_sec` accepts it identically."""
    if not seconds or seconds < 0:
        return ""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.3f}"


_MIME_BY_EXT = {
    ".flac": "audio/flac",
    ".mp3":  "audio/mpeg",
    ".ogg":  "audio/ogg",
    ".opus": "audio/opus",
    ".m4a":  "audio/mp4",
    ".m4b":  "audio/mp4",
    ".aac":  "audio/aac",
    ".wav":  "audio/x-wav",
    ".aiff": "audio/x-aiff",
    ".aif":  "audio/x-aiff",
    ".alac": "audio/mp4",
    ".dsf":  "audio/x-dsf",
    ".dff":  "audio/x-dff",
    ".ape":  "audio/x-ape",
    ".wma":  "audio/x-ms-wma",
}


def _mime_for(suffix: str) -> str:
    return _MIME_BY_EXT.get(suffix.lower(), "application/octet-stream")
