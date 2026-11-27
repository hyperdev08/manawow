"""Combat loop, melee swings and attacker state updates."""
from __future__ import annotations

import asyncio
import random
import sqlite3
import struct
import time

from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import combat_ratings as CR
from src.world import creature
from src.world import databases
from src.world import spells

from .constants import (
    COMBAT_TICK_INTERVAL,
    RESPAWN_SCAN_INTERVAL,
    SPELL_FAILED_INTERRUPTED,
)

class CombatMixin:
    # ------------------------------------------------------------------
    # Melee combat
    # ------------------------------------------------------------------
    # A level 1 character without a weapon still swings; these are the numbers
    # used for bare hands (the client animates the attack either way).
    _BARE_HAND = {"min": 1.0, "max": 3.0, "delay": 2.0,
                  "base_delay": 2.0}

    def start_combat_loop(self) -> None:
        """Start this session's combat/AI task (idempotent)."""
        if getattr(self, "_combat_task", None) is None:
            self._combat_task = asyncio.ensure_future(self._combat_loop())

    def stop_combat_loop(self) -> None:
        task = getattr(self, "_combat_task", None)
        if task is not None:
            task.cancel()
            self._combat_task = None

    async def _combat_loop(self) -> None:
        """Drive swings, casts, creature AI, regeneration and respawns."""
        try:
            while not self.should_close:
                await asyncio.sleep(COMBAT_TICK_INTERVAL)
                if not self.in_world or not self.character:
                    continue
                try:
                    await self._combat_tick(time.monotonic())
                except Exception as exc:  # never let one tick kill the loop
                    self.log.exception(f"Combat tick error: {exc}")
        except asyncio.CancelledError:
            pass

    async def _combat_tick(self, now: float) -> None:
        if not self.in_world or not self.character:
            return
        combat = self._get_combat_state()

        # 1. finish a cast whose casting time elapsed
        pending = combat["pending_cast"]
        if pending and hasattr(self, "_player_aura_mods") \
                and self._player_aura_mods()["stunned"]:
            combat["pending_cast"] = None
            await self.send_packet(
                Opcodes.SMSG_SPELL_FAILURE,
                spells.build_spell_failure(
                    self.player_guid, pending["cast_id"], pending["spell_id"],
                    SPELL_FAILED_INTERRUPTED),
            )
        elif pending and pending["finish_at"] <= now:
            combat["pending_cast"] = None
            await self._apply_pending_cast(pending)

        # 1b. channelled spell periodic triggers
        if hasattr(self, "_tick_channel"):
            await self._tick_channel(now)

        # 2. the player's auto-attack timer
        can_attack = (not hasattr(self, "_player_can_attack")
                      or self._player_can_attack())
        if (combat["auto_attacking"] and combat["target_guid"]
                and not combat["dead"] and can_attack):
            interval = combat["swing_interval"] or 2.0
            if now - combat["last_swing"] >= interval:
                combat["last_swing"] = now
                await self._player_swing(now, self._player_weapon_stats())
            if self._can_dual_wield():
                offhand = self._player_offhand_weapon_stats()
                if offhand is not None:
                    offhand_interval = float(
                        combat.get("offhand_interval")
                        or offhand.get("delay", 2.0) or 2.0)
                    if now - float(combat.get("last_offhand_swing") or 0.0) \
                            >= offhand_interval:
                        combat["last_offhand_swing"] = now
                        await self._player_offhand_swing(now, offhand)

        # 2b. ranged auto-attack (Auto Shot / Shoot / wand)
        if combat.get("ranged_attacking") and not combat["dead"] and can_attack:
            await self._player_ranged_attack(now)

        # 3. damage/heal over time
        if combat["dots"]:
            alive = []
            for dot in combat["dots"]:
                if dot["expires"] <= now:
                    continue
                if dot["next_tick"] <= now:
                    dot["next_tick"] = now + dot["interval"]
                    await self._apply_dot_tick(dot)
                alive.append(dot)
            combat["dots"] = alive

        # 3b. persistent ground AoE effects
        await self._tick_area_effects(now)

        # 3c. expire modifier auras
        await self._tick_auras(now)

        # 4. creatures that are fighting us
        if combat["aggro"] and not combat["dead"]:
            for low_guid in list(combat["aggro"]):
                try:
                    await self._creature_combat_tick(low_guid, now)
                except Exception as exc:
                    # One bad spawn must not abort idle movement, pets and
                    # regeneration for every other creature in this tick.
                    self.log.warning(
                        f"Creature combat tick failed for {low_guid}: {exc}")

        # 5. idle creatures wander or follow their waypoint path
        await self._creature_idle_tick_all(now)

        # 5b. summoned pets/guardians follow the owner and assist in combat
        await self._summon_tick_all(now)

        # 6. out-of-combat regeneration.  A logout can clear ``character``
        # while this tick was awaiting a packet; do not dereference it then.
        if not self.character:
            return
        await self._regenerate(now)
        if hasattr(self, "_party_stats_tick"):
            await self._party_stats_tick(now)

        # 7. corpses that have waited long enough come back
        if now - combat["last_respawn_scan"] >= RESPAWN_SCAN_INTERVAL:
            combat["last_respawn_scan"] = now
            await self._respawn_corpses(now)
            if hasattr(self, "_gameobject_tick"):
                await self._gameobject_tick(now)

        # 8. nothing is fighting us any more: clear the combat icon
        if not combat["aggro"] and not combat["auto_attacking"] \
                and not combat.get("ranged_attacking") \
                and not combat["pending_cast"] and combat["in_combat"]:
            combat["in_combat"] = False

        await self._flush_player_fields()

    async def handle_attack_swing(self, data: bytes):
        """``CMSG_ATTACKSWING``: begin auto-attacking the given victim."""
        if not self.in_world or not self.character:
            return
        if len(data) < 8:
            return
        target_guid = struct.unpack_from("<Q", data, 0)[0]
        combat = self._get_combat_state()
        if combat["dead"]:
            return
        if hasattr(self, "_player_can_attack") and not self._player_can_attack():
            return
        low_guid = creature.creature_low_guid(target_guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return
        if int(low_guid) in getattr(self, "_summoned_creatures", {}):
            # A player cannot attack their own pet; the client should never
            # send this, but a stale selection must not kill the summon.
            return
        if int(spawn["dead_since"] or 0) > 0:
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_DEADTARGET, b"")
            return
        if hasattr(self, "_target_in_front") \
                and not self._target_in_front(target_guid):
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_BADFACING, b"")
            return
        if hasattr(self, "_has_line_of_sight") and not await self._has_line_of_sight(
                self._unit_eye_position(self.player_guid),
                self._unit_eye_position(target_guid)):
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_CANT_ATTACK, b"")
            return
        # A hunter/wand user clicking a distant target starts Auto Shot/Shoot
        # instead of walking into melee range.
        if (self._player_ranged_weapon_stats() is not None
                and self._distance(self._player_position(),
                                   self._creature_position(low_guid))
                > self._melee_reach(spawn)):
            await self._start_ranged_attack(target_guid, 75)
            return
        combat["target_guid"] = target_guid
        combat["auto_attacking"] = True
        combat["in_combat"] = True
        # ``last_swing = 0`` makes the very first swing land on the next tick so
        # the attack feels immediate, like the real client's off-hand timer.
        combat["last_swing"] = 0.0
        combat["last_offhand_swing"] = 0.0
        combat["swing_interval"] = self._player_weapon_stats()["delay"]
        offhand = (self._player_offhand_weapon_stats()
                   if self._can_dual_wield() else None)
        combat["offhand_interval"] = (
            offhand["delay"] if offhand is not None else 0.0)
        await self.send_packet(
            Opcodes.SMSG_ATTACKSTART,
            struct.pack("<QQ", int(self.player_guid), int(target_guid)),
        )
        self._aggro_creature(low_guid)
        self.log.info(
            f"'{self.character['name']}' begins attacking "
            f"{spawn['name']} ({low_guid})"
        )

    async def handle_attack_stop(self, data: bytes):
        """``CMSG_ATTACKSTOP``: stop the current melee attack."""
        if not self.in_world or not self.character:
            return
        combat = self._get_combat_state()
        target_guid = combat["target_guid"]
        if len(data) >= 8:
            requested = struct.unpack_from("<Q", data, 0)[0]
            if requested and requested != target_guid:
                target_guid = requested
        combat["auto_attacking"] = False
        combat["ranged_attacking"] = False
        if target_guid:
            # SMSG_ATTACKSTOP: packed attacker, packed victim, uint32 nowDead.
            payload = (spells.pack_guid(int(self.player_guid))
                       + spells.pack_guid(int(target_guid))
                       + struct.pack("<I", 1 if combat["dead"] else 0))
            await self.send_packet(Opcodes.SMSG_ATTACKSTOP, payload)
        combat["target_guid"] = 0
        await self._update_player_aiface()
        self.log.debug(f"'{self.character['name']}' stops attacking")

    async def _update_player_aiface(self) -> None:
        """Clear the crossed-swords combat icon when nothing is fighting us."""
        combat = self._get_combat_state()
        if combat["aggro"] or combat["auto_attacking"] \
                or combat.get("ranged_attacking") or combat["pending_cast"]:
            return
        if not combat["in_combat"]:
            return
        combat["in_combat"] = False
        combat["target_guid"] = 0

    async def _player_swing(self, now: float, weapon: dict) -> None:
        """One auto-attack swing against the current target."""
        combat = self._get_combat_state()
        target_guid = combat["target_guid"]
        low_guid = creature.creature_low_guid(target_guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            await self.handle_attack_stop(b"")
            return
        if self._distance(self._player_position(),
                          self._creature_position(low_guid)) > self._melee_reach(spawn):
            # Out of reach: the client draws the "out of range" message itself
            # once we simply hold the swing, so do nothing but stay in combat.
            return
        if hasattr(self, "_target_in_front") \
                and not self._target_in_front(target_guid):
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_BADFACING, b"")
            return
        if hasattr(self, "_has_line_of_sight") and not await self._has_line_of_sight(
                self._unit_eye_position(self.player_guid),
                self._unit_eye_position(target_guid)):
            return

        await self._player_melee_attack(weapon, target_guid, spawn, slot=15)

    async def _player_offhand_swing(self, now: float, weapon: dict) -> None:
        """One independent off-hand auto-attack swing."""
        combat = self._get_combat_state()
        target_guid = combat["target_guid"]
        low_guid = creature.creature_low_guid(target_guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            return
        if self._distance(self._player_position(),
                          self._creature_position(low_guid)) > self._melee_reach(
                              spawn):
            return
        if hasattr(self, "_target_in_front") \
                and not self._target_in_front(target_guid):
            return
        if hasattr(self, "_has_line_of_sight") and not await self._has_line_of_sight(
                self._unit_eye_position(self.player_guid),
                self._unit_eye_position(target_guid)):
            return
        await self._player_melee_attack(
            weapon, target_guid, spawn, offhand_swing=True, slot=16)

    def _clamp_chance(self, value: float, high: float = 100.0) -> float:
        return max(0.0, min(float(high), float(value)))

    def _skill_max_for_level(self) -> int:
        return max(1, int(self.character.get("level", 1))) * 5

    def _skill_value(self, skill_id: int) -> int:
        """Stored skill value, floored at the level cap for weapon skills."""
        skills = self.character.get("skills") or {}
        entry = skills.get(int(skill_id))
        if isinstance(entry, dict):
            value = int(entry.get("value", 0) or 0)
        else:
            value = int(entry or 0)
        return max(value, self._skill_max_for_level())

    def _weapon_skill_value(self, slot: int) -> int:
        meta = self._equipped_weapon_meta(int(slot))
        skill_id = (CR.weapon_skill_for_subclass(meta["subclass"])
                    if meta is not None else CR.SKILL_UNARMED)
        value = self._skill_value(skill_id)
        if slot == 16:
            value += int(self.character.get("weapon_skill_bonus_offhand", 0)
                         or 0)
        elif slot == 17:
            value += int(self.character.get("weapon_skill_bonus_ranged", 0)
                         or 0)
        else:
            value += int(self.character.get("weapon_skill_bonus_main", 0)
                         or 0)
        return value

    def _defense_skill_value(self) -> int:
        return int(self._skill_value(CR.SKILL_DEFENSE)
                   + float(self.character.get("defense_skill_bonus", 0.0)
                           or 0.0))

    def _player_melee_table(self, target_level: int, offhand: bool = False,
                            target_spawn=None, white_swing: bool = True) -> list:
        """Player attack table using weapon vs defense skill and ratings."""
        level = max(1, int(self.character.get("level", 1)))
        skill = self._weapon_skill_value(16 if offhand else 15)
        defense = max(1, int(target_level)) * 5
        difference = defense - skill
        hit_difference = difference
        miss = 5.0
        if hit_difference > 0:
            if hit_difference > 10:
                miss += 1.0
                hit_difference -= 10
                factor = 0.4
            else:
                factor = 0.1
            miss += hit_difference * factor
        else:
            miss += hit_difference * 0.04
        if white_swing and self._can_dual_wield():
            miss += 19.0
        hit_bonus = float(self.character.get(
            "hit_bonus", max(0.0, float(
                self.character.get("hit_chance", 5.0) or 5.0) - 5.0)))
        miss -= hit_bonus

        target = target_spawn if target_spawn is not None else {}
        def target_stat(name, default):
            try:
                value = target[name]
            except (KeyError, IndexError, TypeError):
                return default
            return float(value if value is not None else default)

        # Creatures have a 5% base dodge/parry; block only when the spawn
        # actually carries a shield/block chance.
        dodge_base = target_stat("dodge_chance", 5.0)
        parry_base = target_stat("parry_chance", 5.0)
        block_base = target_stat(
            "block_chance", 5.0 if target_spawn is not None else 0.0)
        dodge = dodge_base + (
            difference * 0.1 if difference > 0 else difference * 0.04)
        if difference > 10:
            parry = parry_base + 1.0 + (difference - 10) * 0.4
        elif difference > 0:
            parry = parry_base + difference * 0.1
        else:
            parry = parry_base + difference * 0.04
        block = block_base + (
            0.0 if difference > 0 else difference * 0.04)
        expertise = max(0.0, float(
            self.character.get("expertise", 0.0) or 0.0))
        dodge = max(0.0, dodge - expertise * 0.25)
        parry = max(0.0, parry - expertise * 0.25)
        glancing = (self._clamp_chance(10.0 + difference, 100.0)
                    if white_swing else 0.0)
        crit = float(self.character.get(
            "offhand_crit_chance" if offhand else "crit_chance", 5.0) or 5.0)
        # NPC crit suppression: 0.2% per point of defense above the attacker's
        # level cap (CMaNGOS CalculateEffectiveCritChance, !vsPlayerOrPet).
        crit += (self._skill_max_for_level() - defense) * 0.2
        crit = self._clamp_chance(crit, 100.0)
        return [
            ("miss", self._clamp_chance(miss, 100.0)),
            ("dodge", self._clamp_chance(dodge, 100.0)),
            ("parry", self._clamp_chance(parry, 100.0)),
            ("block", self._clamp_chance(block, 100.0)),
            ("glancing", glancing),
            ("crit", crit),
        ]

    def _player_ranged_table(self, target_level: int) -> list:
        """Ranged Auto Shot/Shoot table: miss and crit only."""
        skill = self._weapon_skill_value(17)
        defense = max(1, int(target_level)) * 5
        difference = defense - skill
        hit_difference = difference
        miss = 5.0
        if hit_difference > 0:
            if hit_difference > 10:
                miss += 1.0
                hit_difference -= 10
                factor = 0.4
            else:
                factor = 0.1
            miss += hit_difference * factor
        else:
            miss += hit_difference * 0.04
        hit_bonus = float(self.character.get(
            "ranged_hit_bonus", max(0.0, float(
                self.character.get("hit_chance", 5.0) or 5.0) - 5.0)))
        miss -= hit_bonus
        crit = float(self.character.get("ranged_crit_chance", 5.0) or 5.0)
        crit += (self._skill_max_for_level() - defense) * 0.2
        return [
            ("miss", self._clamp_chance(miss, 100.0)),
            ("crit", self._clamp_chance(crit, 100.0)),
        ]

    def _creature_melee_table(self, attacker_level: int) -> list:
        """Player defence table against a creature melee swing."""
        level = max(1, int(self.character.get("level", 1)))
        attacker_skill = max(1, int(attacker_level)) * 5
        defense = self._defense_skill_value()
        difference = defense - attacker_skill
        stunned = bool(hasattr(self, "_player_aura_mods")
                       and self._player_aura_mods().get("stunned"))
        resilience = float(self.character.get("resilience_pct", 0.0) or 0.0)
        skill_cap = max(1, level) * 5
        # ``Unit::CalculateEffectiveMissChance`` for a player-controlled victim.
        miss = 5.0 + difference * 0.04
        dodge = float(self.character.get("dodge_chance", 5.0) or 5.0)
        parry = float(self.character.get("parry_chance", 5.0) or 5.0)
        block = (float(self.character.get("block_chance", 0.0) or 0.0)
                 if self.character.get("has_shield") else 0.0)
        dodge += difference * 0.04
        parry += difference * 0.04
        block += difference * 0.04
        if stunned:
            dodge = parry = block = 0.0
        crit = 5.0 + (skill_cap - defense) * 0.04 - resilience
        deficit = attacker_skill - min(defense, skill_cap)
        crushing = ((2 * deficit) - 15) if deficit >= 20 else 0.0
        return [
            ("miss", self._clamp_chance(miss, 100.0)),
            ("dodge", self._clamp_chance(dodge, 40.0)),
            ("parry", self._clamp_chance(parry, 40.0)),
            ("block", self._clamp_chance(block, 40.0)),
            ("crushing", self._clamp_chance(crushing, 100.0)),
            ("crit", self._clamp_chance(crit, 40.0)),
        ]

    @staticmethod
    def _roll_melee(table: list) -> str:
        roll = random.uniform(0.0, 100.0)
        for name, chance in table:
            if chance <= 0.0:
                continue
            if roll < chance:
                return name
            roll -= chance
        return "hit"

    def _can_dual_wield(self) -> bool:
        return 674 in getattr(self, "known_spell_ids", ())

    async def _player_melee_attack(self, weapon: dict, target_guid: int,
                                   spawn, offhand_swing: bool = False,
                                   slot: int = 15) -> bool:
        """Resolve one main-hand/off-hand swing.  Returns True if it killed."""
        target_level = max(1, int(spawn["min_level"] or 1))
        hit_info = 0x00000004 if offhand_swing else 0
        if hasattr(self, "_creature_aura_mods"):
            mods = self._creature_aura_mods(
                creature.creature_low_guid(target_guid))
            if (mods.get("school_immune", 0) & 1
                    or mods.get("damage_immune", 0) & 1):
                await self._send_attacker_state_update(
                    self.player_guid, target_guid, 0, is_miss=True,
                    hit_info=hit_info)
                return False
        outcome = self._roll_melee(
            self._player_melee_table(target_level, offhand_swing, spawn))
        if outcome == "miss":
            await self._send_attacker_state_update(
                self.player_guid, target_guid, 0, is_miss=True,
                hit_info=hit_info)
            return False
        if outcome in ("dodge", "parry"):
            await self._send_attacker_state_update(
                self.player_guid, target_guid, 0,
                victim_state=2 if outcome == "dodge" else 3,
                hit_info=hit_info)
            return False

        damage = random.uniform(weapon["min"], weapon["max"])
        damage += max(1.0, int(self.character.get("level", 1)) * 0.5)
        damage += (int(self.character.get("attack_power", 0) or 0) / 14.0
                   * float(weapon.get("delay", 2.0) or 2.0))
        if offhand_swing:
            damage *= 0.5
        if outcome == "glancing":
            # ``Unit::CalculateGlanceAmount``: the reduction depends on the
            # defense/weapon-skill difference rather than a fixed 70%.
            skill = min(self._weapon_skill_value(16 if offhand_swing else 15),
                        self._skill_max_for_level())
            difference = max(0, target_level * 5 - skill)
            caster = int(self.character.get("class", 0) or 0) in (5, 8, 9)
            high = max(0.2, min(0.99, (0.9 if caster else 1.2)
                                - 0.03 * difference))
            low = max(0.01, (0.6 if caster else 1.3) - 0.05 * difference)
            low = min(low, min(0.6 if caster else 0.91, high))
            damage *= random.uniform(low, high)
            hit_info |= 0x00010000          # HITINFO_GLANCING
        elif outcome == "crit":
            damage *= 2.0
        # ``Unit::MeleeDamageBonusDone``: buffs/talents the attacker has.
        if hasattr(self, "_apply_damage_done"):
            damage = self._apply_damage_done(1, max(1, int(round(damage))))
        # ``Unit::CalcArmorReducedDamage`` against the creature's template armor.
        armor = int(spawn["armor"] or 0) if "armor" in spawn.keys() else 0
        if hasattr(self, "_creature_aura_mods"):
            mods = self._creature_aura_mods(
                creature.creature_low_guid(target_guid))
            armor += int(mods.get("armor", 0) or 0)
            armor = int(armor * (
                1.0 + float(mods.get("armor_pct", 0.0) or 0.0) / 100.0))
        penetration = max(0.0, min(100.0, float(
            self.character.get("armor_penetration_pct", 0.0) or 0.0)))
        if penetration:
            armor = int(armor * (1.0 - penetration / 100.0))
        armor = max(0, armor)
        if armor > 0:
            damage = self._armor_reduced_damage(
                max(1, int(round(damage))), armor, target_level)
        damage = max(0, int(round(damage)))
        if hasattr(self, "_apply_damage_modifiers"):
            damage = self._apply_damage_modifiers(
                "creature", target_guid, 1, damage)
        blocked_amount = 0
        victim_state = 0
        if outcome == "block":
            block_value = int(spawn["block_value"] or 0) \
                if "block_value" in spawn.keys() else 0
            if not block_value:
                # ``Creature::GetShieldBlockValue``.
                block_value = max(1, target_level // 2)
            blocked_amount = min(damage, max(1, block_value))
            damage = max(0, damage - blocked_amount)
            hit_info |= 0x00002000          # HITINFO_BLOCK
            victim_state = 5                # VICTIMSTATE_BLOCKS
        absorbed = 0
        if hasattr(self, "_absorb_damage"):
            absorbed, depleted = self._absorb_damage(
                "creature", target_guid, 1, damage)
            for aura in depleted:
                if hasattr(self, "_send_aura_update"):
                    await self._send_aura_update(
                        "creature", target_guid, aura, remove=True)
            damage = max(0, damage - absorbed)
        await self._send_attacker_state_update(
            self.player_guid, target_guid, damage,
            is_crit=(outcome == "crit"), hit_info=hit_info,
            victim_state=victim_state, absorbed=absorbed,
            blocked=blocked_amount)
        self._gain_rage(damage, weapon_speed=weapon["delay"],
                        crit=(outcome == "crit"))
        result = (False if damage <= 0 else
                  await self._deal_damage_to_creature(
                      target_guid, damage, self.player_guid))
        if not result and hasattr(self, "_roll_weapon_procs"):
            await self._roll_weapon_procs(
                int(slot), float(weapon.get("base_delay",
                                            weapon.get("delay", 2.0)) or 2.0),
                target_guid)
        if hasattr(self, "_break_cc_on_damage"):
            await self._break_cc_on_damage("creature", target_guid, 0)
        return result

    def _equipped_weapon_meta(self, slot: int):
        """Subclass/ammo/stack metadata for one weapon slot."""
        cache = getattr(self, "_weapon_meta_cache", None)
        if cache is None:
            cache = {}
            self._weapon_meta_cache = cache
        slot = int(slot)
        if slot in cache:
            return cache[slot]
        con = databases.connect_character(self.config.database.world_path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT ci.item_entry, ci.stack_count, it.subclass,"
                " it.inventory_type, it.ammo_type, it.damage_min1,"
                " it.damage_max1, it.delay"
                " FROM character_items ci"
                " JOIN item_templates it ON it.entry = ci.item_entry"
                " WHERE ci.character_guid = ? AND ci.slot = ?",
                (int(self.player_guid or 0), slot),
            ).fetchone()
        finally:
            con.close()
        cache[slot] = dict(row) if row is not None else None
        return cache[slot]

    def _item_template_cached(self, entry: int):
        cache = getattr(self, "_item_template_cache", None)
        if cache is None:
            cache = {}
            self._item_template_cache = cache
        entry = int(entry)
        if entry not in cache:
            cache[entry] = character_db.get_item_template(
                self.config.database.world_path, entry)
        return cache[entry]

    def _ranged_ammo(self, weapon):
        """Return the ammo stack used by a ranged weapon, or ``None``.

        Thrown weapons use their own equipped stack.  Bows/crossbows use
        arrows (subclass 2) and guns use bullets (subclass 3), matching the
        weapon's ``ammo_type``.
        """
        if weapon is None:
            return None
        inventory = (self.character or {}).get("inventory") or {}
        if (int(weapon.get("inventory_type") or 0) == 25
                or int(weapon.get("subclass") or 0) == 16):
            item = inventory.get(17)
            if item is None or int(item.get("stack_count", 0) or 0) <= 0:
                return None
            return {
                "kind": "thrown", "slot": 17, "item": item,
                "min": float(weapon.get("damage_min1") or 0.0),
                "max": float(weapon.get("damage_max1") or 0.0),
            }
        ammo_type = int(weapon.get("ammo_type") or 0)
        if ammo_type <= 0:
            return None
        for slot, item in inventory.items():
            if int(item.get("stack_count", 1) or 1) <= 0:
                continue
            template = self._item_template_cached(
                int(item.get("item_entry") or 0))
            if template is None:
                continue
            if int(template["class"] or 0) != 6 \
                    or int(template["subclass"] or 0) != ammo_type:
                continue
            return {
                "kind": "ammo", "slot": int(slot), "item": item,
                "min": float(template["damage_min1"] or 0.0),
                "max": float(template["damage_max1"] or 0.0),
            }
        return None

    async def _consume_ranged_ammo(self, ammo) -> bool:
        if ammo is None:
            return False
        if hasattr(self, "_remove_item_from_slot"):
            removed = await self._remove_item_from_slot(
                int(ammo["slot"]), 1, notify_quests=False)
            return bool(removed)
        return False

    def _ranged_min_range(self, spell_id: int) -> float:
        if int(spell_id or 0) == 75:       # Auto Shot's client dead zone
            return 5.0
        template = spells.get_spell_template(
            self.config.database.world_path, int(spell_id or 0))
        return float(getattr(template, "get_min_range", lambda: 0.0)())

    def _equipped_weapon_stats(self, slot: int, fallback: bool):
        if int(slot) in (15, 16) and hasattr(self, "_player_aura_mods") \
                and self._player_aura_mods().get("disarmed"):
            return dict(self._BARE_HAND) if fallback else None
        cache = getattr(self, "_weapon_stats_cache", None)
        if cache is None:
            cache = {}
            self._weapon_stats_cache = cache
        if int(slot) in cache:
            row = cache[int(slot)]
        else:
            con = databases.connect_character(self.config.database.world_path)
            try:
                db_row = con.execute(
                    "SELECT it.damage_min1, it.damage_max1, it.delay "
                    "FROM character_items ci "
                    "JOIN item_templates it ON it.entry = ci.item_entry "
                    "WHERE ci.character_guid = ? AND ci.slot = ?",
                    (int(self.player_guid or 0), int(slot)),
                ).fetchone()
            finally:
                con.close()
            row = (float(db_row[0] or 0.0), float(db_row[1] or 0.0),
                   int(db_row[2] or 0)) if db_row else None
            cache[int(slot)] = row
        if not row:
            return dict(self._BARE_HAND) if fallback else None
        low, high, delay = row
        if high <= 0.0:
            return dict(self._BARE_HAND) if fallback else None
        if low <= 0.0:
            low = max(1.0, high * 0.5)
        delay = max(800, delay) / 1000.0
        base_delay = delay
        if hasattr(self, "_player_aura_mods"):
            haste = float(self._player_aura_mods()["melee_haste"])
            haste += float(self.character.get("melee_haste", 0.0) or 0.0)
            delay /= max(0.1, 1.0 + haste / 100.0)
        if hasattr(self, "_weapon_enchant_bonus_damage"):
            bonus = self._weapon_enchant_bonus_damage(int(slot))
            low += bonus
            high += bonus
        return {"min": low, "max": high, "delay": delay,
                "base_delay": base_delay}

    def _player_weapon_stats(self) -> dict:
        """Return ``{min, max, delay}`` for the equipped main-hand weapon.

        ``character_items.slot`` 15 is ``INVSLOT_MAINHAND``.  ``delay`` is stored
        in milliseconds in ``item_templates`` and becomes the auto-attack swing
        period in seconds.
        """
        return self._equipped_weapon_stats(15, fallback=True)

    def _player_offhand_weapon_stats(self):
        """Return the off-hand weapon stats, or ``None`` when not dual-wielding."""
        return self._equipped_weapon_stats(16, fallback=False)

    def _player_ranged_weapon_stats(self):
        """Return the equipped ranged weapon's damage, or ``None``."""
        stats = self._equipped_weapon_stats(17, fallback=False)
        if stats is None:
            return None
        delay = float(stats.get("delay", 2.0) or 2.0)
        haste = float(self.character.get("ranged_haste", 0.0) or 0.0)
        if hasattr(self, "_player_aura_mods"):
            haste += float(self._player_aura_mods().get("ranged_haste", 0.0))
        stats = dict(stats)
        base_delay = delay
        stats["delay"] = max(0.4, delay / max(0.1, 1.0 + haste / 100.0))
        stats["base_delay"] = base_delay
        return stats

    # ------------------------------------------------------------------
    # Weapon procs / temporary weapon enchants
    # ------------------------------------------------------------------
    def _equipped_item_proc_entries(self, slot: int) -> list[dict]:
        """Return on-hit item procs for the weapon in ``slot``.

        ``item_template.spelltrigger_N == 2`` is chance-on-hit; the actual rate
        is stored in ``spellppmRate_N`` (procs per minute).  A ppm of zero is
        treated as "not configured" and skipped.  The rows are immutable for a
        session, so cache them and invalidate only when equipment changes.
        """
        cache = getattr(self, "_weapon_proc_cache", None)
        if cache is None:
            cache = {}
            self._weapon_proc_cache = cache
        if int(slot) in cache:
            return cache[int(slot)]
        con = databases.connect_character(self.config.database.world_path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT it.entry, it.spell_id1, it.spell_trigger1,"
                " it.spell_ppm_rate1, it.spell_cooldown1, it.spell_charges1,"
                " it.spell_id2, it.spell_trigger2, it.spell_ppm_rate2,"
                " it.spell_cooldown2, it.spell_charges2,"
                " it.spell_id3, it.spell_trigger3, it.spell_ppm_rate3,"
                " it.spell_cooldown3, it.spell_charges3,"
                " it.spell_id4, it.spell_trigger4, it.spell_ppm_rate4,"
                " it.spell_cooldown4, it.spell_charges4,"
                " it.spell_id5, it.spell_trigger5, it.spell_ppm_rate5,"
                " it.spell_cooldown5, it.spell_charges5"
                " FROM character_items ci"
                " JOIN item_templates it ON it.entry = ci.item_entry"
                " WHERE ci.character_guid = ? AND ci.slot = ?",
                (int(self.player_guid or 0), int(slot)),
            ).fetchone()
        finally:
            con.close()
        if row is None:
            cache[int(slot)] = []
            return []
        result = []
        for index in range(1, 6):
            if int(row[f"spell_trigger{index}"] or 0) != 2:
                continue
            spell_id = int(row[f"spell_id{index}"] or 0)
            ppm = float(row[f"spell_ppm_rate{index}"] or 0.0)
            if not spell_id or ppm <= 0.0:
                continue
            result.append({
                "item_entry": int(row["entry"]),
                "spell_id": spell_id,
                "ppm": ppm,
                "cooldown": int(row[f"spell_cooldown{index}"] or 0),
                "charges": int(row[f"spell_charges{index}"] or 0),
            })
        cache[int(slot)] = result
        return result

    def _weapon_enchant_procs(self, slot: int) -> dict | None:
        combat = self._get_combat_state()
        entry = combat.setdefault("weapon_enchants", {}).get(int(slot))
        if entry is None:
            return None
        if float(entry.get("expires") or 0.0) <= time.monotonic():
            combat["weapon_enchants"].pop(int(slot), None)
            return None
        return entry

    def _weapon_enchant_bonus_damage(self, slot: int) -> int:
        entry = self._weapon_enchant_procs(slot)
        return int(entry.get("bonus_damage", 0) or 0) if entry else 0

    def _proc_spell_target(self, spell_id: int, target_guid: int) -> int:
        template = spells.get_spell_template(
            self.config.database.world_path, int(spell_id))
        if template is None or template.is_self_targeted():
            return int(self.player_guid)
        return int(target_guid or self.player_guid)

    async def _cast_weapon_proc(self, spell_id: int, target_guid: int,
                                source_key: int = 0) -> None:
        now = time.monotonic()
        combat = self._get_combat_state()
        cooldowns = combat.setdefault("weapon_proc_cooldowns", {})
        if float(cooldowns.get(int(source_key or spell_id), 0.0)) > now:
            return
        template = spells.get_spell_template(
            self.config.database.world_path, int(spell_id))
        cooldown_ms = int(getattr(template, "recovery_time", 0) or 0) \
            if template is not None else 0
        if cooldown_ms > 0:
            cooldowns[int(source_key or spell_id)] = (
                now + cooldown_ms / 1000.0)
        await self._cast_triggered(
            int(spell_id), self._proc_spell_target(spell_id, target_guid))

    async def _roll_weapon_procs(self, slot: int, delay: float,
                                 target_guid: int) -> None:
        """Roll item PPM procs plus a temporary poison/enchant for one hit."""
        if not target_guid or not self.character:
            return
        if not self._is_player_guid(target_guid):
            low = creature.creature_low_guid(int(target_guid))
            spawn = self._get_creature_spawn(low)
            if spawn is None or int(spawn["dead_since"] or 0) > 0:
                return
        # Item procs: chance per swing = ppm * weapon_speed / 60.
        for proc in self._equipped_item_proc_entries(slot):
            key = int(proc["item_entry"])
            charges = int(proc.get("charges") or 0)
            if charges > 0:
                remaining = int(self._get_combat_state().setdefault(
                    "weapon_proc_charges", {}).get(key, charges))
                if remaining <= 0:
                    continue
            chance = max(0.0, min(100.0,
                                float(proc["ppm"]) * float(delay) / 60.0 * 100.0))
            if chance and random.random() * 100.0 < chance:
                await self._cast_weapon_proc(
                    int(proc["spell_id"]), target_guid, source_key=key)
                if charges > 0:
                    remaining = int(self._get_combat_state().setdefault(
                        "weapon_proc_charges", {}).get(key, charges)) - 1
                    self._get_combat_state()["weapon_proc_charges"][key] = remaining
        # Temporary poison / weapon enchant.
        enchant = self._weapon_enchant_procs(slot)
        if enchant is None:
            return
        penalty = 0.5 if int(slot) == 16 else 1.0
        for proc in enchant.get("procs", ()):
            if proc.get("ppm"):
                chance = (float(proc["ppm"]) * float(delay) / 60.0
                          * 100.0 * penalty)
            else:
                chance = float(proc.get("chance") or 0.0) * penalty
            if chance <= 0.0 or random.random() * 100.0 >= chance:
                continue
            await self._cast_weapon_proc(
                int(proc["spell_id"]), target_guid,
                source_key=int(enchant.get("enchant_id") or 0))
            if int(enchant.get("charges") or 0) > 0:
                enchant["charges"] = int(enchant["charges"]) - 1
                if enchant["charges"] <= 0:
                    self._get_combat_state()["weapon_enchants"].pop(
                        int(slot), None)
                    break

    def _restore_weapon_enchants(self) -> None:
        """Reload ``enchantment_2`` temporary weapon enchants after login."""
        if not self.player_guid:
            return
        con = databases.connect_character(self.config.database.world_path)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT ci.slot, ci.enchantment_2, it.name1"
                " FROM character_items ci"
                " JOIN item_templates it ON it.entry = ci.item_entry"
                " WHERE ci.character_guid = ? AND ci.slot IN (15,16)"
                " AND ci.enchantment_2 <> 0",
                (int(self.player_guid),),
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for row in rows:
            enchant_id = int(row["enchantment_2"] or 0)
            if not enchant_id:
                continue
            con = sqlite3.connect(self.config.database.world_path)
            con.row_factory = sqlite3.Row
            try:
                enchant = con.execute(
                    "SELECT * FROM spell_item_enchantment WHERE id = ?",
                    (enchant_id,),
                ).fetchone()
            except sqlite3.Error:
                enchant = None
            finally:
                con.close()
            if enchant is None:
                continue
            procs = []
            bonus_damage = 0
            display_name = str(row["name1"] or enchant["name"] or "")
            for index in (1, 2, 3):
                etype = int(enchant[f"effect{index}"] or 0)
                points = int(enchant[f"effect_points_min{index}"] or 0)
                arg = int(enchant[f"effect_arg{index}"] or 0)
                if etype == spells.ITEM_ENCHANTMENT_COMBAT_SPELL and arg:
                    proc_template = spells.get_spell_template(
                        self.config.database.world_path, arg)
                    proc_name = str(getattr(proc_template, "name", "") or "")
                    if proc_name:
                        display_name = proc_name
                    procs.append(spells.item_enchant_proc(
                        proc_name or display_name, points, arg))
                elif etype == spells.ITEM_ENCHANTMENT_DAMAGE:
                    bonus_damage += points
            if not procs and not bonus_damage:
                continue
            self._get_combat_state().setdefault("weapon_enchants", {})[
                int(row["slot"])] = {
                    "enchant_id": enchant_id,
                    "spell_id": 0,
                    "name": display_name,
                    "slot": int(row["slot"]),
                    "procs": procs,
                    "bonus_damage": bonus_damage,
                    "charges": int(enchant["charges"] or 0),
                    "expires": time.monotonic() + 3600.0,
                    "max_duration": 3600.0,
                }

    async def _start_ranged_attack(self, target_guid: int,
                                   spell_id: int = 75) -> None:
        combat = self._get_combat_state()
        target_guid = int(target_guid or combat.get("target_guid")
                          or combat.get("selected_guid") or 0)
        if not target_guid:
            return
        if hasattr(self, "_target_in_front") \
                and not self._target_in_front(target_guid):
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_BADFACING, b"")
            return
        if hasattr(self, "_has_line_of_sight") and not await self._has_line_of_sight(
                self._unit_eye_position(self.player_guid),
                self._unit_eye_position(target_guid)):
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_CANT_ATTACK, b"")
            return
        combat["target_guid"] = int(target_guid)
        combat["auto_attacking"] = False
        combat["ranged_attacking"] = True
        combat["ranged_spell_id"] = int(spell_id)
        combat["last_ranged_swing"] = 0.0
        combat["in_combat"] = True
        stats = self._player_ranged_weapon_stats()
        if stats is None:
            combat["ranged_attacking"] = False
            return
        weapon = self._equipped_weapon_meta(17)
        subclass = int(weapon.get("subclass") or 0) if weapon else 0
        if subclass == 19:
            spell_id = 5019          # wand Shoot
        elif subclass == 16 or (weapon is not None
                                and int(weapon.get("inventory_type") or 0) == 25):
            spell_id = 2764          # Throw
        else:
            spell_id = 75            # Auto Shot
        combat["ranged_spell_id"] = int(spell_id)
        combat["ranged_interval"] = stats["delay"]
        low_target = creature.creature_low_guid(int(target_guid))
        distance = self._distance(
            self._player_position(), self._creature_position(low_target))
        if distance < self._ranged_min_range(int(spell_id)):
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_CANT_ATTACK, b"")
            combat["ranged_attacking"] = False
            return
        await self.send_packet(
            Opcodes.SMSG_ATTACKSTART,
            struct.pack("<QQ", int(self.player_guid), int(target_guid)),
        )
        self._aggro_creature(
            creature.creature_low_guid(int(target_guid)))

    async def _player_ranged_attack(self, now: float) -> None:
        """One Auto Shot/Shoot swing against the current target."""
        combat = self._get_combat_state()
        target_guid = int(combat.get("target_guid") or 0)
        if not target_guid or not combat.get("ranged_attacking"):
            return
        low = creature.creature_low_guid(target_guid)
        spawn = self._get_creature_spawn(low)
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            await self.handle_attack_stop(b"")
            return
        stats = self._player_ranged_weapon_stats()
        if stats is None:
            await self.handle_attack_stop(b"")
            return
        weapon = self._equipped_weapon_meta(17)
        ammo = self._ranged_ammo(weapon)
        is_thrown = bool(weapon is not None and (
            int(weapon.get("inventory_type") or 0) == 25
            or int(weapon.get("subclass") or 0) == 16))
        needs_ammo = bool(is_thrown or (
            weapon is not None and int(weapon.get("ammo_type") or 0) > 0))
        if needs_ammo and ammo is None:
            await self.handle_attack_stop(b"")
            return
        distance = self._distance(self._player_position(),
                                 self._creature_position(low))
        if distance > 41.0:
            return
        min_range = self._ranged_min_range(
            int(combat.get("ranged_spell_id") or 75))
        if min_range > 0.0 and distance < min_range:
            return
        if hasattr(self, "_target_in_front") \
                and not self._target_in_front(target_guid):
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_BADFACING, b"")
            return
        if hasattr(self, "_has_line_of_sight") and not await self._has_line_of_sight(
                self._unit_eye_position(self.player_guid),
                self._unit_eye_position(target_guid)):
            return
        interval = float(combat.get("ranged_interval") or stats["delay"])
        if now - float(combat.get("last_ranged_swing") or 0.0) < interval:
            return
        combat["last_ranged_swing"] = now
        if ammo is not None and not await self._consume_ranged_ammo(ammo):
            await self.handle_attack_stop(b"")
            return
        target_level = max(1, int(spawn["min_level"] or 1))
        outcome = self._roll_melee(self._player_ranged_table(target_level))
        if outcome == "miss":
            await self._send_attacker_state_update(
                self.player_guid, target_guid, 0, is_miss=True)
            return
        damage = random.uniform(stats["min"], stats["max"])
        damage += max(1.0, int(self.character.get("level", 1)) * 0.5)
        damage += (int(self.character.get("ranged_attack_power", 0) or 0)
                   / 14.0 * float(stats["delay"]))
        if ammo is not None and ammo.get("kind") != "thrown":
            damage += random.uniform(ammo["min"], ammo["max"])
        if outcome == "crit":
            damage *= 2.0
        if hasattr(self, "_apply_damage_done"):
            damage = self._apply_damage_done(1, max(1, int(round(damage))))
        damage = max(1, int(round(damage)))
        armor = int(spawn["armor"] or 0) if "armor" in spawn.keys() else 0
        penetration = max(0.0, min(100.0, float(
            self.character.get("armor_penetration_pct", 0.0) or 0.0)))
        if penetration:
            armor = int(armor * (1.0 - penetration / 100.0))
        armor = max(0, armor)
        if armor > 0:
            damage = self._armor_reduced_damage(
                damage, armor, target_level)
        if hasattr(self, "_apply_damage_modifiers"):
            damage = self._apply_damage_modifiers(
                "creature", target_guid, 1, damage)
        absorbed = 0
        if hasattr(self, "_absorb_damage"):
            absorbed, depleted = self._absorb_damage(
                "creature", target_guid, 1, damage)
            for aura in depleted:
                if hasattr(self, "_send_aura_update"):
                    await self._send_aura_update(
                        "creature", target_guid, aura, remove=True)
            damage = max(0, damage - absorbed)
        await self._send_attacker_state_update(
            self.player_guid, target_guid, damage,
            is_crit=(outcome == "crit"), absorbed=absorbed)
        if damage > 0:
            await self._deal_damage_to_creature(
                target_guid, damage, self.player_guid, threat_kind="ranged")
        if hasattr(self, "_roll_weapon_procs") \
                and self._creature_health(target_guid) > 0:
            await self._roll_weapon_procs(
                17, float(stats.get("base_delay", stats["delay"])), target_guid)

    def _victim_health(self, victim_guid: int) -> int:
        """Health the victim has *before* the hit we are about to report.

        Uses :meth:`_creature_health` rather than the raw ``current_health``
        column: a spawn that has never been damaged stores 0 there, and reading
        it directly would report every first hit as full overkill.
        """
        if self._is_player_guid(victim_guid):
            if not self.character:
                return 0
            return int(self.character.get("health", self._max_health()))
        return self._creature_health(victim_guid)
