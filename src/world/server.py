"""Worldserver for WoW 3.3.5a."""
from __future__ import annotations

import asyncio
import errno
from typing import Optional

from src.common.config import Config
from src.common.logger import setup_logger
from src.common import sqlite_tuning
from src.world.realmsocket import WorldSocket
from src.world import character_db
from src.world import creature
from src.world import databases
from src.world import gameobject
from src.world import geometry
from src.world import quests
from src.world.groups import GroupManager
from src.world.world_combat import WorldCombatManager
from src.network.packet_log import get_packet_log


class WorldServer:
    def __init__(self, config: Config):
        self.config = config
        self.log = setup_logger("worldserver", config.logging.log_dir, config.logging.level, config.logging.console)
        self._server: Optional[asyncio.AbstractServer] = None
        self.sessions = []
        # One shared aggro/threat/target/position state per engaged creature.
        self.creature_combat = WorldCombatManager(self)
        # Party/raid membership (session-scoped until persisted group support).
        self.groups = GroupManager(self)
        # Runtime state of game objects (opened doors, looted chests).
        self.gameobjects = gameobject.GameObjectRuntime()
        # Which pooled creatures exist (rare spawns); loaded on first use.
        self.creature_pools = None
        self.packet_log = get_packet_log()
        self.packet_log.Initialize(
            config.logging.log_dir,
            enabled=getattr(config.logging, "packet_log", True))
        # Characters live in their own database when one is configured; every
        # character_db/quests call still receives the world path and is routed
        # by src.world.databases.
        world_path = config.database.world_path
        character_path = getattr(config.database, "character_path", "")
        character_db.configure(world_path, character_path)
        # WAL + synchronous=NORMAL: rollback-journal commits blocked the event
        # loop for seconds on Windows (see src/common/sqlite_tuning.py).
        sqlite_tuning.install(world_path, character_path or None)
        # Creature/spell tables (including the bundled 3.3.5a starter spells)
        # come first so the character backfill can read them.
        creature.init_creature_db(world_path)
        if databases.is_split():
            character_db.init_split_databases(world_path, character_path)
            quests.init_world_quest_db(world_path)
        else:
            # Combined database: keep the test/tool layout working.
            character_db.init_character_db(world_path)
            quests.init_quest_db(world_path)
        gameobject.init_gameobject_db(world_path)
        # Open the shared read connections before players connect: the first
        # connect on a cold Windows disk can take seconds (Defender scans the
        # Python process and database), and it must not happen on the loop
        # while a session is waiting for a query result.
        sqlite_tuning.warm_read(world_path)
        sqlite_tuning.warm_read(databases.character_path(world_path))
        self.log.info(
            f"World database: {world_path}; character database: "
            f"{databases.character_path(world_path)}")
        world_geometry = geometry.get_geometry(config,
                                               config.database.world_path)
        if world_geometry is None:
            self.log.info(
                "Client collision data not configured; LOS is clear and "
                "creatures use straight-line movement. Run "
                "tools/fetch_client_data.py to enable terrain/VMaps/MMaps.")
        else:
            self.log.info(
                f"Client collision data: terrain={world_geometry.terrain_ready}"
                f" vmaps={world_geometry.vmaps_ready}"
                f" mmaps={world_geometry.navmesh_ready}"
                f" ({world_geometry.data.root})")

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
                player_guid = int(getattr(session, "player_guid", 0) or 0)
                self.creature_combat.detach_session(player_guid)
                await self.groups.on_session_end(player_guid)
            except Exception:
                pass
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
