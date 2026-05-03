#!/usr/bin/env python3
"""
db_pool.py — SQLite connection pool for the DLNA Gateway.

Each `with pool.read() / pool.write()` block opens a fresh SQLite
connection and closes it on exit. Open is cheap (~0.5 ms on local
disk) and the OS page cache absorbs the per-connection cold start,
so the cost is invisible compared with the alternative — caching
per thread under HTTPServer.ThreadingMixIn (one new thread per
request) leaks a connection every request because there is no
reliable hook to close on thread death. After 38 h of normal use
that surfaced as 1985 open FDs against library.db, FD numbers >1024,
and `select()` in the stream proxy raising
`ValueError: filedescriptor out of range` — every browser-audio
track skipped as "unsupported format". The fix is to not cache.

WAL mode + per-connection writer lock + busy_timeout are still in
play.

Usage:
    from db_pool import Pool

    pool = Pool("library.db")

    with pool.read() as conn:
        rows = conn.execute("SELECT ...").fetchall()

    with pool.write() as conn:
        conn.execute("INSERT ...")
        # commit happens automatically on exit

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
    SQLite connection helper. Reads are concurrent under WAL mode;
    writes are serialized via a threading.Lock.
    """

    def __init__(self, db_file: str, max_retries: int = 3):
        self._db_file = db_file
        self._write_lock = threading.Lock()
        self._max_retries = max_retries
        self._closed = False

        db_dir = os.path.dirname(db_file) or "."
        if not os.path.isdir(db_dir):
            os.makedirs(db_dir, exist_ok=True)

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

    @contextmanager
    def read(self):
        """Context manager for read-only access. No global lock."""
        if self._closed:
            raise RuntimeError("DB pool is closed")
        conn = self._new_connection()
        try:
            yield conn
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                log.warning(f"DB read contention: {e}")
            raise
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @contextmanager
    def write(self):
        """Context manager for write access. Auto-commits on success."""
        if self._closed:
            raise RuntimeError("DB pool is closed")
        with self._write_lock:
            conn = self._new_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def close(self):
        """Mark the pool closed; future read()/write() calls will raise."""
        self._closed = True
        log.info("DB pool: closed")

    def execute_script(self, sql: str):
        """Execute a multi-statement SQL script (schema init, etc.)."""
        with self._write_lock:
            conn = self._new_connection()
            try:
                conn.executescript(sql)
                conn.commit()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

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
    