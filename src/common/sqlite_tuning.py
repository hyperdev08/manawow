"""SQLite settings for the servers' short-lived connections.

Hot writes no longer run on the asyncio thread (see :mod:`src.common.db_io`),
and hot read helpers share a long-lived connection per thread (see
:func:`read_connection`).  The remaining short-lived connections still get the
tuning below.  On Windows (fsync cost plus on-access antivirus scans) a single
position save or XP award measured up to 16 s with the default rollback
journal.

:func:`install` removes fsync from the event loop:

* the database switches to write-ahead logging (a persistent file property);
* every new connection gets ``synchronous=NORMAL`` (WAL commits do not fsync)
  and ``wal_autocheckpoint=0`` (a commit never runs a checkpoint);
* a background thread keeps one connection open for the server's lifetime -
  otherwise closing the *last* connection checkpoints and fsyncs the whole
  database - and runs a PASSIVE checkpoint every few seconds, which never
  blocks the game's readers or writers.

Commits survive a process crash; a power cut can lose the last moments of
writes but never corrupts the database.

The ``sqlite3.connect`` hook lets the ~90 existing call sites keep calling
``sqlite3.connect(path)`` unchanged; callers passing their own ``factory``
are left alone.
"""
from __future__ import annotations

import atexit
import os
import sqlite3
import threading
from typing import Dict, Optional

_original_connect = sqlite3.connect
_installed = False
_checkpointers: Dict[str, "_Checkpointer"] = {}
_read_connections: Dict[tuple, sqlite3.Connection] = {}
_read_connections_lock = threading.Lock()

CHECKPOINT_INTERVAL = 5.0


class TunedConnection(sqlite3.Connection):
    """A connection that applies the per-connection pragmas on open."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            self.execute("PRAGMA synchronous=NORMAL")
            self.execute("PRAGMA wal_autocheckpoint=0")
        except sqlite3.Error:
            pass


def _tuned_connect(*args, **kwargs):
    kwargs.setdefault("factory", TunedConnection)
    return _original_connect(*args, **kwargs)


def read_connection(path: str, rows: bool = True) -> sqlite3.Connection:
    """Return a long-lived read-only connection for the current thread.

    Opening a fresh connection and running its PRAGMAs was itself blocking the
    event loop for over a second on a cold Windows disk (Defender scanning the
    Python process and database file).  Read helpers share one connection per
    thread and database instead; WAL readers use the latest committed snapshot,
    so a long-lived read connection never waits for the writer and never pins a
    checkpoint open (``SELECT`` does not start an explicit transaction).
    """
    key = (threading.get_ident(), os.path.normcase(os.path.abspath(path)),
           bool(rows))
    con = _read_connections.get(key)
    if con is not None:
        return con
    with _read_connections_lock:
        con = _read_connections.get(key)
        if con is None:
            con = _original_connect(path, timeout=30, check_same_thread=False)
            try:
                con.execute("PRAGMA synchronous=NORMAL")
                con.execute("PRAGMA wal_autocheckpoint=0")
            except sqlite3.Error:
                pass
            if rows:
                con.row_factory = sqlite3.Row
            _read_connections[key] = con
    return con


def warm_read(path: Optional[str], rows: bool = True) -> None:
    """Open the shared read connection during startup (before players log in)."""
    if path and path != ":memory:":
        read_connection(path, rows=rows)


def close_read_connections() -> None:
    with _read_connections_lock:
        connections = list(_read_connections.values())
        _read_connections.clear()
    for con in connections:
        try:
            con.close()
        except sqlite3.Error:
            pass


class _Checkpointer(threading.Thread):
    """Holds the database open and folds the WAL back in, off the loop."""

    def __init__(self, path: str, interval: float):
        super().__init__(name=f"sqlite-checkpoint:{path}", daemon=True)
        self.path = path
        self.interval = interval
        self._stop_event = threading.Event()
        self._ready = threading.Event()

    def run(self) -> None:
        con = _original_connect(self.path, timeout=30,
                                check_same_thread=False)
        try:
            con.execute("PRAGMA synchronous=NORMAL")
            self._ready.set()
            while not self._stop_event.wait(self.interval):
                try:
                    con.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                except sqlite3.Error:
                    pass
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            self._ready.set()
            con.close()

    def wait_ready(self, timeout: float = 30.0) -> None:
        self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=60)


def enable_wal(path: Optional[str]) -> str:
    """Switch ``path`` to WAL; returns the resulting journal mode."""
    if not path or path == ":memory:":
        return "memory"
    con = _original_connect(path, timeout=30)
    try:
        return str(con.execute("PRAGMA journal_mode=WAL").fetchone()[0])
    finally:
        con.close()


def install(*paths: Optional[str],
            checkpoint_interval: float = CHECKPOINT_INTERVAL) -> None:
    """Enable WAL on ``paths`` and tune every later ``sqlite3.connect``."""
    global _installed
    for path in paths:
        if enable_wal(path) != "wal":
            continue
        if path not in _checkpointers:
            worker = _Checkpointer(path, checkpoint_interval)
            worker.start()
            worker.wait_ready()
            _checkpointers[path] = worker
    if not _installed:
        sqlite3.connect = _tuned_connect
        # Daemon threads die without cleanup on Ctrl+C; fold the WAL back
        # into the .db file at interpreter exit so it can be copied/committed.
        atexit.register(shutdown)
        _installed = True


def shutdown() -> None:
    """Stop the checkpoint threads, folding the whole WAL into the database.

    Call before copying or committing the ``.db`` file.
    """
    for path in list(_checkpointers):
        _checkpointers.pop(path).stop()
    close_read_connections()
