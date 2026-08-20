#!/usr/bin/env python3
"""
api_playback_state.py — shared runtime handles AND request helpers for
the api_playback family.

Split out of api_playback.py on 2026-08-20, when that module reached 749
lines mixing cover art, playback control, and the metadata/position layer:

    api_playback_state.py  the shared handles every module binds against
    api_playback_art.py    the /art subsystem: fetch, cache, downscale, serve
    api_playback_meta.py   track metadata, lyrics, positions, book meta
    api_playback.py        playback control, index, status + re-exports

api_playback re-exports every public name, so callers (dlna_asgi_*,
dlna_routes, api_subsonic_media) and the ~36 test patch sites that reach
through it keep working.

⚠ SHARED HANDLES ARE BOUND HERE, ONCE — `DB`, `INDEXER`, `QUEUES`,
`SERVERS`, `RENDERERS`, `get_provider`. Siblings use `_st.<name>`, an
attribute lookup resolved at CALL time, so a test patch actually lands:

    patch.object(api_playback_state, "QUEUES", fake_registry)

Binding any of them in a second module would leave that module on the real
singletons while the tests still passed — a false pass, which is worse than
a crash.
"""
import json
import logging

from dlna_discovery import RENDERERS, SERVERS  # noqa: F401
from dlna_library import DB, INDEXER  # noqa: F401
from dlna_player import QUEUES, proxy_stream  # noqa: F401
from dlna_providers import get_provider  # noqa: F401

log = logging.getLogger("dlna.api.playback")


def _parse_json_or_400(h, body):
    """Parse a JSON request body into a dict. On failure (malformed JSON
    OR top-level non-object like '[]' / '"string"' / '42'), send 400
    and return None so the caller can bail.

    Malformed input is a client error, not a server error — returning
    500 would be wrong and trip the chaos suite's 5xx gate. The dict
    check is important: json.loads('[]') succeeds but then data.get()
    raises AttributeError, which is what surfaced in the chaos run."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as e:
        h._json(400, {"error": f"invalid JSON: {e}"})
        return None
    if not isinstance(data, dict):
        h._json(400, {"error": f"expected JSON object, got {type(data).__name__}"})
        return None
    return data
