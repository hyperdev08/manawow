"""Authserver for WoW 3.3.5a (logon/realmd)."""
from __future__ import annotations

import asyncio
import errno
import os
from typing import Optional

from src.common.config import Config
from src.common.logger import setup_logger
from src.auth.realmd import AuthSession
from src.network.packet_log import get_packet_log


class AuthServer:
    def __init__(self, config: Config):
        self.config = config
        self.log = setup_logger("authserver", config.logging.log_dir, config.logging.level, config.logging.console)
        self._server: Optional[asyncio.AbstractServer] = None
        self.packet_log = get_packet_log()
        self.packet_log.Initialize(
            config.logging.log_dir,
            enabled=getattr(config.logging, "packet_log", True))

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        self.log.info(f"Auth client connected: {peer}")
        session = AuthSession(reader, writer, self.config)
        try:
            await session.handle()
        except Exception as e:
            self.log.exception(f"Auth client error: {e}")
        finally:
            self.log.info(f"Auth client disconnected: {peer}")

    async def start(self):
        try:
            self._server = await asyncio.start_server(
                self.handle_client, self.config.authserver.host, self.config.authserver.port
            )
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10048 or exc.errno == errno.EADDRINUSE:
                self.log.error(
                    f"Port {self.config.authserver.port} is already in use. "
                    "Another authserver is still running - run 'python tools/stop_servers.py' "
                    "or close the other server window, then start this one again."
                )
                raise SystemExit(1)
            raise
        addrs = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        self.log.info(f"AuthServer listening on {addrs} (realm '{self.config.authserver.realm_name}')")
        self.log.info(f"Ready for client connections on port {self.config.authserver.port}")
        async with self._server:
            await self._server.serve_forever()
