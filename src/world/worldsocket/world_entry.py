"""Entering/leaving the world, respawns and creature visibility."""
from __future__ import annotations

import sqlite3
import struct
import time

from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import login_packets
from src.world import object_update
from src.world import update_fields as F

from .constants import (
    CHAR_LOGIN_NO_CHARACTER,
    CREATURE_RESCAN_DISTANCE,
    CREATURE_VISIBILITY_RADIUS,
)

class WorldEntryMixin:
    async def handle_player_login(self, data: bytes):
        if not self.authenticated:
            return

        guid = struct.unpack_from("<Q", data, 0)[0] if len(data) >= 8 else 0
        character = character_db.get_character(
            self.config.database.world_path, self.account_id, guid
        )
        if character is None or self.in_world:
            self.log.warning(
                f"CMSG_PLAYER_LOGIN for guid {guid} rejected "
                f"(unknown, not owned by '{self.account_name}', or already in world)"
            )
            await self.send_packet(Opcodes.SMSG_CHARACTER_LOGIN_FAILED, bytes([CHAR_LOGIN_NO_CHARACTER]))
            return

        self.in_world = True
        self.player_guid = guid
        # Work on a mutable copy: movement updates have to change the position
        # of the in-memory character without re-reading the whole row.
        self.character = dict(character)
        # Load character's inventory for the create block (visible items + slots)
        inventory = character_db.get_character_inventory(
            self.config.database.world_path, guid
        )
        if inventory:
            # Item guid must be *stable* across a swap: the client keeps the
            # guid it first saw when the item moves, so we derive it from the
            # item's own DB row id, not from its current slot.
            mutable_inventory = {}
            for slot, item in inventory.items():
                item_dict = dict(item)
                item_dict["guid"] = F.ITEM_HIGH_GUID | (int(item_dict["id"]) & 0xFFFFFFFF)
                item_dict["owner_guid"] = guid
                mutable_inventory[slot] = item_dict
            self.character["inventory"] = mutable_inventory
        else:
            # First login: grant starter items and give them guids.
            character_db.grant_starter_items(self.config.database.world_path, guid, self.character["race"], self.character["class"])
            inventory = character_db.get_character_inventory(
                self.config.database.world_path, guid
            )
            mutable_inventory = {}
            for slot, item in inventory.items():
                item_dict = dict(item)
                item_dict["guid"] = F.ITEM_HIGH_GUID | (int(item_dict["id"]) & 0xFFFFFFFF)
                item_dict["owner_guid"] = guid
                mutable_inventory[slot] = item_dict
            self.character["inventory"] = mutable_inventory
        self.movement_info = None
        self.active_mover_guid = guid
        self._last_position_save = time.monotonic()
        self.world_entry_complete = False
        # Fresh combat state per login: the previous stay's corpses, cooldowns
        # and aggro list must not leak into the new session.
        if hasattr(self, "_combat_state"):
            del self._combat_state
        self._get_combat_state()
        self._apply_combat_stats()
        # ``Player::InitRunes`` runs for death knights before the create block;
        # the authoritative cooldowns follow the login burst as
        # ``SMSG_RESYNC_RUNES``.
        if self._is_death_knight():
            self._init_runes()
        self._despawn_sent = False
        self.log.info(
            f"Entering world with '{character['name']}' (guid {guid}, "
            f"map {character['map']} zone {character['zone']})"
        )
        flat_rate, interrupted_rate = self._mana_regen_rates()
        self.log.info(
            f"  derived: class {self.character.get('class')} level "
            f"{self.character.get('level')} health {self.character.get('health')}/"
            f"{self.character.get('max_health')} power {self.character.get('power')}/"
            f"{self.character.get('max_power')} base_mana {self.character.get('base_mana')} "
            f"stats {self.character.get('stats')} mana regen "
            f"{flat_rate:.3f}/s (interrupted {interrupted_rate:.3f}/s)"
        )

        # ``self.character`` rather than the raw row: the create block reads the
        # inventory we attached above, and a sqlite3.Row has no ``get``.
        # Static creatures are added to the same update packet so the client
        # receives the whole visible world in one atomic create update.
        creature_blocks = await self.send_npc_spawns()
        # Fetch known spells for this character
        known_spells = character_db.get_character_spells(
            self.config.database.world_path, self.player_guid
        )
        known_spell_ids = [int(row["spell"]) for row in known_spells]
        # The cast handler refuses anything the character has not learned.
        self.known_spell_ids = set(known_spell_ids)
        action_buttons = character_db.get_character_action_buttons(
            self.config.database.world_path, self.player_guid
        )
        for opcode, payload in login_packets.build_login_packets(
            self.character, self.config.worldserver.motd, creature_blocks,
            known_spell_ids, action_buttons,
        ):
            await self.send_packet(opcode, payload)
        if self._is_death_knight():
            await self._send_resync_runes()

        self.log.info(
            f"Sent world entry packets for '{character['name']}' "
            f"(SMSG_LOGIN_VERIFY_WORLD, player update object, time sync)"
        )

        # Cross-account visibility: show the players already in the world to
        # the newcomer (after its own create block, so the client's object
        # manager exists), then introduce the newcomer to them.  Marking the
        # burst complete first also lets a peer that logged in at the same
        # time deliver its own block instead of being skipped.
        self.world_entry_complete = True
        await self.send_peer_spawns()
        await self.announce_self_to_peers()

    async def handle_logout_request(self, data: bytes):
        if self.in_world:
            self.persist_position(force=True)
            # Remove pets/guardians while the session still has its map/guid so
            # peers receive the destroy packet.
            await self.dismiss_all_summons()
            # Remove the character from other clients before local state is
            # cleared; despawn_for_peers() needs this session's map/guid.
            await self.despawn_for_peers()
            self.log.info(f"Returning '{self.account_name}' to character selection")
            self.in_world = False
            self.world_entry_complete = False
            self.player_guid = None
            self.character = None
            self.movement_info = None
            self._visible_creatures = {}
            self._creature_spawn_cache = {}
            self._creature_scan_pos = None
            combat = self._get_combat_state()
            combat["idle"].clear()
            combat["idle_settle"].clear()
            combat["dots"].clear()
            combat.setdefault("area_effects", []).clear()
            combat["auras"] = []
            combat["creature_auras"] = {}
            combat["aura_state"] = {}
        await self.send_packet(Opcodes.SMSG_LOGOUT_RESPONSE, struct.pack("<IB", 0, 0))
        await self.send_packet(Opcodes.SMSG_LOGOUT_COMPLETE, b"")

    async def handle_set_active_mover(self, data: bytes):
        """``CMSG_SET_ACTIVE_MOVER``: the client tells us which unit it drives."""
        guid = struct.unpack_from("<Q", data, 0)[0] if len(data) >= 8 else 0
        self.active_mover_guid = guid
        if not self.in_world:
            return
        if guid != self.player_guid:
            # Possession/far sight would legitimately do this; we do not
            # implement either, so log it as a curiosity.
            self.log.warning(
                f"CMSG_SET_ACTIVE_MOVER for guid {guid}, "
                f"but the logged-in character is {self.player_guid}"
            )
        else:
            self.log.info(f"Active mover confirmed: guid {guid}")

    async def handle_repop_request(self, data: bytes):
        """``CMSG_REPOP_REQUEST``: release spirit and teleport to the graveyard.

        The client performs a map load on release.  The real core answers with
        ``SMSG_NEW_WORLD`` and waits for ``MSG_MOVE_WORLDPORT_ACK`` before it
        sends the new map contents; sending them too early makes the 3.3.5
        client hang on the loading screen.  Only the teleport is started here;
        :meth:`handle_worldport_ack` finishes the transfer once the client says
        it is ready.
        """
        if not self.in_world or not self.character:
            return
        combat = self._get_combat_state()
        if not combat["dead"]:
            return
        map_id, zone, x, y, z, o = character_db.start_position(
            int(self.character["race"]), int(self.character["class"])
        )
        self.character["map"] = map_id
        self.character["zone"] = zone
        self.character["x"] = x
        self.character["y"] = y
        self.character["z"] = z
        self.character["o"] = o
        self._apply_combat_stats()
        self.character["health"] = int(self.character["max_health"])
        self.character["power"] = self._power_after_reset()
        con = sqlite3.connect(self.config.database.world_path)
        try:
            con.execute(
                "UPDATE characters SET map = ?, zone = ?, x = ?, y = ?, z = ?, o = ?"
                " WHERE guid = ?",
                (map_id, zone, x, y, z, o, int(self.player_guid)),
            )
            con.commit()
        finally:
            con.close()

        self._repop_pending = True
        self.movement_info = None
        await self.send_packet(
            Opcodes.SMSG_NEW_WORLD,
            login_packets.build_login_verify_world(self.character),
        )
        self.log.info(
            f"'{self.character['name']}' released spirit at "
            f"map {map_id} zone {zone}, awaiting worldport ack"
        )

    async def handle_worldport_ack(self, data: bytes):
        """``MSG_MOVE_WORLDPORT_ACK``: the client finished loading the new map.

        Only now is it safe to send the create blocks and control packets; this
        is the second half of the release-spirit teleport started in
        :meth:`handle_repop_request`.  The payload is empty.
        """
        if not getattr(self, "_repop_pending", False):
            return
        self._repop_pending = False
        if not self.in_world or not self.character:
            return

        # Pets do not survive a death release/map transfer; remove them before
        # the world is rebuilt so the stale coordinates are not re-sent.
        await self.dismiss_all_summons()

        # Leaving the death state before re-sending the world keeps the combat
        # tick from immediately killing the player again.
        combat = self._get_combat_state()
        combat["dead"] = False
        combat["in_combat"] = False
        combat["auto_attacking"] = False
        combat["target_guid"] = 0
        combat["pending_cast"] = None
        combat["dots"] = []
        combat.setdefault("area_effects", []).clear()
        combat["auras"] = []
        combat["creature_auras"] = {}
        combat["aura_state"] = {}
        combat["aggro"].clear()
        # CMaNGOS resurrects with a full rune set; re-init and resync so the
        # client does not keep the cooldowns from before the death.
        if self._is_death_knight():
            self._init_runes()
        await self._close_loot()

        # Rebuild the visible world: destroy everything the client still has,
        # then create the new neighbourhood and the player itself again.
        for object_guid in list(self._visible_creatures.values()):
            await self.send_packet(
                Opcodes.SMSG_DESTROY_OBJECT,
                login_packets.build_destroy_object(object_guid, death=True),
            )
        self._visible_creatures = {}
        self._creature_spawn_cache = {}
        self._creature_scan_pos = None
        idle = self._get_combat_state()["idle"]
        idle.clear()
        self._get_combat_state()["idle_settle"].clear()

        opcode, payload = object_update.build_update_object(self.character,
                                                           self_player=True)
        await self.send_packet(opcode, payload)
        await self.send_packet(
            Opcodes.SMSG_CLIENT_CONTROL_UPDATE,
            login_packets.build_client_control_update(self.character),
        )
        await self._send_player_fields({
            F.UNIT_FIELD_HEALTH: int(self.character["health"]),
            self._power_field(F.UNIT_FIELD_POWER1): int(self.character["power"]),
            self._power_field(F.UNIT_FIELD_MAXPOWER1): int(self.character["max_power"]),
        })
        if self._is_death_knight():
            await self._send_resync_runes()
        await self.send_packet(
            Opcodes.SMSG_INIT_WORLD_STATES,
            login_packets.build_init_world_states(self.character),
        )
        await self.send_packet(
            Opcodes.SMSG_LOGIN_SETTIMESPEED,
            login_packets.build_login_set_time_speed(),
        )
        await self.send_packet(Opcodes.SMSG_TIME_SYNC_REQ, struct.pack("<I", 0))
        # uint32(-1) removes the spirit-healer marker from the minimap, exactly
        # like CMaNGOS ``Player::ResurrectPlayer``.
        await self.send_packet(
            Opcodes.SMSG_DEATH_RELEASE_LOC,
            struct.pack("<Ifff", 0xFFFFFFFF, 0.0, 0.0, 0.0),
        )
        await self.refresh_creature_visibility(force=True)
        await self.announce_self_to_peers()
        self.log.info(
            f"'{self.character['name']}' resumed at "
            f"map {self.character['map']} zone {self.character['zone']}"
        )

    async def send_npc_spawns(self) -> list[bytes]:
        """Return create blocks for the static creatures near the player."""
        if not self.character:
            return []
        point = self._creature_scan_point()
        if point is None:
            return []
        x, y, z = point
        spawns = creature.get_creature_spawns_near(
            self.config.database.world_path,
            int(self.character["map"]),
            x, y, z,
            CREATURE_VISIBILITY_RADIUS,
        )
        self._visible_creatures = {
            int(row["guid"]): creature.creature_guid(
                int(row["guid"]), int(row["entry"])
            )
            for row in spawns
        }
        self._creature_spawn_cache = {int(row["guid"]): row for row in spawns}
        self._creature_scan_pos = point
        if not spawns:
            return []
        blocks = object_update.build_creature_create_blocks(spawns)
        self.log.info(
            f"Adding {len(spawns)} static creature(s) to "
            f"'{self.character['name']}' on map {self.character['map']}"
        )
        return blocks

    async def refresh_creature_visibility(self, force: bool = False) -> None:
        """Send create/destroy updates when the player reaches new spawns.

        A full 3.3.5 spawn table holds ~100k creatures, so only a 150-yard
        neighbourhood is kept on the client.  The scan is skipped until the
        player has moved ``CREATURE_RESCAN_DISTANCE`` yards, keeping
        heartbeat processing cheap.
        """
        if not self.in_world or not self.character:
            return
        point = self._creature_scan_point()
        if point is None:
            return
        if not force and self._creature_scan_pos is not None:
            px, py, pz = self._creature_scan_pos
            dx, dy, dz = point[0] - px, point[1] - py, point[2] - pz
            if dx * dx + dy * dy + dz * dz < CREATURE_RESCAN_DISTANCE ** 2:
                return

        spawns = creature.get_creature_spawns_near(
            self.config.database.world_path,
            int(self.character["map"]),
            point[0], point[1], point[2],
            CREATURE_VISIBILITY_RADIUS,
        )
        current = {int(row["guid"]): row for row in spawns}
        # Summons are session-local and never appear in the spatial query, so
        # carry them across rescans instead of destroying them as "left range".
        current.update(getattr(self, "_summoned_creatures", {}))
        previous = self._visible_creatures

        newly_visible = [row for guid, row in current.items() if guid not in previous]
        if newly_visible:
            packet = object_update.build_creature_update(newly_visible)
            if packet is not None:
                opcode, payload = packet
                await self.send_packet(opcode, payload)
            self.log.info(
                f"Visibility: +{len(newly_visible)} creature(s) for "
                f"'{self.character['name']}'"
            )

        for guid, object_guid in previous.items():
            if guid in current:
                continue
            await self.send_packet(
                Opcodes.SMSG_DESTROY_OBJECT,
                login_packets.build_destroy_object(object_guid),
            )

        gone = sum(1 for guid in previous if guid not in current)
        if gone:
            self.log.info(
                f"Visibility: -{gone} creature(s) for "
                f"'{self.character['name']}'"
            )

        self._visible_creatures = {
            guid: creature.creature_guid(guid, int(row["entry"]))
            for guid, row in current.items()
        }
        self._creature_spawn_cache = current
        # Drop idle state for creatures that left the neighbourhood.
        idle = self._get_combat_state()["idle"]
        for guid in list(idle):
            if guid not in current:
                idle.pop(guid, None)
        self._creature_scan_pos = point

    def _creature_scan_point(self) -> tuple[float, float, float] | None:
        if not self.character:
            return None
        return (
            float(self.character["x"]),
            float(self.character["y"]),
            float(self.character["z"]),
        )
