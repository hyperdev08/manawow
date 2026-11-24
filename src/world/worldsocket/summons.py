"""Summoned creatures: pets and guardians created by spells.

The world database only describes static spawns, so a summon is a session-local
``creature_spawns``-shaped row.  The guid allocator uses the top of the 24-bit
counter range (database spawns stop far below it), and the row carries
``summoned_by`` so ``build_creature_values`` marks it as the player's pet.
"""
from __future__ import annotations

import math
import time

from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import login_packets
from src.world import object_update
from src.world import update_fields as F

from .constants import (
    SUMMON_FOLLOW_DISTANCE,
    SUMMON_FOLLOW_STOP_DISTANCE,
    SUMMON_GUID_BASE,
    SUMMON_GUID_MAX,
    SUMMON_LEASH_DISTANCE,
    SUMMON_MOVE_MAX_MS,
    SUMMON_PET_SWING_INTERVAL,
)


class SummonsMixin:
    # ------------------------------------------------------------------
    # Runtime summon table
    # ------------------------------------------------------------------
    def _summon_spawns(self) -> dict:
        """Session-local creature rows created by summon spells."""
        if not hasattr(self, "_summoned_creatures"):
            self._summoned_creatures = {}
        return self._summoned_creatures

    def _summon_state(self, low_guid: int) -> dict:
        """Per-summon AI state (expiry, swing timer, target)."""
        if not hasattr(self, "_summon_states"):
            self._summon_states = {}
        return self._summon_states.setdefault(int(low_guid), {})

    def _next_summon_guid(self) -> int:
        """Allocate a free 24-bit spawn counter for a runtime creature."""
        summons = self._summon_spawns()
        counter = int(getattr(self, "_summon_serial", 0))
        if counter < SUMMON_GUID_BASE or counter > SUMMON_GUID_MAX:
            counter = SUMMON_GUID_BASE
        for _ in range(SUMMON_GUID_MAX - SUMMON_GUID_BASE + 1):
            if counter not in summons and self._get_db_creature_low(counter) is None:
                self._summon_serial = counter + 1
                return counter
            counter = SUMMON_GUID_BASE if counter >= SUMMON_GUID_MAX else counter + 1
        raise RuntimeError("no free creature guid for a summon")

    def _get_db_creature_low(self, low_guid: int):
        """Look up a database spawn *without* consulting the summon table."""
        return creature.get_creature_spawn(
            self.config.database.world_path, int(low_guid))

    # ------------------------------------------------------------------
    # Creation / dismissal
    # ------------------------------------------------------------------
    def _build_summon_spawn(self, template, low_guid: int, x: float, y: float,
                            z: float, o: float) -> dict:
        """Turn a ``creature_templates`` row into a summon spawn row.

        Creature templates describe the *wild* version of the entry: the imp is
        level 5, the felhunter level 80, and the templates carry no pet stats.
        The summon therefore takes its level from the owner and computes a
        health pool from the template's relative toughness.  Pet stat DBCs are
        not imported, so this is an approximation, but it keeps every demon in
        the same level band as the warlock who summoned it.
        """
        entry = int(template["entry"])
        level = max(1, int(self.character.get("level", 1)))
        race = int(self.character.get("race", 1))
        template_level = max(1, int(template["min_level"] or 1))
        raw_health = max(
            1.0,
            float(template["base_health"] or 1)
            * float(template["hp_multiplier"] or 1.0),
        )
        toughness = max(0.5, min(3.0, raw_health / (template_level * 40.0)))
        health = max(1, int((55 + 22 * level) * toughness))
        display = int(template["display_id1"] or 0)
        if not display:
            for index in (2, 3, 4):
                display = int(template[f"display_id{index}"] or 0)
                if display:
                    break
        return {
            "guid": int(low_guid),
            "entry": entry,
            "map": int(self.character.get("map", 0)),
            "zone": int(self.character.get("zone", 0)),
            "x": float(x),
            "y": float(y),
            "z": float(z),
            "o": float(o),
            "spawn_display_id": display,
            "current_health": health,
            "current_power": 0,
            "combat_state": 0,
            "target_guid": 0,
            "dead_since": 0,
            "loot_json": "",
            # Summons never wander: their movement comes from _summon_tick.
            "spawn_movement_type": 0,
            "spawndist": 0.0,
            "name": str(template["name"] or ""),
            "subname": str(template["subname"] or ""),
            "gossip_menu_id": 0,
            "min_level": level,
            "max_level": level,
            # The owner's faction template makes the client treat the summon
            # as friendly (and lets enemy NPCs see it as the player's pet).
            "faction": object_update.faction_template_for_race(race),
            "npc_flags": 0,
            "unit_flags": int(template["unit_flags"] or 0),
            "unit_flags2": 0,
            "unit_class": int(template["unit_class"] or 1),
            "unit_gender": int(template["unit_gender"] or 0),
            "unit_power_type": int(template["unit_power_type"] or 0),
            "scale": float(template["scale"] or 1.0),
            "type": int(template["type"] or 7),
            "family": int(template["family"] or 0),
            "classification": int(template["classification"] or 0),
            "display_id1": int(template["display_id1"] or 0),
            "display_id2": int(template["display_id2"] or 0),
            "display_id3": int(template["display_id3"] or 0),
            "display_id4": int(template["display_id4"] or 0),
            "hp_multiplier": 1.0,
            "energy_multiplier": 1.0,
            "leader": 0,
            "movement_info_id": 0,
            "bounding_radius": float(template["bounding_radius"] or 0.389),
            "combat_reach": float(template["combat_reach"] or 1.5),
            "base_health": health,
            "template_movement_type": 0,
            "respawn_time": 0,
            "aggro_radius": 0.0,
            "chase_distance": 0.0,
            "loot_id": 0,
            "spell_list_id": 0,
            "ai_name": "",
            "speed_walk": float(template["speed_walk"] or 2.5),
            "speed_run": float(template["speed_run"] or 8.0),
            "min_damage": 0.0,
            "max_damage": 0.0,
            "attack_power": 0,
            "ranged_attack_power": 0,
            "armor": 0,
            "melee_attack_speed": int(template["melee_attack_speed"] or 2000),
            "ranged_attack_speed": 3000,
            "crit_chance": 0.0,
            # Marks the row as a runtime summon for the update builder and the
            # idle-movement tick; the value is the owner's full guid.
            "summoned_by": int(self.player_guid or 0),
        }

    def _summon_spawn_position(self) -> tuple[float, float, float, float]:
        """Place a new summon two yards behind the caster."""
        x, y, z = self._player_position()
        facing = float(self.character.get("o", 0.0) or 0.0)
        return (x - math.cos(facing) * 2.0, y - math.sin(facing) * 2.0,
                z, facing)

    async def _summon_creature(self, entry: int, duration_ms: int = -1,
                               is_pet: bool = True,
                               stationary: bool = False):
        """Create a summoned creature owned by this player.

        ``duration_ms`` is ``-1`` for "until dismissed".  A second summon
        replaces the first one, which is what happens when a warlock summons a
        different demon while the old one is out.  ``stationary`` marks totems:
        they neither follow the owner nor pick a target.
        """
        template = creature.get_creature_template(
            self.config.database.world_path, int(entry))
        if template is None:
            self.log.debug(f"Summon entry {entry} is not in creature_templates")
            return None
        if not self.in_world or not self.player_guid:
            return None
        await self.dismiss_all_summons()
        low_guid = self._next_summon_guid()
        x, y, z, o = self._summon_spawn_position()
        spawn = self._build_summon_spawn(template, low_guid, x, y, z, o)
        self._summon_spawns()[low_guid] = spawn
        self._creature_spawn_cache[low_guid] = spawn
        self._visible_creatures[low_guid] = creature.creature_guid(
            low_guid, int(entry))
        state = self._summon_state(low_guid)
        state.update({
            "expires": (None if duration_ms is None or duration_ms <= 0
                        else time.monotonic() + duration_ms / 1000.0),
            "last_swing": 0.0,
            "moving_until": 0.0,
            "target": 0,
            "stationary": bool(stationary),
        })
        packet = object_update.build_creature_update([spawn])
        if packet is not None:
            opcode, payload = packet
            await self.send_packet(opcode, payload)
            await self.send_packet_to_peers(opcode, payload)
        self.log.info(
            f"'{self.character['name']}' summons {template['name']} "
            f"(entry {entry}, guid {low_guid}, "
            f"{'pet' if is_pet else 'guardian'})"
        )
        return low_guid

    async def send_summons_to(self, session) -> None:
        """Give ``session`` create blocks for this player's active summons.

        Called when a second player enters the world after the pet already
        exists; the normal announce path only covers the character itself.
        """
        summons = list(self._summon_spawns().values())
        if not summons:
            return
        packet = object_update.build_creature_update(summons)
        if packet is not None:
            await session.send_packet(*packet)

    async def _dismiss_summon(self, low_guid: int) -> None:
        """Remove one summoned creature from this client and its peers."""
        spawn = self._summon_spawns().pop(int(low_guid), None)
        if spawn is None:
            return
        if hasattr(self, "_summon_states"):
            self._summon_states.pop(int(low_guid), None)
        self._creature_spawn_cache.pop(int(low_guid), None)
        object_guid = self._visible_creatures.pop(
            int(low_guid), creature.creature_guid(int(low_guid), int(spawn["entry"])))
        payload = login_packets.build_destroy_object(object_guid)
        await self.send_packet(Opcodes.SMSG_DESTROY_OBJECT, payload)
        await self.send_packet_to_peers(Opcodes.SMSG_DESTROY_OBJECT, payload)
        self.log.debug(f"Summon {low_guid} ({spawn['name']}) dismissed")

    async def dismiss_all_summons(self) -> None:
        """Remove every summon this session owns (logout/disconnect)."""
        for low_guid in list(self._summon_spawns()):
            await self._dismiss_summon(low_guid)

    # ------------------------------------------------------------------
    # Damage
    # ------------------------------------------------------------------
    async def _damage_summon(self, low_guid: int, damage: int,
                             attacker_guid: int = 0) -> bool:
        """Apply damage to a summon.  Returns True when it dies."""
        spawn = self._summon_spawns().get(int(low_guid))
        if spawn is None:
            return False
        current = int(spawn["current_health"] or 0)
        if current <= 0:
            return False
        max_health = self._creature_max_health(spawn)
        new_health = max(0, current - max(1, int(damage)))
        spawn["current_health"] = new_health
        await self._send_creature_fields(int(low_guid), {
            F.UNIT_FIELD_HEALTH: new_health,
            F.UNIT_FIELD_MAXHEALTH: max_health,
            F.UNIT_DYNAMIC_FLAGS: 0,
        })
        if new_health <= 0:
            self.log.info(f"Summon {spawn['name']} ({low_guid}) died")
            await self._dismiss_summon(int(low_guid))
            return True
        return False

    # ------------------------------------------------------------------
    # AI tick
    # ------------------------------------------------------------------
    async def _summon_tick_all(self, now: float) -> None:
        """Advance every summon: expiry, following and attacking."""
        summons = self._summon_spawns()
        if not summons:
            return
        for low_guid in list(summons):
            spawn = summons.get(low_guid)
            if spawn is None:
                continue
            state = self._summon_state(low_guid)
            expires = state.get("expires")
            if expires is not None and now >= expires:
                await self._dismiss_summon(low_guid)
                continue
            if self._get_combat_state().get("dead"):
                continue
            if state.get("stationary"):
                continue                     # totems only wait for their expiry
            await self._summon_tick(low_guid, spawn, state, now)

    async def _summon_tick(self, low_guid: int, spawn: dict, state: dict,
                           now: float) -> None:
        if now < float(state.get("moving_until") or 0.0):
            return                       # the client is still animating a move
        owner = self._player_position()
        here = (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))
        target_guid, target_spawn = self._summon_pick_target()
        if target_spawn is None:
            await self._summon_follow(low_guid, spawn, state, here, owner, now)
            return
        low_target = creature.creature_low_guid(target_guid)
        target_pos = self._creature_position(low_target)
        # Do not chase a target the owner themselves ran away from.
        if self._distance(owner, target_pos) > SUMMON_LEASH_DISTANCE:
            await self._summon_follow(low_guid, spawn, state, here, owner, now)
            return
        reach = max(self._melee_reach(spawn), self._melee_reach(target_spawn))
        distance = self._distance(here, target_pos)
        if distance > reach:
            await self._summon_move_towards(
                low_guid, spawn, state, here, target_pos, now,
                float(spawn["speed_run"] or 8.0))
            return
        # In melee: stand still on the pet's own swing timer.
        state["moving_until"] = 0.0
        state["target"] = int(target_guid)
        if now - float(state.get("last_swing") or 0.0) < SUMMON_PET_SWING_INTERVAL:
            return
        state["last_swing"] = now
        pet_guid = self._creature_object_guid(low_guid, int(spawn["entry"]))
        damage = max(1, int(round(self._creature_damage(spawn))))
        await self._send_attacker_state_update(pet_guid, int(target_guid), damage)
        died = await self._deal_damage_to_creature(
            int(target_guid), damage, pet_guid)
        if died:
            state["target"] = 0

    def _summon_pick_target(self):
        """Choose what the summon attacks, or ``(0, None)``.

        Pets assist the owner: they only engage while the owner is in combat,
        and they prefer the owner's explicit attack target, then the selected
        unit, then any creature currently fighting the owner.
        """
        combat = self._get_combat_state()
        engaged = bool(combat.get("auto_attacking") or combat.get("aggro")
                       or combat.get("in_combat"))
        if not engaged:
            return 0, None
        candidates = [int(combat.get("target_guid") or 0),
                      int(combat.get("selected_guid") or 0)]
        for candidate in candidates:
            if not candidate:
                continue
            low = creature.creature_low_guid(candidate)
            if low in self._summon_spawns():
                continue
            spawn = self._get_creature_spawn(low)
            if spawn is None or int(spawn["dead_since"] or 0) > 0:
                continue
            return candidate, spawn
        for low in list(combat.get("aggro") or {}):
            if low in self._summon_spawns():
                continue
            spawn = self._get_creature_spawn(int(low))
            if spawn is None or int(spawn["dead_since"] or 0) > 0:
                continue
            return (self._creature_object_guid(int(low), int(spawn["entry"])),
                    spawn)
        return 0, None

    async def _summon_follow(self, low_guid: int, spawn: dict, state: dict,
                             here, owner, now: float) -> None:
        """Walk a summon back to its owner when it falls behind."""
        distance = self._distance(here, owner)
        if distance <= SUMMON_FOLLOW_DISTANCE:
            return
        if distance > SUMMON_LEASH_DISTANCE:
            # Way too far (teleport, ghost walk, ...): snap instead of a long
            # cross-country spline.
            spawn["x"], spawn["y"], spawn["z"] = owner
            await self._send_summon_move(
                low_guid, spawn, state, here, owner, now, 0.05)
            return
        await self._summon_move_towards(
            low_guid, spawn, state, here, owner, now,
            float(spawn["speed_run"] or 8.0),
            stop_distance=SUMMON_FOLLOW_STOP_DISTANCE)

    async def _summon_move_towards(self, low_guid: int, spawn: dict,
                                   state: dict, here, dest, now: float,
                                   speed: float,
                                   stop_distance: float = 0.0) -> None:
        """Send a straight-line spline from ``here`` towards ``dest``."""
        distance = self._distance(here, dest)
        travel = distance - max(0.0, stop_distance)
        if travel <= 0.15:
            return
        speed = max(0.5, float(speed))
        duration_ms = int(travel / speed * 1000.0)
        duration_ms = max(200, min(duration_ms, SUMMON_MOVE_MAX_MS))

        path = None
        if hasattr(self, "_find_world_path") and distance > 2.0:
            if not self._has_line_of_sight(
                    (here[0], here[1], here[2] + 0.9),
                    (dest[0], dest[1], dest[2] + 0.9)):
                path = await self._find_world_path(here, dest)
        points = None
        if path and len(path) > 1:
            points = self._path_prefix(here, path,
                                       travel + max(0.0, stop_distance),
                                       stop_distance=stop_distance)
            if points:
                target = points[-1]
                traveled = sum(self._distance(a, b) for a, b in
                               zip([here] + points[:-1], points))
                duration_ms = max(200, min(
                    int(traveled / speed * 1000.0), SUMMON_MOVE_MAX_MS))
            else:
                points = None
        if points is None:
            travel = min(travel, speed * duration_ms / 1000.0)
            scale = travel / distance
            target = (here[0] + (dest[0] - here[0]) * scale,
                      here[1] + (dest[1] - here[1]) * scale,
                      here[2] + (dest[2] - here[2]) * scale)
        await self._send_summon_move(low_guid, spawn, state, here, target, now,
                                     duration_ms / 1000.0, points=points)

    async def _send_summon_move(self, low_guid: int, spawn: dict, state: dict,
                                here, target, now: float,
                                duration_s: float, points=None) -> None:
        facing = math.atan2(target[1] - here[1], target[0] - here[0])
        duration_ms = max(1, int(duration_s * 1000.0))
        payload = object_update.build_monster_move_path(
            self._creature_object_guid(low_guid, int(spawn["entry"])),
            float(here[0]), float(here[1]), float(here[2]), facing,
            points if points else [(float(target[0]), float(target[1]),
                                    float(target[2]))],
            duration_ms=duration_ms,
            move_id=self._next_move_id(),
        )
        spawn["x"], spawn["y"], spawn["z"] = (
            float(target[0]), float(target[1]), float(target[2]))
        spawn["o"] = facing
        state["moving_until"] = now + duration_ms / 1000.0
        await self.send_packet(Opcodes.SMSG_MONSTER_MOVE, payload)
        await self.send_packet_to_peers(Opcodes.SMSG_MONSTER_MOVE, payload)
