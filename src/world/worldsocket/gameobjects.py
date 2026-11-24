"""Game objects: visibility and ``CMSG_GAMEOBJECT_QUERY``."""
from __future__ import annotations

import asyncio
import struct

from src.protocol.opcodes import Opcodes
from src.world import gameobject
from src.world import login_packets


class GameObjectsMixin:
    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------
    def _gameobject_scan_point(self) -> tuple[int, float, float, float] | None:
        if not self.character:
            return None
        return (
            int(self.character["map"]),
            float(self.character["x"]),
            float(self.character["y"]),
            float(self.character["z"]),
        )

    async def _gameobjects_near(self, point) -> list[dict]:
        map_id, x, y, z = point
        path = self.config.database.world_path
        if not gameobject.is_map_loaded(path, map_id):
            # The first visit to a map reads all of its spawns; do it off the
            # event loop so other players do not freeze while it loads.
            await asyncio.to_thread(gameobject.load_map, path, map_id)
        return gameobject.gameobjects_near(path, map_id, x, y, z)

    async def send_gameobject_spawns(self) -> list[bytes]:
        """Create blocks for the objects around the player (login burst)."""
        point = self._gameobject_scan_point()
        if point is None:
            return []
        try:
            spawns = await self._gameobjects_near(point)
        except Exception as exc:
            self.log.warning(f"Could not load game objects: {exc}")
            return []
        self._visible_gameobjects = {
            int(s["guid"]): gameobject.gameobject_guid(s["guid"], s["entry"])
            for s in spawns
        }
        self._gameobject_scan_pos = point
        if spawns:
            self.log.info(
                f"Adding {len(spawns)} game object(s) to "
                f"'{self.character['name']}' on map {point[0]}"
            )
        return [gameobject.build_gameobject_create_block(s) for s in spawns]

    async def refresh_gameobject_visibility(self, force: bool = False) -> None:
        """Create objects that came into range and destroy those that left."""
        if not self.in_world or not self.character:
            return
        point = self._gameobject_scan_point()
        if point is None:
            return
        last = self._gameobject_scan_pos
        if not force and last is not None and last[0] == point[0]:
            dx, dy, dz = point[1] - last[1], point[2] - last[2], point[3] - last[3]
            if dx * dx + dy * dy + dz * dz < gameobject.GAMEOBJECT_RESCAN_DISTANCE ** 2:
                return
        # Claim the scan before awaiting a map load so the heartbeats that
        # arrive meanwhile do not start the same scan again.
        self._gameobject_scan_pos = point
        try:
            spawns = await self._gameobjects_near(point)
        except Exception as exc:
            self.log.warning(f"Could not load game objects: {exc}")
            return
        if not self.in_world or not self.character:
            return
        current = {int(s["guid"]): s for s in spawns}
        previous = self._visible_gameobjects

        new = [s for guid, s in current.items() if guid not in previous]
        packet = gameobject.build_gameobject_update(new)
        if packet is not None:
            await self.send_packet(*packet)
        gone = [guid for guid in previous if guid not in current]
        for guid in gone:
            await self.send_packet(
                Opcodes.SMSG_DESTROY_OBJECT,
                login_packets.build_destroy_object(previous[guid]),
            )
        if new or gone:
            self.log.info(
                f"Visibility: +{len(new)} -{len(gone)} game object(s) for "
                f"'{self.character['name']}'"
            )
        self._visible_gameobjects = {
            guid: gameobject.gameobject_guid(guid, s["entry"])
            for guid, s in current.items()
        }

    async def destroy_visible_gameobjects(self) -> None:
        """Remove every object from the client (world rebuild)."""
        for object_guid in list(self._visible_gameobjects.values()):
            await self.send_packet(
                Opcodes.SMSG_DESTROY_OBJECT,
                login_packets.build_destroy_object(object_guid),
            )
        self.forget_gameobjects()

    def forget_gameobjects(self) -> None:
        self._visible_gameobjects = {}
        self._gameobject_scan_pos = None

    # ------------------------------------------------------------------
    # Opcode handlers
    # ------------------------------------------------------------------
    async def handle_gameobject_query(self, data: bytes):
        """``CMSG_GAMEOBJECT_QUERY``: ``uint32 entry, uint64 guid``."""
        if len(data) < 4:
            return
        entry = struct.unpack_from("<I", data, 0)[0]
        template = gameobject.get_gameobject_template(
            self.config.database.world_path, entry)
        if template is None:
            self.log.debug(f"Game object query for unknown entry {entry}")
        await self.send_packet(
            Opcodes.SMSG_GAMEOBJECT_QUERY_RESPONSE,
            gameobject.build_gameobject_query_response(entry, template),
        )
