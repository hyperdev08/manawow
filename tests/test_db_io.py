"""SQLite writes must run off the asyncio thread and preserve ordering."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

from src.common.db_io import execute_write, run_db_write, submit_db_write


def _make_db(path: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, value INTEGER)")
        con.commit()
    finally:
        con.close()


def test_awaited_write_persists_before_returning(tmp_path):
    path = str(tmp_path / "test.db")
    _make_db(path)

    async def flow():
        await run_db_write(execute_write, path,
                           "INSERT INTO t(value) VALUES(?)", (7,))
        con = sqlite3.connect(path)
        try:
            return con.execute("SELECT value FROM t").fetchall()
        finally:
            con.close()

    assert asyncio.run(flow()) == [(7,)]


def test_writer_does_not_block_the_event_loop(tmp_path):
    path = str(tmp_path / "test.db")
    _make_db(path)
    started = threading.Event()

    def slow_write():
        started.set()
        time.sleep(0.25)
        return "done"

    async def flow():
        task = asyncio.create_task(run_db_write(slow_write))
        while not started.is_set():
            await asyncio.sleep(0.01)
        begin = time.monotonic()
        await asyncio.sleep(0.02)          # loop still schedules promptly
        elapsed = time.monotonic() - begin
        assert elapsed < 0.15, elapsed
        assert await task == "done"

    asyncio.run(flow())


def test_queued_and_awaited_writes_keep_submission_order(tmp_path):
    path = str(tmp_path / "test.db")
    _make_db(path)

    async def flow():
        submit_db_write(execute_write, path,
                        "INSERT INTO t(value) VALUES(?)", (1,))
        await run_db_write(execute_write, path,
                           "INSERT INTO t(value) VALUES(?)", (2,))
        con = sqlite3.connect(path)
        try:
            return [row[0] for row in con.execute(
                "SELECT value FROM t ORDER BY id")]
        finally:
            con.close()

    assert asyncio.run(flow()) == [1, 2]
