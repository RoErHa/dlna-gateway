#!/usr/bin/env python3
"""
dlna_proxy_common.py — the handful of constants and the one helper that
BOTH audio relays need: the byte-perfect library relay
(`dlna_stream_proxy`) and the internet-radio ICY relay
(`dlna_radio_proxy`).

Extracted when those two were split apart (2026-08-20). It exists purely
to keep that split acyclic — without it the radio module would have to
import the stream module for `PROXY_IDLE_SEC` / `_MIME_MAP` while the
stream module re-exports the radio entry points, which is a cycle.

Deliberately dependency-free, for the same reason `dlna_library_sql` is:
anything imported here would be pulled in by both relays.

`PROXY_IDLE_SEC` stays module-level so a test can monkey-patch it. Note
that each relay binds it at import, so patch it on the module whose
relay you are exercising.
"""
from __future__ import annotations

PROXY_IDLE_SEC = 300  # 5 min — covers a closed-laptop / sleeping-browser gap


# Browser-compat MIME normalisation. Safari refuses audio/x-flac but
# accepts audio/flac; same story for x-m4a and a few others.
_MIME_MAP = {
    "audio/x-flac":   "audio/flac",
    "audio/x-m4a":    "audio/mp4",
    "audio/x-alac":   "audio/mp4",
    "audio/x-aiff":   "audio/aiff",
    "audio/x-wav":    "audio/wav",
    "audio/x-ms-wma": "audio/x-ms-wma",
}


def normalize_audio_ctype(ctype: str) -> str:
    """Map Safari-rejected MIME types (audio/x-flac → audio/flac, …). Pure."""
    if not ctype:
        return "application/octet-stream"
    base = ctype.split(";")[0].strip().lower()
    return _MIME_MAP.get(base, base)
