#!/usr/bin/env python3
"""
db_pool.py — SQLite connection pool for the DLNA Gateway.

Provides thread-safe database access with:
  - WAL mode for concurrent reads
  - Connection reuse via thread-local storage
  - Write lock (SQLite allows only one writer)
  - busy_timeout to handle contention gracefully
  - Proper cleanup on shutdown

Usage:
    from db_pool import Pool

    pool = Pool("library.db")

    # Read (concurrent, no global lock):
    with pool.read() as conn:
        rows = conn.execute("SELECT ...").fetchall()

    # Write (serialized via write lock):
    with pool.write() as conn:
        conn.execute("INSERT ...")
        conn.commit()

    # Shutdown:
    pool.close()
"""
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

log = logging.getLogger("dlna.db")

# Default pragmas applied to every new connection
_PRAGMAS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=8000",
    "PRAGMA foreign_keys=ON",
    "PRAGMA cache_size=-8000",   # 8 MB per-connection cache
]


class Pool:
    """
    SQLite connection pool.

    Connections are cached per-thread (thread-local storage).
    Reads are fully concurrent under WAL mode.
    Writes are serialized via a threading.Lock.
    """

    def __init__(self, db_file: str, max_retries: int = 3):
        self._db_file = db_file
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self._max_retries = max_retries
        self._closed = False

        # Validate the path early
        db_dir = os.path.dirname(db_file) or "."
        if not os.path.isdir(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        # Open one connection to set WAL mode (affects the whole database file)
        conn = self._new_connection()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        log.info(f"DB pool: {db_file} (journal_mode={mode})")
        conn.close()

    def _new_connection(self) -> sqlite3.Connection:
        """Create a fresh connection with all pragmas set."""
        conn = sqlite3.connect(
            self._db_file,
            check_same_thread=False,
            timeout=15,
        )
        conn.row_factory = sqlite3.Row
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        return conn

    def _get_conn(self) -> sqlite3.Connection:
        """Get or create a thread-local connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except Exception:
                # Stale or broken — close and reopen
                try:
                    conn.close()
                except Exception:
                    pass
                self._local.conn = None

        conn = self._new_connection()
        self._local.conn = conn
        log.debug(f"DB pool: new connection (thread {threading.current_thread().name})")
        return conn

    @contextmanager
    def read(self):
        """
        Context manager for read-only access.
        No global lock — WAL mode allows concurrent readers.

        Usage:
            with pool.read() as conn:
                rows = conn.execute("SELECT ...").fetchall()
        """
        conn = self._get_conn()
        try:
            yield conn
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                log.warning(f"DB read contention: {e}")
            raise

    @contextmanager
    def write(self):
        """
        Context manager for write access.
        Acquires a write lock so only one thread writes at a time.
        Auto-commits on success, rolls back on exception.

        Usage:
            with pool.write() as conn:
                conn.execute("INSERT ...")
                # commit happens automatically on exit
        """
        with self._write_lock:
            conn = self._get_conn()
            try:
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise

    def close(self):
        """Close all connections. Call on shutdown."""
        self._closed = True
        conn = getattr(self._local, "conn", None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None
        log.info("DB pool: closed")

    def execute_script(self, sql: str):
        """Execute a multi-statement SQL script (schema init, etc.)."""
        with self._write_lock:
            conn = self._get_conn()
            conn.executescript(sql)

    @property
    def db_file(self) -> str:
        return self._db_file

    def integrity_check(self) -> str:
        """Run PRAGMA integrity_check and return the result."""
        with self.read() as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
            return result[0] if result else "unknown"


# ── Standalone test ───────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile
    import time

    logging.basicConfig(level=logging.DEBUG,
                        format="%(asctime)s %(levelname)-5s %(message)s")

    # Create a temp database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    pool = Pool(db_path)

    # Init schema
    pool.execute_script("""
        CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT);
    """)

    # Test concurrent reads + writes
    results = []
    errors = []

    def writer(n):
        try:
            for i in range(50):
                with pool.write() as conn:
                    conn.execute("INSERT INTO test (val) VALUES (?)",
                                 (f"thread-{n}-{i}",))
        except Exception as e:
            errors.append(f"writer-{n}: {e}")

    def reader(n):
        try:
            for _ in range(50):
                with pool.read() as conn:
                    rows = conn.execute("SELECT COUNT(*) FROM test").fetchone()
                    results.append(rows[0])
                time.sleep(0.001)
        except Exception as e:
            errors.append(f"reader-{n}: {e}")

    threads = []
    for i in range(3):
        threads.append(threading.Thread(target=writer, args=(i,)))
    for i in range(5):
        threads.append(threading.Thread(target=reader, args=(i,)))

    start = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - start

    # Verify
    with pool.read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM test").fetchone()[0]

    print(f"\nResults:")
    print(f"  Threads: 3 writers + 5 readers")
    print(f"  Rows written: {count} (expected 150)")
    print(f"  Read samples: {len(results)}")
    print(f"  Errors: {len(errors)}")
    for e in errors:
        print(f"    {e}")
    print(f"  Integrity: {pool.integrity_check()}")
    print(f"  Time: {elapsed:.2f}s")
    print(f"  Status: {'PASS' if count == 150 and not errors else 'FAIL'}")

    pool.close()
    os.unlink(db_path)
    