#!/usr/bin/env python3
"""Start the authserver and worldserver together in one terminal.

Equivalent to running ``run_auth.py`` and ``run_world.py`` in two separate
windows. Press Ctrl+C to stop both.

``--watchdog [SECONDS]`` logs the blocking stack to ``logs/stalls.log``
whenever the event loop stalls (see ``src/common/stall_watchdog.py``).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.common import db_io, geometry_io, stall_watchdog
from src.common.config import Config
from src.common.utils import ensure_dirs, pause_if_interactive
from src.network.packet_log import get_packet_log
from src.auth.server import AuthServer
from src.world.server import WorldServer


async def main(watchdog: float | None = None):
    cfg_path = os.path.join(os.path.dirname(__file__), "config", "config.yaml")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(os.path.dirname(__file__), "config", "config.example.yaml")
        print(f"config.yaml not found; using {cfg_path}")
    cfg = Config.load(cfg_path)
    # Resolve "./data/..." against the repository, not the launching CWD, so
    # double-clicking or running from another directory uses the same files.
    cfg.resolve_database_paths(os.path.dirname(os.path.abspath(__file__)))
    character_path = getattr(cfg.database, "character_path", "")
    ensure_dirs(
        os.path.dirname(cfg.database.auth_path),
        os.path.dirname(cfg.database.world_path),
        os.path.dirname(character_path) if character_path else "",
        cfg.logging.log_dir,
    )
    stall_watchdog.start_for(watchdog, cfg.logging)
    auth = AuthServer(cfg)
    world = WorldServer(cfg)
    await asyncio.gather(auth.start(), world.start())
    # Keep the event loop running
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    stall_watchdog.add_argument(parser)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.watchdog))
    except SystemExit:
        get_packet_log().Close()
        db_io.shutdown()
        geometry_io.shutdown()
        pause_if_interactive()
        raise
    except KeyboardInterrupt:
        pass
    finally:
        get_packet_log().Close()
        db_io.shutdown()
        geometry_io.shutdown()
