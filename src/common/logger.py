"""Simple logger setup."""
from __future__ import annotations

import logging
import os
from datetime import datetime


def _capture_asyncio(fh: logging.FileHandler) -> None:
    """Route asyncio transport warnings to the process log that owns ``fh``."""
    asyncio_logger = logging.getLogger("asyncio")
    for handler in list(asyncio_logger.handlers):
        if handler is not fh:
            asyncio_logger.removeHandler(handler)
    if fh not in asyncio_logger.handlers:
        asyncio_logger.addHandler(fh)
    asyncio_logger.setLevel(logging.WARNING)
    asyncio_logger.propagate = False


def setup_logger(name: str, log_dir: str = "./logs", level: str = "INFO",
                 console: bool = True,
                 capture_asyncio: bool = False) -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        if capture_asyncio:
            for handler in logger.handlers:
                if isinstance(handler, logging.FileHandler):
                    _capture_asyncio(handler)
                    break
        return logger
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(os.path.join(log_dir, f"manawow_{datetime.now().strftime('%Y%m%d')}.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # asyncio's transport/proactor logger has no handler of its own.  Without
    # this it falls through to logging.lastResort, which writes to the console
    # synchronously; a socket send warning was measured blocking the event loop
    # for almost half a second on Windows.  Only the world server opts in so the
    # shared handler always points at the world log, not the auth log.
    if capture_asyncio:
        _capture_asyncio(fh)

    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return logger
