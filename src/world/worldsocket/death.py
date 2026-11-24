"""Player death, ghosts, corpses, reclaiming and spirit healers."""
from __future__ import annotations

import asyncio
import math
import sqlite3
import struct
import time

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import graveyard
from src.world import login_packets
from src.world import spells
from src.world import update_fields as F
from src.world import object_update

CORPSE_RECLAIM_RADIUS = 39.0
CORPSE_RECLAIM_DELAY = 30.0
CORPSE_VISIBILITY_RADIUS = 100.0
SPIRIT_HEALER_INTERACT_RANGE = 30.0
SPIRIT_HEALER_NPC_FLAG = 0x4000
SPIRIT_HEAL_RESURRECT_PERCENT = 0.5


def _write_item_durability(path: str, rows) -> None:
    con = sqlite3.connect(path)
    try:
        con.executemany(
            "UPDATE character_items SET durability = ? WHERE id = ?", rows)
        con.commit()
    finally:
        con.close()


class DeathMixin:
    # ------------------------------------------------------------------
    # Corpse and graveyard helpers
    # ------------------------------------------------------------------
    async def _corpse(self):
        """Return the cached corpse row, loading it from the database once."""
        if not self.character:
            return None
        combat = self._get_combat_state()
        corpse = combat.get("corpse")
        if corpse is not None:
            return corpse
        guid = int(self.player_guid or 0)
        if not guid:
            return None
        corpse = await asyncio.to_thread(
            character_db.get_character_corpse,
            self.config.database.world_path, guid)
        if corpse is not None:
            corpse = dict(corpse)
            corpse.setdefault("owner_guid", int(corpse["character_guid"]))
        combat["corpse"] = corpse
        return corpse

    async def _nearest_graveyard(self):
        if not self.character:
            return None
        return await asyncio.to_thread(
            graveyard.closest_graveyard,
            self.config.database.world_path,
            int(self.character.get("map", 0) or 0),
            int(self.character.get("zone", 0) or 0),
            float(self.character.get("x", 0.0) or 0.0),
            float(self.character.get("y", 0.0) or 0.0),
            float(self.character.get("z", 0.0) or 0.0),
            int(self.character.get("race", 1) or 1),
        )

    def _grave_or_start(self, grave):
        if grave is not None:
            return grave
        map_id, zone, x, y, z, o = character_db.start_position(
            int(self.character.get("race", 1) or 1),
            int(self.character.get("class", 1) or 1),
        )
        return {"id": 0, "map": map_id, "zone": zone, "x": x, "y": y,
                "z": z, "o": o, "name": "Starting graveyard"}

    def _corpse_object_data(self, row) -> dict:
        """Build the client-facing corpse object from a character/corpse row."""
        owner = int(row.get("owner_guid")
                    or row.get("character_guid")
                    or row.get("guid") or 0)
        items = row.get("items")
        if items is None:
            inventory = (row.get("inventory")
                         if row is self.character else None)
            if inventory:
                items = []
                for slot in range(19):
                    item = inventory.get(slot)
                    items.append(int(item.get("item_entry") or 0) if item else 0)
            else:
                items = character_db.get_equipment_entries(
                    self.config.database.world_path, owner)
        skin = int(row.get("skin", 0) or 0) & 0xFF
        face = int(row.get("face", 0) or 0) & 0xFF
        hair = int(row.get("hair_style", 0) or 0) & 0xFF
        color = int(row.get("hair_color", 0) or 0) & 0xFF
        return {
            "owner_guid": owner,
            "map": int(row.get("map", 0) or 0),
            "x": float(row.get("x", 0.0) or 0.0),
            "y": float(row.get("y", 0.0) or 0.0),
            "z": float(row.get("z", 0.0) or 0.0),
            "o": float(row.get("o", 0.0) or 0.0),
            "display_id": object_update.player_display_id(row),
            "items": list(items or []),
            "bytes1": skin | (face << 8) | (hair << 16) | (color << 24),
            "bytes2": int(row.get("facial_style", 0) or 0) & 0xFF,
            "guild": int(row.get("guild_id", 0) or 0),
        }

    async def _broadcast_corpse_create(self, corpse: dict) -> None:
        server = getattr(self, "server", None)
        sessions = getattr(server, "sessions", ()) if server is not None else ()
        for session in list(sessions):
            if session is self or not getattr(session, "in_world", False):
                continue
            character = getattr(session, "character", None)
            if not character or int(character.get("map", 0) or 0) != int(
                    corpse["map"]):
                continue
            if math.dist(
                    (float(character.get("x", 0.0) or 0.0),
                     float(character.get("y", 0.0) or 0.0),
                     float(character.get("z", 0.0) or 0.0)),
                    (corpse["x"], corpse["y"], corpse["z"])) \
                    > CORPSE_VISIBILITY_RADIUS:
                continue
            try:
                opcode, payload = object_update.build_corpse_update(corpse)
                await session.send_packet(opcode, payload)
                visible = getattr(session, "_visible_corpses", None)
                if visible is None:
                    visible = {}
                    session._visible_corpses = visible
                visible[int(corpse["owner_guid"])] = (
                    object_update.corpse_guid(int(corpse["owner_guid"])))
            except Exception:
                pass

    async def _broadcast_corpse_destroy(self, corpse: dict) -> None:
        server = getattr(self, "server", None)
        sessions = getattr(server, "sessions", ()) if server is not None else ()
        owner_guid = int(corpse.get("owner_guid")
                         or corpse.get("character_guid") or 0)
        guid = object_update.corpse_guid(owner_guid)
        payload = struct.pack("<QB", guid, 0)
        for session in list(sessions):
            if session is self or not getattr(session, "in_world", False):
                continue
            try:
                await session.send_packet(
                    Opcodes.SMSG_DESTROY_OBJECT, payload)
                visible = getattr(session, "_visible_corpses", None)
                if visible is not None:
                    visible.pop(owner_guid, None)
            except Exception:
                pass

    async def _send_login_corpse_spawns(self) -> None:
        """Show nearby other players' corpses to a newly arrived session."""
        await self.refresh_corpse_visibility(force=True)

    async def refresh_corpse_visibility(self, force: bool = False) -> None:
        """Create/destroy nearby player-corpse objects as the player moves."""
        if not self.in_world or not self.character:
            return
        point = (float(self.character.get("x", 0.0) or 0.0),
                 float(self.character.get("y", 0.0) or 0.0),
                 float(self.character.get("z", 0.0) or 0.0))
        last = getattr(self, "_corpse_scan_pos", None)
        if not force and last is not None:
            if math.dist(point, last) < 20.0:
                return
        rows = await asyncio.to_thread(
            character_db.get_corpses_near,
            self.config.database.world_path,
            int(self.character.get("map", 0) or 0),
            point[0], point[1], point[2], CORPSE_VISIBILITY_RADIUS)
        current = {}
        for row in rows:
            owner = int(row.get("character_guid") or 0)
            if not owner or owner == int(self.player_guid or 0):
                continue
            current[owner] = row
        previous = getattr(self, "_visible_corpses", {})
        for owner, row in current.items():
            if owner in previous:
                continue
            corpse = self._corpse_object_data(row)
            try:
                opcode, payload = object_update.build_corpse_update(corpse)
                await self.send_packet(opcode, payload)
            except Exception:
                pass
        for owner in previous.keys() - current.keys():
            try:
                await self.send_packet(
                    Opcodes.SMSG_DESTROY_OBJECT,
                    struct.pack("<QB",
                                object_update.corpse_guid(owner), 0))
            except Exception:
                pass
        self._visible_corpses = {
            owner: object_update.corpse_guid(owner)
            for owner in current
        }
        self._corpse_scan_pos = point

    async def _record_death(self) -> None:
        """Create the corpse, persist death state and send the release loc."""
        if not self.character:
            return
        combat = self._get_combat_state()
        map_id = int(self.character.get("map", 0) or 0)
        zone = int(self.character.get("zone", 0) or 0)
        x = float(self.character.get("x", 0.0) or 0.0)
        y = float(self.character.get("y", 0.0) or 0.0)
        z = float(self.character.get("z", 0.0) or 0.0)
        o = float(self.character.get("o", 0.0) or 0.0)
        guid = int(self.player_guid or 0)
        corpse = self._corpse_object_data(self.character)
        corpse.update({"character_guid": guid, "zone": zone, "released": 0})
        combat["corpse"] = corpse
        grave = await self._nearest_graveyard()
        grave = self._grave_or_start(grave)
        combat["death_graveyard"] = grave
        self.character["death_state"] = 1
        self.character["player_flags"] = 0
        self.character["health"] = 0
        await run_db_write(
            character_db.save_character_corpse,
            self.config.database.world_path, guid, map_id, zone, x, y, z, o)
        await run_db_write(
            character_db.set_character_death_state,
            self.config.database.world_path, guid, 1)
        await self.send_packet(
            Opcodes.SMSG_DEATH_RELEASE_LOC,
            struct.pack("<Ifff", int(grave["map"]), float(grave["x"]),
                        float(grave["y"]), float(grave["z"])),
        )
        await self._broadcast_corpse_create(corpse)

    async def _restore_death_state(self) -> None:
        """Restore a ghost/dead character after login."""
        if not self.character:
            return
        state = int(self.character.get("death_state", 0) or 0)
        if not state:
            return
        combat = self._get_combat_state()
        combat["dead"] = True
        combat["ghost"] = state == 2
        self.character["player_flags"] = (
            F.PLAYER_FLAGS_GHOST if state == 2 else 0)
        self.character["health"] = 1 if state == 2 else 0
        guid = int(self.player_guid or 0)
        if guid:
            corpse = await asyncio.to_thread(
                character_db.get_character_corpse,
                self.config.database.world_path, guid)
            if corpse is not None:
                corpse = dict(corpse)
                corpse.setdefault("owner_guid", int(corpse["character_guid"]))
            combat["corpse"] = corpse
        combat["death_graveyard"] = await self._nearest_graveyard()

    async def _send_login_death_location(self) -> None:
        if not self.character or not self._get_combat_state().get("dead"):
            return
        combat = self._get_combat_state()
        grave = combat.get("death_graveyard")
        if grave is None:
            grave = await self._nearest_graveyard()
        grave = self._grave_or_start(grave)
        await self.send_packet(
            Opcodes.SMSG_DEATH_RELEASE_LOC,
            struct.pack("<Ifff", int(grave["map"]), float(grave["x"]),
                        float(grave["y"]), float(grave["z"])),
        )

    # ------------------------------------------------------------------
    # Spirit release / ghost
    # ------------------------------------------------------------------
    async def _release_spirit(self) -> None:
        if not self.in_world or not self.character:
            return
        combat = self._get_combat_state()
        if not combat.get("dead") or combat.get("ghost"):
            return
        grave = combat.get("death_graveyard") or await self._nearest_graveyard()
        grave = self._grave_or_start(grave)
        self.character["map"] = int(grave["map"])
        self.character["zone"] = int(grave.get("zone") or
                                     self.character.get("zone", 0))
        self.character["x"] = float(grave["x"])
        self.character["y"] = float(grave["y"])
        self.character["z"] = float(grave["z"])
        self.character["o"] = float(grave.get("o") or 0.0)
        self.character["health"] = 1
        self.character["player_flags"] = F.PLAYER_FLAGS_GHOST
        self.character["death_state"] = 2
        combat["ghost"] = True
        combat["death_graveyard"] = grave
        guid = int(self.player_guid or 0)
        await run_db_write(
            character_db.update_character_position,
            self.config.database.world_path, guid,
            float(self.character["x"]), float(self.character["y"]),
            float(self.character["z"]), float(self.character["o"]),
            map_id=int(self.character["map"]),
            zone=int(self.character["zone"]))
        await run_db_write(
            character_db.set_character_death_state,
            self.config.database.world_path, guid, 2)
        released = int(time.time())
        corpse = combat.get("corpse")
        if corpse is not None:
            corpse["released"] = released
            await run_db_write(
                character_db.mark_character_corpse_released,
                self.config.database.world_path, guid, released)

        # BuildPlayerRepop: PRE_RESURRECT + the reclaim timer, then teleport.
        await self.send_packet(
            Opcodes.SMSG_PRE_RESURRECT, object_update.pack_guid(guid))
        await self.send_packet(
            Opcodes.SMSG_CORPSE_RECLAIM_DELAY,
            struct.pack("<I", int(CORPSE_RECLAIM_DELAY * 1000)))
        self._repop_pending = True
        self.movement_info = None
        await self.send_packet(
            Opcodes.SMSG_NEW_WORLD,
            login_packets.build_login_verify_world(self.character),
        )
        self.log.info(
            f"'{self.character['name']}' released spirit at "
            f"map {self.character['map']} zone {self.character['zone']}")

    # ------------------------------------------------------------------
    # Reclaim / resurrect
    # ------------------------------------------------------------------
    async def _resurrect_player(self, *, at_corpse: bool = False,
                                sickness: bool = False,
                                healer_guid: int = 0) -> bool:
        if not self.character:
            return False
        combat = self._get_combat_state()
        if not combat.get("dead"):
            return False
        corpse = combat.get("corpse") or await self._corpse()
        if at_corpse and corpse is None:
            return False
        old_map = int(self.character.get("map", 0) or 0)
        if at_corpse:
            map_id = int(corpse["map"])
            zone = int(corpse.get("zone") or 0)
            x, y, z, o = (float(corpse["x"]), float(corpse["y"]),
                          float(corpse["z"]), float(corpse.get("o") or 0.0))
        else:
            map_id = int(self.character.get("map", 0) or 0)
            zone = int(self.character.get("zone", 0) or 0)
            x = float(self.character.get("x", 0.0) or 0.0)
            y = float(self.character.get("y", 0.0) or 0.0)
            z = float(self.character.get("z", 0.0) or 0.0)
            o = float(self.character.get("o", 0.0) or 0.0)
        self.character["map"] = map_id
        self.character["zone"] = zone
        self.character["x"], self.character["y"] = x, y
        self.character["z"], self.character["o"] = z, o
        self.character["death_state"] = 0
        self.character["player_flags"] = 0
        combat["dead"] = False
        combat["ghost"] = False
        combat["in_combat"] = False
        combat["auto_attacking"] = False
        combat["target_guid"] = 0
        combat["pending_cast"] = None
        combat["channel"] = None
        combat["dots"] = []
        combat.setdefault("area_effects", []).clear()
        combat["aggro"].clear()
        if hasattr(self, "_close_loot"):
            await self._close_loot()
        level = int(self.character.get("level", 1) or 1)
        if sickness:
            await self._apply_resurrection_sickness(level)
        self._apply_combat_stats()
        max_health = int(self.character.get("max_health", 1) or 1)
        max_power = int(self.character.get("max_power", 0) or 0)
        self.character["health"] = max(
            1, int(max_health * SPIRIT_HEAL_RESURRECT_PERCENT))
        char_class = int(self.character.get("class", 1) or 1)
        self.character["power"] = (
            0 if char_class in (1, 6)
            else max(0, int(max_power * SPIRIT_HEAL_RESURRECT_PERCENT)))
        guid = int(self.player_guid or 0)
        await run_db_write(
            character_db.update_character_position,
            self.config.database.world_path, guid, x, y, z, o,
            map_id=map_id, zone=zone)
        await run_db_write(
            character_db.set_character_death_state,
            self.config.database.world_path, guid, 0)
        await run_db_write(
            character_db.delete_character_corpse,
            self.config.database.world_path, guid)
        if corpse is not None:
            await self._broadcast_corpse_destroy(corpse)
        combat["corpse"] = None
        cross_map = (at_corpse and corpse is not None
                     and int(corpse["map"]) != old_map)
        if cross_map:
            # Instance death: the corpse is on another map.  Resurrect into
            # the corpse's map and let the worldport ack rebuild the client.
            self._repop_pending = True
            self.movement_info = None
            await self._send_system_message("You have been resurrected.")
            await self.send_packet(
                Opcodes.SMSG_NEW_WORLD,
                login_packets.build_login_verify_world(self.character))
            self.log.info(
                f"'{self.character.get('name')}' reclaimed an instance "
                f"corpse on map {map_id}")
            return True
        if at_corpse and hasattr(self, "_teleport_nearby"):
            await self._teleport_nearby(x, y, z, o)
        await self.send_packet(
            Opcodes.SMSG_DEATH_RELEASE_LOC,
            struct.pack("<Ifff", 0xFFFFFFFF, 0.0, 0.0, 0.0))
        await self._send_player_fields({
            F.UNIT_FIELD_HEALTH: int(self.character["health"]),
            F.PLAYER_FLAGS: 0,
            self._power_field(F.UNIT_FIELD_POWER1): int(self.character["power"]),
        })
        if sickness:
            await self._apply_durability_loss()
            await self.send_packet(Opcodes.SMSG_DURABILITY_DAMAGE_DEATH, b"")
        await self._send_system_message("You have been resurrected.")
        self.log.info(f"'{self.character.get('name')}' resurrected")
        return True

    async def _apply_resurrection_sickness(self, level: int) -> None:
        if level <= 10:
            return
        minutes = min(10, level - 10) if level < 20 else 10
        duration_ms = minutes * 60 * 1000
        template = spells.get_spell_template(
            self.config.database.world_path, 15007)
        if template is None:
            return
        for index, effect in enumerate(template.effects):
            if int(effect.get("effect") or 0) != spells.SPELL_EFFECT_APPLY_AURA:
                continue
            await self._apply_aura_from_effect(
                template, effect, index, int(self.player_guid),
                int(self.player_guid), level, target_kind="player",
                duration_ms=duration_ms)

    async def _apply_durability_loss(self) -> None:
        inventory = self.character.get("inventory") or {}
        rows = []
        changed = []
        for slot, item in inventory.items():
            if not 0 <= int(slot) <= 18:
                continue
            max_dur = int(item.get("max_durability") or 0)
            if max_dur <= 0:
                continue
            current = int(item.get("durability") or 0)
            loss = max(1, int(max_dur * 0.25))
            new_value = max(0, current - loss)
            if new_value == current:
                continue
            item["durability"] = new_value
            rows.append((new_value, int(item["id"])))
            changed.append(item)
        if not rows:
            return
        await run_db_write(
            _write_item_durability, self.config.database.world_path, rows)
        for item in changed:
            opcode, payload = object_update.build_item_values_update(
                item, {F.ITEM_FIELD_DURABILITY: int(item["durability"])})
            await self.send_packet(opcode, payload)

    # ------------------------------------------------------------------
    # Client opcodes
    # ------------------------------------------------------------------
    async def handle_reclaim_corpse(self, data: bytes) -> None:
        if not self.in_world or not self.character:
            return
        combat = self._get_combat_state()
        if not combat.get("dead") or not combat.get("ghost"):
            return
        corpse = await self._corpse()
        if corpse is None:
            return
        corpse_map = int(corpse["map"])
        current_map = int(self.character.get("map", 0) or 0)
        if corpse_map != current_map:
            # Instance corpse: the ghost may reclaim from the instance
            # entrance (the graveyard nearest the corpse) without walking the
            # whole instance.  The new-world handshake then moves the client
            # into the instance map.
            entrance = await asyncio.to_thread(
                graveyard.closest_graveyard,
                self.config.database.world_path, corpse_map,
                int(corpse.get("zone") or 0), float(corpse["x"]),
                float(corpse["y"]), float(corpse["z"]),
                int(self.character.get("race", 1) or 1))
            if entrance is None or int(entrance["map"]) != current_map:
                await self._send_system_message("Your corpse is too far away.")
                return
            distance = math.dist(
                (float(self.character.get("x", 0.0)),
                 float(self.character.get("y", 0.0)),
                 float(self.character.get("z", 0.0))),
                (float(entrance["x"]), float(entrance["y"]),
                 float(entrance["z"])))
        else:
            distance = math.dist(
                (float(self.character.get("x", 0.0)),
                 float(self.character.get("y", 0.0)),
                 float(self.character.get("z", 0.0))),
                (float(corpse["x"]), float(corpse["y"]), float(corpse["z"])))
        if distance > CORPSE_RECLAIM_RADIUS:
            await self._send_system_message("Your corpse is too far away.")
            return
        released = int(corpse.get("released") or 0)
        remaining = (released + int(CORPSE_RECLAIM_DELAY)) - int(time.time())
        if remaining > 0:
            await self.send_packet(
                Opcodes.SMSG_CORPSE_RECLAIM_DELAY,
                struct.pack("<I", int(remaining * 1000)))
            return
        await self._resurrect_player(at_corpse=True, sickness=False)

    async def handle_spirit_healer_activate(self, data: bytes) -> None:
        if not self.in_world or not self.character:
            return
        combat = self._get_combat_state()
        if not combat.get("ghost"):
            return
        guid = struct.unpack_from("<Q", data, 0)[0] if len(data) >= 8 else 0
        if not guid:
            return
        low_guid = int(self._creature_low(guid)) if hasattr(
            self, "_creature_low") else guid & 0xFFFFFFFF
        spawn = self._get_creature_spawn(low_guid) if hasattr(
            self, "_get_creature_spawn") else None
        if spawn is None:
            return
        try:
            npc_flags = int(spawn["npc_flags"] or 0)
        except (KeyError, TypeError):
            npc_flags = 0
        if not (npc_flags & SPIRIT_HEALER_NPC_FLAG):
            return
        distance = math.dist(
            (float(self.character.get("x", 0.0)),
             float(self.character.get("y", 0.0)),
             float(self.character.get("z", 0.0))),
            (float(spawn["x"]), float(spawn["y"]), float(spawn["z"])))
        if distance > SPIRIT_HEALER_INTERACT_RANGE:
            return
        await self._resurrect_player(
            at_corpse=False, sickness=True, healer_guid=guid)

    async def handle_corpse_query(self, data: bytes) -> None:
        corpse = await self._corpse()
        if corpse is None:
            await self.send_packet(Opcodes.MSG_CORPSE_QUERY, b"\x00")
            return
        map_id = int(corpse["map"])
        payload = (b"\x01"
                   + struct.pack("<Ifff", map_id, float(corpse["x"]),
                                 float(corpse["y"]), float(corpse["z"]))
                   + struct.pack("<iI", map_id, 0))
        await self.send_packet(Opcodes.MSG_CORPSE_QUERY, payload)

    async def handle_corpse_map_position_query(self, data: bytes) -> None:
        corpse = await self._corpse()
        if corpse is None:
            return
        await self.send_packet(
            Opcodes.SMSG_CORPSE_MAP_POSITION_QUERY_RESPONSE,
            struct.pack("<ffff", float(corpse["x"]), float(corpse["y"]),
                        float(corpse["z"]), float(corpse.get("o") or 0.0)),
        )

    async def handle_ghost(self, data: bytes) -> None:
        return
