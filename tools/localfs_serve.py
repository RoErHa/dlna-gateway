#!/usr/bin/env python3
"""
tools/localfs_serve.py — Phase 3 driver. Stands up the LocalFs file
server against an existing library.db without booting the full
gateway. Lets you prove bit-perfect serving (`curl -r 0-1023`,
`sha256` comparison) and Naim playback before P4 wires the server
into `dlna_gateway.main()`.

Usage:

    # Default: read DB_FILE from dlna_config, listen on 0.0.0.0:8200
    python3 tools/localfs_serve.py

    # Override port / DB / allowed roots:
    python3 tools/localfs_serve.py --port 8201 \\
        --root /Volumes/SAMDATA/Music \\
        --db library.db

    # Quick proof: full file in one stream, sha256 check
    curl -s http://localhost:8200/localfs/stream/<track_id> \\
        | sha256sum
    sha256sum /Volumes/SAMDATA/Music/Pink\\ Floyd/...

    # Quick proof: range request
    curl -v -r 0-1023 http://localhost:8200/localfs/stream/<track_id> \\
        -o /tmp/first1k.bin

Ctrl-C stops the server. The DB is opened read-only-ish in the
sense that the server only reads from `tracks`; no writes happen
unless a future caller does.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from pathlib import Path

# Reach the project root from tools/
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from dlna_config import DB_FILE            # noqa: E402
from dlna_localfs_server import start_server   # noqa: E402


def _setup_logging(verbose: bool):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Start the LocalFs file server on its own port.")
    p.add_argument("--port", type=int,
                   default=int(os.environ.get("LOCALFS_PORT", "8200")),
                   help="HTTP port (default: %(default)s; honors "
                        "$LOCALFS_PORT)")
    p.add_argument("--host", default="0.0.0.0",
                   help="Listen address (default: %(default)s — bind "
                        "on all interfaces so the Naim can reach us)")
    p.add_argument("--db", default=str(DB_FILE),
                   help="library.db path (default: %(default)s)")
    p.add_argument("--root", action="append", default=None,
                   help="Allowed music root. May be passed multiple "
                        "times. Defaults to $LOCALFS_MUSIC_ROOT or "
                        "/Volumes/SAMDATA/Music. Path-traversal "
                        "defence: file_path values outside any root "
                        "are refused (403).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)

    # Default --root if none provided
    roots: list[str] = list(args.root or [])
    if not roots:
        env_root = os.environ.get("LOCALFS_MUSIC_ROOT",
                                  "/Volumes/SAMDATA/Music")
        roots = [env_root]
    roots_resolved = tuple(str(Path(r).expanduser().resolve())
                            for r in roots)

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(f"ERROR: library.db not found at {db_path}",
              file=sys.stderr)
        return 2

    print(f"library.db   : {db_path}")
    print(f"allowed roots: {roots_resolved}")
    print(f"listen on    : http://{args.host}:{args.port}/localfs/stream/<id>")
    print()
    print("Stop with Ctrl-C.")
    print()

    srv = start_server(str(db_path), port=args.port,
                       host=args.host,
                       allowed_roots=roots_resolved)

    # Graceful shutdown on SIGINT / SIGTERM
    stop_flag = {"stop": False}

    def _signal_handler(signum, _frame):
        log_name = signal.Signals(signum).name
        print(f"\nReceived {log_name} — shutting down…")
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        while not stop_flag["stop"]:
            time.sleep(0.5)
    finally:
        srv.shutdown()
        srv.server_close()
        print("LocalFs server stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
