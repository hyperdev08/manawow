"""Run SQLite writes on a dedicated thread.

The worldserver is one asyncio loop.  Opening a connection and committing a
write on that loop means a slow disk (antivirus scan, laptop drive, Windows
fsync) freezes every session.  WAL + ``synchronous=NORMAL`` removed the fsync
from most commits, but the remaining file writes still belong off the loop.

Critical writes (position, inventory, money, XP, quests, loot) are awaited:
the caller sees the committed row before continuing, so read-your-writes is
preserved exactly as before.  Non-critical creature state (health, power,
aggro) is queued with :func:`submit_db_write`; a crash may lose at most the
last moment of a creature's HP, which the server rebuilds on respawn anyway.

A single worker serialises all writes, so WAL never sees two writers racing
for the same lock.  Reads deliberately stay on the event loop: in WAL mode
they use the last committed snapshot and never wait for the writer, and the
hot templates are already served from the OS page cache.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import sqlite3
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Iterable, Sequence, TypeVar

_WRITE_POOL = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="sqlite-writer")
_log = logging.getLogger("sqlite-writer")

T = TypeVar("T")


def execute_write(path: str, sql: str,
                  params: Sequence[Any] = ()) -> None:
    """Open a short-lived connection, execute one statement, commit."""
    con = sqlite3.connect(path)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def execute_many(path: str, sql: str,
                 rows: Iterable[Sequence[Any]]) -> None:
    con = sqlite3.connect(path)
    try:
        con.executemany(sql, rows)
        con.commit()
    finally:
        con.close()


def _log_failure(future: Future) -> None:
    try:
        exc = future.exception()
    except Exception:  # cancelled
        return
    if exc is not None:
        _log.error("SQLite background write failed: %s", exc,
                   exc_info=(type(exc), exc, exc.__traceback__))


def submit_db_write(func: Callable[..., T], /, *args: Any,
                    **kwargs: Any) -> Future:
    """Queue a write without awaiting it; order is preserved by the worker."""
    future = _WRITE_POOL.submit(func, *args, **kwargs)
    future.add_done_callback(_log_failure)
    return future


async def run_db_write(func: Callable[..., T], /, *args: Any,
                       **kwargs: Any) -> T:
    """Await a write on the dedicated worker thread."""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(_WRITE_POOL, call)


def shutdown(wait: bool = True) -> None:
    _WRITE_POOL.shutdown(wait=wait)
