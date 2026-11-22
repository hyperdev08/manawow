"""Combat loop, melee swings and attacker state updates."""
from __future__ import annotations

import asyncio
import random
import sqlite3
import struct
import time

from src.protocol.opcodes import Opcodes
from src.world import creature
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
    _BARE_HAND = {"min": 1.0, "max": 3.0, "delay": 2.0}

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
                await self._creature_combat_tick(low_guid, now)

        # 5. idle creatures wander or follow their waypoint path
        await self._creature_idle_tick_all(now)

        # 5b. summoned pets/guardians follow the owner and assist in combat
        await self._summon_tick_all(now)

        # 6. out-of-combat regeneration
        await self._regenerate(now)

        # 7. corpses that have waited long enough come back
        if now - combat["last_respawn_scan"] >= RESPAWN_SCAN_INTERVAL:
            combat["last_respawn_scan"] = now
            await self._respawn_corpses(now)

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
        combat["swing_interval"] = self._player_weapon_stats()["delay"]
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

        await self._player_melee_attack(weapon, target_guid, spawn)
        # Dual wield: a second, weaker swing from the off-hand weapon on the
        # same timer.  Retail gives the off-hand a higher miss chance.
        if int(spawn["dead_since"] or 0) == 0 and self._can_dual_wield():
            offhand = self._player_offhand_weapon_stats()
            if offhand is not None:
                await self._player_melee_attack(
                    offhand, target_guid, spawn, offhand_swing=True)

    def _clamp_chance(self, value: float, high: float = 100.0) -> float:
        return max(0.0, min(float(high), float(value)))

    def _player_melee_table(self, target_level: int,
                            offhand: bool = False) -> list:
        """Chance table in the reference core's order (miss..crit)."""
        level = max(1, int(self.character.get("level", 1)))
        diff = max(0, int(target_level) - level)
        miss = 5.0 + diff - (
            float(self.character.get("hit_chance", 5.0)) - 5.0)
        if offhand:
            miss += 15.0
        expertise = max(0, int(self.character.get("expertise", 0) or 0))
        dodge = max(0.0, 5.0 + diff * 0.5 - expertise * 0.25)
        parry = max(0.0, 5.0 + diff * 0.5 - expertise * 0.25)
        return [
            ("miss", self._clamp_chance(miss, 60.0)),
            ("dodge", self._clamp_chance(dodge, 30.0)),
            ("parry", self._clamp_chance(parry, 30.0)),
            ("glancing", 15.0 if diff >= 3 else 0.0),
            ("crit", self._clamp_chance(
                float(self.character.get("crit_chance", 5.0)), 50.0)),
        ]

    def _creature_melee_table(self, attacker_level: int) -> list:
        """Player defence table against a creature melee swing."""
        level = max(1, int(self.character.get("level", 1)))
        diff = max(0, int(attacker_level) - level)
        resilience = float(self.character.get("resilience", 0) or 0) / 25.0
        block = (float(self.character.get("block_chance", 0.0))
                 if self.character.get("has_shield") else 0.0)
        return [
            ("miss", self._clamp_chance(5.0 + diff, 60.0)),
            ("dodge", self._clamp_chance(
                float(self.character.get("dodge_chance", 5.0)) + diff * 0.5,
                40.0)),
            ("parry", self._clamp_chance(
                float(self.character.get("parry_chance", 5.0)) + diff * 0.5,
                40.0)),
            ("block", self._clamp_chance(block + diff * 0.5, 40.0)),
            ("crushing", 15.0 if diff >= 3 else 0.0),
            ("crit", self._clamp_chance(5.0 + diff * 0.5 - resilience, 40.0)),
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
                                   spawn, offhand_swing: bool = False) -> bool:
        """Resolve one main-hand/off-hand swing.  Returns True if it killed."""
        target_level = max(1, int(spawn["min_level"] or 1))
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
            self._player_melee_table(target_level, offhand_swing))
        hit_info = 0x00000004 if offhand_swing else 0
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
            damage *= 0.7
            hit_info |= 0x00010000          # HITINFO_GLANCING
        elif outcome == "crit":
            damage *= 2.0
        # ``Unit::CalcArmorReducedDamage`` against the creature's template armor.
        armor = int(spawn["armor"] or 0) if "armor" in spawn.keys() else 0
        if hasattr(self, "_creature_aura_mods"):
            mods = self._creature_aura_mods(
                creature.creature_low_guid(target_guid))
            armor += int(mods.get("armor", 0) or 0)
            armor = int(armor * (
                1.0 + float(mods.get("armor_pct", 0.0) or 0.0) / 100.0))
        armor = max(0, armor - int(
            self.character.get("armor_penetration", 0) or 0))
        if armor > 0:
            damage = self._armor_reduced_damage(
                max(1, int(round(damage))), armor, target_level)
        damage = max(1, int(round(damage)))
        if hasattr(self, "_apply_damage_modifiers"):
            damage = self._apply_damage_modifiers(
                "creature", target_guid, 1, damage)
        await self._send_attacker_state_update(
            self.player_guid, target_guid, damage,
            is_crit=(outcome == "crit"), hit_info=hit_info)
        self._gain_rage(damage, weapon_speed=weapon["delay"],
                        crit=(outcome == "crit"))
        result = await self._deal_damage_to_creature(
            target_guid, damage, self.player_guid)
        if hasattr(self, "_break_cc_on_damage"):
            await self._break_cc_on_damage("creature", target_guid, 0)
        return result

    def _equipped_weapon_stats(self, slot: int, fallback: bool):
        con = sqlite3.connect(self.config.database.world_path)
        try:
            row = con.execute(
                "SELECT it.damage_min1, it.damage_max1, it.delay "
                "FROM character_items ci "
                "JOIN item_templates it ON it.entry = ci.item_entry "
                "WHERE ci.character_guid = ? AND ci.slot = ?",
                (int(self.player_guid or 0), int(slot)),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return dict(self._BARE_HAND) if fallback else None
        low = float(row[0] or 0.0)
        high = float(row[1] or 0.0)
        delay = int(row[2] or 0)
        if high <= 0.0:
            return dict(self._BARE_HAND) if fallback else None
        if low <= 0.0:
            low = max(1.0, high * 0.5)
        delay = max(800, delay) / 1000.0
        if hasattr(self, "_player_aura_mods"):
            haste = float(self._player_aura_mods()["melee_haste"])
            haste += float(self.character.get("melee_haste", 0.0) or 0.0)
            delay /= max(0.1, 1.0 + haste / 100.0)
        return {"min": low, "max": high, "delay": delay}

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
        stats["delay"] = max(0.4, delay / max(0.1, 1.0 + haste / 100.0))
        return stats

    async def _start_ranged_attack(self, target_guid: int,
                                   spell_id: int = 75) -> None:
        combat = self._get_combat_state()
        target_guid = int(target_guid or combat.get("target_guid")
                          or combat.get("selected_guid") or 0)
        if not target_guid:
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
        combat["ranged_interval"] = stats["delay"]
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
            return
        distance = self._distance(self._player_position(),
                                 self._creature_position(low))
        if distance > 41.0:
            return
        interval = float(combat.get("ranged_interval") or stats["delay"])
        if now - float(combat.get("last_ranged_swing") or 0.0) < interval:
            return
        combat["last_ranged_swing"] = now
        target_level = max(1, int(spawn["min_level"] or 1))
        outcome = self._roll_melee(
            self._player_melee_table(target_level, offhand=False))
        if outcome == "miss":
            await self._send_attacker_state_update(
                self.player_guid, target_guid, 0, is_miss=True)
            return
        if outcome in ("dodge", "parry"):
            await self._send_attacker_state_update(
                self.player_guid, target_guid, 0,
                victim_state=2 if outcome == "dodge" else 3)
            return
        damage = random.uniform(stats["min"], stats["max"])
        damage += max(1.0, int(self.character.get("level", 1)) * 0.5)
        damage += (int(self.character.get("ranged_attack_power", 0) or 0)
                   / 14.0 * float(stats["delay"]))
        if outcome == "crit":
            damage *= 2.0
        damage = max(1, int(round(damage)))
        armor = int(spawn["armor"] or 0) if "armor" in spawn.keys() else 0
        armor = max(0, armor - int(
            self.character.get("armor_penetration", 0) or 0))
        if armor > 0:
            damage = self._armor_reduced_damage(
                damage, armor, target_level)
        if hasattr(self, "_apply_damage_modifiers"):
            damage = self._apply_damage_modifiers(
                "creature", target_guid, 1, damage)
        await self._send_attacker_state_update(
            self.player_guid, target_guid, damage,
            is_crit=(outcome == "crit"))
        await self._deal_damage_to_creature(
            target_guid, damage, self.player_guid)


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
