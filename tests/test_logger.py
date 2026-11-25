"""Logging setup: asyncio warnings belong to the world server log."""
from __future__ import annotations

import logging

from src.common import logger as logger_module


def test_asyncio_warnings_are_routed_to_the_world_log(tmp_path):
    asyncio_logger = logging.getLogger("asyncio")
    saved_handlers = list(asyncio_logger.handlers)
    saved_level = asyncio_logger.level
    saved_propagate = asyncio_logger.propagate
    auth = world = None
    try:
        auth = logger_module.setup_logger(
            "auth-log-routing-test", str(tmp_path / "auth"), console=False)
        world = logger_module.setup_logger(
            "world-log-routing-test", str(tmp_path / "world"),
            console=False, capture_asyncio=True)
        handlers = [handler for handler in asyncio_logger.handlers
                    if isinstance(handler, logging.FileHandler)]
        assert handlers
        assert all(str(tmp_path / "world") in handler.baseFilename
                   for handler in handlers)
        assert not any(str(tmp_path / "auth") in handler.baseFilename
                       for handler in handlers)
    finally:
        asyncio_logger.handlers[:] = saved_handlers
        asyncio_logger.setLevel(saved_level)
        asyncio_logger.propagate = saved_propagate
        for logger in (auth, world):
            if logger is None:
                continue
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.close()
