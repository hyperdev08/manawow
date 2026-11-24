#!/usr/bin/env python3
"""Start the worldserver.  ``--watchdog [SECONDS]`` logs event-loop stalls."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from src.common import stall_watchdog
from src.common.config import Config
from src.common.utils import ensure_dirs, pause_if_interactive
from src.network.packet_log import get_packet_log
from src.world.server import WorldServer


async def main(watchdog: float | None = None):
    cfg_path = os.path.join(os.path.dirname(__file__), "config", "config.yaml")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(os.path.dirname(__file__), "config", "config.example.yaml")
        print(f"config.yaml not found; using {cfg_path}")
    cfg = Config.load(cfg_path)
    cfg.resolve_database_paths(os.path.dirname(os.path.abspath(__file__)))
    character_path = getattr(cfg.database, "character_path", "")
    ensure_dirs(
        os.path.dirname(cfg.database.auth_path),
        os.path.dirname(cfg.database.world_path),
        os.path.dirname(character_path) if character_path else "",
        cfg.logging.log_dir,
    )
    stall_watchdog.start_for(watchdog, cfg.logging)
    server = WorldServer(cfg)
    await server.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    stall_watchdog.add_argument(parser)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.watchdog))
    except SystemExit:
        get_packet_log().Close()
        pause_if_interactive()
        raise
    except KeyboardInterrupt:
        pass
    finally:
        get_packet_log().Close()
