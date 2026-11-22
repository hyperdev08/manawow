"""Creature spell-list AI and cast resolution."""
from __future__ import annotations

import math
import random
import sqlite3
import struct
import time

from typing import List

from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import spells
from src.world import update_fields as F

from .constants import (
    CREATURE_SPELL_GCD,
    CREATURE_SPELL_LIST_INTERVAL,
    SPELL_FAILED_INTERRUPTED,
    SPELL_LIST_FLAG_CATEGORY_COOLDOWN,
    SPELL_LIST_FLAG_RANGED_ACTION,
    SPELL_LIST_FLAG_SUPPORT_ACTION,
)

class CreatureAIMixin:
    @staticmethod
    def _creature_spell_targets_self(template) -> bool:
        """Whether a legacy ``creature_template_spells`` entry is a self buff.

        ``creature_template_spells`` predates ``creature_spell_list`` and has no
        target column.  A spell whose range is "self only", or whose effects all
        point at the caster (Frost Armor and friends), is cast on the creature;
        everything else is aimed at its victim, like ``DoCastSpellIfCan``.
        """
        if int(template.range_index) == 1:
            return True
        has_self = False
        has_enemy = False
        for effect in template.effects:
            target = int(effect["target_a"])
            if target == 1:            # TARGET_UNIT_CASTER
                has_self = True
            elif target in (5, 6):     # TARGET_UNIT_TARGET_ENEMY / CHAIN
                has_enemy = True
        return has_self and not has_enemy

    def _legacy_creature_spell_entries(self, spawn) -> List[dict]:
        """Build spell-list entries from ``creature_template_spells``.

        Creatures whose ``creature_template.SpellList`` is 0 still carry the
        legacy ten-slot spell set (and optional ``creature_cooldowns``).  CMaNGOS
        synthesises a list for them as well, so give each slot the same
        target/availability/probability fields the real spell-list rows have.
        """
        path = self.config.database.world_path
        entry = int(spawn["entry"])
        spell_ids = spells.get_creature_spells(path, entry)
        if not spell_ids:
            return []
        cooldowns = spells.get_creature_cooldowns(path, entry)
        rows = []
        for position, spell_id in enumerate(spell_ids):
            template = spells.get_spell_template(path, spell_id)
            if template is None:
                continue
            repeat_min, repeat_max = cooldowns.get(spell_id, (0, 0))
            rows.append({
                "position": position,
                "spell_id": int(spell_id),
                "flags": 0,
                "combat_condition": 0,
                "target_id": 2 if self._creature_spell_targets_self(template) else 1,
                "script_id": 0,
                "availability": 100,
                "probability": 1,
                "initial_min": 0,
                "initial_max": 0,
                "repeat_min": int(repeat_min),
                "repeat_max": int(repeat_max),
                "template": template,
            })
        return rows

    def _build_creature_ai(self, spawn, now: float) -> dict:
        """Create the per-fight AI state for one creature.

        Availability is rolled once here (CMaNGOS does it when the spell list is
        assigned), the initial cooldown is drawn from ``InitialMin/Max`` and the
        mana pool comes from ``creature_template_classlevelstats`` scaled by the
        template's ``energy_multiplier``.
        """
        path = self.config.database.world_path
        spell_list_id = int(spawn["spell_list_id"] or 0)
        header = spells.get_creature_spell_list_entry(path, spell_list_id)
        raw = (spells.get_creature_spell_list(path, spell_list_id)
               if spell_list_id else [])
        if not raw:
            raw = self._legacy_creature_spell_entries(spawn)

        entries = []
        for data in raw:
            availability = int(data.get("availability") or 0)
            if availability < 100 and random.randint(0, 100) > availability:
                continue
            template = data.get("template")
            if template is None:
                template = spells.get_spell_template(path, data["spell_id"])
            if template is None:
                continue
            # ``CreatureSpellListSpell::DisabledForAI``: spells flagged
            # ``SPELL_ATTR_EX_NO_AUTOCAST_AI`` are never picked by the AI.
            if int(getattr(template, "attributes_ex", 0)) & \
                    spells.SPELL_ATTR_EX_NO_AUTOCAST_AI:
                continue
            # ``CombatCondition`` needs the condition/expression tables, which
            # are not imported; skipping the entry is what CMaNGOS does when the
            # condition is not satisfied.  -1 and 0 both mean "no condition".
            if int(data.get("combat_condition") or 0) not in (-1, 0):
                continue
            initial_min = max(0, int(data.get("initial_min") or 0))
            initial_max = max(initial_min, int(data.get("initial_max") or 0))
            initial_ms = random.randint(initial_min, initial_max)
            entries.append({
                "position": int(data.get("position") or 0),
                "spell_id": int(data["spell_id"]),
                "flags": int(data.get("flags") or 0),
                "combat_condition": int(data.get("combat_condition") or 0),
                "target_id": int(data.get("target_id") or 0),
                "script_id": int(data.get("script_id") or 0),
                "probability": max(0, int(data.get("probability") or 0)),
                "repeat_min": max(0, int(data.get("repeat_min") or 0)),
                "repeat_max": max(0, int(data.get("repeat_max") or 0)),
                "template": template,
                "next_ready": now + initial_ms / 1000.0,
            })

        level = max(1, int(spawn["min_level"] or 1))
        unit_class = int(spawn["unit_class"] or 1)
        base_mana = spells.get_creature_base_mana(path, level, unit_class)
        max_mana = max(0, int(base_mana * float(spawn["energy_multiplier"] or 1.0)))
        current = int(spawn["current_power"] or 0)
        mana = current if 0 < current <= max_mana else max_mana

        return {
            "entry": int(spawn["entry"]),
            "level": level,
            "power_type": int(spawn["unit_power_type"] or 0),
            "max_mana": max_mana,
            "mana": mana,
            "spells": entries,
            "chance_support": int(header.get("chance_support_action", 0)),
            "chance_ranged": int(header.get("chance_ranged_attack", 0)),
            "next_tick": now,
            "gcd_until": 0.0,
            "cast": None,
            "cast_id": 0,
        }

    async def _creature_ai_state(self, low_guid: int, spawn, now: float):
        """Return (and on first use create) the AI state of a fighting creature."""
        state = self._get_combat_state()["aggro"].get(int(low_guid))
        if state is None:
            return None
        ai = state.get("ai")
        if ai is not None:
            return ai
        ai = self._build_creature_ai(spawn, now)
        state["ai"] = ai
        if ai["max_mana"] > 0:
            await self._send_creature_power(low_guid, ai)
        return ai

    def _set_creature_power(self, low_guid: int, power: int) -> None:
        """Persist a creature's current power (mana)."""
        con = sqlite3.connect(self.config.database.world_path)
        try:
            con.execute(
                "UPDATE creature_spawns SET current_power = ? WHERE guid = ?",
                (max(0, int(power)), int(low_guid)),
            )
            con.commit()
        finally:
            con.close()

    async def _send_creature_power(self, low_guid: int, ai: dict) -> None:
        """Mirror a creature's mana in its update fields and ``SMSG_POWER_UPDATE``."""
        power_type = int(ai.get("power_type") or 0)
        power = max(0, int(ai["mana"]))
        max_power = max(0, int(ai["max_mana"]))
        await self._send_creature_fields(low_guid, {
            F.UNIT_FIELD_POWER1 + power_type: power,
            F.UNIT_FIELD_MAXPOWER1 + power_type: max_power,
        })
        await self.send_packet(
            Opcodes.SMSG_POWER_UPDATE,
            spells.pack_guid(self._creature_object_guid(int(low_guid), ai["entry"]))
            + struct.pack("<BI", power_type, power & 0xFFFFFFFF),
        )

    @staticmethod
    def _creature_spell_cost(template, max_mana: int) -> int:
        """Mana cost of a creature spell (flat plus percentage, like the core)."""
        if int(template.power_type) != spells.POWER_MANA:
            return 0
        cost = int(template.mana_cost)
        cost += int(max_mana * int(template.mana_cost_percentage) / 100.0)
        return max(0, cost)

    def _resolve_creature_spell_target(self, entry: dict, low_guid: int,
                                       spawn, ai: dict, player_guid: int):
        """Pick a target for one spell-list entry.

        Returns ``(ok, target_guid)``; ``target_guid`` may be 0 for a spell with
        no explicit target.  ``ok`` is False when the entry has no eligible
        target this tick (CMaNGOS ``ChooseTarget`` returning false).
        """
        target_id = int(entry.get("target_id") or 0)
        caster_guid = self._creature_object_guid(int(low_guid), ai["entry"])
        if target_id == 0:
            return True, 0
        if target_id == 1:
            return True, int(player_guid)
        if target_id == 2:
            return True, caster_guid
        if target_id in (3, 4):          # friendly dispel: no auras tracked
            return False, 0
        if target_id in (5, 6):          # missing buff: buff the creature
            return True, caster_guid
        if target_id == 7:               # current target not alone
            return False, 0

        targeting = spells.get_creature_spell_targeting(
            self.config.database.world_path, target_id)
        if targeting is None:
            return True, int(player_guid)
        if int(targeting["type"]) == 2:  # support
            if not int(targeting["param3"]):
                return False, 0
            current = self._creature_health(caster_guid)
            maximum = self._creature_max_health(spawn)
            missing = max(0, maximum - current)
            if missing <= 0:
                return False, 0
            threshold = int(targeting["param1"])
            if int(targeting["param2"]):
                threshold = int(maximum * threshold / 100.0)
            if missing < threshold:
                return False, 0
            return True, caster_guid
        # Attack targeting: this session only ever has the player as a hostile
        # unit, so every select flag resolves to that player.
        if int(targeting["param3"]) & (8192 | 262144):
            # SELECT_FLAG_USE_EFFECT_RADIUS: only checks that a target exists.
            return True, 0
        return True, int(player_guid)

    def _creature_spell_in_range(self, template, spawn, low_guid: int,
                                 target_guid: int, distance: float) -> bool:
        caster_guid = self._creature_object_guid(int(low_guid))
        if not target_guid or target_guid == caster_guid:
            return True
        spell_range = template.get_range()
        if spell_range >= 49000.0:
            return True
        return distance <= max(0.5, spell_range) + self._melee_reach(spawn)

    async def _creature_spell_list_tick(self, low_guid: int, spawn, ai: dict,
                                        now: float, in_melee: bool) -> None:
        """One 1200 ms spell-list evaluation (CMaNGOS ``UpdateSpellLists``)."""
        if ai["cast"] is not None or now < ai["next_tick"]:
            return
        if hasattr(self, "_creature_can_cast") \
                and not self._creature_can_cast(int(low_guid)):
            return
        ai["next_tick"] = now + CREATURE_SPELL_LIST_INTERVAL
        if now < ai["gcd_until"] or not ai["spells"]:
            return

        support_roll = random.randint(0, 100)
        ranged_roll = random.randint(0, 100)
        ranged_chance = int(ai["chance_ranged"])
        if in_melee:
            ranged_chance = max(0, ranged_chance - 50)

        player_guid = int(self.player_guid or 0)
        distance = self._distance(self._player_position(),
                                  self._creature_position(int(low_guid)))
        eligible = []
        total = 0
        for entry in ai["spells"]:
            if entry["next_ready"] > now:
                continue
            flags = int(entry["flags"])
            if (flags & SPELL_LIST_FLAG_SUPPORT_ACTION
                    and support_roll > ai["chance_support"]):
                continue
            if (flags & SPELL_LIST_FLAG_RANGED_ACTION
                    and ranged_roll > ranged_chance):
                continue
            template = entry["template"]
            lockout = ai.get("school_lockout") or {}
            if lockout and float(lockout.get("until", 0)) > now \
                    and int(lockout.get("school", 0)) & (
                        int(template.school) or 1):
                continue
            cost = self._creature_spell_cost(template, ai["max_mana"])
            if cost > ai["mana"]:
                continue
            ok, target_guid = self._resolve_creature_spell_target(
                entry, low_guid, spawn, ai, player_guid)
            if not ok:
                continue
            if target_guid and not self._creature_spell_in_range(
                    template, spawn, low_guid, target_guid, distance):
                continue
            eligible.append((entry, target_guid))
            total += max(0, int(entry["probability"]))

        if not eligible:
            return

        # Probability is a weight; when every entry has 0 the list is priority
        # ordered and the first eligible spell wins (CMaNGOS roll -1).
        if total <= 0:
            chosen = eligible[0]
        else:
            roll = random.randint(0, total - 1)
            chosen = None
            for item in eligible:
                probability = max(0, int(item[0]["probability"]))
                if roll < probability:
                    chosen = item
                    break
                roll -= probability
            if chosen is None:
                chosen = eligible[-1]
        await self._start_creature_cast(low_guid, spawn, ai, chosen[0],
                                        chosen[1], now)

    async def _start_creature_cast(self, low_guid: int, spawn, ai: dict,
                                   entry: dict, target_guid: int,
                                   now: float) -> bool:
        template = entry["template"]
        cost = self._creature_spell_cost(template, ai["max_mana"])
        if cost > ai["mana"]:
            return False

        # The repeat timer is a real spell cooldown: it starts when the cast
        # begins, not when it lands (Creature::GetSpellCooldown).
        repeat_min = max(0, int(entry["repeat_min"]))
        repeat_max = max(repeat_min, int(entry["repeat_max"]))
        entry["next_ready"] = now + random.randint(repeat_min, repeat_max) / 1000.0
        # ``SPELL_LIST_FLAG_CATEGORY_COOLDOWN``: ``Creature::GetSpellCooldown``
        # answers ``COOLDOWN_RESULT_CATEGORY_FOUND`` so ``Spell::AddCooldown``
        # locks the whole spell category, not just this spell id.
        if (int(entry["flags"]) & SPELL_LIST_FLAG_CATEGORY_COOLDOWN
                and int(getattr(template, "category", 0))):
            category = int(template.category)
            until = float(entry["next_ready"])
            for other in ai["spells"]:
                if int(getattr(other["template"], "category", 0)) == category:
                    other["next_ready"] = max(float(other["next_ready"]), until)
        ai["gcd_until"] = now + CREATURE_SPELL_GCD

        cast_id = int(ai["cast_id"])
        ai["cast_id"] = (cast_id + 1) & 0xFF
        caster_guid = self._creature_object_guid(int(low_guid), ai["entry"])
        cast = spells.SpellCast(caster_guid, template.id, target_guid)
        cast.template = template
        cast_time_ms = template.get_casting_time_ms()
        cast.cast_time_ms = cast_time_ms
        ai["cast"] = {
            "cast_id": cast_id,
            "spell_id": template.id,
            "target_guid": target_guid,
            "template": template,
            "finish_at": now + cast_time_ms / 1000.0,
            "cost": cost,
        }
        await self.send_packet(
            Opcodes.SMSG_SPELL_START,
            spells.build_spell_start(cast, caster_guid, target_guid, cast_id),
        )
        if cast_time_ms <= 0:
            await self._finish_creature_cast(low_guid, spawn, ai)
        return True

    async def _finish_creature_cast(self, low_guid: int, spawn,
                                    ai: dict) -> None:
        cast = ai["cast"]
        if cast is None:
            return
        ai["cast"] = None
        template = cast["template"]
        target_guid = int(cast["target_guid"])
        caster_guid = self._creature_object_guid(int(low_guid), ai["entry"])

        # ``Spell::finish`` takes the power cost after the cast time has run.
        cost = int(cast["cost"])
        if cost > 0:
            ai["mana"] = max(0, int(ai["mana"]) - cost)
            self._set_creature_power(low_guid, ai["mana"])
            await self._send_creature_power(low_guid, ai)

        cast_obj = spells.SpellCast(caster_guid, template.id, target_guid)
        cast_obj.template = template
        await self.send_packet(
            Opcodes.SMSG_SPELL_GO,
            spells.build_spell_go(
                cast_obj, caster_guid, target_guid, cast["cast_id"],
                timestamp_ms=self._server_tick_ms(),
                off_gcd=template.start_recovery_time == 0,
            ),
        )
        await self._apply_creature_spell_effects(
            low_guid, spawn, ai, template, target_guid)
        self.log.debug(
            f"Creature {ai['entry']} casts {template.name} ({template.id})"
        )

    async def _heal_creature(self, low_guid: int, spawn, amount: int,
                             template=None, caster_guid: int = 0) -> None:
        """Heal a creature and write the matching update/heal log."""
        amount = max(0, int(amount))
        if amount <= 0:
            return
        guid = self._creature_object_guid(int(low_guid))
        current = self._creature_health(guid)
        maximum = self._creature_max_health(spawn)
        healed = min(amount, max(0, maximum - current))
        if healed <= 0:
            return
        self._set_creature_health(low_guid, current + healed)
        await self._send_creature_fields(low_guid, {
            F.UNIT_FIELD_HEALTH: current + healed,
            F.UNIT_FIELD_MAXHEALTH: maximum,
        })
        if template is not None:
            await self._send_spell_heal_log(
                guid, caster_guid or guid, template.id, healed,
                max(0, amount - healed),
            )

    async def _cast_creature_triggered(self, low_guid: int, spawn, ai: dict,
                                       spell_id: int, target_guid: int) -> None:
        """Cast a triggered creature spell immediately (no cost/cooldown)."""
        template = spells.get_spell_template(self.config.database.world_path,
                                             spell_id)
        if template is None:
            return
        caster_guid = self._creature_object_guid(int(low_guid), ai["entry"])
        cast = spells.SpellCast(caster_guid, spell_id, target_guid)
        cast.template = template
        await self.send_packet(
            Opcodes.SMSG_SPELL_START,
            spells.build_spell_start(cast, caster_guid, target_guid, 0),
        )
        await self.send_packet(
            Opcodes.SMSG_SPELL_GO,
            spells.build_spell_go(
                cast, caster_guid, target_guid, 0,
                timestamp_ms=self._server_tick_ms(),
                off_gcd=template.start_recovery_time == 0,
            ),
        )
        await self._apply_creature_spell_effects(
            low_guid, spawn, ai, template, target_guid)

    async def _creature_spell_lands(self, template, target_guid: int,
                                    effect: dict | None = None,
                                    caster_guid: int = 0) -> bool:
        """Check immunities/reflect for a creature spell.

        Creatures always land their spells against an equal-level player in
        this simulator; what matters for 3.3.5 feel is the immunity/reflect
        layer, which is driven by auras.  Sending the miss packet is enough for
        the client to show "Immune" or "Reflected".
        """
        if not target_guid:
            return True
        kind = self._spell_target_kind(target_guid)
        if kind == "peer":
            return True
        if hasattr(self, "_spell_immune") and self._spell_immune(
                kind, target_guid, template, effect):
            await self._send_spell_miss_log(
                target_guid, caster_guid or self.player_guid, template.id,
                spells.SPELL_MISS_IMMUNE)
            return False
        if hasattr(self, "_spell_reflects") and self._spell_reflects(
                target_guid):
            await self._send_spell_miss_log(
                target_guid, caster_guid or self.player_guid, template.id,
                spells.SPELL_MISS_REFLECT, 0)
            return False
        return True


    async def _apply_creature_spell_effects(self, low_guid: int, spawn,
                                            ai: dict, template,
                                            target_guid: int) -> None:
        """Roll and apply a creature spell's effects on this session's units."""
        caster_guid = self._creature_object_guid(int(low_guid), ai["entry"])
        level = int(ai["level"])
        crit_chance = float(spawn["crit_chance"] or 0.0) if "crit_chance" in spawn.keys() else 0.0
        ground_started = False
        aura_patterns: set = set()

        def area_center(effect: dict):
            """Reference position for the creature's area/cone effect."""
            a = int(effect.get("target_a") or 0)
            b = int(effect.get("target_b") or 0)
            if (a in (spells.TARGET_DEST_CASTER, spells.TARGET_SRC_CASTER)
                    or b == spells.TARGET_UNIT_SRC_AREA_ENEMY):
                return self._creature_position(int(low_guid))
            if target_guid:
                if self._is_player_guid(target_guid):
                    return self._player_position()
                return self._creature_position(
                    creature.creature_low_guid(target_guid))
            return self._creature_position(int(low_guid))

        def area_hits_player(center, radius: float, kind: str) -> bool:
            player = self._player_position()
            if self._distance(center, player) > radius + 0.5:
                return False
            if kind == "cone":
                facing = float(spawn["o"] or 0.0) if "o" in spawn.keys() else 0.0
                angle = math.atan2(player[1] - center[1], player[0] - center[0])
                delta = (angle - facing + math.pi) % (2.0 * math.pi) - math.pi
                if abs(delta) > math.pi / 4.0:
                    return False
            return True

        for index, effect in enumerate(template.effects):
            etype = int(effect["effect"])
            if not etype:
                continue
            if (etype == spells.SPELL_EFFECT_PERSISTENT_AREA_AURA
                    and template.is_ground_area(index)
                    and not ground_started):
                ground_started = True
                self._start_ground_area(
                    None, template, index, level,
                    center=area_center(effect),
                    caster_guid=caster_guid,
                    hostile_to_player=True,
                )
                continue
            area_kind = template.effect_area_kind(index)
            if area_kind in ("enemy", "cone"):
                radius = template.effect_radius(index)
                if radius <= 0.0:
                    radius = 10.0
                center = area_center(effect)
                if not area_hits_player(center, radius, area_kind):
                    continue
                if etype in (spells.SPELL_EFFECT_SCHOOL_DAMAGE,
                             spells.SPELL_EFFECT_HEALTH_LEECH):
                    amount = template.effect_value(index, level)
                    if amount <= 0:
                        continue
                    lands = getattr(self, "_creature_spell_lands", None)
                    if lands is not None and not await lands(
                            template, int(self.player_guid), effect,
                            caster_guid):
                        continue
                    critical = random.uniform(0.0, 100.0) < crit_chance
                    if critical:
                        amount *= 2
                    await self._deal_spell_damage(
                        int(self.player_guid), amount, template.school or 1,
                        template, caster_guid, critical=critical)
                    if etype == spells.SPELL_EFFECT_HEALTH_LEECH:
                        await self._heal_creature(low_guid, spawn, amount,
                                                  template, caster_guid)
                elif etype in (spells.SPELL_EFFECT_WEAPON_DAMAGE,
                               spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
                               spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
                               spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE):
                    amount = max(1, int(self._creature_damage(spawn))
                                 + template.effect_value(index, level))
                    await self._send_attacker_state_update(
                        caster_guid, int(self.player_guid), amount)
                    await self._damage_player(
                        amount, f"{spawn['name']}'s {template.name}")
                elif etype == spells.SPELL_EFFECT_APPLY_AURA:
                    aura = int(effect["apply_aura"])
                    if self._effect_is_harmful(template, effect):
                        if not await self._creature_spell_lands(
                                template, int(self.player_guid), effect,
                                caster_guid):
                            continue
                    if aura == spells.SPELL_AURA_PERIODIC_DAMAGE:
                        amount = template.effect_value(index, level)
                        if amount <= 0:
                            continue
                        amplitude = max(
                            1, int(effect["amplitude"] or 2000)) // 1000
                        now = time.monotonic()
                        duration_ms = spells.spell_duration_ms(
                            self.config.database.world_path,
                            template.duration_index, level)
                        if duration_ms <= 0:
                            duration_ms = 15000
                        self._get_combat_state()["dots"].append({
                            "target_guid": int(self.player_guid),
                            "caster_guid": int(caster_guid),
                            "spell_id": int(template.id),
                            "amount": amount,
                            "school": int(template.school),
                            "interval": float(amplitude),
                            "next_tick": now + float(amplitude),
                            "expires": now + duration_ms / 1000.0,
                            "is_heal": False,
                            "leech": aura == spells.SPELL_AURA_PERIODIC_LEECH,
                        })
                    if hasattr(self, "_apply_aura_from_effect"):
                        await self._apply_aura_from_effect(
                            template, effect, index, caster_guid,
                            self.player_guid, level, target_kind="player")
                continue
            if etype in (spells.SPELL_EFFECT_SCHOOL_DAMAGE,
                         spells.SPELL_EFFECT_HEALTH_LEECH):
                if not target_guid:
                    continue
                amount = template.effect_value(index, level)
                if amount <= 0:
                    continue
                lands = getattr(self, "_creature_spell_lands", None)
                if lands is not None and not await lands(
                        template, target_guid, effect, caster_guid):
                    continue
                critical = random.uniform(0.0, 100.0) < crit_chance
                if critical:
                    amount *= 2
                await self._deal_spell_damage(
                    target_guid, amount, template.school or 1, template,
                    caster_guid, critical=critical)
                if etype == spells.SPELL_EFFECT_HEALTH_LEECH:
                    await self._heal_creature(low_guid, spawn, amount,
                                              template, caster_guid)
            elif etype in (spells.SPELL_EFFECT_WEAPON_DAMAGE,
                           spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
                           spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
                           spells.SPELL_EFFECT_ATTACK):
                if not target_guid:
                    continue
                amount = max(1, int(self._creature_damage(spawn))
                             + template.effect_value(index, level))
                await self._send_attacker_state_update(
                    caster_guid, target_guid, amount)
                if self._is_player_guid(target_guid):
                    await self._damage_player(
                        amount, f"{spawn['name']}'s {template.name}")
                else:
                    await self._deal_damage_to_creature(
                        target_guid, amount, caster_guid)
            elif etype in (spells.SPELL_EFFECT_HEAL,
                           spells.SPELL_EFFECT_HEAL_MAX_HEALTH):
                amount = template.effect_value(index, level)
                await self._heal_creature(low_guid, spawn, amount, template,
                                          caster_guid)
            elif etype == spells.SPELL_EFFECT_TRIGGER_SPELL:
                triggered = int(effect["trigger_spell"])
                if triggered:
                    await self._cast_creature_triggered(
                        low_guid, spawn, ai, triggered, target_guid)
            elif etype == spells.SPELL_EFFECT_INTERRUPT_CAST:
                if target_guid and self._is_player_guid(target_guid):
                    combat = self._get_combat_state()
                    mask = int(template.school) or 1
                    combat.setdefault("school_lockout", {})[
                        mask] = time.monotonic() + 4.0
                    if hasattr(self, "_interrupt_player_cast"):
                        await self._interrupt_player_cast(
                            SPELL_FAILED_INTERRUPTED)
            elif etype == spells.SPELL_EFFECT_INSTAKILL:
                if target_guid and self._is_player_guid(target_guid):
                    await self._damage_player(
                        1 << 24, f"{spawn['name']}'s {template.name}")
            elif etype == spells.SPELL_EFFECT_APPLY_AURA:
                aura = int(effect["apply_aura"])
                if target_guid and self._effect_is_harmful(template, effect):
                    lands = getattr(self, "_creature_spell_lands", None)
                    if lands is not None and not await lands(
                            template, target_guid, effect, caster_guid):
                        continue
                if (aura == spells.SPELL_AURA_PERIODIC_DAMAGE
                        and target_guid):
                    amount = template.effect_value(index, level)
                    if amount <= 0:
                        continue
                    amplitude = max(1, int(effect["amplitude"] or 2000)) // 1000
                    now = time.monotonic()
                    duration_ms = spells.spell_duration_ms(
                        self.config.database.world_path,
                        template.duration_index, level)
                    if duration_ms <= 0:
                        duration_ms = 15000
                    self._get_combat_state()["dots"].append({
                        "target_guid": int(target_guid),
                        "caster_guid": int(caster_guid),
                        "spell_id": int(template.id),
                        "amount": amount,
                        "school": int(template.school),
                        "interval": float(amplitude),
                        "next_tick": now + float(amplitude),
                        "expires": now + duration_ms / 1000.0,
                        "is_heal": False,
                        "leech": aura == spells.SPELL_AURA_PERIODIC_LEECH,
                    })
                if hasattr(self, "_apply_aura_from_effect"):
                    target_a = int(effect.get("target_a") or 0)
                    target_b = int(effect.get("target_b") or 0)
                    if target_a in (1, 22) or target_b in (1, 22):
                        target_kind = "creature"
                        aura_target = caster_guid
                    else:
                        target_kind = None
                        aura_target = target_guid
                    pattern = (target_a, target_b)
                    if pattern not in aura_patterns:
                        aura_patterns.add(pattern)
                        await self._apply_aura_from_effect(
                            template, effect, index, caster_guid, aura_target,
                            level, target_kind=target_kind)
