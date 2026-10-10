"""SQLite infrastructure layer.

Single state.sqlite per vault. WAL on, foreign_keys on, busy_timeout
set. Connection is thread-local via a small factory.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from memory_agent.infrastructure.sqlite.migrations import apply_initial_migration, MIGRATIONS

__all__ = [
    "open_state_db",
    "transaction",
    "MIGRATIONS",
]


_local = threading.local()


def open_state_db(path: Path) -> sqlite3.Connection:
    """Open (or create) a state.sqlite with our standard pragmas.

    ``check_same_thread=False`` is required because uvicorn dispatches
    loopback HTTP requests across worker threads; without this flag the
    first read on a worker thread raises ``ProgrammingError``. WAL plus
    the explicit ``transaction`` context manager keep concurrent access
    safe — readers never block writers, and writers serialise via
    ``BEGIN IMMEDIATE`` / ``COMMIT``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    # First-time migration.
    apply_initial_migration(conn)
    return conn


def get_default_connection(path: Path) -> sqlite3.Connection:
    """Thread-local cached connection. Caller controls lifetime."""
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != str(path):
        conn = open_state_db(path)
        _local.conn = conn
        _local.path = str(path)
    return conn


def close_default_connection() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None
            _local.path = None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a short write transaction; auto-rollback on exception.

    We use explicit BEGIN IMMEDIATE / COMMIT for write transactions so
    concurrent readers always see consistent snapshots.
    """
    cur = conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")