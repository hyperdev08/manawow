"""Client movement relay and position persistence."""
from __future__ import annotations

import struct
import time

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import login_packets
from src.world import movement

from .constants import (
    MOVEMENT_OPCODES_QUIET,
    MOVEMENT_OPCODES_TIME_ONLY,
    MOVEMENT_OPCODES_WITHOUT_GUID,
    POSITION_SAVE_INTERVAL,
    SPELL_FAILED_INTERRUPTED,
)

class PlayerMovementMixin:
    async def handle_zone_update(self, data: bytes):
        """``CMSG_ZONEUPDATE``: the client walked into a new zone/area."""
        if not self.in_world or not self.character:
            return
        zone = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else 0
        if zone == 0 or zone == self.character.get("zone"):
            return
        self.character["zone"] = zone
        self.log.info(
            f"'{self.character['name']}' entered zone {zone} "
            f"(map {self.character['map']})"
        )
        await self.persist_position(force=True)
        # Keep the minimap/PvP pane in sync with the new zone.
        await self.send_packet(
            Opcodes.SMSG_INIT_WORLD_STATES,
            login_packets.build_init_world_states(self.character),
        )

    async def handle_movement(self, opcode: int, data: bytes):
        """Accept a client movement packet and remember the new position.

        The client is authoritative about its own position: it predicts
        movement locally and expects the server to store/relay the result.
        No acknowledgement is required for normal walking, jumping or turning.
        """
        if not self.in_world or not self.character:
            return

        name = Opcodes(opcode).name if opcode in Opcodes._value2member_map_ else f"0x{opcode:04X}"
        guid = None
        info = None

        if opcode in MOVEMENT_OPCODES_TIME_ONLY:
            if len(data) >= 4:
                ack_time = struct.unpack_from("<I", data, -4)[0]
                self.log.debug(f"{name}: ack time {ack_time}")
            return

        try:
            if opcode in MOVEMENT_OPCODES_WITHOUT_GUID:
                info, _ = movement.parse_movement_info(data, 0)
            elif name.startswith("MSG_MOVE_"):
                guid, info = movement.parse_movement_packet(data)
            else:
                guid, info, _shape = movement.parse_movement_packet_leniently(data)
        except movement.MovementParseError as exc:
            self.log.warning(f"{name}: could not parse movement info ({exc})")
            return

        if info is None:
            self.log.debug(f"{name}: no movement info")
            return

        if guid not in (None, 0, self.player_guid):
            self.log.warning(
                f"{name}: movement for guid {guid} ignored "
                f"(active mover is {self.player_guid})"
            )
            return

        if not info.is_valid():
            self.log.warning(
                f"{name}: implausible position ({info.x}, {info.y}, {info.z}) ignored"
            )
            return

        self.movement_info = info
        old_x = float(self.character.get("x", info.x) or 0.0)
        old_y = float(self.character.get("y", info.y) or 0.0)
        moved = ((info.x - old_x) ** 2 + (info.y - old_y) ** 2) ** 0.5
        if (hasattr(self, "_player_can_move")
                and not self._player_can_move() and moved > 0.5):
            self.log.debug(
                f"{name}: movement rejected while rooted/stunned")
            return
        self.character["x"] = info.x
        self.character["y"] = info.y
        self.character["z"] = info.z
        self.character["o"] = info.o

        # Moving cancels a normal cast or channel unless the spell is flagged
        # uninterruptible.  The client sends a movement packet for tiny
        # corrections as well, so only a real displacement triggers it.
        if moved > 0.5:
            combat = self._get_combat_state()
            if combat.get("pending_cast") or combat.get("channel"):
                if hasattr(self, "_interrupt_player_cast"):
                    await self._interrupt_player_cast(
                        SPELL_FAILED_INTERRUPTED)

        message = f"{name}: {info.describe()}"
        if opcode in MOVEMENT_OPCODES_QUIET:
            self.log.debug(message)
        else:
            self.log.info(message)

        await self.persist_position()

        # Relay the same MSG_MOVE_* packet to the other players.  The client
        # expects a packed guid followed by the MovementInfo, exactly what it
        # itself sent; only server-to-client movement opcodes (MSG_MOVE_*)
        # are broadcast, never CMSG_MOVE_* control packets.
        if name.startswith("MSG_MOVE_"):
            relay = movement.build_movement_packet(int(self.player_guid), info)
            await self.send_packet_to_peers(opcode, relay)

        # Load/unload static creatures as the player crosses the visibility
        # boundary.  The distance throttle inside the method makes this a cheap
        # no-op for most heartbeats.
        await self.refresh_creature_visibility()

    async def persist_position(self, force: bool = False):
        """Write the current position to the character row (throttled)."""
        if not self.character or not self.player_guid:
            return
        now = time.monotonic()
        if not force and (now - self._last_position_save) < POSITION_SAVE_INTERVAL:
            return
        self._last_position_save = now
        try:
            await run_db_write(
                character_db.update_character_position,
                self.config.database.world_path,
                int(self.player_guid),
                float(self.character["x"]),
                float(self.character["y"]),
                float(self.character["z"]),
                float(self.character["o"]),
                map_id=int(self.character["map"]),
                zone=int(self.character["zone"]),
            )
        except Exception as exc:
            self.log.warning(f"Could not persist position: {exc}")
