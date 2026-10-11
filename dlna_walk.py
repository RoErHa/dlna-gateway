"""
dlna_walk.py — can this directory walk be trusted to say a file is GONE?

A rescan decides "removed" by absence: a cached path the walk did not
visit is treated as deleted, and its `tracks` row goes with it. Absence
is only evidence when the walk actually LOOKED, and `os.walk` hides the
two ways it can fail to:

* It SWALLOWS directory errors by default. A folder that fails to list —
  an I/O error, a drive dropping mid-scan — is skipped silently, so
  everything under it reads as deleted.
* A walk that finds no audio at all, where the index held some, is a
  volume gone wrong (a stale mount point, leftover folders), not a
  library emptied. `LocalFsProvider.probe()` refuses an EMPTY root, but
  a root holding a few non-audio folders passes it.

The video scanner learned this on 2026-10-03, when a walk that saw 0
files pruned all 4,104 videos. It refuses the whole prune; the music
scanner is finer-grained, because a single permanently unreadable folder
should not freeze removal across 27,000 tracks forever — so only the
removals UNDER an errored directory are held back.

Pure and dependency-free so the rule is tested directly
(tests/test_walk_trust.py).
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("dlna.walk")


def trusted_removals(removed: list[str], walk_errors: list[OSError],
                     saw_any: bool) -> list[str]:
    """Return the subset of `removed` (absolute paths absent from the walk)
    that the walk can actually vouch for."""
    if not removed:
        return removed
    if not saw_any:
        log.warning(f"rescan: walk found no audio — NOT removing "
                    f"{len(removed)} indexed file(s); is the volume mounted?")
        return []
    if not walk_errors:
        return removed
    blind = tuple(os.path.join(str(e.filename), "") for e in walk_errors
                  if getattr(e, "filename", None))
    if len(blind) < len(walk_errors):   # an error naming no directory
        log.warning(f"rescan: walk error without a path ({walk_errors[0]})"
                    f" — NOT removing {len(removed)} indexed file(s)")
        return []
    kept = [p for p in removed if not p.startswith(blind)]
    log.warning(f"rescan: {len(walk_errors)} folder(s) could not be read "
                f"(first: {walk_errors[0]}) — holding back "
                f"{len(removed) - len(kept)} removal(s) under them")
    return kept
