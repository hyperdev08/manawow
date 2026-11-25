"""Misc utils."""
from __future__ import annotations

import os
import sys


def ensure_dirs(*paths: str) -> None:
    for p in paths:
        if not p:
            continue
        os.makedirs(p, exist_ok=True)


def add_src_to_path() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)


def pause_if_interactive(message: str = "Press Enter to close...") -> None:
    """Keep a double-clicked console window open so startup errors stay readable."""
    try:
        if sys.stdin and sys.stdin.isatty():
            input(message)
    except (EOFError, KeyboardInterrupt, OSError):
        pass
