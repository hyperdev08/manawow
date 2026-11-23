"""WAL + synchronous=NORMAL keep database commits off the critical path."""
from __future__ import annotations

import os
import sqlite3
import tempfile

from src.common import sqlite_tuning


def test_install_enables_wal_and_tunes_new_connections():
    path = os.path.join(tempfile.mkdtemp(prefix="manawow-sqlite-"), "w.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(x INTEGER)")
    con.commit()
    con.close()

    sqlite_tuning.install(path)

    con = sqlite3.connect(path)
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        con.execute("INSERT INTO t VALUES(1)")
        con.commit()
    finally:
        con.close()
    sqlite_tuning.shutdown()
    # WAL is a property of the file: a plain connection still sees it.
    raw = sqlite_tuning._original_connect(path)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert raw.execute("SELECT x FROM t").fetchall() == [(1,)]
    finally:
        raw.close()


def test_closing_connections_does_not_checkpoint_and_worker_folds_wal():
    path = os.path.join(tempfile.mkdtemp(prefix="manawow-sqlite-"), "w.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(x INTEGER)")
    con.commit()
    con.close()
    sqlite_tuning.install(path, checkpoint_interval=0.05)
    try:
        con = sqlite3.connect(path)
        assert con.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 0
        con.execute("INSERT INTO t VALUES(7)")
        con.commit()
        con.close()
        # The worker still holds the database open, so the WAL survives the
        # close instead of being checkpointed on the game thread.
        assert os.path.exists(path + "-wal")
    finally:
        sqlite_tuning.shutdown()
    # Shutdown folds the WAL back into the main file.
    assert not os.path.exists(path + "-wal") or os.path.getsize(path + "-wal") == 0
    raw = sqlite_tuning._original_connect(path)
    try:
        assert raw.execute("SELECT x FROM t").fetchall() == [(7,)]
    finally:
        raw.close()


def test_explicit_factory_is_respected():
    class Mine(sqlite3.Connection):
        pass

    sqlite_tuning.install()
    con = sqlite3.connect(":memory:", factory=Mine)
    try:
        assert type(con) is Mine
    finally:
        con.close()
