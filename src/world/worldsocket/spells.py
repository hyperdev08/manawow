"""Player spell casting, power/cooldown spend and spell effects."""
from __future__ import annotations

import math
import random
import struct
import time

from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import spells

from .constants import (
    SPELL_FAILED_BAD_TARGETS,
    SPELL_FAILED_CASTER_AURASTATE,
    SPELL_FAILED_CASTER_DEAD,
    SPELL_FAILED_INTERRUPTED,
    SPELL_FAILED_NOT_KNOWN,
    SPELL_FAILED_NOT_READY,
    SPELL_FAILED_NO_COMBO_POINTS,
    SPELL_FAILED_NO_POWER,
    SPELL_FAILED_OUT_OF_RANGE,
    SPELL_FAILED_SPELL_IN_PROGRESS,
    SPELL_FAILED_SPELL_UNAVAILABLE,
)

# ``SpellCastTargetFlags`` (3.3.5a SpellDefines.h).  ``SpellCastTargets::Read``
# consumes a packed guid for object targets, then item guids, then source/dest
# locations (each a packed transport guid plus three floats) and finally a
# string.  Ground-targeted AoE spells put the click position in the dest block.
TARGET_FLAG_UNIT = 0x00000002
TARGET_FLAG_ITEM = 0x00000010
TARGET_FLAG_SOURCE_LOCATION = 0x00000020
TARGET_FLAG_DEST_LOCATION = 0x00000040
TARGET_FLAG_CORPSE_ENEMY = 0x00000200
TARGET_FLAG_GAMEOBJECT = 0x00000800
TARGET_FLAG_TRADE_ITEM = 0x00001000
TARGET_FLAG_STRING = 0x00002000
TARGET_FLAG_CORPSE_ALLY = 0x00008000
TARGET_FLAG_UNIT_MINIPET = 0x00010000

OBJECT_TARGET_FLAGS = (TARGET_FLAG_UNIT | TARGET_FLAG_UNIT_MINIPET
                       | TARGET_FLAG_GAMEOBJECT | TARGET_FLAG_CORPSE_ENEMY
                       | TARGET_FLAG_CORPSE_ALLY)
ITEM_TARGET_FLAGS = TARGET_FLAG_ITEM | TARGET_FLAG_TRADE_ITEM

# Effect types that can be applied to every unit in an area/cone.  The direct
# damage and aura branches share the existing single-target code paths.
AREA_EFFECT_TYPES = frozenset((
    spells.SPELL_EFFECT_SCHOOL_DAMAGE,
    spells.SPELL_EFFECT_HEALTH_LEECH,
    spells.SPELL_EFFECT_WEAPON_DAMAGE,
    spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
    spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
    spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE,
    spells.SPELL_EFFECT_APPLY_AURA,
))

DIRECT_DAMAGE_TYPES = frozenset((
    spells.SPELL_EFFECT_SCHOOL_DAMAGE,
    spells.SPELL_EFFECT_HEALTH_LEECH,
    spells.SPELL_EFFECT_WEAPON_DAMAGE,
    spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
    spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
    spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE,
))

CONE_HALF_ANGLE = math.pi / 4.0        # 90-degree cone, like the 3.3.5 core
CHAIN_JUMP_RADIUS = 10.0
CHAIN_DAMAGE_FALLOFF = 0.7

class SpellsMixin:
    async def handle_cast_spell(self, data: bytes):
        """``CMSG_CAST_SPELL``: the client pressed a spell.

        Layout (``WorldPackets::Spells::CastSpell::Read`` -> ``SpellCastRequest``):
        ``uint8 castId``, ``int32 spellId``, ``uint8 sendCastFlags``,
        ``SpellTargetData`` (``uint32 flags`` + packed unit guid when one of the
        guid flags is set), and finally a missile trajectory when
        ``sendCastFlags & 0x2``.
        """
        if not self.in_world or not self.character:
            return
        if len(data) < 6:
            return
        combat = self._get_combat_state()
        cast_id = data[0]
        spell_id = struct.unpack_from("<i", data, 1)[0] & 0xFFFFFFFF
        pos = 5
        send_cast_flags = data[pos] if len(data) > pos else 0
        pos += 1
        target_guid = 0
        dest = None
        if len(data) >= pos + 4:
            target_flags = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            if target_flags & OBJECT_TARGET_FLAGS:
                target_guid, pos = self._read_packed_guid(data, pos)
            if target_flags & ITEM_TARGET_FLAGS and pos < len(data):
                _item_guid, pos = self._read_packed_guid(data, pos)
            if target_flags & TARGET_FLAG_SOURCE_LOCATION:
                _transport, pos = self._read_packed_guid(data, pos)
                if len(data) >= pos + 12:
                    pos += 12                 # source position (unused)
            if target_flags & TARGET_FLAG_DEST_LOCATION:
                _transport, pos = self._read_packed_guid(data, pos)
                if len(data) >= pos + 12:
                    dest = struct.unpack_from("<fff", data, pos)
                    pos += 12
            # A trailing target string (TARGET_FLAG_STRING) is not used by any
            # spell the server executes, so the remainder is ignored.
        _ = send_cast_flags

        if combat["dead"]:
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_CASTER_DEAD)
            return
        if hasattr(self, "_player_aura_mods"):
            mods = self._player_aura_mods()
            if mods["stunned"] or mods["silenced"]:
                await self._cast_failed(cast_id, spell_id,
                                        SPELL_FAILED_CASTER_AURASTATE)
                return
        if spell_id not in self.known_spell_ids:
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_NOT_KNOWN)
            return
        template = spells.get_spell_template(self.config.database.world_path, spell_id)
        if template is None:
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_SPELL_UNAVAILABLE)
            return
        if combat["pending_cast"]:
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_SPELL_IN_PROGRESS)
            return

        now = time.monotonic()
        if combat["cooldowns"].get(spell_id, 0.0) > now:
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_NOT_READY)
            return
        if combat["gcd_until"] > now and template.start_recovery_time != 0:
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_NOT_READY)
            return

        # A live target must be within the spell's range before anything else.
        target_low = creature.creature_low_guid(target_guid) if target_guid else 0
        spawn = self._get_creature_spawn(target_low) if target_low else None
        if target_guid and not self._is_player_guid(target_guid) and spawn is None:
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_BAD_TARGETS)
            return
        if spawn is not None:
            spell_range = template.get_range()
            if spell_range < 49000.0:
                spell_range = max(spell_range, 0.5)
                if (self._distance(self._player_position(),
                                   self._creature_position(target_low))
                        > spell_range + self._melee_reach(spawn)):
                    await self._cast_failed(cast_id, spell_id, SPELL_FAILED_OUT_OF_RANGE)
                    return
        if dest is not None:
            spell_range = template.get_range()
            if spell_range < 49000.0:
                spell_range = max(spell_range, 0.5)
                if self._distance(self._player_position(), dest) > spell_range + 0.5:
                    await self._cast_failed(cast_id, spell_id,
                                            SPELL_FAILED_OUT_OF_RANGE)
                    return

        # Finishing moves (Eviscerate, Slice and Dice, Kidney Shot, ...) need
        # the target to be the unit the rogue already has combo points on.
        # CMaNGOS ``Spell::CheckCast`` reports ``SPELL_FAILED_NO_COMBO_POINTS``
        # for every class except warriors (who reuse the same attributes for
        # Overpower's "target must be dodging" state).
        if template.needs_combo_points():
            target = int(target_guid or 0)
            if not target or target != self._combo_target():
                reason = (SPELL_FAILED_CASTER_AURASTATE
                          if int(self.character.get("class", 1)) == 1
                          else SPELL_FAILED_NO_COMBO_POINTS)
                await self._cast_failed(cast_id, spell_id, reason)
                return

        # Summon spells need a real creature entry.  Call Pet (and the tame
        # helpers) store ``0`` because the unit lives in the player's stables,
        # which this server does not model yet.
        for summon_entry, _is_pet in template.summon_entries():
            if not summon_entry:
                await self._cast_failed(cast_id, spell_id, SPELL_FAILED_BAD_TARGETS)
                return
            if creature.get_creature_template(
                    self.config.database.world_path, summon_entry) is None:
                self.log.debug(
                    f"Summon spell {spell_id} references unknown creature "
                    f"{summon_entry}")
                await self._cast_failed(cast_id, spell_id,
                                        SPELL_FAILED_SPELL_UNAVAILABLE)
                return

        cost = self._spell_power_cost(template)
        if cost > int(self.character.get("power", self._max_power())):
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_NO_POWER)
            return
        # Death knight abilities paid with runes use ``POWER_RUNE``.  A spell
        # with a ``RuneCostID`` but a different power type (Death Coil costs
        # runic power) must not touch the rune slots.
        if (self._is_death_knight()
                and int(template.power_type) == spells.POWER_RUNE
                and int(getattr(template, "rune_cost_id", 0))
                and not self._can_afford_runes(template)):
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_NO_POWER)
            return

        cast = spells.SpellCast(self.player_guid, spell_id, target_guid)
        cast.template = template
        if dest is not None:
            cast.x, cast.y, cast.z = (float(dest[0]), float(dest[1]),
                                      float(dest[2]))
            cast.has_dest = True
        cast_time_ms = template.get_casting_time_ms()
        # ``Spell::SendSpellStart`` also sets ``CAST_FLAG_NO_GCD`` for rune
        # spells ("not needed, but B sends it"), so mirror the reference.
        start_flags = spells.CAST_FLAG_SPELL_START
        if (self._is_death_knight()
                and int(template.power_type) == spells.POWER_RUNE
                and int(getattr(template, "rune_cost_id", 0))):
            start_flags |= spells.CAST_FLAG_NO_GCD

        # Only the affordability check above happens at cast start; the cost is
        # actually paid when the spell finishes, see :meth:`_take_power`.
        self._start_cooldowns(template, now)

        if cast_time_ms > 0:
            await self.send_packet(
                Opcodes.SMSG_SPELL_START,
                spells.build_spell_start(cast, self.player_guid, target_guid,
                                         cast_id, flags=start_flags),
            )
            combat["pending_cast"] = {
                "cast_id": cast_id,
                "spell_id": spell_id,
                "target_guid": target_guid,
                "template": template,
                "dest": dest,
                "finish_at": now + cast_time_ms / 1000.0,
            }
            return

        await self.send_packet(
            Opcodes.SMSG_SPELL_START,
            spells.build_spell_start(cast, self.player_guid, target_guid,
                                     cast_id, flags=start_flags),
        )
        # ``Spell::finish`` pays the cost (``TakePower``) and only then calls
        # ``SendSpellGo``, so the client's predicted power in the GO packet is the
        # value *after* the spend and there is exactly one power write per cast.
        if not await self._take_power(template):
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_NO_POWER)
            return
        # ``Spell::update`` sends GO as the cast completes and only then runs
        # ``finish()``, so the damage/heal logs follow the packet that starts the
        # spell's visuals.  Sending GO after the effects also reads a power
        # value that already includes rage gained by the cast itself.
        await self._send_spell_go(cast, template, target_guid, cast_id)
        await self._apply_spell_effects(cast, template, target_guid)
        # ``Spell::finish`` clears the combo points after the effects have run,
        # so the finisher itself still scales with the full count.
        if template.needs_combo_points():
            await self._clear_combo_points()
        self.log.info(
            f"'{self.character['name']}' casts {template.name} ({spell_id})"
        )

    async def _send_spell_go(self, cast, template, target_guid: int,
                             cast_id: int = 0) -> None:
        """Send ``SMSG_SPELL_GO`` for ``cast`` with the 3.3.5 extras filled in.

        A normal spell deliberately carries no ``CAST_FLAG_PREDICTED_POWER``.
        CMaNGOS sets that flag for every spell with a power cost and appends
        ``uint32 GetPower(powerType)`` -- the power *left* after ``TakePower``
        already ran.  We send the identical number in the ``SMSG_POWER_UPDATE``
        that ``_take_power`` emits microseconds earlier, so the field carries no
        information the client does not already have; and the 3.3.5 client does
        not treat it as an absolute value -- a Fireball that left 152 of 160 mana
        came back as an empty bar, i.e. the value was *subtracted*.  Dropping the
        field is correct under either reading, because the power-update packet
        (which the bar demonstrably handles: it is what makes rage work) already
        carries the post-cost value.

        Death knight spells with a ``RuneCostID`` are the exception: the client
        reads the rune masks *after* the predicted-power field, so that field has
        to be present for ``CAST_FLAG_PREDICTED_RUNES`` to parse.  ``POWER_RUNE``
        spells write 0 there (the core's ``GetPower(POWER_RUNE)``); runic-power
        spells such as Death Coil omit it and rely on ``SMSG_POWER_UPDATE``.
        """
        combat = self._get_combat_state()
        prediction = combat.get("rune_prediction")
        combat["rune_prediction"] = None
        if prediction:
            # CMaNGOS writes ``GetPower(POWER_RUNE)`` here, which is 0 because
            # runes are not a stored power type.  Power 5 has no visible bar,
            # but the flag has to be present because the client reads the rune
            # masks after the predicted-power field.  Spells such as Death Coil
            # also carry a RuneCostID but are paid with runic power; they get
            # the (unchanged) rune masks without a predicted-power field, so the
            # client's runic-power bar keeps the value from SMSG_POWER_UPDATE.
            rune_power = (0 if int(template.power_type) == spells.POWER_RUNE
                          else None)
            await self.send_packet(
                Opcodes.SMSG_SPELL_GO,
                spells.build_spell_go(
                    cast, self.player_guid, target_guid, cast_id,
                    timestamp_ms=self._server_tick_ms(),
                    predicted_power=rune_power,
                    off_gcd=True,
                    rune_state_before=prediction["before"],
                    rune_state_after=prediction["after"],
                    rune_progress=prediction["progress"],
                ),
            )
            return
        await self.send_packet(
            Opcodes.SMSG_SPELL_GO,
            spells.build_spell_go(
                cast, self.player_guid, target_guid, cast_id,
                timestamp_ms=self._server_tick_ms(),
                off_gcd=template.start_recovery_time == 0,
            ),
        )

    async def _cast_failed(self, cast_id: int, spell_id: int, reason: int) -> None:
        """Report a rejected cast; the client shows the matching error text."""
        await self.send_packet(
            Opcodes.SMSG_CAST_FAILED,
            spells.build_cast_failed(cast_id, spell_id, reason),
        )
        self.log.debug(
            f"Cast of spell {spell_id} refused (reason {reason})"
        )

    def _note_power_spend(self, template) -> None:
        """Start the five-second mana-regen interruption after a completed cast.

        ``Player::IsUnderLastManaUseEffect`` only cares about mana spends; rage
        and energy abilities never suppress mana regeneration.  A spell with
        ``SPELL_ATTR_EX2_DONT_BLOCK_MANA_REGEN`` (a few channeled/instant
        utility spells) also leaves the five-second timer alone.
        """
        if int(template.power_type) != spells.POWER_MANA:
            return
        if (int(getattr(template, "attributes_ex2", 0))
                & spells.SPELL_ATTR_EX2_DONT_BLOCK_MANA_REGEN):
            return
        self._get_combat_state()["last_power_use"] = time.monotonic()

    def _spell_power_cost(self, template) -> int:
        """Cost in the same units ``UNIT_FIELD_POWER1`` is stored in.

        Rage spells are the interesting ones: ``Spell.dbc`` already stores the
        rage cost in tenths (Heroic Strike is ``150`` for 15 rage) and we keep
        rage in tenths too, so the DBC value is used as-is.  Dividing it by ten
        was the old rage-in-points convention and made the client and the
        server disagree about what the ability costs.

        A percentage cost follows ``Spell::CalculatePowerCost`` exactly: the flat
        ``ManaCost`` and the percentage *add up* (the core uses ``+=`` for both),
        mana percentages are taken from :meth:`_create_mana` (``GetCreateMana``)
        and rage/focus/energy/happiness percentages from ``GetMaxPower``.  The
        client runs the same arithmetic to predict the cost, so anything else
        here desynchronizes its power bar.
        """
        power_type = int(template.power_type)
        # ``SPELL_ATTR_EX_USE_ALL_MANA`` (Lay on Hands) drains the whole pool
        # instead of a fixed cost.
        if int(getattr(template, "attributes_ex", 0)) & spells.SPELL_ATTR_EX_USE_ALL_MANA:
            if power_type == spells.POWER_HEALTH:
                return int(self.character.get("health", self._max_health()))
            if power_type == spells.POWER_MANA:
                return int(self.character.get("power", self._max_power()))
            return 0
        cost = int(template.mana_cost)
        # ``CalculatePowerCost``: ``manaCostPerlevel * (GetSpellRank / 5 -
        # baseLevel)``.  A player's spell skill is (almost always) level * 5,
        # so ``GetSpellRank / 5`` is the character level clamped to ``maxLevel``.
        per_level = int(getattr(template, "mana_cost_per_level", 0) or 0)
        if per_level:
            rank = max(1, int(self.character.get("level", 1)))
            max_level = int(getattr(template, "max_level", 0) or 0)
            if max_level > 0:
                rank = min(rank, max_level)
            cost += per_level * (rank - int(template.base_level or 0))
        percentage = int(template.mana_cost_percentage or 0)
        if percentage:
            if power_type == spells.POWER_MANA:
                basis = self._create_mana()
            elif power_type in (spells.POWER_RAGE, spells.POWER_FOCUS,
                                spells.POWER_ENERGY, spells.POWER_HAPPINESS):
                basis = self._max_power()
            else:
                basis = 0
            cost += int(basis * percentage / 100.0)
        return max(0, cost)

    async def _take_power(self, template) -> bool:
        """``Spell::TakePower``: pay for a spell that is just finishing.

        The real core spends power in ``Spell::finish``, right before
        ``SendSpellGo``, never when the button was pressed.  Spending at cast
        *start* meant the client got a power write, then watched the value crawl
        back up from regeneration during the cast, then got a
        ``SMSG_SPELL_GO`` that re-predicted a third number -- which is what made
        the mana bar swing around the real cost instead of dropping by it once.
        One write per cast, immediately before ``SMSG_SPELL_GO`` carries the
        same number, keeps the client's prediction and ours identical.

        Returns ``False`` when the player cannot afford it (a cast that started
        while the cost was affordable can be made unaffordable by a level-up
        changing the base pool, so this is re-checked here like the core does).
        """
        # ``Spell::TakePower`` delegates to ``CheckOrTakeRunePower`` only for
        # ``POWER_RUNE`` spells.  Death Coil also carries a RuneCostID but is
        # paid with runic power through the normal power path.
        if (self._is_death_knight()
                and int(template.power_type) == spells.POWER_RUNE
                and int(getattr(template, "rune_cost_id", 0))):
            return await self._take_runes(template)
        cost = self._spell_power_cost(template)
        if cost <= 0:
            self._note_rune_prediction(template)
            return True
        power = int(self.character.get("power", self._max_power()))
        if power < cost:
            return False
        await self._set_player_power(power - cost)
        self._note_power_spend(template)
        self._note_rune_prediction(template)
        self.log.info(
            f"'{self.character['name']}' spends {cost} for {template.name} "
            f"({template.id}): {power} -> {self.character['power']} "
            f"of {self._max_power()}"
        )
        return True

    def _start_cooldowns(self, template, now: float) -> None:
        """Record the spell's own cooldown and the global cooldown.

        ``StartRecoveryTime`` *is* the GCD in 3.3.5: 1500 for a normal spell and
        0 for spells that are off the global cooldown (Heroic Strike, Battle
        Shout and the rest of the warrior instant toolkit).  The DBC never
        stores an explicit value for "use the default", so a zero has to mean
        off-GCD rather than "apply 1.5 s".
        """
        combat = self._get_combat_state()
        cooldown_ms = max(int(template.recovery_time),
                          int(template.category_recovery))
        if cooldown_ms > 0:
            combat["cooldowns"][template.id] = now + cooldown_ms / 1000.0
        gcd_ms = int(template.start_recovery_time)
        if gcd_ms > 0:
            combat["gcd_until"] = now + gcd_ms / 1000.0

    async def _apply_spell_effects(self, cast, template, target_guid: int) -> None:
        """Roll and apply every effect of a completed cast."""
        caster_level = int(self.character.get("level", 1))
        player_guid = int(self.player_guid)
        # ``WorldObject::CalculateSpellEffectValue`` adds the per-combo-point
        # scaling only while the spell's target is the caster's combo target.
        # Snapshot the count here so a target that dies to the first effect
        # cannot zero the scaling of the remaining effects of the same finisher.
        combo_points = 0
        if target_guid and int(target_guid) == self._combo_target():
            combo_points = self._combo_points()
        ground_started = False
        aura_patterns: set = set()
        for index, effect in enumerate(template.effects):
            etype = int(effect["effect"])
            if not etype:
                continue
            # Persistent ground auras (Blizzard, Consecration, Death and Decay,
            # ...) are created once per cast; their companion
            # PERIODIC_TRIGGER_SPELL effect supplies the tick damage.
            if etype == spells.SPELL_EFFECT_PERSISTENT_AREA_AURA:
                if template.is_ground_area(index) and not ground_started:
                    ground_started = True
                    self._start_ground_area(cast, template, index, caster_level)
                continue
            area_kind = template.effect_area_kind(index)
            if area_kind is not None and etype in AREA_EFFECT_TYPES:
                await self._apply_area_effect(
                    cast, template, index, effect, area_kind, target_guid,
                    combo_points, caster_level)
                continue
            chain_count = int(effect.get("chain_targets") or 0)
            if (chain_count > 1 and target_guid
                    and etype in DIRECT_DAMAGE_TYPES):
                await self._apply_chain_effect(
                    cast, template, index, effect, target_guid,
                    combo_points, caster_level, chain_count)
                continue
            if etype == spells.SPELL_EFFECT_SCHOOL_DAMAGE:
                if not target_guid:
                    continue
                amount = template.effect_value(index, caster_level, combo_points)
                if amount <= 0:
                    continue
                await self._deal_damage_to_target(
                    target_guid, amount, template.school, template.id, player_guid
                )
            elif etype in (spells.SPELL_EFFECT_WEAPON_DAMAGE,
                           spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
                           spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
                           spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE):
                if not target_guid:
                    continue
                amount = self._direct_effect_damage(
                    etype, template, index, effect, caster_level, combo_points)
                await self._deal_damage_to_target(
                    target_guid, amount, 1, template.id, player_guid
                )
            elif etype == spells.SPELL_EFFECT_ATTACK:
                if not target_guid:
                    continue
                weapon = self._player_weapon_stats()
                amount = int(random.uniform(weapon["min"], weapon["max"]))
                amount += max(1, int(caster_level * 0.5))
                await self._send_attacker_state_update(
                    player_guid, target_guid, max(1, amount)
                )
                await self._deal_damage_to_target(
                    target_guid, max(1, amount), 1, template.id, player_guid
                )
            elif etype in (spells.SPELL_EFFECT_HEAL,
                           spells.SPELL_EFFECT_HEAL_MAX_HEALTH):
                amount = template.effect_value(index, caster_level, combo_points)
                if amount <= 0:
                    continue
                healed, overheal = await self._heal_player(amount)
                await self._send_spell_heal_log(
                    target_guid or player_guid, player_guid, template.id,
                    healed, overheal,
                )
            elif etype == spells.SPELL_EFFECT_HEALTH_LEECH:
                if not target_guid:
                    continue
                amount = template.effect_value(index, caster_level, combo_points)
                if amount <= 0:
                    continue
                await self._deal_damage_to_target(
                    target_guid, amount, template.school, template.id, player_guid
                )
                healed, _ = await self._heal_player(amount)
                await self._send_spell_heal_log(
                    player_guid, player_guid, template.id, healed, 0
                )
            elif etype == spells.SPELL_EFFECT_INSTAKILL:
                if target_guid:
                    await self._deal_damage_to_target(
                        target_guid, 1 << 24, template.school, template.id,
                        player_guid,
                    )
            elif etype == spells.SPELL_EFFECT_TRIGGER_SPELL:
                triggered = int(effect["trigger_spell"])
                if triggered:
                    await self._cast_triggered(triggered, target_guid)
            elif etype in (spells.SPELL_EFFECT_SUMMON,
                           spells.SPELL_EFFECT_SUMMON_PET):
                entry = int(effect.get("misc_value") or 0)
                if entry:
                    duration = spells.summon_duration_ms(
                        self.config.database.world_path,
                        template.duration_index,
                        caster_level,
                    )
                    await self._summon_creature(
                        entry, duration,
                        is_pet=etype == spells.SPELL_EFFECT_SUMMON_PET,
                        stationary=template.is_totem(),
                    )
            elif etype in (spells.SPELL_EFFECT_SUMMON_OBJECT_WILD,
                           spells.SPELL_EFFECT_SUMMON_OBJECT_SLOT1):
                # Gameobjects (traps, ritual circles, chests) are not part of
                # this server's object model.
                self.log.debug(
                    f"Spell {template.id} summons gameobject "
                    f"{int(effect.get('misc_value') or 0)}; ignored"
                )
            elif etype == spells.SPELL_EFFECT_ADD_COMBO_POINTS:
                # ``Spell::EffectAddComboPoints``: the effect's own rolled value
                # is the number of points added (Sinister Strike adds 1).
                if not target_guid:
                    continue
                points = template.effect_value(index, caster_level)
                if points > 0:
                    await self._add_combo_points(target_guid, points)
            elif etype == spells.SPELL_EFFECT_APPLY_AURA:
                self._apply_periodic_aura(template, effect, index, caster_level,
                                          target_guid, combo_points)
                if hasattr(self, "_apply_aura_from_effect"):
                    target_a = int(effect.get("target_a") or 0)
                    target_b = int(effect.get("target_b") or 0)
                    if (not target_guid or target_a in (1, 22)
                            or target_b in (1, 22)):
                        target_kind = "player"
                        aura_target = int(self.player_guid)
                    else:
                        target_kind = None
                        aura_target = target_guid
                    pattern = (target_a, target_b)
                    if pattern not in aura_patterns:
                        aura_patterns.add(pattern)
                        await self._apply_aura_from_effect(
                            template, effect, index, player_guid, aura_target,
                            caster_level, combo_points, target_kind=target_kind)

    # ------------------------------------------------------------------
    # Area of effect (AoE)
    # ------------------------------------------------------------------
    def _direct_effect_damage(self, etype: int, template, index: int,
                              effect: dict, caster_level: int,
                              combo_points: int = 0) -> int:
        """Damage one target of a direct-damage effect would take."""
        if etype in (spells.SPELL_EFFECT_SCHOOL_DAMAGE,
                     spells.SPELL_EFFECT_HEALTH_LEECH):
            return max(0, template.effect_value(index, caster_level,
                                                combo_points))
        weapon = self._player_weapon_stats()
        roll = random.uniform(weapon["min"], weapon["max"])
        if etype == spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE:
            percent = (int(effect.get("base_points") or 0) + 1) / 100.0
            return max(1, int(roll * percent))
        amount = int(roll)
        amount += template.effect_value(index, caster_level, combo_points)
        amount += max(1, int(caster_level * 0.5))
        return max(1, amount)

    def _area_center(self, template, index: int, cast, target_guid: int):
        """Reference position for an area/cone effect."""
        effect = template.get_effect(index)
        a = int(effect.get("target_a") or 0) if effect else 0
        b = int(effect.get("target_b") or 0) if effect else 0
        # Caster-centred spells (Arcane Explosion 22/15, Consecration 18/16,
        # Whirlwind 22/15) never use the click location.
        if (a in (spells.TARGET_DEST_CASTER, spells.TARGET_SRC_CASTER)
                or b == spells.TARGET_UNIT_SRC_AREA_ENEMY):
            return self._player_position()
        if cast is not None and getattr(cast, "has_dest", False):
            return (float(cast.x), float(cast.y), float(cast.z))
        if (target_guid and not self._is_player_guid(target_guid)):
            low = creature.creature_low_guid(target_guid)
            if self._get_creature_spawn(low) is not None:
                return self._creature_position(low)
        return self._player_position()

    def _select_area_targets(self, center, radius: float, kind: str,
                             max_targets: int = 0):
        """Return ``[(low_guid, spawn), ...]`` inside an area or cone."""
        facing = float(self.character.get("o", 0.0) or 0.0)
        gathered = []
        for low_guid, spawn in list(self._creature_spawn_cache.items()):
            if int(low_guid) in getattr(self, "_summoned_creatures", {}):
                continue
            if spawn is None or int(spawn["dead_since"] or 0) > 0:
                continue
            pos = self._creature_position(int(low_guid))
            distance = self._distance(center, pos)
            if radius > 0.0 and distance > radius + 0.5:
                continue
            if kind == "cone":
                angle = math.atan2(pos[1] - center[1], pos[0] - center[0])
                delta = (angle - facing + math.pi) % (2.0 * math.pi) - math.pi
                if abs(delta) > CONE_HALF_ANGLE:
                    continue
            gathered.append((distance, int(low_guid), spawn))
        gathered.sort(key=lambda item: item[0])
        if max_targets and max_targets > 0:
            gathered = gathered[:max_targets]
        return [(low_guid, spawn) for _distance, low_guid, spawn in gathered]

    async def _apply_area_effect(self, cast, template, index: int, effect: dict,
                                 kind: str, target_guid: int,
                                 combo_points: int, caster_level: int) -> None:
        """Apply one direct/aura effect to every unit in its area or cone."""
        etype = int(effect["effect"])
        player_guid = int(self.player_guid)
        if kind == "ally":
            # Party/raid membership is not modelled; the caster is always in
            # their own area effect, so at least self-heals/buffs work.
            if etype in (spells.SPELL_EFFECT_HEAL,
                         spells.SPELL_EFFECT_HEAL_MAX_HEALTH):
                amount = template.effect_value(index, caster_level, combo_points)
                if amount > 0:
                    healed, overheal = await self._heal_player(amount)
                    await self._send_spell_heal_log(
                        player_guid, player_guid, template.id, healed, overheal)
            elif etype == spells.SPELL_EFFECT_APPLY_AURA:
                self._apply_periodic_aura(template, effect, index, caster_level,
                                          player_guid, combo_points)
                if hasattr(self, "_apply_aura_from_effect"):
                    await self._apply_aura_from_effect(
                        template, effect, index, player_guid, player_guid,
                        caster_level, combo_points, target_kind="player")
            return
        radius = template.effect_radius(index)
        if radius <= 0.0:
            radius = 10.0
        center = self._area_center(template, index, cast, target_guid)
        targets = self._select_area_targets(center, radius, kind,
                                            template.max_targets)
        if not targets:
            return
        amount = 0
        if etype in DIRECT_DAMAGE_TYPES:
            amount = self._direct_effect_damage(
                etype, template, index, effect, caster_level, combo_points)
        for low_guid, spawn in targets:
            guid = self._creature_object_guid(low_guid, int(spawn["entry"]))
            if etype in (spells.SPELL_EFFECT_SCHOOL_DAMAGE,
                         spells.SPELL_EFFECT_WEAPON_DAMAGE,
                         spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
                         spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
                         spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE):
                if amount <= 0:
                    continue
                await self._deal_damage_to_target(
                    guid, amount, template.school or 1, template.id, player_guid)
            elif etype == spells.SPELL_EFFECT_HEALTH_LEECH:
                if amount <= 0:
                    continue
                await self._deal_damage_to_target(
                    guid, amount, template.school or 1, template.id, player_guid)
                healed, _ = await self._heal_player(amount)
                await self._send_spell_heal_log(
                    player_guid, player_guid, template.id, healed, 0)
            elif etype == spells.SPELL_EFFECT_APPLY_AURA:
                self._apply_periodic_aura(template, effect, index, caster_level,
                                          guid, combo_points)
                if hasattr(self, "_apply_aura_from_effect"):
                    await self._apply_aura_from_effect(
                        template, effect, index, player_guid, guid,
                        caster_level, combo_points, target_kind="creature")

    async def _apply_chain_effect(self, cast, template, index: int,
                                  effect: dict, target_guid: int,
                                  combo_points: int, caster_level: int,
                                  chain_count: int) -> None:
        """Chain Lightning/Multi-Shot: primary target plus nearby jumps."""
        etype = int(effect["effect"])
        player_guid = int(self.player_guid)
        if etype not in (spells.SPELL_EFFECT_SCHOOL_DAMAGE,
                         spells.SPELL_EFFECT_WEAPON_DAMAGE,
                         spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
                         spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
                         spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE):
            return
        base = self._direct_effect_damage(
            etype, template, index, effect, caster_level, combo_points)
        hit: list[int] = []
        current = int(target_guid)
        for jump in range(max(1, chain_count)):
            if not current:
                break
            low = creature.creature_low_guid(current)
            if low in getattr(self, "_summoned_creatures", {}):
                break
            spawn = self._get_creature_spawn(low)
            if spawn is None or int(spawn["dead_since"] or 0) > 0:
                break
            amount = max(1, int(base * (CHAIN_DAMAGE_FALLOFF ** jump)))
            await self._deal_damage_to_target(
                current, amount, template.school or 1, template.id, player_guid)
            hit.append(current)
            # Find the closest not-yet-hit enemy near the last jump target.
            here = self._creature_position(low)
            candidates = []
            for other_low, other in list(self._creature_spawn_cache.items()):
                if other is None or int(other["dead_since"] or 0) > 0:
                    continue
                other_guid = self._creature_object_guid(
                    int(other_low), int(other["entry"]))
                if other_guid in hit:
                    continue
                if int(other_low) in getattr(self, "_summoned_creatures", {}):
                    continue
                distance = self._distance(
                    here, self._creature_position(int(other_low)))
                if distance <= CHAIN_JUMP_RADIUS:
                    candidates.append((distance, other_guid))
            candidates.sort(key=lambda item: item[0])
            current = candidates[0][1] if candidates else 0

    def _start_ground_area(self, cast, template, index: int,
                           caster_level: int, center=None,
                           caster_guid: int | None = None,
                           hostile_to_player: bool = False) -> None:
        """Create a persistent ground effect (Blizzard, Consecration, ...)."""
        effect = template.get_effect(index)
        if effect is None:
            return
        radius = template.effect_radius(index)
        if radius <= 0.0:
            radius = 10.0
        if center is None:
            center = self._area_center(template, index, cast, 0)
        duration_ms = spells.spell_duration_ms(
            self.config.database.world_path, template.duration_index,
            caster_level)
        if duration_ms <= 0:
            duration_ms = 8000
        interval = max(250, int(effect.get("amplitude") or 1000)) / 1000.0
        amount = template.effect_value(index, caster_level)
        school = int(template.school) or 1
        aura = int(effect.get("apply_aura") or 0)
        # Blizzard/Rain of Fire/Volley/Hurricane keep the damage in a companion
        # PERIODIC_TRIGGER_SPELL effect; the persistent aura itself is a dummy.
        for other in template.effects:
            if int(other.get("apply_aura") or 0) != spells.SPELL_AURA_PERIODIC_TRIGGER_SPELL:
                continue
            trigger_id = int(other.get("trigger_spell") or 0)
            if not trigger_id:
                continue
            triggered = spells.get_spell_template(
                self.config.database.world_path, trigger_id)
            if triggered is None:
                break
            for trigger_index, trigger_effect in enumerate(triggered.effects):
                if int(trigger_effect.get("effect") or 0) == \
                        spells.SPELL_EFFECT_SCHOOL_DAMAGE:
                    amount = triggered.effect_value(trigger_index, caster_level)
                    school = int(triggered.school) or school
                    break
            interval = max(250, int(other.get("amplitude") or 1000)) / 1000.0
            break
        if aura in (spells.SPELL_AURA_PERIODIC_DAMAGE,
                    spells.SPELL_AURA_PERIODIC_DUMMY):
            interval = max(250, int(effect.get("amplitude") or 1000)) / 1000.0
        if amount <= 0:
            return
        now = time.monotonic()
        combat = self._get_combat_state()
        effects = combat.setdefault("area_effects", [])
        effects.append({
            "spell_id": int(template.id),
            "caster_guid": int(caster_guid if caster_guid is not None
                               else self.player_guid),
            "center": (float(center[0]), float(center[1]), float(center[2])),
            "radius": float(radius),
            "school": int(school),
            "amount": max(1, int(amount)),
            "interval": float(interval),
            "next_tick": now + float(interval),
            "expires": now + duration_ms / 1000.0,
            "max_targets": int(template.max_targets or 0),
            "hostile_to_player": bool(hostile_to_player),
        })
        self.log.debug(
            f"Ground AoE {template.name} ({template.id}) at {center} "
            f"r={radius:.1f} for {duration_ms} ms"
        )

    async def _tick_area_effects(self, now: float) -> None:
        """Tick persistent ground effects (Blizzard, Consecration, ...)."""
        combat = self._get_combat_state()
        effects = combat.get("area_effects")
        if not effects:
            return
        alive = []
        for area in effects:
            if area["expires"] <= now:
                continue
            if area["next_tick"] <= now:
                area["next_tick"] = now + area["interval"]
                targets = self._select_area_targets(
                    area["center"], area["radius"], "enemy",
                    area.get("max_targets") or 0)
                for low_guid, spawn in targets:
                    guid = self._creature_object_guid(
                        low_guid, int(spawn["entry"]))
                    await self._deal_damage_to_target(
                        guid, area["amount"], area["school"],
                        area["spell_id"], area["caster_guid"])
                if (area.get("hostile_to_player")
                        and self._distance(area["center"],
                                           self._player_position())
                        <= area["radius"] + 0.5):
                    await self._damage_player(
                        area["amount"], f"spell {area['spell_id']}")
                    await self._send_spell_damage_log(
                        int(self.player_guid), int(area["caster_guid"]),
                        int(area["spell_id"]), int(area["amount"]),
                        int(area["school"]) or 1, periodic=1)
            alive.append(area)
        combat["area_effects"] = alive

    async def _cast_triggered(self, spell_id: int, target_guid: int) -> None:
        """Cast a triggered spell immediately, ignoring costs and cooldowns."""
        template = spells.get_spell_template(self.config.database.world_path,
                                             spell_id)
        if template is None:
            return
        cast = spells.SpellCast(self.player_guid, spell_id, target_guid)
        cast.template = template
        await self.send_packet(
            Opcodes.SMSG_SPELL_START,
            spells.build_spell_start(cast, self.player_guid, target_guid, 0),
        )
        await self._send_spell_go(cast, template, target_guid, 0)
        await self._apply_spell_effects(cast, template, target_guid)

    def _apply_periodic_aura(self, template, effect, index: int,
                             caster_level: int, target_guid: int,
                             combo_points: int = 0) -> None:
        """Register a damage/heal/energize over-time effect on the target."""
        aura = int(effect["apply_aura"])
        if aura not in (spells.SPELL_AURA_PERIODIC_DAMAGE,
                        spells.SPELL_AURA_PERIODIC_HEAL,
                        spells.SPELL_AURA_PERIODIC_LEECH,
                        spells.SPELL_AURA_PERIODIC_ENERGIZE):
            return
        if not target_guid:
            return
        amplitude = max(250, int(effect["amplitude"] or 2000)) / 1000.0
        amount = template.effect_value(index, caster_level, combo_points)
        if amount <= 0:
            return
        duration_ms = spells.spell_duration_ms(
            self.config.database.world_path, template.duration_index,
            caster_level)
        if duration_ms <= 0:
            duration_ms = 15000
        now = time.monotonic()
        combat = self._get_combat_state()
        # Re-casting the same periodic aura refreshes it instead of stacking a
        # second copy on the target.
        for dot in combat["dots"]:
            if (dot["spell_id"] == int(template.id)
                    and int(dot["target_guid"]) == int(target_guid)
                    and int(dot.get("caster_guid") or self.player_guid)
                    == int(self.player_guid)):
                dot["amount"] = amount
                dot["interval"] = float(amplitude)
                dot["next_tick"] = now + float(amplitude)
                dot["expires"] = now + duration_ms / 1000.0
                dot["school"] = int(template.school)
                dot["is_heal"] = aura == spells.SPELL_AURA_PERIODIC_HEAL
                dot["energize"] = (int(effect.get("misc_value") or 0)
                                   if aura == spells.SPELL_AURA_PERIODIC_ENERGIZE
                                   else None)
                return
        combat["dots"].append({
            "target_guid": int(target_guid),
            "caster_guid": int(self.player_guid),
            "spell_id": int(template.id),
            "amount": amount,
            "school": int(template.school),
            "interval": float(amplitude),
            "next_tick": now + float(amplitude),
            "expires": now + duration_ms / 1000.0,
            "is_heal": aura == spells.SPELL_AURA_PERIODIC_HEAL,
            "energize": (int(effect.get("misc_value") or 0)
                         if aura == spells.SPELL_AURA_PERIODIC_ENERGIZE
                         else None),
        })

    async def _deal_damage_to_target(self, target_guid: int, damage: int,
                                     school: int, spell_id: int,
                                     caster_guid: int) -> None:
        """Damage a creature (or the player) and write the combat-log packet."""
        if self._is_player_guid(target_guid):
            await self._damage_player(damage, f"spell {spell_id}")
            return
        died = await self._deal_damage_to_creature(target_guid, damage, caster_guid)
        await self._send_spell_damage_log(
            target_guid, caster_guid, spell_id, damage, school or 1
        )

    async def _apply_pending_cast(self, pending: dict) -> None:
        """Finish a cast whose casting time has elapsed."""
        template = pending["template"]
        target_guid = pending["target_guid"]
        cast = spells.SpellCast(self.player_guid, pending["spell_id"], target_guid)
        cast.template = template
        dest = pending.get("dest")
        if dest is not None:
            cast.x, cast.y, cast.z = (float(dest[0]), float(dest[1]),
                                      float(dest[2]))
            cast.has_dest = True
        # ``Spell::update`` -> ``finish``: pay for the spell, then tell the
        # client it went off.  Both the ``SMSG_POWER_UPDATE`` from the spend and
        # the predicted power inside ``SMSG_SPELL_GO`` therefore carry the same
        # post-cost number, which is what keeps the client's bar from jumping.
        # The five-second regen rule starts here too (patch 1.4.0), not at cast
        # start, and a spell that cannot be paid for any more simply does not go
        # off (the client still has to be told, hence the GO we skip).
        if not await self._take_power(template):
            await self._cast_failed(pending["cast_id"], template.id,
                                    SPELL_FAILED_NO_POWER)
            self.log.info(
                f"'{self.character['name']}' could not pay for "
                f"{template.name} ({pending['spell_id']})"
            )
            return
        await self._send_spell_go(cast, template, target_guid,
                                  pending["cast_id"])
        await self._apply_spell_effects(cast, template, target_guid)
        if template.needs_combo_points():
            await self._clear_combo_points()
        self.log.info(
            f"'{self.character['name']}' finishes {template.name} "
            f"({pending['spell_id']})"
        )

    async def _apply_dot_tick(self, dot: dict) -> None:
        """One periodic tick."""
        amount = int(dot["amount"])
        target_guid = int(dot["target_guid"])
        # Player casts tag the dot with the player; creature spells carry their
        # own caster guid so the combat log names the right source.
        caster_guid = int(dot.get("caster_guid") or self.player_guid)
        if dot.get("energize") is not None:
            power_type = int(dot["energize"])
            if self._is_player_guid(target_guid) \
                    and power_type == self._power_type():
                current = int(self.character.get("power", 0))
                await self._set_player_power(
                    min(self._max_power(), current + amount))
            return
        if dot["is_heal"]:
            if self._is_player_guid(target_guid):
                healed, _ = await self._heal_player(amount)
                await self._send_spell_heal_log(
                    target_guid, caster_guid, dot["spell_id"], healed, 0,
                )
            else:
                low = creature.creature_low_guid(target_guid)
                spawn = self._get_creature_spawn(low)
                if spawn is not None:
                    current = self._creature_health(low)
                    maximum = self._creature_max_health(spawn)
                    healed = min(maximum - current, amount)
                    if healed > 0:
                        self._set_creature_health(low, current + healed)
                        await self._send_creature_fields(low, {
                            F.UNIT_FIELD_HEALTH: current + healed,
                            F.UNIT_FIELD_MAXHEALTH: maximum,
                        })
                    await self._send_spell_heal_log(
                        target_guid, caster_guid, dot["spell_id"],
                        max(0, healed), max(0, amount - healed))
            return
        if self._is_player_guid(target_guid):
            await self._damage_player(amount, f"periodic {dot['spell_id']}")
            await self._send_spell_damage_log(
                target_guid, caster_guid, dot["spell_id"], amount,
                dot["school"] or 1, periodic=1,
            )
            return
        died = await self._deal_damage_to_creature(target_guid, amount,
                                                   caster_guid)
        await self._send_spell_damage_log(
            target_guid, caster_guid, dot["spell_id"], amount,
            dot["school"] or 1, periodic=1,
        )
        if died:
            dot["expires"] = 0.0

    async def handle_cancel_cast(self, data: bytes):
        """``CMSG_CANCEL_CAST``: interrupt the spell being cast."""
        if len(data) < 4:
            return
        spell_id = struct.unpack_from("<I", data, 0)[0]
        combat = self._get_combat_state()
        pending = combat["pending_cast"]
        if pending and pending["spell_id"] == spell_id:
            await self.send_packet(
                Opcodes.SMSG_SPELL_FAILURE,
                spells.build_spell_failure(self.player_guid, pending["cast_id"],
                                           spell_id, SPELL_FAILED_INTERRUPTED),
            )
            combat["pending_cast"] = None
            self.log.debug(f"Cancelled cast of spell {spell_id}")

    async def handle_set_selection(self, data: bytes):
        """``CMSG_SET_SELECTION``: the client selected a target."""
        if len(data) >= 8:
            guid = struct.unpack_from("<Q", data, 0)[0]
            combat = self._get_combat_state()
            combat["selected_guid"] = guid

    async def _send_spell_damage_log(self, victim_guid: int, caster_guid: int,
                                     spell_id: int, damage: int, school: int,
                                     periodic: int = 0, critical: int = 0) -> None:
        """Send ``SMSG_SPELLNONMELEEDAMAGELOG`` (overkill/absorb/resist = 0)."""
        out = bytearray()
        out += spells.pack_guid(int(victim_guid))
        out += spells.pack_guid(int(caster_guid))
        out += struct.pack("<ii", int(spell_id) & 0xFFFFFFFF, int(damage))
        out += struct.pack("<i", 0)                  # overkill
        out += struct.pack("<B", school & 0xFF)      # school mask
        out += struct.pack("<I", 0)                  # absorbed
        out += struct.pack("<I", 0)                  # resisted
        out += struct.pack("<B", periodic & 0xFF)
        out += struct.pack("<B", 0)                  # unused
        out += struct.pack("<I", 0)                  # shield block
        out += struct.pack("<I", 2 if critical else 0)  # SPELL_HIT_TYPE_CRIT
        out += struct.pack("<B", 0)                  # no debug info
        await self.send_packet(Opcodes.SMSG_SPELLNONMELEEDAMAGELOG, bytes(out))

    async def _send_spell_heal_log(self, target_guid: int, healer_guid: int,
                                   spell_id: int, heal: int, overheal: int,
                                   critical: bool = False) -> None:
        """Send ``SMSG_SPELLHEALLOG`` for a direct heal."""
        out = bytearray()
        out += spells.pack_guid(int(target_guid))
        out += spells.pack_guid(int(healer_guid))
        out += struct.pack("<I", int(spell_id) & 0xFFFFFFFF)
        out += struct.pack("<I", int(heal) & 0xFFFFFFFF)
        out += struct.pack("<I", int(overheal) & 0xFFFFFFFF)
        out += struct.pack("<I", 0)                  # absorbed
        out += struct.pack("<B", 1 if critical else 0)
        out += struct.pack("<B", 0)                  # unused
        await self.send_packet(Opcodes.SMSG_SPELLHEALLOG, bytes(out))

    async def _send_attacker_state_update(self, attacker_guid: int, victim_guid: int,
                                          damage: int, is_miss: bool = False,
                                          is_crit: bool = False,
                                          school: int = 1) -> None:
        """Send ``SMSG_ATTACKERSTATEUPDATE`` for one melee hit.

        Layout from CMaNGOS 3.3.5 ``Unit::SendAttackStateUpdate``: ``uint32
        flags``, packed attacker, packed victim, ``uint32 damage``, ``uint32
        overdamage``, ``uint8 subcount``, then per sub-damage ``uint32
        schoolMask, float fDamage, uint32 damage``, ``uint8 victimState``,
        ``uint32 attackerState``, ``uint32 meleeSpellId``.

        ``overdamage`` is ``max(0, damage - victim_health)`` measured *before*
        the damage is applied -- the newer core's ``-1`` sentinel is a post-3.3.5
        convention and the 3.3.5 client renders it as a nonsense overkill value.
        """
        flags = 0
        victim_state = 0
        if not is_miss:
            flags |= 0x00000002          # HITINFO_AFFECTS_VICTIM
            victim_state = 1             # VICTIMSTATE_HIT
            if is_crit:
                flags |= 0x00000200      # HITINFO_CRITICALHIT
        else:
            flags |= 0x00000010          # HITINFO_MISS

        overkill = max(0, int(damage) - self._victim_health(victim_guid)) if not is_miss else 0

        out = bytearray()
        out += struct.pack("<I", flags)
        out += spells.pack_guid(int(attacker_guid))
        out += spells.pack_guid(int(victim_guid))
        out += struct.pack("<I", damage & 0xFFFFFFFF)
        out += struct.pack("<I", overkill & 0xFFFFFFFF)
        out += struct.pack("<B", 1)      # one sub-damage entry
        out += struct.pack("<I", school & 0xFF)
        out += struct.pack("<f", float(damage))
        out += struct.pack("<I", damage & 0xFFFFFFFF)
        out += struct.pack("<B", victim_state)
        out += struct.pack("<I", 0)      # attacker state
        out += struct.pack("<I", 0)      # melee spell id
        await self.send_packet(Opcodes.SMSG_ATTACKERSTATEUPDATE, bytes(out))
