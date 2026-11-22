"""Worldserver for WoW 3.3.5a."""
from __future__ import annotations

import asyncio
import errno
from typing import Optional

from src.common.config import Config
from src.common.logger import setup_logger
from src.world.realmsocket import WorldSocket
from src.world import character_db
from src.world import creature
from src.network.packet_log import get_packet_log


class WorldServer:
    def __init__(self, config: Config):
        self.config = config
        self.log = setup_logger("worldserver", config.logging.log_dir, config.logging.level, config.logging.console)
        self._server: Optional[asyncio.AbstractServer] = None
        self.sessions = []
        self.packet_log = get_packet_log()
        self.packet_log.Initialize(config.logging.log_dir)
        # Creature/spell tables (including the bundled 3.3.5a starter spells)
        # come first so the character backfill can read them.
        creature.init_creature_db(config.database.world_path)
        # Ensure the character schema exists and is up to date (adds the
        # orientation column to databases created before world entry existed).
        character_db.init_character_db(config.database.world_path)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        self.log.info(f"World client connected: {peer}")
        session = WorldSocket(reader, writer, self.config, server=self)
        self.sessions.append(session)
        try:
            await session.handle()
        except Exception as e:
            self.log.exception(f"World client error: {e}")
        finally:
            try:
                if session in self.sessions:
                    self.sessions.remove(session)
            except Exception:
                pass
            self.log.info(f"World client disconnected: {peer}")

    async def start(self):
        try:
            self._server = await asyncio.start_server(
                self.handle_client, self.config.worldserver.host, self.config.worldserver.port
            )
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10048 or exc.errno == errno.EADDRINUSE:
                self.log.error(
                    f"Port {self.config.worldserver.port} is already in use. "
                    "Another worldserver is still running - run 'python tools/stop_servers.py' "
                    "or close the other server window, then start this one again."
                )
                raise SystemExit(1)
            raise
        addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        self.log.info(f"WorldServer listening on {addrs} (motd: {self.config.worldserver.motd})")
        self.log.info(f"Ready for client connections on port {self.config.worldserver.port}")
        async with self._server:
            await self._server.serve_forever()
