"""Shared test cleanup."""
from __future__ import annotations

import pytest

from src.common import sqlite_tuning
from src.world import databases


@pytest.fixture(autouse=True)
def _close_shared_read_connections():
    """Do not keep temp SQLite files open across tests on Windows."""
    yield
    sqlite_tuning.close_read_connections()
    databases.reset()
