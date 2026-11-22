"""Creature spawn state, health, aggro and combat ticks."""
from __future__ import annotations

import json
import math
import random
import sqlite3
import time

from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import login_packets
from src.world import object_update
from src.world import update_fields as F

from .constants import (
    CREATURE_LEASH_RADIUS,
)

class CreaturesMixin:
    def _creature_position(self, low_guid: int) -> tuple[float, float, float]:
        """Where a creature currently is: chase, wander or spawn position.

        A creature that is moving: the client animates it along the
        ``SMSG_MONSTER_MOVE`` splines we send, so the server has to remember the
        destination it handed out or the range checks would keep failing.
        """
        combat = self._get_combat_state()
        tracked = combat["aggro"].get(int(low_guid))
        if tracked and tracked.get("pos"):
            return tuple(tracked["pos"])
        idle = (combat.get("idle") or {}).get(int(low_guid))
        if idle and idle.get("pos"):
            return tuple(idle["pos"])
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None:
            return (0.0, 0.0, 0.0)
        return (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))

    def _creature_object_guid(self, low_guid: int, entry: int = 0) -> int:
        """Full creature guid, packing the template entry the client queries.

        The client reads the creature entry from guid bits 24-47.  When callers
        do not already have the entry we look the spawn up so every packet uses
        exactly the same guid.
        """
        if not entry:
            spawn = self._get_creature_spawn(int(low_guid))
            if spawn is not None:
                entry = int(spawn["entry"])
        return creature.creature_guid(int(low_guid), int(entry))

    async def _send_creature_fields(self, low_guid: int, fields: dict) -> None:
        """Send one ``UPDATETYPE_VALUES`` update for a creature."""
        opcode, payload = object_update.build_values_update(
            self._creature_object_guid(int(low_guid)), F.UNIT_END, fields,
            force_uncompressed=True,
        )
        await self.send_packet(opcode, payload)

    def _creature_health(self, guid: int) -> int:
        """Get the current health of a creature from the DB.

        ``dead_since > 0`` marks a corpse even though ``current_health`` is 0
        both for a corpse and for a spawn that has never been damaged.
        """
        low_guid = creature.creature_low_guid(guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return 0
        if int(spawn["dead_since"] or 0) > 0:
            return 0
        current = int(spawn["current_health"] or 0)
        if current > 0:
            return current
        base = int(spawn["base_health"] or 0) if "base_health" in spawn.keys() else 0
        return base if base > 0 else 100

    def _creature_max_health(self, spawn) -> int:
        base = max(1, int(spawn["base_health"] or 1))
        multiplier = float(spawn["hp_multiplier"] or 1.0)
        return max(1, int(base * multiplier))

    def _set_creature_health(self, low_guid: int, health: int) -> None:
        """Persist a creature's current health (summons stay in memory)."""
        summon = getattr(self, "_summoned_creatures", {}).get(int(low_guid))
        if summon is not None:
            summon["current_health"] = max(0, int(health))
            return
        con = sqlite3.connect(self.config.database.world_path)
        try:
            con.execute(
                "UPDATE creature_spawns SET current_health = ? WHERE guid = ?",
                (max(0, int(health)), int(low_guid)),
            )
            con.commit()
        finally:
            con.close()

    def _get_creature_spawn(self, low_guid: int):
        """Get a creature spawn row (session summons first, then the DB)."""
        summon = getattr(self, "_summoned_creatures", {}).get(int(low_guid))
        if summon is not None:
            return summon
        return creature.get_creature_spawn(self.config.database.world_path, low_guid)

    async def _deal_damage_to_creature(self, guid: int, damage: int,
                                       attacker_guid: int = 0) -> bool:
        """Apply damage to a creature. Returns True if it died."""
        low_guid = creature.creature_low_guid(guid)
        if int(low_guid) in getattr(self, "_summoned_creatures", {}):
            return await self._damage_summon(int(low_guid), damage, attacker_guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return False
        if int(spawn["dead_since"] or 0) > 0:
            return False
        current = self._creature_health(guid)
        if current <= 0:
            return False
        max_health = self._creature_max_health(spawn)
        damage = max(1, int(damage))
        new_health = max(0, current - damage)
        self._set_creature_health(low_guid, new_health)
        combat = self._get_combat_state()
        combat["in_combat"] = True
        if new_health <= 0:
            await self._on_creature_killed(guid)
            return True
        await self._send_creature_fields(low_guid, {
            F.UNIT_FIELD_HEALTH: new_health,
            F.UNIT_FIELD_MAXHEALTH: max_health,
            F.UNIT_DYNAMIC_FLAGS: 0,
        })
        self._aggro_creature(low_guid)
        return False

    def _aggro_creature(self, low_guid: int) -> None:
        """Put a creature into combat with this player (summons excluded)."""
        if int(low_guid) in getattr(self, "_summoned_creatures", {}):
            # A pet is driven by _summon_tick, not by the creature AI/DB state.
            return
        combat = self._get_combat_state()
        now = time.monotonic()
        entry = combat["aggro"].get(int(low_guid))
        if entry is None:
            # Start the fight from wherever the creature wandered to; otherwise
            # the first chase spline would teleport it back to its spawn point.
            idle = combat.setdefault("idle", {}).pop(int(low_guid), None)
            start_pos = idle.get("pos") if idle else None
            combat["aggro"][int(low_guid)] = {
                "last_swing": now - 1.0,
                "chase_until": 0.0,
                "pos": start_pos,
                "facing": None,
                "face_until": 0.0,
                # Lazily built on the first AI tick: spell list, mana pool,
                # cooldowns and the spell currently being cast.
                "ai": None,
            }
        con = sqlite3.connect(self.config.database.world_path)
        try:
            con.execute(
                "UPDATE creature_spawns SET combat_state = 1, target_guid = ? "
                "WHERE guid = ? AND dead_since = 0",
                (int(self.player_guid or 0), int(low_guid)),
            )
            con.commit()
        finally:
            con.close()

    async def _on_creature_killed(self, guid: int) -> None:
        """Handle creature death: stop combat, award XP and make a corpse."""
        low_guid = creature.creature_low_guid(guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return
        entry = int(spawn["entry"])
        level = max(1, int(spawn["min_level"])) if "min_level" in spawn.keys() else 1
        player_level = int(self.character.get("level", 1))
        base_xp = level * 5 + 45
        if player_level >= level + 5:
            xp = 0
        elif player_level <= level:
            xp = base_xp
        else:
            xp = int(base_xp * max(0.1, 1.0 - (player_level - level) * 0.1))
        # Generate loot ONCE when the creature dies.  CMaNGOS stores the loot
        # template id separately from the creature entry; 190 creatures (Naxx
        # and a few event NPCs) share another creature's loot table.
        loot_id = int(spawn["loot_id"] or 0) if "loot_id" in spawn.keys() else 0
        loot_items = self._generate_loot(entry, loot_id or entry)
        loot_money = self._loot_money(entry, level)
        loot_json = json.dumps({"items": loot_items, "money": loot_money})
        con = sqlite3.connect(self.config.database.world_path)
        try:
            con.execute(
                "UPDATE creature_spawns SET current_health = 0, dead_since = ?, "
                "combat_state = 3, target_guid = 0, loot_json = ? WHERE guid = ?",
                (int(time.time()), loot_json, low_guid),
            )
            con.commit()
        finally:
            con.close()
        combat = self._get_combat_state()
        # ``Unit::ClearComboPointHolders``: a target's death drops every
        # player's combo points on it.  Only this session can hold points, so
        # clearing them here is the whole job.
        if int(combat.get("combo_target") or 0) == int(guid):
            await self._clear_combo_points()
        combat["aggro"].pop(low_guid, None)
        if hasattr(self, "_clear_creature_auras"):
            self._clear_creature_auras(low_guid)
        # Stop the idle generator: the corpse must not keep walking.
        combat.setdefault("idle", {}).pop(low_guid, None)
        combat.setdefault("idle_settle", {}).pop(low_guid, None)
        self._creature_spawn_cache.pop(low_guid, None)
        await self._send_creature_fields(low_guid, {
            F.UNIT_FIELD_HEALTH: 0,
            F.UNIT_DYNAMIC_FLAGS: 1,      # UNIT_DYNFLAG_LOOTABLE
        })
        if xp > 0:
            await self._award_xp(xp, guid)
        self.log.info(f"Creature {entry} ({low_guid}) killed, awarded {xp} XP")

    async def _creature_combat_tick(self, low_guid: int, now: float) -> None:
        """One AI tick for a creature that is fighting this player."""
        if int(low_guid) in getattr(self, "_summoned_creatures", {}):
            return
        combat = self._get_combat_state()
        state = combat["aggro"].get(int(low_guid))
        if state is None:
            return
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            combat["aggro"].pop(int(low_guid), None)
            return

        # Stunned creatures do nothing at all; rooted ones can still swing and
        # cast while in range but cannot chase.
        if hasattr(self, "_creature_aura_mods"):
            if self._creature_aura_mods(int(low_guid))["stunned"]:
                ai = (state.get("ai") if state else None)
                if ai is not None and ai.get("cast") is not None:
                    ai["cast"] = None
                return
            rooted = self._creature_aura_mods(int(low_guid))["rooted"]
            pacified = self._creature_aura_mods(int(low_guid))["pacified"]
            melee_interval = self._creature_melee_interval(int(low_guid))
            speed_mult = self._creature_speed_mult(int(low_guid))
        else:
            rooted = pacified = False
            melee_interval = 2.0
            speed_mult = 1.0

        player = self._player_position()
        here = self._creature_position(int(low_guid))
        distance = self._distance(player, here)
        reach = self._melee_reach(spawn)

        # Leash: too far from home, or the player is dead or gone.
        home = (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))
        if (self._distance(home, player) > CREATURE_LEASH_RADIUS
                or self._distance(home, here) > CREATURE_LEASH_RADIUS + 15.0):
            await self._reset_creature(int(low_guid))
            return

        in_melee = distance <= reach
        ai = await self._creature_ai_state(int(low_guid), spawn, now)

        # A creature that is casting stands still, faces its victim and neither
        # swings nor chases until the cast has finished (Spell::update runs
        # before the AI tick in the reference core).
        if ai is not None and ai["cast"] is not None:
            if now >= ai["cast"]["finish_at"]:
                await self._finish_creature_cast(int(low_guid), spawn, ai)
            else:
                # A casting creature turns towards its victim but does not move.
                await self._face_creature_at_player(int(low_guid), state,
                                                    spawn, here, player, now)
                return

        # ``UnitAI::UpdateAI`` evaluates the spell list before melee.
        if ai is not None:
            await self._creature_spell_list_tick(
                int(low_guid), spawn, ai, now, in_melee)
            if ai["cast"] is not None:
                return

        if distance <= reach:
            # In melee range the creature stops moving, so its orientation only
            # changes when we explicitly send a face spline.  Without this it
            # keeps staring at whatever direction it spawned with while the
            # player circles around it.
            await self._face_creature_at_player(int(low_guid), state, spawn,
                                                here, player, now)
            # In melee range: swing on the creature's own timer.
            if not pacified and now - state["last_swing"] >= melee_interval:
                state["last_swing"] = now
                raw_damage = max(1, int(round(self._creature_damage(spawn))))
                # ``Unit::CalcArmorReducedDamage``: normal-school melee is
                # mitigated by the player's armor (item armor + agility*2)
                # before it reaches the health pool.  The client shows the
                # mitigated number, so the attacker-state update uses it too.
                damage = self._armor_reduced_damage(
                    raw_damage, int(self.character.get("armor", 0) or 0),
                    int(spawn["min_level"] or 1))
                await self._send_attacker_state_update(
                    self._creature_object_guid(int(low_guid), int(spawn["entry"])),
                    self.player_guid, damage,
                )
                # Creature auto-attacks are normal-school melee, the only
                # damage source that fills a warrior's rage bar.
                await self._damage_player(damage, str(spawn["name"]),
                                          physical=True)
            return

        # Out of reach: walk towards the player.  One spline per second keeps
        # the packet rate sane while still looking smooth to the client.
        if rooted:
            return
        if now < state["chase_until"]:
            return
        speed = 7.0 * max(0.1, speed_mult)
        duration_ms = max(250, int((distance - reach * 0.5) / speed * 1000))
        duration_ms = min(duration_ms, 1500)
        travel = min(distance - 1.0, speed * duration_ms / 1000.0)
        if travel <= 0.1:
            return
        scale = travel / distance
        dest = (here[0] + (player[0] - here[0]) * scale,
                here[1] + (player[1] - here[1]) * scale,
                here[2] + (player[2] - here[2]) * scale)
        state["pos"] = dest
        state["chase_until"] = now + duration_ms / 1000.0
        facing = math.atan2(player[1] - here[1], player[0] - here[0])
        state["facing"] = facing
        await self.send_packet(
            Opcodes.SMSG_MONSTER_MOVE,
            object_update.build_monster_move(
                self._creature_object_guid(int(low_guid), int(spawn["entry"])),
                here[0], here[1], here[2], facing,
                dest[0], dest[1], dest[2],
                duration_ms=duration_ms,
                move_id=self._next_move_id(),
            ),
        )

    async def _reset_creature(self, low_guid: int) -> None:
        """Leave combat, walk the creature home and heal it back up."""
        combat = self._get_combat_state()
        state = combat["aggro"].pop(int(low_guid), None)
        if hasattr(self, "_clear_creature_auras"):
            self._clear_creature_auras(int(low_guid))
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            return
        max_health = self._creature_max_health(spawn)
        self._set_creature_health(int(low_guid), max_health)
        await self._send_creature_fields(int(low_guid), {
            F.UNIT_FIELD_HEALTH: max_health,
            F.UNIT_FIELD_MAXHEALTH: max_health,
        })
        # Evading also resets power to full (``Creature::RegenerateAll``), so a
        # caster that ran itself dry does not start the next fight empty.
        ai = state.get("ai") if state else None
        if ai and int(ai.get("max_mana") or 0) > 0:
            ai["mana"] = int(ai["max_mana"])
            self._set_creature_power(int(low_guid), ai["mana"])
            await self._send_creature_power(int(low_guid), ai)
        home = (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))
        here = self._creature_position(int(low_guid))
        con = sqlite3.connect(self.config.database.world_path)
        try:
            con.execute(
                "UPDATE creature_spawns SET combat_state = 0, target_guid = 0 "
                "WHERE guid = ?",
                (int(low_guid),),
            )
            con.commit()
        finally:
            con.close()
        distance = self._distance(home, here)
        settle_until = time.monotonic()
        if distance > 1.0:
            duration_ms = max(250, int(distance / 7.0 * 1000))
            settle_until = time.monotonic() + min(duration_ms, 6000) / 1000.0
            await self.send_packet(
                Opcodes.SMSG_MONSTER_MOVE,
                object_update.build_monster_move(
                    self._creature_object_guid(int(low_guid), int(spawn["entry"])),
                    here[0], here[1], here[2],
                    math.atan2(home[1] - here[1], home[0] - here[0]),
                    home[0], home[1], home[2],
                    duration_ms=min(duration_ms, 6000),
                    move_id=self._next_move_id(),
                ),
            )
        # Restart the idle generator at home, but only after the walk-home
        # spline has finished so it cannot immediately override it.
        combat.setdefault("idle", {}).pop(int(low_guid), None)
        combat.setdefault("idle_settle", {})[int(low_guid)] = settle_until + 0.5
        if state is not None:
            self.log.debug(f"Creature {spawn['entry']} left combat")

    async def _respawn_corpses(self, now: float) -> None:
        """Re-create dead spawns whose respawn timer expired."""
        if not self._visible_creatures:
            return
        rows = []
        con = sqlite3.connect(self.config.database.world_path)
        try:
            for low_guid in list(self._visible_creatures):
                row = con.execute(
                    "SELECT s.dead_since, t.respawn_time, s.map "
                    "FROM creature_spawns s "
                    "JOIN creature_templates t ON t.entry = s.entry "
                    "WHERE s.guid = ?",
                    (int(low_guid),),
                ).fetchone()
                if row is None or int(row[0] or 0) <= 0:
                    continue
                delay = max(5, int(row[1] or 0))
                if int(row[0]) + delay <= int(time.time()):
                    rows.append(int(low_guid))
        finally:
            con.close()
        for low_guid in rows:
            con = sqlite3.connect(self.config.database.world_path)
            try:
                con.execute(
                    "UPDATE creature_spawns SET dead_since = 0, combat_state = 0, "
                    "target_guid = 0, current_health = 0, current_power = 0, "
                    "loot_json = '' "
                    "WHERE guid = ?",
                    (int(low_guid),),
                )
                con.commit()
            finally:
                con.close()
            combat = self._get_combat_state()
            combat["aggro"].pop(int(low_guid), None)
            combat.setdefault("idle", {}).pop(int(low_guid), None)
            if hasattr(self, "_clear_creature_auras"):
                self._clear_creature_auras(int(low_guid))
            combat.setdefault("idle_settle", {}).pop(int(low_guid), None)
            self._creature_spawn_cache.pop(int(low_guid), None)
            if combat["loot_target"] and creature.creature_low_guid(
                    combat["loot_target"]) == int(low_guid):
                await self._close_loot()
            if self.player_guid and low_guid in self._visible_creatures:
                object_guid = self._visible_creatures.pop(low_guid)
                await self.send_packet(
                    Opcodes.SMSG_DESTROY_OBJECT,
                    login_packets.build_destroy_object(object_guid, death=True),
                )
            await self.refresh_creature_visibility()
            self.log.debug(f"Creature spawn {low_guid} respawned")

    def _melee_reach(self, spawn) -> float:
        """Distance at which this session can hit ``spawn`` (yard, centre to centre)."""
        reach = 3.0
        if "combat_reach" in spawn.keys():
            try:
                reach = float(spawn["combat_reach"] or 0.0) + 3.0
            except (TypeError, ValueError):
                reach = 3.0
        return min(max(reach, 3.0), 15.0)

    def _creature_damage(self, spawn) -> float:
        """A creature's melee damage roll, scaled from its level."""
        level = max(1, int(spawn["min_level"] or 1))
        low = max(1.0, level * 0.9)
        high = max(low + 1.0, level * 1.6)
        return random.uniform(low, high)
