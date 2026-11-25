"""Flight masters: discovering nodes, the flight map and taxi flights."""
from __future__ import annotations

import asyncio
import inspect
import sqlite3
import struct
import time

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import geometry
from src.world import object_update
from src.world import taxi
from src.world import update_fields as F
from src.world.worldsocket.constants import SPELL_FAILED_NOT_ON_TAXI

# How often the server moves its copy of a flying player (visibility updates).
FLIGHT_TICK_SECONDS = 1.0
TAXI_INTERACT_DISTANCE = 15.0


class TaxiMixin:
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _taxi_data(self):
        return taxi.get_taxi_data(geometry.configured_data_dir(self.config) or "")

    def _taxi_db_path(self) -> str:
        # The world path; databases.connect_character routes it to
        # characters.db when world and character data are split.
        return self.config.database.world_path

    def _taxi_alliance(self) -> bool:
        return taxi.is_alliance(int(self.character.get("race", 1)))

    async def _taxi_known(self) -> set:
        known = getattr(self, "_taxi_known_nodes", None)
        if known is None:
            # Seeds the race's starting nodes on first load, so it is a write.
            known = await run_db_write(
                taxi.load_known_nodes,
                self._taxi_db_path(), int(self.player_guid),
                int(self.character.get("race", 1)),
                int(self.character.get("level", 1)))
            self._taxi_known_nodes = known
        return known

    def _flight_master(self, guid: int):
        """The spawn behind ``guid`` if it is a flight master, else ``None``."""
        spawn = creature.get_creature_spawn(
            self.config.database.world_path, creature.creature_low_guid(guid))
        if spawn is None or not int(spawn["npc_flags"]) & taxi.NPC_FLAG_FLIGHTMASTER:
            return None
        return spawn

    def _flight_master_node(self, guid: int, spawn=None) -> int:
        data = self._taxi_data()
        spawn = spawn if spawn is not None else self._flight_master(guid)
        if data is None or spawn is None:
            return 0
        return data.nearest_node(int(spawn["map"]), float(spawn["x"]),
                                 float(spawn["y"]), float(spawn["z"]),
                                 self._taxi_alliance())

    def _flight_master_in_range(self, guid: int) -> bool:
        low_guid = creature.creature_low_guid(guid)
        return self._distance(self._player_position(),
                              self._creature_position(low_guid)) <= TAXI_INTERACT_DISTANCE

    def _in_flight(self) -> bool:
        return getattr(self, "_taxi_flight", None) is not None

    async def _learn_taxi_node(self, guid: int, node: int) -> bool:
        """``WorldSession::SendLearnNewTaxiNode``: True when ``node`` was new."""
        known = await self._taxi_known()
        if not node or node in known:
            return False
        known.add(node)
        await run_db_write(taxi.save_known_node, self._taxi_db_path(),
                           int(self.player_guid), node)
        await self.send_packet(Opcodes.SMSG_NEW_TAXI_PATH, b"")
        await self.send_packet(Opcodes.SMSG_TAXINODE_STATUS,
                               taxi.build_taxinode_status(guid, True))
        self.log.info(f"'{self.character['name']}' discovered flight node {node}")
        return True

    async def _send_taxi_menu(self, guid: int) -> None:
        """Learn the flight master's node if needed, then open the flight map."""
        spawn = self._flight_master(guid)
        node = self._flight_master_node(guid, spawn)
        if not node:
            self.log.debug(f"No taxi node near flight master {guid}")
            return
        await self._learn_taxi_node(guid, node)
        await self.send_packet(
            Opcodes.SMSG_SHOWTAXINODES,
            taxi.build_show_taxi_nodes(guid, node, (await self._taxi_known())))

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------
    async def handle_taxinode_status_query(self, data: bytes):
        """``CMSG_TAXINODE_STATUS_QUERY``: does the player know this master's node?"""
        if not self.in_world or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        node = self._flight_master_node(guid)
        if not node:
            return
        await self.send_packet(
            Opcodes.SMSG_TAXINODE_STATUS,
            taxi.build_taxinode_status(guid, node in (await self._taxi_known())))

    async def handle_taxi_query_available_nodes(self, data: bytes):
        """``CMSG_TAXIQUERYAVAILABLENODES``: right-click on a flight master."""
        if not self.in_world or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        if self._flight_master(guid) is None or not self._flight_master_in_range(guid):
            return
        await self._send_taxi_menu(guid)

    async def handle_gossip_hello(self, data: bytes):
        # A flight master the player has not met yet teaches its node and
        # opens the map straight away, like TrinityCore's SendLearnNewTaxiNode.
        if self.in_world and len(data) >= 8:
            guid = struct.unpack_from("<Q", data, 0)[0]
            spawn = self._flight_master(guid)
            if spawn is not None and self._taxi_data() is not None:
                node = self._flight_master_node(guid, spawn)
                if node and node not in (await self._taxi_known()):
                    await self._send_taxi_menu(guid)
                    return
        await super().handle_gossip_hello(data)

    async def handle_gossip_select_option(self, data: bytes):
        if self.in_world and len(data) >= 16:
            guid, menu_id, option_index = struct.unpack_from("<QII", data, 0)
            option = creature.get_gossip_option(
                self.config.database.world_path, menu_id, option_index)
            if (option is not None
                    and int(option["option_type"]) == taxi.GOSSIP_OPTION_TAXIVENDOR
                    and self._flight_master(guid) is not None):
                await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")
                await self._send_taxi_menu(guid)
                return
        await super().handle_gossip_select_option(data)

    # ------------------------------------------------------------------
    # Nothing to do on a gryphon (Spell::CheckCast: SPELL_FAILED_NOT_ON_TAXI)
    # ------------------------------------------------------------------
    async def handle_cast_spell(self, data: bytes):
        if self._in_flight() and len(data) >= 5:
            cast_id = data[0]
            spell_id = struct.unpack_from("<I", data, 1)[0]
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_NOT_ON_TAXI)
            return
        await super().handle_cast_spell(data)

    async def handle_attack_swing(self, data: bytes):
        if self._in_flight():
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_CANT_ATTACK, b"")
            return
        await super().handle_attack_swing(data)

    async def handle_use_item(self, data: bytes):
        if self._in_flight():
            return
        await super().handle_use_item(data)

    async def handle_cancel_mount_aura(self, data: bytes):
        """``CMSG_CANCEL_MOUNT_AURA``: the client dismounts itself on a cast.

        Pressing a spell while mounted makes the 3.3.5 client drop the rider
        from the mount locally and ask the server to cancel it, even on a
        gryphon.  The server keeps the flight, so it puts the rider back:
        clearing and re-sending the mount display re-applies the mounted pose
        (otherwise the rider "swims" for the rest of the flight).
        """
        flight = getattr(self, "_taxi_flight", None)
        if flight is None:
            return
        display = int(flight.get("display") or 0)
        await self._set_flight_fields(0, True)
        await self._set_flight_fields(display, True)

    async def handle_activate_taxi(self, data: bytes):
        parsed = taxi.parse_activate_taxi(data)
        if parsed is not None:
            await self._activate_taxi(*parsed)

    async def handle_activate_taxi_express(self, data: bytes):
        parsed = taxi.parse_activate_taxi_express(data)
        if parsed is not None:
            await self._activate_taxi(*parsed)

    async def _taxi_reply(self, error: int) -> None:
        await self.send_packet(Opcodes.SMSG_ACTIVATETAXIREPLY,
                               taxi.build_activate_taxi_reply(error))

    async def _activate_taxi(self, guid: int, nodes: list) -> None:
        """``Player::ActivateTaxiPathTo``: validate, charge and take off."""
        if not self.in_world or not self.character:
            return
        data = self._taxi_data()
        spawn = self._flight_master(guid)
        if data is None or spawn is None:
            await self._taxi_reply(taxi.ERR_TAXINOVENDORNEARBY)
            return
        if not self._flight_master_in_range(guid):
            await self._taxi_reply(taxi.ERR_TAXITOOFARAWAY)
            return
        combat = self._get_combat_state()
        if self._in_flight() or combat.get("dead") or combat.get("in_combat"):
            await self._taxi_reply(taxi.ERR_TAXIPLAYERBUSY)
            return
        if nodes[0] != self._flight_master_node(guid, spawn):
            await self._taxi_reply(taxi.ERR_TAXIUNSPECIFIEDSERVERERROR)
            return
        if nodes[0] == nodes[-1]:
            await self._taxi_reply(taxi.ERR_TAXISAMENODE)
            return
        known = await self._taxi_known()
        if any(node not in known for node in nodes):
            await self._taxi_reply(taxi.ERR_TAXINOTVISITED)
            return
        route = data.route(nodes, self._taxi_alliance())
        if route is None:
            await self._taxi_reply(taxi.ERR_TAXINOSUCHPATH)
            return
        if route.cost > int(self.character.get("money", 0)):
            await self._taxi_reply(taxi.ERR_TAXINOTENOUGHMONEY)
            return
        display = _mount_display(self.config.database.world_path, route.mount_entry)
        if not display:
            await self._taxi_reply(taxi.ERR_TAXIUNSPECIFIEDSERVERERROR)
            return

        if route.cost:
            await self._modify_money(-route.cost)
        await self._taxi_reply(taxi.ERR_TAXIOK)
        await self._start_flight(route, display)

    # ------------------------------------------------------------------
    # The flight
    # ------------------------------------------------------------------
    async def _set_flight_fields(self, display: int, flying: bool) -> None:
        state = self._get_combat_state().setdefault("aura_state", {})
        current = int(state.get("flags", object_update.UNIT_FLAG_PLAYER_CONTROLLED))
        flags = (current | taxi.FLIGHT_UNIT_FLAGS if flying
                 else current & ~taxi.FLIGHT_UNIT_FLAGS)
        state["flags"] = flags
        changed = {F.UNIT_FIELD_FLAGS: flags, F.UNIT_FIELD_MOUNTDISPLAYID: int(display)}
        opcode, payload = object_update.build_player_values_update(
            self.character, changed, force_uncompressed=True)
        await self.send_packet(opcode, payload)
        await self.send_packet_to_peers(opcode, payload)

    async def _start_flight(self, route, display: int) -> None:
        start = self._player_position()
        duration_ms = taxi.flight_duration_ms(start, route.points)
        track = taxi.FlightTrack(start, route.points, duration_ms)
        self._taxi_flight = {"track": track, "started": time.monotonic(),
                             "route": route, "display": int(display)}
        await self._set_flight_fields(display, True)
        packet = taxi.build_flight_move(int(self.player_guid), start, route.points,
                                        duration_ms, move_id=self._next_move_id())
        await self.send_packet(Opcodes.SMSG_MONSTER_MOVE, packet)
        await self.send_packet_to_peers(Opcodes.SMSG_MONSTER_MOVE, packet)
        self._taxi_flight["task"] = asyncio.create_task(self._flight_loop())
        self.log.info(
            f"'{self.character['name']}' flies {route.nodes} for {route.cost} copper "
            f"({duration_ms / 1000:.0f} s)")

    async def _flight_loop(self) -> None:
        try:
            while True:
                flight = self._taxi_flight
                if flight is None:
                    return
                elapsed = time.monotonic() - flight["started"]
                if elapsed >= flight["track"].duration:
                    break
                x, y, z = flight["track"].position(elapsed)
                self.character["x"], self.character["y"], self.character["z"] = x, y, z
                await self.refresh_creature_visibility()
                await asyncio.sleep(min(FLIGHT_TICK_SECONDS,
                                        flight["track"].duration - elapsed))
            await self._land()
        except asyncio.CancelledError:
            raise
        except Exception as exc:              # never leave a player stuck in the air
            self.log.exception(f"Taxi flight failed: {exc}")
            await self._land()

    async def _land(self) -> None:
        """``FlightPathMovementGenerator::DoFinalize``: dismount at the last node."""
        flight = self._taxi_flight
        if flight is None:
            return
        self._taxi_flight = None
        x, y, z = flight["track"].destination
        self.character["x"], self.character["y"], self.character["z"] = x, y, z
        await self._set_flight_fields(0, False)
        stop = taxi.build_move_stop(int(self.player_guid), (x, y, z),
                                    move_id=self._next_move_id())
        await self.send_packet(Opcodes.SMSG_MONSTER_MOVE, stop)
        await self.send_packet_to_peers(Opcodes.SMSG_MONSTER_MOVE, stop)
        result = self.persist_position(force=True)
        if inspect.isawaitable(result):
            await result
        await self.refresh_creature_visibility(force=True)
        self.log.info(f"'{self.character['name']}' landed at node {flight['route'].nodes[-1]}")

    def _abandon_flight(self) -> None:
        """Stop a flight without landing packets; the player stays at its end.

        A player who logs out (or disconnects) in the air is saved at the
        destination instead of wherever the spline was at that moment.
        """
        flight = getattr(self, "_taxi_flight", None)
        if flight is None:
            return
        self._taxi_flight = None
        task = flight.get("task")
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if self.character:
            x, y, z = flight["track"].destination
            self.character["x"], self.character["y"], self.character["z"] = x, y, z

    async def handle_logout_request(self, data: bytes):
        self._abandon_flight()
        await super().handle_logout_request(data)
        self._taxi_known_nodes = None        # the next character has its own

    def stop_combat_loop(self) -> None:
        # Called first when the socket closes, before the position is saved.
        self._abandon_flight()
        super().stop_combat_loop()


def _mount_display(world_path: str, entry: int) -> int:
    """Model of the taxi mount creature (``creature_templates.display_id1``)."""
    con = sqlite3.connect(world_path)
    try:
        row = con.execute(
            "SELECT display_id1 FROM creature_templates WHERE entry = ?",
            (int(entry),)).fetchone()
    finally:
        con.close()
    return int(row[0] or 0) if row else 0
