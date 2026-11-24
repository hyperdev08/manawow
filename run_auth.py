#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import sys

# Ensure src importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from src.common import db_io
from src.common.config import Config
from src.common.utils import ensure_dirs, pause_if_interactive
from src.network.packet_log import get_packet_log
from src.auth.server import AuthServer


async def main():
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
    server = AuthServer(cfg)
    await server.start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        get_packet_log().Close()
        db_io.shutdown()
        pause_if_interactive()
        raise
    except KeyboardInterrupt:
        pass
    finally:
        get_packet_log().Close()
        db_io.shutdown()
