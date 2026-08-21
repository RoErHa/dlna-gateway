#!/usr/bin/env python3
"""
dlna_localfs_http.py — the two pure HTTP/DLNA helpers the LocalFs file
server needs: building the DLNA response-header pair for a MIME type,
and parsing a `Range:` request header.

Split out of dlna_localfs_server.py (2026-08-20), which had reached 450
lines. Both functions are PURE — string/int in, string/tuple out, no
sockets and no filesystem — which is what makes them worth isolating
from the request handler they serve.

Why these headers are hand-built rather than left to a static-file
helper: the Naim (and DLNA validators generally) require
`contentFeatures.dlna.org` and `transferMode.dlna.org` on the response,
and a wrong or missing DLNA Profile Name is enough for a renderer to
refuse the stream outright.

Correct Range handling is equally non-optional: the Naim seeks with
`bytes=N-M`, so a malformed or unsatisfiable range must produce
`416`, never a silent full-body 200.

`resolve_within` joined them on 2026-08-21 (audit Track B1). It is pure in
the same sense — it touches the filesystem only to canonicalise — and it is
here rather than in the handler because the handler had TWO copies of the
containment test, which is how one of them drifted.
"""
from __future__ import annotations

from pathlib import Path

# Default Content-Type when we can't infer one. Keeps Range responses
# working even for unknown extensions.
_FALLBACK_MIME = "application/octet-stream"


def _dlna_headers_for_mime(mime: str) -> dict:
    """Return the `contentFeatures.dlna.org` + `transferMode.dlna.org`
    pair expected by renderer-side DLNA validators. The Profile Name
    (PN) is a hint — most modern renderers (the Naim included) tolerate
    a generic PN, so we map by container family rather than tagging
    each codec variation. Renderers that don't read these headers
    just ignore them; renderers that do see a plausible answer.

    DLNA.ORG_OP=01 → Range requests supported (the half we care about).
    DLNA.ORG_FLAGS → streaming + background transfer eligible."""
    mime = (mime or "").lower()
    # Video: emit OP/FLAGS but NO DLNA.ORG_PN — a wrong codec-specific PN makes
    # strict renderers reject; lenient ones (LG webOS) play fine without it.
    if mime.startswith("video/"):
        return {
            "contentFeatures.dlna.org": ("DLNA.ORG_OP=01;"
                "DLNA.ORG_FLAGS=01700000000000000000000000000000"),
            "transferMode.dlna.org": "Streaming",
        }
    pn = "MP3"
    if mime.startswith("audio/flac")  or mime == "audio/x-flac":
        pn = "FLAC"
    elif mime.startswith("audio/mpeg"):
        pn = "MP3"
    elif mime.startswith(("audio/aac", "audio/mp4")):
        pn = "AAC_ISO_320"
    elif mime.startswith(("audio/x-wav", "audio/wav")):
        pn = "LPCM"
    elif mime.startswith(("audio/ogg", "audio/opus")):
        pn = "OGG"
    elif "dsd" in mime or "dsf" in mime or "dff" in mime:
        pn = "DSD"           # non-standard; Naim/MinimServer accept it
    contentFeatures = (
        f"DLNA.ORG_PN={pn};"
        "DLNA.ORG_OP=01;"
        "DLNA.ORG_FLAGS=01700000000000000000000000000000")
    return {
        "contentFeatures.dlna.org": contentFeatures,
        "transferMode.dlna.org":    "Streaming",
    }


def resolve_within(file_path: str,
                   allowed_roots: tuple[str, ...]) -> Path | None:
    """Canonicalise `file_path` and return it only if it lies inside one of
    `allowed_roots`. `None` means refuse — the caller answers 403.

    Two things here are load-bearing, and both were wrong before 2026-08-21:

    * **Containment is asked about path COMPONENTS, not string prefixes.**
      `str(p).startswith(root)` says `/Volumes/SAMDATA/Music Videos` is
      inside `/Volumes/SAMDATA/Music`, because as text it is. With the
      deployed roots that admitted any sibling directory whose name began
      with a root's name — every byte of it, to any unauthenticated peer on
      the LAN, since renderers fetch from this server directly.
    * **The RESOLVED path is what the caller must then open.** Resolving
      follows symlinks, which is what stops a link planted inside the root
      from reading outside it; opening the original path afterwards would
      hand that back through a time-of-check/time-of-use gap.

    An empty `allowed_roots` disables the check — the CLI driver and some
    tests construct a server with no roots on purpose.
    """
    try:
        resolved = Path(file_path).resolve()
    except (OSError, ValueError):
        # ValueError covers a NUL byte in the path; both mean "not a file
        # we can reason about", and both must refuse rather than raise.
        return None
    if not allowed_roots:
        return resolved
    for root in allowed_roots:
        try:
            if resolved.is_relative_to(Path(root)):
                return resolved
        except (OSError, ValueError):
            continue
    return None


def _parse_range_header(value: str, file_size: int) -> tuple[int, int] | None:
    """Parse `bytes=N-M`, `bytes=N-`, `bytes=-N`. Returns (start, end)
    inclusive, or None when the header is malformed / unsatisfiable.
    Multipart ranges are not supported — the Naim doesn't use them.

    `416 Range Not Satisfiable` is the right response to None — the
    handler emits it explicitly."""
    if not value or not value.startswith("bytes="):
        return None
    spec = value[len("bytes="):].strip()
    if "," in spec:                                # multipart not supported
        return None
    if "-" not in spec:
        return None
    left, _, right = spec.partition("-")
    try:
        if not left:                               # suffix range: bytes=-N
            if not right:
                return None
            n = int(right)
            if n <= 0 or n > file_size:
                n = file_size
            return (file_size - n, file_size - 1)
        start = int(left)
        end = int(right) if right else file_size - 1
    except ValueError:
        return None
    if start < 0 or end < start or start >= file_size:
        return None
    if end >= file_size:
        end = file_size - 1
    return (start, end)

