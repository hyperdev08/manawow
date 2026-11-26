"""Player spell casting, power/cooldown spend and spell effects."""
from __future__ import annotations

import math
import random
import sqlite3
import struct
import time

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import databases
from src.world import gameobject
from src.world import login_packets
from src.world import object_update
from src.world import spells

from .constants import (
    SPELL_FAILED_BAD_TARGETS,
    SPELL_FAILED_CASTER_AURASTATE,
    SPELL_FAILED_CASTER_DEAD,
    SPELL_FAILED_INTERRUPTED,
    SPELL_FAILED_LINE_OF_SIGHT,
    SPELL_FAILED_NOT_BEHIND,
    SPELL_FAILED_NOT_INFRONT,
    SPELL_FAILED_NOT_KNOWN,
    SPELL_FAILED_NOT_READY,
    SPELL_FAILED_NO_COMBO_POINTS,
    SPELL_FAILED_NO_POWER,
    SPELL_FAILED_OUT_OF_RANGE,
    SPELL_FAILED_REAGENTS,
    SPELL_FAILED_REQUIRES_SPELL_FOCUS,
    SPELL_FAILED_SPELL_IN_PROGRESS,
    SPELL_FAILED_SPELL_UNAVAILABLE,
    SPELL_FAILED_TOTEM_CATEGORY,
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
        item_guid = 0
        dest = None
        if len(data) >= pos + 4:
            target_flags = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            if target_flags & OBJECT_TARGET_FLAGS:
                target_guid, pos = self._read_packed_guid(data, pos)
            if target_flags & ITEM_TARGET_FLAGS and pos < len(data):
                item_guid, pos = self._read_packed_guid(data, pos)
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
        if hasattr(self, "_player_can_cast"):
            if not self._player_can_cast(spell_id):
                await self._cast_failed(cast_id, spell_id,
                                        SPELL_FAILED_CASTER_AURASTATE)
                return
        elif hasattr(self, "_player_aura_mods"):
            mods = self._player_aura_mods()
            if (mods["stunned"] or mods["silenced"] or mods["feared"]
                    or mods["confused"] or mods["charmed"]):
                await self._cast_failed(cast_id, spell_id,
                                        SPELL_FAILED_CASTER_AURASTATE)
                return
        if spell_id not in self.known_spell_ids:
            if not self._item_has_spell(item_guid, spell_id):
                await self._cast_failed(cast_id, spell_id, SPELL_FAILED_NOT_KNOWN)
                return
        template = spells.get_spell_template(self.config.database.world_path, spell_id)
        if template is None:
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_SPELL_UNAVAILABLE)
            return
        if not await self._has_spell_focus(template):
            await self._cast_failed(cast_id, spell_id,
                                    SPELL_FAILED_REQUIRES_SPELL_FOCUS)
            return
        if not self._has_spell_tools(template):
            await self._cast_failed(cast_id, spell_id,
                                    SPELL_FAILED_TOTEM_CATEGORY)
            return
        if not self._has_spell_reagents(template):
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_REAGENTS)
            return
        if not self._can_store_create_items(template):
            # ``Spell::CheckCast`` sends an equip error and reports
            # ``SPELL_FAILED_DONT_REPORT``; the client has no cast to fail.
            if hasattr(self, "_send_inventory_failure"):
                await self._send_inventory_failure(
                    int(getattr(self, "INVENTORY_FULL", 50)))
            return
        # Auto Shot / Shoot / Throw toggle the ranged auto-attack instead of
        # running a normal spell effect loop.
        if int(template.id) in (75, 2764, 5019):
            cast = spells.SpellCast(self.player_guid, spell_id, target_guid)
            cast.template = template
            await self.send_packet(
                Opcodes.SMSG_SPELL_START,
                spells.build_spell_start(cast, self.player_guid, target_guid,
                                         cast_id),
            )
            await self.send_packet(
                Opcodes.SMSG_SPELL_GO,
                spells.build_spell_go(
                    cast, self.player_guid, target_guid, cast_id,
                    timestamp_ms=self._server_tick_ms(), off_gcd=True),
            )
            await self._start_ranged_attack(target_guid, int(template.id))
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

        # Opening spells target a game object; it has to be visible, spawned
        # and within reach (the spells' range is the interaction distance).
        go_target = bool(target_guid) and gameobject.is_gameobject_guid(target_guid)
        if go_target:
            reach = max(self._spell_range(template), gameobject.GAMEOBJECT_USE_DISTANCE)
            if self._usable_gameobject(target_guid, max_distance=reach) is None:
                await self._cast_failed(cast_id, spell_id, SPELL_FAILED_BAD_TARGETS)
                return

        # A live target must be within the spell's range before anything else.
        target_low = (creature.creature_low_guid(target_guid)
                      if target_guid and not go_target else 0)
        spawn = self._get_creature_spawn(target_low) if target_low else None
        is_peer_target = bool(
            hasattr(self, "_is_peer_player_guid") and target_guid
            and self._is_peer_player_guid(target_guid))
        target_peer = (self._peer_session_for_guid(target_guid)
                       if is_peer_target
                       and hasattr(self, "_peer_session_for_guid") else None)
        if (target_guid and not self._is_player_guid(target_guid)
                and not is_peer_target and spawn is None and not go_target):
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_BAD_TARGETS)
            return
        if target_peer is not None and target_peer.character:
            spell_range = self._spell_range(template)
            if spell_range < 49000.0:
                spell_range = max(spell_range, 0.5)
                peer_pos = (
                    float(target_peer.character.get("x", 0.0)),
                    float(target_peer.character.get("y", 0.0)),
                    float(target_peer.character.get("z", 0.0)))
                if self._distance(self._player_position(), peer_pos) > spell_range:
                    await self._cast_failed(cast_id, spell_id,
                                            SPELL_FAILED_OUT_OF_RANGE)
                    return
        if spawn is not None:
            spell_range = self._spell_range(template)
            if spell_range < 49000.0:
                spell_range = max(spell_range, 0.5)
                if (self._distance(self._player_position(),
                                   self._creature_position(target_low))
                        > spell_range + self._melee_reach(spawn)):
                    await self._cast_failed(cast_id, spell_id, SPELL_FAILED_OUT_OF_RANGE)
                    return
        if dest is not None:
            spell_range = self._spell_range(template)
            if spell_range < 49000.0:
                spell_range = max(spell_range, 0.5)
                if self._distance(self._player_position(), dest) > spell_range + 0.5:
                    await self._cast_failed(cast_id, spell_id,
                                            SPELL_FAILED_OUT_OF_RANGE)
                    return
            if hasattr(self, "_has_line_of_sight") and \
                    not await self._has_line_of_sight(
                        self._unit_eye_position(self.player_guid), dest):
                await self._cast_failed(cast_id, spell_id,
                                        SPELL_FAILED_LINE_OF_SIGHT)
                return

        # Facing and line-of-sight checks.  Self-casts and caster-centred AoE
        # are exempt.  Weapon abilities always require the victim in front.
        if target_guid and not self._is_player_guid(target_guid) and not go_target:
            needs_front = bool(
                template.facing_caster_flags & spells.SPELL_FACING_FLAG_INFRONT
                or template.has_weapon_effect())
            if needs_front and hasattr(self, "_target_in_front") \
                    and not self._target_in_front(target_guid):
                await self._cast_failed(cast_id, spell_id,
                                        SPELL_FAILED_NOT_INFRONT)
                return
            if target_peer is not None and target_peer.character:
                target_eye = (
                    float(target_peer.character.get("x", 0.0)),
                    float(target_peer.character.get("y", 0.0)),
                    float(target_peer.character.get("z", 0.0)) + 1.6)
            elif spawn is not None:
                pos = self._creature_position(target_low)
                target_eye = (pos[0], pos[1], pos[2] + 1.0)
            else:
                target_eye = None
            if target_eye is not None and hasattr(self, "_has_line_of_sight") \
                    and not await self._has_line_of_sight(
                        self._unit_eye_position(self.player_guid), target_eye):
                await self._cast_failed(cast_id, spell_id,
                                        SPELL_FAILED_LINE_OF_SIGHT)
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
        cast.item_guid = int(item_guid or 0)
        if dest is not None:
            cast.x, cast.y, cast.z = (float(dest[0]), float(dest[1]),
                                      float(dest[2]))
            cast.has_dest = True
        cast_time_ms = self._spell_cast_time_ms(template)
        cast.cast_time_ms = cast_time_ms
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
                "item_guid": int(item_guid or 0),
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
        if not await self._consume_spell_reagents(template):
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_REAGENTS)
            return
        if not await self._take_power(template):
            await self._cast_failed(cast_id, spell_id, SPELL_FAILED_NO_POWER)
            return
        # ``Spell::update`` sends GO as the cast completes and only then runs
        # ``finish()``, so the damage/heal logs follow the packet that starts the
        # spell's visuals.  Sending GO after the effects also reads a power
        # value that already includes rage gained by the cast itself.
        await self._send_spell_go(cast, template, target_guid, cast_id)
        await self._apply_spell_effects(cast, template, target_guid)
        if template.is_channeled():
            await self._start_channel(cast, template, target_guid, cast_id)
        # ``Spell::finish`` clears the combo points after the effects have run,
        # so the finisher itself still scales with the full count.
        if template.needs_combo_points():
            await self._clear_combo_points()
        self.log.info(
            f"'{self.character['name']}' casts {template.name} ({spell_id})"
        )

    def _item_has_spell(self, item_guid: int, spell_id: int) -> bool:
        """True when an inventory item's on-use spell is ``spell_id``.

        ``item_guid`` may be zero when the client used an item without setting
        TARGET_FLAG_ITEM; in that case any matching item in the character's
        inventory is accepted.
        """
        import sqlite3
        if not self.player_guid:
            return False
        con = databases.connect_character(self.config.database.world_path)
        try:
            if item_guid:
                row = con.execute(
                    "SELECT 1 FROM character_items ci"
                    " JOIN item_templates it ON it.entry = ci.item_entry"
                    " WHERE ci.id = ? AND ci.character_guid = ? AND ("
                    " it.spell_id1 = ? OR it.spell_id2 = ? OR it.spell_id3 = ?"
                    " OR it.spell_id4 = ? OR it.spell_id5 = ?)",
                    (int(item_guid), int(self.player_guid), int(spell_id),
                     int(spell_id), int(spell_id), int(spell_id),
                     int(spell_id)),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT 1 FROM character_items ci"
                    " JOIN item_templates it ON it.entry = ci.item_entry"
                    " WHERE ci.character_guid = ? AND ("
                    " it.spell_id1 = ? OR it.spell_id2 = ? OR it.spell_id3 = ?"
                    " OR it.spell_id4 = ? OR it.spell_id5 = ?)",
                    (int(self.player_guid), int(spell_id), int(spell_id),
                     int(spell_id), int(spell_id), int(spell_id)),
                ).fetchone()
        finally:
            con.close()
        return row is not None

    _SPELL_EFFECT_MOD_OP = {
        0: spells.SPELLMOD_EFFECT1,
        1: spells.SPELLMOD_EFFECT2,
        2: spells.SPELLMOD_EFFECT3,
    }

    def _spell_mod_totals(self, template) -> dict:
        """Aggregate active talent spell modifiers that affect ``template``."""
        totals: dict[int, list[float]] = {}
        for aura in self._get_combat_state().get("auras", ()):
            for component in aura.get("components", ()):
                kind = int(component.get("type") or 0)
                if kind not in (spells.SPELL_AURA_ADD_FLAT_MODIFIER,
                                spells.SPELL_AURA_ADD_PCT_MODIFIER):
                    continue
                family = int(component.get("family") or 0)
                if family and family != int(template.spell_family):
                    continue
                mask = tuple(component.get("class_mask") or (0, 0, 0))
                if any(mask):
                    flags = getattr(template, "spell_family_flags",
                                    (0, 0, 0))
                    if not any(int(mask[i]) & int(flags[i])
                               for i in range(3)):
                        continue
                op = int(component.get("misc") or 0)
                slot = totals.setdefault(op, [0.0, 0.0])
                amount = float(component.get("amount") or 0)
                if kind == spells.SPELL_AURA_ADD_FLAT_MODIFIER:
                    slot[0] += amount
                else:
                    slot[1] += amount
        return totals

    def _spell_mod_value(self, template, op: int, value: float,
                         effect_index: int | None = None,
                         include_damage: bool = False) -> float:
        totals = self._spell_mod_totals(template)
        keys = {int(op)}
        if effect_index is not None:
            mapped = self._SPELL_EFFECT_MOD_OP.get(int(effect_index))
            if mapped is not None:
                keys.add(mapped)
            keys.add(spells.SPELLMOD_ALL_EFFECTS)
        if include_damage:
            keys.add(spells.SPELLMOD_DAMAGE)
        flat = sum(totals.get(key, (0.0, 0.0))[0] for key in keys)
        pct = sum(totals.get(key, (0.0, 0.0))[1] for key in keys)
        return (float(value) + flat) * (1.0 + pct / 100.0)

    def _spell_effect_value(self, template, index: int, effect: dict,
                            caster_level: int, combo_points: int = 0,
                            damage: bool = False) -> int:
        amount = int(template.effect_value(index, caster_level, combo_points))
        return int(self._spell_mod_value(
            template, spells.SPELLMOD_ALL_EFFECTS, amount,
            effect_index=index, include_damage=damage))

    def _spell_range(self, template) -> float:
        return max(0.0, float(self._spell_mod_value(
            template, spells.SPELLMOD_RANGE, template.get_range())))

    def _spell_cast_time_ms(self, template) -> int:
        """Cast time after spell haste, preserving instant casts."""
        base = int(template.get_casting_time_ms())
        if base <= 0:
            return 0
        base = max(0, int(self._spell_mod_value(
            template, spells.SPELLMOD_CASTING_TIME, base)))
        if base <= 0:
            return 0
        haste = float(self.character.get("spell_haste", 0.0) or 0.0)
        if not haste and hasattr(self, "_player_aura_mods"):
            haste = float(self._player_aura_mods().get("spell_haste", 0.0))
        return max(50, int(base / max(0.05, 1.0 + haste / 100.0)))

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

    async def _start_channel(self, cast, template, target_guid: int,
                             cast_id: int = 0) -> None:
        """Begin a channelled spell and schedule its periodic trigger."""
        caster_level = int(self.character.get("level", 1))
        duration_ms = spells.spell_duration_ms(
            self.config.database.world_path, template.duration_index,
            caster_level)
        if duration_ms <= 0:
            duration_ms = 3000
        interval_ms = 1000
        trigger_spell = 0
        for effect in template.effects:
            if int(effect.get("apply_aura") or 0) not in (
                    spells.SPELL_AURA_PERIODIC_TRIGGER_SPELL,
                    spells.SPELL_AURA_PERIODIC_TRIGGER_SPELL_WITH_VALUE):
                continue
            interval_ms = max(250, int(effect.get("amplitude") or 1000))
            trigger_spell = int(effect.get("trigger_spell") or 0)
            break
        now = time.monotonic()
        self._get_combat_state()["channel"] = {
            "spell_id": int(template.id),
            "template": template,
            "target_guid": int(target_guid or 0),
            "caster_guid": int(self.player_guid),
            "cast_id": int(cast_id) & 0xFF,
            "expires": now + duration_ms / 1000.0,
            "next_tick": now + interval_ms / 1000.0,
            "interval": interval_ms / 1000.0,
            "trigger_spell": trigger_spell,
            "ticks": 0,
        }
        await self.send_packet(
            Opcodes.MSG_CHANNEL_START,
            spells.pack_guid(int(self.player_guid))
            + struct.pack("<II", int(template.id) & 0xFFFFFFFF,
                          int(duration_ms) & 0xFFFFFFFF),
        )

    async def _tick_channel(self, now: float) -> None:
        """Run the periodic trigger of the player's active channel."""
        combat = self._get_combat_state()
        channel = combat.get("channel")
        if not channel:
            return
        if now >= float(channel["expires"]):
            await self._finish_channel()
            return
        if channel.get("trigger_spell") and now >= float(channel["next_tick"]):
            channel["next_tick"] = now + float(channel["interval"])
            channel["ticks"] = int(channel["ticks"]) + 1
            await self._cast_triggered(
                int(channel["trigger_spell"]), int(channel["target_guid"]))

    async def _finish_channel(self) -> None:
        combat = self._get_combat_state()
        channel = combat.get("channel")
        if not channel:
            return
        combat["channel"] = None
        await self.send_packet(
            Opcodes.MSG_CHANNEL_UPDATE,
            spells.pack_guid(int(self.player_guid)) + struct.pack("<I", 0),
        )
        self.log.debug(f"Channel of {channel['spell_id']} finished")

    async def _interrupt_player_cast(self, reason: int) -> None:
        """Cancel a pending cast/channel after damage, movement or an interrupt."""
        combat = self._get_combat_state()
        pending = combat.get("pending_cast")
        if pending:
            combat["pending_cast"] = None
            await self.send_packet(
                Opcodes.SMSG_SPELL_FAILURE,
                spells.build_spell_failure(
                    self.player_guid, pending["cast_id"], pending["spell_id"],
                    reason),
            )
        if combat.get("channel"):
            channel = combat["channel"]
            combat["channel"] = None
            await self.send_packet(
                Opcodes.SMSG_SPELL_FAILURE,
                spells.build_spell_failure(
                    self.player_guid, channel.get("cast_id", 0),
                    channel["spell_id"], reason),
            )
            await self.send_packet(
                Opcodes.MSG_CHANNEL_UPDATE,
                spells.pack_guid(int(self.player_guid)) + struct.pack("<I", 0),
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

    def _reagent_slots(self, item_entry: int) -> list[tuple[int, dict]]:
        """Backpack/bag slots holding ``item_entry``.

        ``0..22`` are equipment and bag-container slots, so reagents can only
        come from the backpack (``256..271``) and any bag contents stored
        above that range.
        """
        inventory = (self.character or {}).get("inventory") or {}
        result = []
        for slot, item in inventory.items():
            if int(slot) < 256:
                continue
            if int(item.get("item_entry") or 0) == int(item_entry):
                result.append((int(slot), item))
        return sorted(result, key=lambda pair: pair[0])

    def _spell_item_requirements(self, template) -> list[tuple[int, int]]:
        """Reagents plus literal totem items (``Spell::TakeReagents``)."""
        requirements = [
            (int(entry), max(1, int(count)))
            for entry, count in getattr(template, "reagents", ())
        ]
        requirements += [(int(entry), 1)
                         for entry in getattr(template, "totem_items", ())
                         if int(entry)]
        return requirements

    def _has_spell_reagents(self, template) -> bool:
        for item_entry, count in self._spell_item_requirements(template):
            total = sum(max(1, int(item.get("stack_count", 1) or 1))
                        for _slot, item in self._reagent_slots(item_entry))
            if total < int(count):
                return False
        return True

    async def _consume_spell_reagents(self, template) -> bool:
        """Consume the spell's reagents, atomically checking every stack first."""
        requirements = self._spell_item_requirements(template)
        if not requirements:
            return True
        if not self._has_spell_reagents(template):
            return False
        for item_entry, count in requirements:
            remaining = int(count)
            while remaining > 0:
                slots = self._reagent_slots(item_entry)
                if not slots:
                    return False
                slot, item = slots[0]
                current = max(1, int(item.get("stack_count", 1) or 1))
                take = min(remaining, current)
                await self._remove_item_from_slot(
                    slot, 0 if take >= current else take,
                    notify_quests=False)
                remaining -= take
        return True

    def _has_spell_tools(self, template) -> bool:
        """``TotemCategory`` requirement: hammer, pick, knife, ... in bags."""
        required = [int(category)
                    for category in getattr(template, "totem_category", ())
                    if int(category)]
        if not required:
            return True
        path = self.config.database.world_path
        owned = set()
        for item in ((self.character or {}).get("inventory") or {}).values():
            entry = int(item.get("item_entry") or 0)
            if not entry:
                continue
            category, _max_stack = spells.item_tool_fields(path, entry)
            if category:
                owned.add(category)
        for category in required:
            if not any(
                    spells.totem_category_compatible(owned_category, category,
                                                     path)
                    for owned_category in owned):
                return False
        return True

    async def _has_spell_focus(self, template) -> bool:
        """``RequiresSpellFocus``: an anvil/forge/fire nearby."""
        focus_id = int(getattr(template, "requires_spell_focus", 0) or 0)
        if not focus_id:
            return True
        if not hasattr(self, "_gameobjects_near"):
            return False
        point = self._gameobject_scan_point()
        if point is None:
            return False
        try:
            spawns = await self._gameobjects_near(point)
        except Exception as exc:            # map data unavailable
            self.log.warning(f"Spell focus scan failed: {exc}")
            return False
        limit = gameobject.GAMEOBJECT_USE_DISTANCE ** 2
        focus_type = gameobject.GAMEOBJECT_TYPE_SPELL_FOCUS
        for spawn in spawns:
            if int(spawn.get("type") or 0) != focus_type:
                continue
            if int(spawn.get("data0") or 0) != focus_id:
                continue
            dx = float(spawn.get("x", 0.0)) - float(point[1])
            dy = float(spawn.get("y", 0.0)) - float(point[2])
            dz = float(spawn.get("z", 0.0)) - float(point[3])
            if dx * dx + dy * dy + dz * dz <= limit:
                return True
        return False

    def _can_store_create_items(self, template) -> bool:
        """Cast-time bag-space check for ``CREATE_ITEM[_2]`` effects."""
        path = self.config.database.world_path
        for effect in template.effects:
            if int(effect.get("effect") or 0) not in (
                    spells.SPELL_EFFECT_CREATE_ITEM,
                    spells.SPELL_EFFECT_CREATE_ITEM_2):
                continue
            item_entry = int(effect.get("item_type") or 0)
            if not item_entry:
                continue
            _category, max_stack = spells.item_tool_fields(path, item_entry)
            count = min(max(1, int(effect.get("base_points") or 0) + 1),
                        max_stack)
            if not character_db.can_add_item_to_inventory(
                    path, int(self.player_guid), item_entry, count):
                return False
        return True

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
        cost = int(self._spell_mod_value(
            template, spells.SPELLMOD_COST, cost))
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
            cooldown_ms = max(0, int(self._spell_mod_value(
                template, spells.SPELLMOD_COOLDOWN, cooldown_ms)))
            if cooldown_ms > 0:
                combat["cooldowns"][template.id] = now + cooldown_ms / 1000.0
        gcd_ms = int(template.start_recovery_time)
        if gcd_ms > 0:
            gcd_ms = max(0, int(self._spell_mod_value(
                template, spells.SPELLMOD_GLOBAL_COOLDOWN, gcd_ms)))
            if gcd_ms > 0:
                combat["gcd_until"] = now + gcd_ms / 1000.0

    def _spell_target_kind(self, target_guid: int) -> str:
        if self._is_player_guid(target_guid):
            return "player"
        if hasattr(self, "_is_peer_player_guid") \
                and self._is_peer_player_guid(target_guid):
            return "peer"
        return "creature"

    def _target_max_health(self, target_guid: int) -> int:
        """Maximum health of a player, peer or creature target."""
        kind = self._spell_target_kind(target_guid)
        if kind == "player":
            return int(self._max_health())
        if kind == "peer":
            peer = (self._peer_session_for_guid(target_guid)
                    if hasattr(self, "_peer_session_for_guid") else None)
            return int(peer._max_health()) if peer is not None else 0
        low = creature.creature_low_guid(int(target_guid or 0))
        spawn = self._get_creature_spawn(low)
        if spawn is None:
            return 0
        return int(self._creature_max_health(spawn))

    def _spell_target_level(self, target_guid: int) -> int:
        if self._is_player_guid(target_guid):
            return max(1, int(self.character.get("level", 1)))
        if hasattr(self, "_is_peer_player_guid") \
                and self._is_peer_player_guid(target_guid):
            return max(1, int(self.character.get("level", 1)))
        low = creature.creature_low_guid(int(target_guid or 0))
        spawn = self._get_creature_spawn(low)
        if spawn is None:
            return 1
        return max(1, int(spawn["min_level"] or 1))

    @staticmethod
    def _effect_is_harmful(template, effect: dict) -> bool:
        etype = int(effect.get("effect") or 0)
        if etype in DIRECT_DAMAGE_TYPES or etype in (
                spells.SPELL_EFFECT_INSTAKILL,
                spells.SPELL_EFFECT_INTERRUPT_CAST,
                spells.SPELL_EFFECT_DISPEL,
                spells.SPELL_EFFECT_STEAL_BENEFICIAL,
                spells.SPELL_EFFECT_POWER_BURN,
                spells.SPELL_EFFECT_CHARGE):
            return True
        aura = int(effect.get("apply_aura") or 0)
        if aura in (
                spells.SPELL_AURA_PERIODIC_DAMAGE,
                spells.SPELL_AURA_PERIODIC_LEECH,
                spells.SPELL_AURA_MOD_STUN,
                spells.SPELL_AURA_MOD_ROOT,
                spells.SPELL_AURA_MOD_FEAR,
                spells.SPELL_AURA_MOD_CHARM,
                spells.SPELL_AURA_MOD_CONFUSE,
                spells.SPELL_AURA_MOD_SILENCE,
                spells.SPELL_AURA_MOD_PACIFY,
                spells.SPELL_AURA_MOD_PACIFY_SILENCE,
                spells.SPELL_AURA_MOD_DECREASE_SPEED,
                spells.SPELL_AURA_MOD_DAMAGE_PERCENT_TAKEN):
            return True
        return bool(aura) and template.is_harmful()

    def _spell_attack_roll(self, target_guid: int, template,
                           effect: dict | None = None) -> dict:
        """Resolve one spell hit/crit against a hostile target.

        A synthetic/test session that has never run ``_apply_combat_stats`` has
        no ``spell_hit_chance`` and always lands, which keeps the simulator
        deterministic for unit tests.  In a real session the 3.3.5 base 4%
        miss against an equal-level target applies, modified by hit rating.
        """
        result = {"hit": True, "crit": False,
                  "miss_type": spells.SPELL_MISS_NONE}
        if not target_guid or self._is_player_guid(target_guid):
            return result
        if hasattr(self, "_is_peer_player_guid") \
                and self._is_peer_player_guid(target_guid):
            return result
        if template.attributes_ex3 & spells.SPELL_ATTR_EX3_CANT_MISS:
            return result
        if "spell_hit_chance" not in self.character:
            return result
        etype = int((effect or {}).get("effect") or 0)
        if etype in DIRECT_DAMAGE_TYPES and etype != spells.SPELL_EFFECT_SCHOOL_DAMAGE \
                and hasattr(self, "_player_melee_table"):
            target_level = self._spell_target_level(target_guid)
            outcome = self._roll_melee(
                self._player_melee_table(target_level))
            if outcome == "miss":
                result["hit"] = False
                result["miss_type"] = spells.SPELL_MISS_MISS
            elif outcome == "dodge":
                result["hit"] = False
                result["miss_type"] = spells.SPELL_MISS_DODGE
            elif outcome == "parry":
                result["hit"] = False
                result["miss_type"] = spells.SPELL_MISS_PARRY
            elif outcome == "crit":
                result["crit"] = True
            return result
        target_level = self._spell_target_level(target_guid)
        caster_level = max(1, int(self.character.get("level", 1)))
        level_delta = target_level - caster_level
        base_miss = 4.0 + max(0, level_delta) * 2.0
        hit_bonus = max(0.0, float(self.character.get("spell_hit_chance", 5.0))
                        - 5.0)
        miss_chance = max(0.0, min(100.0, base_miss - hit_bonus))
        roll = random.uniform(0.0, 100.0)
        if roll < miss_chance:
            result["hit"] = False
            result["miss_type"] = spells.SPELL_MISS_MISS
            return result
        crit_chance = max(0.0, float(
            self.character.get("spell_crit_chance", 0.0) or 0.0))
        crit_chance = max(0.0, float(self._spell_mod_value(
            template, spells.SPELLMOD_CRITICAL_CHANCE, crit_chance)))
        if crit_chance and random.uniform(0.0, 100.0) < crit_chance:
            result["crit"] = True
        return result

    def _spell_resist_amount(self, target_guid: int, school: int,
                             damage: int) -> int:
        """Partial resistance using the 3.3.5 resistance-to-level ratio."""
        if damage <= 0 or not school:
            return 0
        kind = self._spell_target_kind(target_guid)
        name = ""
        for bit, school_name in spells.RESISTANCE_AURA_SCHOOLS:
            if school & bit:
                name = school_name
                break
        if not name:
            return 0
        if kind == "creature":
            spawn = self._get_creature_spawn(
                creature.creature_low_guid(target_guid))
            if spawn is not None and ("resistance_" + name) in spawn.keys():
                resistance = int(spawn["resistance_" + name] or 0)
                level = max(1, int(spawn["min_level"] or 1))
            else:
                return 0
            penetration = int(self.character.get(
                "spell_penetration", 0) or 0)
            resistance = max(0, resistance - penetration)
            if not resistance:
                return 0
            chance = min(0.75, resistance / (5.0 * level))
            return max(0, int(damage * random.uniform(0.0, chance)))
        if kind != "player":
            return 0
        resistance = max(0, int(self.character.get("res_" + name, 0) or 0))
        penetration = int(self.character.get("spell_penetration", 0) or 0)
        resistance = max(0, resistance - penetration)
        if not resistance:
            return 0
        level = max(1, int(self.character.get("level", 1)))
        chance = min(0.75, resistance / (5.0 * level))
        return max(0, int(damage * random.uniform(0.0, chance)))

    def _spell_reflects(self, target_guid: int) -> bool:
        kind = self._spell_target_kind(target_guid)
        if kind == "player":
            if not hasattr(self, "_player_aura_mods"):
                return False
            mods = self._player_aura_mods()
        elif kind == "creature":
            if not hasattr(self, "_creature_aura_mods"):
                return False
            mods = self._creature_aura_mods(
                creature.creature_low_guid(target_guid))
        else:
            return False
        return bool(mods["reflect"])

    async def _send_spell_miss_log(self, target_guid: int, caster_guid: int,
                                   spell_id: int, miss_type: int,
                                   reflect_result: int = 0) -> None:
        payload = bytearray()
        payload += struct.pack("<I", int(spell_id) & 0xFFFFFFFF)
        payload += struct.pack("<Q", int(caster_guid) & 0xFFFFFFFFFFFFFFFF)
        payload += struct.pack("<B", 0)
        payload += struct.pack("<I", 1)
        payload += struct.pack("<Q", int(target_guid) & 0xFFFFFFFFFFFFFFFF)
        payload += struct.pack("<B", int(miss_type) & 0xFF)
        if int(miss_type) == spells.SPELL_MISS_REFLECT:
            payload += struct.pack("<B", int(reflect_result) & 0xFF)
        await self.send_packet(Opcodes.SMSG_SPELLLOGMISS, bytes(payload))

    async def _resolve_spell_target(self, target_guid: int, template,
                                    effect: dict, rolls: dict) -> dict | None:
        """Return a successful roll for a hostile target, or ``None``.

        Sends the appropriate miss/immune/reflect packet when the effect cannot
        land.  The cache keyed by target makes a multi-effect spell (Frostbolt's
        slow + damage, for example) share one roll.
        """
        if not target_guid or self._is_player_guid(target_guid):
            return {"hit": True, "crit": False, "miss_type": 0}
        kind = self._spell_target_kind(target_guid)
        if kind == "peer":
            return {"hit": True, "crit": False, "miss_type": 0}
        key = (int(target_guid), int(template.id))
        if key not in rolls:
            if hasattr(self, "_spell_immune") and self._spell_immune(
                    "creature", target_guid, template, effect):
                await self._send_spell_miss_log(
                    target_guid, self.player_guid, template.id,
                    spells.SPELL_MISS_IMMUNE)
                rolls[key] = None
            elif self._spell_reflects(target_guid):
                await self._send_spell_miss_log(
                    target_guid, self.player_guid, template.id,
                    spells.SPELL_MISS_REFLECT, 0)
                rolls[key] = {"hit": False, "crit": False, "reflect": True}
            else:
                roll = self._spell_attack_roll(target_guid, template, effect)
                if not roll["hit"]:
                    await self._send_spell_miss_log(
                        target_guid, self.player_guid, template.id,
                        roll["miss_type"])
                    rolls[key] = None
                else:
                    rolls[key] = roll
        return rolls.get(key)

    def _spell_power_bonus(self, effect: dict, healing: bool = False) -> int:
        coefficient = float(effect.get("bonus_coefficient") or 0.0)
        if coefficient <= 0.0:
            return 0
        power = int(self.character.get("spell_power", 0) or 0)
        if healing:
            power = max(power, int(
                self.character.get("healing_power", 0) or 0))
        return int(power * coefficient)

    def _apply_damage_done(self, school: int, amount: int) -> int:
        modifier = getattr(self, "_damage_done_modifier", None)
        if modifier is None:
            return max(0, int(amount))
        return modifier(school, max(0, int(amount)))

    async def _break_cc_on_damage(self, target_kind: str, target_guid: int,
                                  spell_id: int) -> None:
        """Damage breaks polymorph/sleep effects (not roots or fears)."""
        if not hasattr(self, "_aura_store"):
            return
        entries = self._aura_store(target_kind, target_guid)
        broken = []
        for aura in list(entries):
            if int(aura.get("spell_id") or 0) == int(spell_id):
                continue
            if int(aura.get("mechanic") or 0) not in (
                    spells.MECHANIC_POLYMORPH, spells.MECHANIC_SLEEP,
                    spells.MECHANIC_DISORIENTED):
                continue
            entries.remove(aura)
            broken.append(aura)
        if not broken:
            return
        if hasattr(self, "_normalise_aura_slots"):
            self._normalise_aura_slots(entries)
        for aura in broken:
            if hasattr(self, "_send_aura_update"):
                await self._send_aura_update(
                    target_kind, target_guid, aura, remove=True)
        if target_kind == "player" and hasattr(self, "_refresh_player_auras"):
            await self._refresh_player_auras()
        elif target_kind == "creature" and hasattr(self, "_sync_creature_flags"):
            await self._sync_creature_flags(
                creature.creature_low_guid(target_guid))

    async def _apply_weapon_enchant(self, cast, template, effect: dict) -> None:
        """Apply a temporary/permanent weapon enchant (poisons, stones, oils).

        The item enchant row points at a combat spell id; poisons use that spell
        as their proc.  The enchant is stored per weapon slot and persists in
        ``character_items.enchantment_2`` so it survives a relog.
        """
        import sqlite3
        enchant_id = int(effect.get("misc_value") or 0)
        if not enchant_id:
            return
        path = self.config.database.world_path
        item_guid = int(getattr(cast, "item_guid", 0) or 0)
        temporary = int(effect.get("effect") or 0) == \
            spells.SPELL_EFFECT_ENCHANT_ITEM_TEMPORARY
        slot = 15
        item_slot = None
        if item_guid:
            con = databases.connect_character(path)
            try:
                row = con.execute(
                    "SELECT slot FROM character_items"
                    " WHERE id = ? AND character_guid = ?",
                    (item_guid, int(self.player_guid or 0)),
                ).fetchone()
                if row is not None:
                    item_slot = int(row[0])
                    if item_slot in (15, 16):
                        slot = item_slot
            finally:
                con.close()
        if not temporary and item_slot not in (15, 16):
            return
        con = sqlite3.connect(path)
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
            return
        procs = []
        bonus_damage = 0
        display_name = str(getattr(template, "name", "") or "")
        for index in (1, 2, 3):
            etype = int(enchant[f"effect{index}"] or 0)
            points = int(enchant[f"effect_points_min{index}"] or 0)
            arg = int(enchant[f"effect_arg{index}"] or 0)
            if etype == spells.ITEM_ENCHANTMENT_COMBAT_SPELL and arg:
                proc_template = spells.get_spell_template(path, arg)
                proc_name = str(getattr(proc_template, "name", "") or "")
                if proc_name:
                    display_name = proc_name
                procs.append(spells.item_enchant_proc(
                    proc_name or display_name, points, arg))
            elif etype == spells.ITEM_ENCHANTMENT_DAMAGE:
                bonus_damage += points
        if not procs and not bonus_damage:
            return
        duration_ms = spells.spell_duration_ms(
            path, template.duration_index,
            int(self.character.get("level", 1)))
        if duration_ms <= 0:
            duration_ms = 3600 * 1000
        now = time.monotonic()
        combat = self._get_combat_state()
        combat.setdefault("weapon_enchants", {})[slot] = {
            "enchant_id": enchant_id,
            "spell_id": int(template.id),
            "name": display_name,
            "slot": slot,
            "procs": procs,
            "bonus_damage": int(bonus_damage),
            "charges": int(enchant["charges"] or 0),
            "expires": now + duration_ms / 1000.0,
            "max_duration": duration_ms / 1000.0,
        }
        await run_db_write(
            character_db.update_character_item_enchant, path,
            int(self.player_guid or 0), int(slot), enchant_id,
        )
        inventory = getattr(self, "character", {}).get("inventory") or {}
        item = inventory.get(int(slot))
        if item is not None:
            item["enchantment_2"] = enchant_id
        if hasattr(self, "broadcast_equipment_update"):
            await self.broadcast_equipment_update()
        if hasattr(self, "_send_system_message"):
            await self._send_system_message(
                f"Your weapon is coated with {display_name}.")

    async def _deal_spell_damage(self, target_guid: int, amount: int,
                                 school: int, template, caster_guid: int,
                                 critical: bool = False,
                                 periodic: bool = False) -> bool:
        """Apply spell damage with resistance, absorbs and damage modifiers."""
        amount = max(0, int(amount))
        if amount <= 0:
            return False
        kind = self._spell_target_kind(target_guid)
        if kind in ("player", "creature") and hasattr(self, "_spell_immune") \
                and self._spell_immune(kind, target_guid, template):
            if hasattr(self, "_send_spell_miss_log"):
                await self._send_spell_miss_log(
                    target_guid, caster_guid, template.id,
                    spells.SPELL_MISS_IMMUNE)
            return False
        # Synthetic test sessions only implement the old target helper.
        if (kind not in ("player", "peer")
                and not hasattr(self, "_deal_damage_to_creature")):
            await self._deal_damage_to_target(
                target_guid, amount, school, template.id, caster_guid)
            return False
        if int(school) & 1 and hasattr(self, "_armor_reduced_damage"):
            if kind == "player":
                armor = int(self.character.get("armor", 0) or 0)
            else:
                spawn = self._get_creature_spawn(
                    creature.creature_low_guid(target_guid))
                armor = int(spawn["armor"] or 0) \
                    if spawn is not None and "armor" in spawn.keys() else 0
                if hasattr(self, "_creature_aura_mods"):
                    mods = self._creature_aura_mods(
                        creature.creature_low_guid(target_guid))
                    armor += int(mods.get("armor", 0) or 0)
                    armor = int(armor * (1.0 + float(
                        mods.get("armor_pct", 0.0) or 0.0) / 100.0))
            amount = self._armor_reduced_damage(
                amount, armor, self._spell_target_level(target_guid))
        absorbed = 0
        depleted = []
        if hasattr(self, "_absorb_damage"):
            absorbed, depleted = self._absorb_damage(
                "player" if kind == "player" else "creature",
                target_guid, school, amount)
            for aura in depleted:
                if hasattr(self, "_send_aura_update"):
                    await self._send_aura_update(
                        "player" if kind == "player" else "creature",
                        target_guid, aura, remove=True)
        resisted = self._spell_resist_amount(target_guid, school, amount)
        remaining = max(0, amount - absorbed - resisted)
        if hasattr(self, "_apply_damage_modifiers"):
            remaining = self._apply_damage_modifiers(
                "player" if kind == "player" else "creature",
                target_guid, school, remaining)
        died = False
        if kind == "player":
            if remaining > 0:
                await self._damage_player(remaining, f"spell {template.id}")
            overkill = max(0, remaining - (
                self._victim_health(target_guid)
                if hasattr(self, "_victim_health") else 0))
        elif kind == "peer":
            peer = (self._peer_session_for_guid(target_guid)
                    if hasattr(self, "_peer_session_for_guid") else None)
            if peer is not None and remaining > 0:
                await peer._damage_player(
                    remaining, f"{self.character.get('name', 'player')}'s "
                    f"{template.name}", attacker_guid=self.player_guid)
            overkill = 0
        else:
            overkill = max(0, remaining - (
                self._victim_health(target_guid)
                if hasattr(self, "_victim_health") else 0))
            if remaining > 0:
                died = await self._deal_damage_to_creature(
                    target_guid, remaining, caster_guid,
                    threat_kind="spell")
        await self._send_spell_damage_log(
            target_guid, caster_guid, template.id, remaining, school or 1,
            periodic=1 if periodic else 0,
            critical=1 if critical else 0,
            overkill=overkill, absorbed=absorbed, resisted=resisted)
        if remaining > 0 and kind in ("player", "creature"):
            await self._break_cc_on_damage(kind, target_guid, template.id)
        # Damage shields (Thorns, Lightning Shield, ...) retaliate against the
        # caster of a direct hostile spell.
        if not periodic and remaining > 0 and caster_guid:
            if kind == "player" and hasattr(self, "_player_aura_mods"):
                thorns = int(self._player_aura_mods().get("thorns", 0) or 0)
                if thorns and not self._is_player_guid(caster_guid):
                    await self._deal_damage_to_creature(
                        caster_guid, thorns, self.player_guid,
                        threat_kind="spell")
            elif kind == "creature" and hasattr(self, "_creature_aura_mods"):
                thorns = int(self._creature_aura_mods(
                    creature.creature_low_guid(target_guid)).get("thorns", 0)
                    or 0)
                if thorns:
                    await self._damage_player(thorns, "damage shield")
        if depleted:
            if kind == "player" and hasattr(self, "_refresh_player_auras"):
                await self._refresh_player_auras()
            elif kind == "creature" and hasattr(self, "_sync_creature_flags"):
                await self._sync_creature_flags(
                    creature.creature_low_guid(target_guid))
        return died

    async def _apply_knockback(self, target_guid: int, speed: int,
                               school: int = 1) -> None:
        """Push a unit away from the caster (``SPELL_EFFECT_KNOCK_BACK``)."""
        speed = max(5, min(60, int(speed)))
        if self._is_player_guid(target_guid):
            caster = self._player_position()
            target = self._player_position()
            dx = target[0] - caster[0]
            dy = target[1] - caster[1]
            length = max(0.001, (dx * dx + dy * dy) ** 0.5)
            await self.send_packet(
                Opcodes.SMSG_MOVE_KNOCK_BACK,
                spells.pack_guid(int(self.player_guid))
                + struct.pack("<Iffff", 0, dx / length, dy / length,
                              float(speed), 3.0),
            )
            return
        low = creature.creature_low_guid(int(target_guid))
        spawn = self._get_creature_spawn(low)
        if spawn is None:
            return
        here = self._creature_position(low)
        caster = self._player_position()
        dx = here[0] - caster[0]
        dy = here[1] - caster[1]
        length = max(0.001, (dx * dx + dy * dy) ** 0.5)
        travel = min(10.0, speed * 0.15)
        dest = (here[0] + dx / length * travel,
                here[1] + dy / length * travel, here[2])
        combat = self._get_combat_state()
        state = combat.get("aggro", {}).get(low)
        if state is not None:
            state["pos"] = dest
            state["chase_until"] = time.monotonic() + 0.25
        await self.send_packet(
            Opcodes.SMSG_MONSTER_MOVE,
            object_update.build_monster_move(
                self._creature_object_guid(low, int(spawn["entry"])),
                here[0], here[1], here[2],
                math.atan2(dest[1] - here[1], dest[0] - here[0]),
                dest[0], dest[1], dest[2], duration_ms=250,
                move_id=self._next_move_id()),
        )

    async def _interrupt_target_cast(self, target_guid: int,
                                     school: int) -> None:
        """``SPELL_EFFECT_INTERRUPT_CAST``: stop a cast and lock its school."""
        now = time.monotonic()
        if self._is_player_guid(target_guid):
            combat = self._get_combat_state()
            school_mask = int(school or 1)
            combat.setdefault("school_lockout", {})[school_mask] = now + 4.0
            await self._interrupt_player_cast(SPELL_FAILED_INTERRUPTED)
            return
        low = creature.creature_low_guid(int(target_guid))
        state = self._get_combat_state().get("aggro", {}).get(low)
        if state is None:
            return
        ai = state.get("ai")
        if ai is None:
            return
        cast = ai.get("cast")
        if cast is not None:
            ai["cast"] = None
            await self.send_packet(
                Opcodes.SMSG_SPELL_FAILURE,
                spells.build_spell_failure(
                    self._creature_object_guid(low, ai["entry"]),
                    cast.get("cast_id", 0), int(cast["spell_id"]),
                    SPELL_FAILED_INTERRUPTED),
            )
        ai["school_lockout"] = {
            "school": int(school or 1),
            "until": now + 4.0,
        }
        self.log.debug(
            f"Interrupted creature {low} and locked school {school}")

    def _dispel_candidates(self, target_kind: str, target_guid: int,
                           dispel_type: int, offensive: bool) -> list:
        """Auras on ``target_guid`` matching a dispel type and polarity."""
        entries = self._aura_store(target_kind, target_guid)
        mask = (spells.DISPEL_ALL_MASK if int(dispel_type) in (0, spells.DISPEL_ALL)
                else 1 << int(dispel_type))
        result = []
        for aura in entries:
            aura_dispel = int(aura.get("dispel_type") or 0)
            if not aura_dispel or not (mask & (1 << aura_dispel)):
                continue
            # Offensive dispels remove buffs; defensive dispels remove debuffs.
            if offensive and not aura.get("positive"):
                continue
            if not offensive and aura.get("positive"):
                continue
            result.append(aura)
        return result

    async def _remove_aura_entries(self, kind: str, target_guid: int,
                                   removed: list) -> None:
        """Delete auras from a target and refresh its state/packets."""
        entries = self._aura_store(kind, target_guid)
        for aura in removed:
            if aura in entries:
                entries.remove(aura)
                if hasattr(self, "_remove_dots_for_aura"):
                    self._remove_dots_for_aura(
                        kind, target_guid, int(aura["spell_id"]))
                if hasattr(self, "_send_aura_update"):
                    await self._send_aura_update(
                        kind, target_guid, aura, remove=True)
        if kind == "player":
            if hasattr(self, "_refresh_player_auras"):
                await self._refresh_player_auras()
        else:
            low = creature.creature_low_guid(target_guid)
            if hasattr(self, "_sync_creature_flags"):
                await self._sync_creature_flags(low)

    async def _send_dispel_log(self, target_guid: int, removed: list) -> None:
        payload = bytearray()
        payload += spells.pack_guid(int(target_guid))
        payload += spells.pack_guid(int(self.player_guid))
        payload += struct.pack("<I", int(getattr(self, "_last_cast_spell_id", 0) or 0))
        payload += struct.pack("<B", 0)
        payload += struct.pack("<I", len(removed))
        for aura in removed:
            payload += struct.pack("<I", int(aura["spell_id"]))
            payload += struct.pack("<B", 0)
        await self.send_packet(Opcodes.SMSG_SPELLDISPELLOG, bytes(payload))

    async def _dispel_target(self, target_guid: int, dispel_type: int,
                             count: int = 1, offensive: bool = False) -> None:
        """Remove up to ``count`` matching auras and send the dispel log."""
        kind = self._spell_target_kind(target_guid)
        if kind == "peer":
            peer = self._peer_session_for_guid(target_guid)
            if peer is not None and hasattr(peer, "_dispel_target"):
                return await peer._dispel_target(
                    int(peer.player_guid), dispel_type, count,
                    offensive=offensive)
            return
        if hasattr(self, "_dispel_immune") and self._dispel_immune(
                kind, target_guid, int(dispel_type)):
            await self._send_dispel_failed(target_guid, dispel_type)
            return
        candidates = self._dispel_candidates(
            kind, target_guid, int(dispel_type), offensive)
        if not candidates:
            await self._send_dispel_failed(target_guid, dispel_type)
            return
        removed = candidates[:max(1, int(count))]
        await self._remove_aura_entries(kind, target_guid, removed)
        await self._send_dispel_log(target_guid, removed)

    async def _dispel_mechanic(self, target_guid: int,
                               mechanic: int) -> None:
        """``SPELL_EFFECT_DISPEL_MECHANIC``: strip auras of one mechanic."""
        if mechanic <= 0:
            return
        kind = self._spell_target_kind(target_guid)
        if kind == "peer":
            peer = self._peer_session_for_guid(target_guid)
            if peer is not None and hasattr(peer, "_dispel_mechanic"):
                return await peer._dispel_mechanic(
                    int(peer.player_guid), mechanic)
            return
        entries = self._aura_store(kind, target_guid)
        path = self.config.database.world_path
        removed = []
        for aura in entries:
            if int(aura.get("mechanic") or 0) == mechanic:
                removed.append(aura)
                continue
            template = spells.get_spell_template(
                path, int(aura.get("spell_id") or 0))
            if template is not None and int(template.mechanic) == mechanic:
                removed.append(aura)
        if not removed:
            return
        await self._remove_aura_entries(kind, target_guid, removed)
        await self._send_dispel_log(target_guid, removed)

    async def _send_dispel_failed(self, target_guid: int,
                                  dispel_type: int) -> None:
        payload = (struct.pack("<Q", int(self.player_guid))
                   + struct.pack("<Q", int(target_guid))
                   + struct.pack("<I", int(getattr(self, "_last_cast_spell_id", 0) or 0)))
        await self.send_packet(Opcodes.SMSG_DISPEL_FAILED, payload)

    async def _steal_beneficial(self, target_guid: int) -> None:
        """``SPELL_EFFECT_STEAL_BENEFICIAL``: move a buff from target to self."""
        kind = self._spell_target_kind(target_guid)
        if kind != "creature":
            return
        candidates = [aura for aura in self._dispel_candidates(
            kind, target_guid, spells.DISPEL_ALL, offensive=True)
            if aura.get("positive")]
        if not candidates:
            await self._send_dispel_failed(target_guid, spells.DISPEL_ALL)
            return
        aura = candidates[0]
        entries = self._aura_store(kind, target_guid)
        if aura in entries:
            entries.remove(aura)
        if hasattr(self, "_remove_dots_for_aura"):
            self._remove_dots_for_aura(kind, target_guid, int(aura["spell_id"]))
        await self._send_aura_update(kind, target_guid, aura, remove=True)
        # Re-apply the stolen aura on the caster with its remaining duration.
        target_entries = self._aura_store("player", self.player_guid)
        copy = dict(aura)
        copy["caster_guid"] = int(self.player_guid)
        copy["expires"] = time.monotonic() + min(
            10.0, max(1.0, float(aura.get("expires", 0)) - time.monotonic()))
        copy["max_duration"] = max(
            1.0, float(copy["expires"]) - time.monotonic())
        copy["caster_level"] = int(self.character.get("level", 1))
        target_entries.append(copy)
        self._normalise_aura_slots(target_entries)
        await self._refresh_player_auras()
        await self._send_aura_update("player", self.player_guid, copy)
        payload = bytearray()
        payload += spells.pack_guid(int(target_guid))
        payload += spells.pack_guid(int(self.player_guid))
        payload += struct.pack("<I", int(getattr(
            self, "_last_cast_spell_id", 0) or 0))
        payload += struct.pack("<B", 0)
        payload += struct.pack("<I", 1)
        payload += struct.pack("<I", int(aura["spell_id"]))
        payload += struct.pack("<B", 1)
        await self.send_packet(Opcodes.SMSG_SPELLSTEALLOG, bytes(payload))

    async def _spell_teleport(self, template) -> None:
        """Apply a ``spell_target_position`` teleport to the caster."""
        destination = spells.get_spell_target_position(
            self.config.database.world_path, int(template.id))
        if destination is None:
            self.log.debug(
                f"Teleport spell {template.id} has no spell_target_position")
            return
        map_id = int(destination["map"])
        x, y, z = (float(destination["x"]), float(destination["y"]),
                   float(destination["z"]))
        o = float(destination["o"])
        old_map = int(self.character.get("map", 0) or 0)
        zone = (int(self.character.get("zone", 0) or 0)
                if map_id == old_map else 0)
        self.character["map"] = map_id
        self.character["zone"] = zone
        if map_id == old_map and hasattr(self, "_teleport_nearby"):
            await self._teleport_nearby(x, y, z, o)
            return
        self.character["x"], self.character["y"] = x, y
        self.character["z"], self.character["o"] = z, o
        await run_db_write(
            character_db.update_character_position,
            self.config.database.world_path, int(self.player_guid),
            x, y, z, o, map_id=map_id, zone=zone)
        self._repop_pending = True
        self.movement_info = None
        await self.send_packet(
            Opcodes.SMSG_NEW_WORLD,
            login_packets.build_login_verify_world(self.character))

    async def _spell_send_event(self, event_id: int, template) -> None:
        """Dispatch hook for ``SPELL_EFFECT_SEND_EVENT`` (scripts come later)."""
        self.log.info(
            f"Spell {template.id} ({template.name}) sends event {event_id}")

    async def _apply_spell_effects(self, cast, template, target_guid: int) -> None:
        """Roll and apply every effect of a completed cast."""
        self._last_cast_spell_id = int(template.id)
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
        crafted = False
        aura_patterns: set = set()
        rolls: dict = {}
        weapon_proc_slot = None
        weapon_proc_delay = 2.0
        for index, effect in enumerate(template.effects):
            etype = int(effect["effect"])
            if not etype:
                continue
            if etype in (spells.SPELL_EFFECT_TELEPORT_UNITS,
                         spells.SPELL_EFFECT_TELEPORT_UNITS_FACE_CASTER,
                         spells.SPELL_EFFECT_TELEPORT_UNITS_SET_FACING):
                await self._spell_teleport(template)
                continue
            if etype in (spells.SPELL_EFFECT_APPLY_AREA_AURA_PARTY,
                         spells.SPELL_EFFECT_APPLY_AREA_AURA_RAID,
                         spells.SPELL_EFFECT_APPLY_AREA_AURA_OWNER):
                await self._apply_group_area_aura(
                    cast, template, index, effect, caster_level,
                    combo_points, etype)
                continue
            if etype in (spells.SPELL_EFFECT_APPLY_AREA_AURA_PET,
                         spells.SPELL_EFFECT_APPLY_AREA_AURA_FRIEND,
                         spells.SPELL_EFFECT_APPLY_AREA_AURA_ENEMY):
                await self._apply_nearby_area_aura(
                    cast, template, index, effect, caster_level,
                    combo_points, etype)
                continue
            if etype == spells.SPELL_EFFECT_JUMP_DEST:
                if cast is not None and getattr(cast, "has_dest", False):
                    await self._teleport_nearby(
                        float(cast.x), float(cast.y), float(cast.z),
                        float(self.character.get("o", 0.0) or 0.0))
                continue
            if etype == spells.SPELL_EFFECT_CHARGE:
                if target_guid:
                    await self._charge_target(target_guid)
                continue
            if etype == spells.SPELL_EFFECT_SPAWN:
                # ``SPELL_EFFECT_SPAWN`` is client-side only (TrinityCore
                # handles it with ``EffectUnused``); nothing to do server-side.
                continue
            if etype == spells.SPELL_EFFECT_OPEN_LOCK:
                if target_guid and gameobject.is_gameobject_guid(target_guid):
                    await self._open_lock(target_guid, int(template.id))
                continue
            if target_guid and gameobject.is_gameobject_guid(target_guid):
                if etype == spells.SPELL_EFFECT_ACTIVATE_OBJECT:
                    await self._spell_activate_gameobject(
                        target_guid, int(effect.get("misc_value") or 0))
                elif etype == spells.SPELL_EFFECT_GAMEOBJECT_DAMAGE:
                    amount = self._spell_effect_value(
                        template, index, effect, caster_level, combo_points)
                    await self._spell_damage_gameobject(
                        target_guid, amount, template)
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
                    combo_points, caster_level, rolls)
                continue
            chain_count = int(effect.get("chain_targets") or 0)
            if (chain_count > 1 and target_guid
                    and etype in DIRECT_DAMAGE_TYPES):
                await self._apply_chain_effect(
                    cast, template, index, effect, target_guid,
                    combo_points, caster_level, chain_count, rolls)
                continue

            # One hit/crit roll per hostile target and spell, shared by all of
            # its effects (Frostbolt's chill and damage, Flame Shock's direct
            # hit and DoT, ...).  Self and friendly effects never miss.
            harmful = self._effect_is_harmful(template, effect)
            roll = None
            if harmful and target_guid and not self._is_player_guid(target_guid):
                roll = await self._resolve_spell_target(
                    target_guid, template, effect, rolls)
                if roll is None:
                    continue
            critical = bool(roll and roll.get("crit"))

            if etype == spells.SPELL_EFFECT_SCHOOL_DAMAGE:
                if not target_guid:
                    continue
                amount = self._spell_effect_value(
                    template, index, effect, caster_level, combo_points,
                    damage=True)
                amount += self._spell_power_bonus(effect)
                amount = self._apply_damage_done(
                    int(template.school), max(0, amount))
                if critical:
                    amount = int(amount * 2.0)
                if amount <= 0:
                    continue
                if roll and roll.get("reflect"):
                    await self._damage_player(
                        amount, f"reflected {template.name}")
                    await self._send_spell_damage_log(
                        self.player_guid, target_guid, template.id, amount,
                        template.school or 1, critical=1 if critical else 0)
                    continue
                await self._deal_spell_damage(
                    target_guid, amount, template.school or 1, template,
                    player_guid, critical=critical)
            elif etype in (spells.SPELL_EFFECT_WEAPON_DAMAGE,
                           spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
                           spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
                           spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE):
                if not target_guid:
                    continue
                amount = self._direct_effect_damage(
                    etype, template, index, effect, caster_level, combo_points)
                amount = self._apply_damage_done(1, max(0, amount))
                if critical:
                    amount = int(amount * 2.0)
                await self._deal_spell_damage(
                    target_guid, amount, 1, template, player_guid,
                    critical=critical)
                weapon_proc_slot = 17 if template.uses_ranged_slot() else 15
                if weapon_proc_slot == 17:
                    ranged_stats = self._player_ranged_weapon_stats()
                    weapon_proc_delay = ranged_stats.get(
                        "base_delay", 2.0) if ranged_stats else 2.0
                else:
                    weapon_proc_delay = self._player_weapon_stats().get(
                        "base_delay", 2.0)
            elif etype == spells.SPELL_EFFECT_ATTACK:
                if not target_guid:
                    continue
                weapon = self._player_weapon_stats()
                amount = int(random.uniform(weapon["min"], weapon["max"]))
                amount += max(1, int(caster_level * 0.5))
                if critical:
                    amount *= 2
                await self._deal_spell_damage(
                    target_guid, max(1, amount), 1, template, player_guid,
                    critical=critical)
            elif etype in (spells.SPELL_EFFECT_HEAL,
                           spells.SPELL_EFFECT_HEAL_MAX_HEALTH):
                if etype == spells.SPELL_EFFECT_HEAL_MAX_HEALTH:
                    amount = self._max_health()
                else:
                    amount = self._spell_effect_value(
                        template, index, effect, caster_level, combo_points)
                    amount += self._spell_power_bonus(effect, healing=True)
                amount = int(amount * (
                    1.0 + float(self.character.get("healing_pct", 0.0) or 0.0)
                    / 100.0))
                heal_crit = False
                crit_chance = float(
                    self.character.get("spell_crit_chance", 0.0) or 0.0)
                if crit_chance and random.uniform(0.0, 100.0) < crit_chance:
                    amount = int(amount * 1.5)
                    heal_crit = True
                if amount <= 0:
                    continue
                peer = (self._peer_session_for_guid(target_guid)
                        if target_guid and hasattr(self, "_peer_session_for_guid")
                        else None)
                if peer is not None:
                    healed, overheal = await peer._heal_player(
                        amount, healer_guid=player_guid)
                else:
                    healed, overheal = await self._heal_player(amount)
                await self._send_spell_heal_log(
                    target_guid or player_guid, player_guid, template.id,
                    healed, overheal, critical=heal_crit,
                )
            elif etype == spells.SPELL_EFFECT_HEALTH_LEECH:
                if not target_guid:
                    continue
                amount = self._spell_effect_value(
                    template, index, effect, caster_level, combo_points,
                    damage=True)
                amount += self._spell_power_bonus(effect)
                if critical:
                    amount = int(amount * 2.0)
                if amount <= 0:
                    continue
                await self._deal_spell_damage(
                    target_guid, amount, template.school or 1, template,
                    player_guid, critical=critical)
                healed, _ = await self._heal_player(amount)
                await self._send_spell_heal_log(
                    player_guid, player_guid, template.id, healed, 0
                )
            elif etype == spells.SPELL_EFFECT_HEAL_PCT:
                target = int(target_guid or self.player_guid)
                maximum = self._target_max_health(target)
                if maximum <= 0:
                    continue
                percent = max(0, self._spell_effect_value(
                    template, index, effect, caster_level, combo_points))
                amount = int(maximum * percent / 100.0)
                amount += int(self.character.get("healing", 0) or 0)
                amount = int(amount * (
                    1.0 + float(self.character.get("healing_pct", 0.0) or 0.0)
                    / 100.0))
                await self._heal_unit(target, amount, template)
            elif etype == spells.SPELL_EFFECT_POWER_BURN:
                if not target_guid:
                    continue
                burned = await self._burn_target_power(
                    target_guid, template, index, effect, caster_level,
                    combo_points)
                if burned <= 0:
                    continue
                if critical:
                    burned = int(burned * 2.0)
                await self._deal_spell_damage(
                    target_guid, burned, template.school or 1, template,
                    player_guid, critical=critical)
            elif etype == spells.SPELL_EFFECT_INSTAKILL:
                if target_guid:
                    await self._deal_spell_damage(
                        target_guid, 1 << 24, template.school or 1, template,
                        player_guid)
            elif etype == spells.SPELL_EFFECT_THREAT:
                if target_guid and not self._is_player_guid(target_guid):
                    amount = self._spell_effect_value(
                        template, index, effect, caster_level, combo_points)
                    amount = int(self._spell_mod_value(
                        template, spells.SPELLMOD_THREAT, amount))
                    manager = (self._creature_combat_manager()
                               if hasattr(self, "_creature_combat_manager")
                               else None)
                    if manager is not None:
                        manager.modify_threat(
                            creature.creature_low_guid(target_guid),
                            player_guid, amount)
            elif etype == spells.SPELL_EFFECT_MODIFY_THREAT_PERCENT:
                if target_guid and not self._is_player_guid(target_guid):
                    percent = self._spell_effect_value(
                        template, index, effect, caster_level, combo_points)
                    manager = (self._creature_combat_manager()
                               if hasattr(self, "_creature_combat_manager")
                               else None)
                    if manager is not None:
                        manager.modify_threat_percent(
                            creature.creature_low_guid(target_guid),
                            player_guid, percent)
            elif etype == spells.SPELL_EFFECT_ATTACK_ME:
                if target_guid and not self._is_player_guid(target_guid):
                    manager = (self._creature_combat_manager()
                               if hasattr(self, "_creature_combat_manager")
                               else None)
                    if manager is not None:
                        self._aggro_creature(
                            creature.creature_low_guid(target_guid))
                        manager.taunt(
                            creature.creature_low_guid(target_guid),
                            player_guid, duration=3.0)
            elif etype == spells.SPELL_EFFECT_REDIRECT_THREAT:
                if target_guid and (self._is_player_guid(target_guid)
                                    or (hasattr(self, "_is_peer_player_guid")
                                        and self._is_peer_player_guid(
                                            target_guid))):
                    if not hasattr(self, "_is_group_member_with") \
                            or self._is_group_member_with(target_guid):
                        duration_ms = spells.spell_duration_ms(
                            self.config.database.world_path,
                            template.duration_index, caster_level)
                        if duration_ms <= 0:
                            duration_ms = 30_000
                        self._get_combat_state()["threat_redirect"] = {
                            "guid": int(target_guid),
                            "until": time.monotonic()
                            + duration_ms / 1000.0,
                        }
            elif etype == spells.SPELL_EFFECT_KNOCK_BACK:
                if target_guid:
                    await self._apply_knockback(
                        target_guid, int(template.effect_value(
                            index, caster_level) or 20),
                        template.school or 1)
            elif etype == spells.SPELL_EFFECT_INTERRUPT_CAST:
                if target_guid:
                    await self._interrupt_target_cast(
                        target_guid, int(template.school) or 1)
            elif etype == spells.SPELL_EFFECT_DISPEL:
                if target_guid:
                    count = int(effect.get("base_points") or 0) + 1
                    offensive = (not self._is_player_guid(target_guid)
                                 and not (hasattr(self, "_is_peer_player_guid")
                                          and self._is_peer_player_guid(
                                              target_guid)))
                    await self._dispel_target(
                        target_guid, int(effect.get("misc_value") or 0),
                        max(1, count), offensive=offensive)
            elif etype == spells.SPELL_EFFECT_DISPEL_MECHANIC:
                if target_guid:
                    await self._dispel_mechanic(
                        target_guid, int(effect.get("misc_value") or 0))
            elif etype == spells.SPELL_EFFECT_STEAL_BENEFICIAL:
                if target_guid:
                    await self._steal_beneficial(target_guid)
            elif etype in (spells.SPELL_EFFECT_ENCHANT_ITEM,
                           spells.SPELL_EFFECT_ENCHANT_ITEM_TEMPORARY):
                await self._apply_weapon_enchant(cast, template, effect)
            elif etype == spells.SPELL_EFFECT_ENERGIZE:
                amount = self._spell_effect_value(
                    template, index, effect, caster_level, combo_points)
                if amount <= 0:
                    continue
                power_type = int(effect.get("misc_value") or 0)
                victim = int(target_guid or self.player_guid)
                if self._is_player_guid(victim):
                    if power_type == self._power_type():
                        current = int(self.character.get("power", 0))
                        await self._set_player_power(
                            min(self._max_power(), current + amount))
                await self._send_spell_energize_log(
                    victim, player_guid, template.id, power_type, amount)
            elif etype in (spells.SPELL_EFFECT_CREATE_ITEM,
                           spells.SPELL_EFFECT_CREATE_ITEM_2):
                item_entry = int(effect.get("item_type") or 0)
                if not item_entry:
                    continue
                _category, max_stack = spells.item_tool_fields(
                    self.config.database.world_path, item_entry)
                count = min(max(1, int(effect.get("base_points") or 0) + 1),
                            max_stack)
                changes = await run_db_write(
                    character_db.add_item_to_inventory,
                    self.config.database.world_path, int(self.player_guid),
                    item_entry, count)
                if changes:
                    crafted = True
                    if hasattr(self, "_deliver_item_changes"):
                        await self._deliver_item_changes(item_entry, changes)
                elif hasattr(self, "_send_inventory_failure"):
                    await self._send_inventory_failure(
                        int(getattr(self, "INVENTORY_FULL", 50)))
            elif etype == spells.SPELL_EFFECT_TRADE_SKILL:
                continue                    # recipe marker; item effect follows
            elif etype == spells.SPELL_EFFECT_SKILL:
                if hasattr(self, "_apply_trainer_skill"):
                    await self._apply_trainer_skill(template)
            elif etype == spells.SPELL_EFFECT_APPLY_GLYPH:
                glyph_id = int(effect.get("misc_value") or 0)
                glyph = spells.get_glyph_properties(
                    self.config.database.world_path, glyph_id)
                if glyph and glyph["spell_id"]:
                    await self._cast_triggered(
                        int(glyph["spell_id"]), int(self.player_guid))
                    self.character.setdefault("glyphs", {})[
                        int(glyph_id)] = int(glyph["spell_id"])
            elif etype in (spells.SPELL_EFFECT_FORCE_CAST,
                           spells.SPELL_EFFECT_FORCE_CAST_WITH_VALUE,
                           spells.SPELL_EFFECT_FORCE_CAST_2):
                triggered = int(effect.get("trigger_spell") or 0)
                if triggered:
                    target = int(target_guid or self.player_guid)
                    peer = (self._peer_session_for_guid(target)
                            if hasattr(self, "_peer_session_for_guid")
                            else None)
                    if peer is not None and peer is not self:
                        await peer._cast_triggered(triggered, target)
                    else:
                        await self._cast_triggered(triggered, target)
            elif etype == spells.SPELL_EFFECT_LEARN_SPELL:
                learned = int(effect.get("trigger_spell") or 0)
                if learned and hasattr(self, "_learn_spell"):
                    await self._learn_spell(learned)
            elif etype == spells.SPELL_EFFECT_SKILL_STEP:
                if hasattr(self, "_apply_trainer_skill"):
                    await self._apply_trainer_skill(template)
            elif etype == spells.SPELL_EFFECT_PROFICIENCY:
                if hasattr(self, "_apply_trainer_proficiency"):
                    await self._apply_trainer_proficiency(template)
            elif etype == spells.SPELL_EFFECT_QUEST_COMPLETE:
                quest_id = int(effect.get("misc_value") or 0)
                if quest_id and hasattr(self, "_spell_complete_quest"):
                    await self._spell_complete_quest(quest_id)
            elif etype == spells.SPELL_EFFECT_SEND_EVENT:
                event_id = int(effect.get("misc_value") or 0)
                if event_id:
                    await self._spell_send_event(event_id, template)
            elif etype == spells.SPELL_EFFECT_INEBRIATE:
                amount = max(0, self._spell_effect_value(
                    template, index, effect, caster_level, combo_points))
                if amount <= 0:
                    continue
                victim = int(target_guid or self.player_guid)
                kind = self._spell_target_kind(victim)
                item_entry = self._cast_item_entry(cast)
                if kind == "player" and hasattr(self, "_set_drunk_value"):
                    await self._set_drunk_value(
                        int(self.character.get("drunk", 0) or 0) + amount,
                        item_entry)
                elif kind == "peer":
                    peer = self._peer_session_for_guid(victim)
                    if peer is not None and hasattr(peer, "_set_drunk_value"):
                        await peer._set_drunk_value(
                            int(peer.character.get("drunk", 0) or 0) + amount,
                            item_entry)
            elif etype in (spells.SPELL_EFFECT_KILL_CREDIT,
                           spells.SPELL_EFFECT_KILL_CREDIT_2):
                entry = int(effect.get("misc_value") or 0)
                if not entry:
                    continue
                if hasattr(self, "_quest_credit_kill_entry"):
                    await self._quest_credit_kill_entry(entry, 0)
                if (etype == spells.SPELL_EFFECT_KILL_CREDIT_2
                        and hasattr(self, "_group_member_sessions")):
                    center = self._player_position()
                    for peer in self._group_member_sessions(
                            exclude_self=True):
                        character = getattr(peer, "character", None)
                        if not character:
                            continue
                        pos = (float(character.get("x", 0.0) or 0.0),
                               float(character.get("y", 0.0) or 0.0),
                               float(character.get("z", 0.0) or 0.0))
                        if self._distance(center, pos) > 100.0:
                            continue
                        if hasattr(peer, "_quest_credit_kill_entry"):
                            await peer._quest_credit_kill_entry(entry, 0)
            elif etype in (spells.SPELL_EFFECT_TRIGGER_SPELL,
                           spells.SPELL_EFFECT_TRIGGER_SPELL_WITH_VALUE,
                           spells.SPELL_EFFECT_TRIGGER_SPELL_2):
                triggered = int(effect["trigger_spell"])
                if triggered:
                    await self._cast_triggered(triggered, target_guid)
            elif etype in (spells.SPELL_EFFECT_TRIGGER_MISSILE,
                           spells.SPELL_EFFECT_TRIGGER_MISSILE_SPELL_WITH_VALUE):
                triggered = int(effect["trigger_spell"])
                if triggered:
                    await self._cast_triggered(
                        triggered, target_guid or self.player_guid)
            elif etype == spells.SPELL_EFFECT_TRANSMITTED_OBJECT:
                entry = int(effect.get("misc_value") or 0)
                if entry and hasattr(self, "_summon_gameobject"):
                    duration = spells.summon_duration_ms(
                        self.config.database.world_path,
                        template.duration_index, caster_level)
                    if cast is not None and getattr(cast, "has_dest", False):
                        x, y, z = (float(cast.x), float(cast.y),
                                   float(cast.z))
                    else:
                        x, y, z = self._player_position()
                    await self._summon_gameobject(
                        entry, x, y, z,
                        float(self.character.get("o", 0.0) or 0.0),
                        duration)
            elif etype in (spells.SPELL_EFFECT_SUMMON_OBJECT_WILD,
                           spells.SPELL_EFFECT_SUMMON_OBJECT_SLOT1,
                           spells.SPELL_EFFECT_SUMMON_OBJECT_SLOT2,
                           spells.SPELL_EFFECT_SUMMON_OBJECT_SLOT3,
                           spells.SPELL_EFFECT_SUMMON_OBJECT_SLOT4):
                entry = int(effect.get("misc_value") or 0)
                if entry and hasattr(self, "_summon_gameobject"):
                    duration = spells.summon_duration_ms(
                        self.config.database.world_path,
                        template.duration_index, caster_level)
                    x, y, z = (cast.x, cast.y, cast.z) if (
                        cast is not None and getattr(cast, "has_dest", False)
                    ) else self._player_position()
                    await self._summon_gameobject(
                        entry, x, y, z,
                        float(self.character.get("o", 0.0) or 0.0),
                        duration)
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
            elif etype == spells.SPELL_EFFECT_ADD_COMBO_POINTS:
                # ``Spell::EffectAddComboPoints``: the effect's own rolled value
                # is the number of points added (Sinister Strike adds 1).
                if not target_guid:
                    continue
                points = template.effect_value(index, caster_level)
                if points > 0:
                    await self._add_combo_points(target_guid, points)
            elif etype == spells.SPELL_EFFECT_APPLY_AURA:
                if roll and roll.get("reflect"):
                    continue
                if roll is None and harmful and target_guid \
                        and not self._is_player_guid(target_guid):
                    continue
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
        if crafted and hasattr(self, "_advance_craft_skill"):
            await self._advance_craft_skill(template)
        # Poison/item procs are checked once per weapon ability, not once per
        # effect (a finisher with damage + combo points should only roll once).
        if (weapon_proc_slot and target_guid
                and hasattr(self, "_roll_weapon_procs")):
            await self._roll_weapon_procs(
                weapon_proc_slot, weapon_proc_delay, target_guid)

    # ------------------------------------------------------------------
    # Area of effect (AoE)
    # ------------------------------------------------------------------
    def _direct_effect_damage(self, etype: int, template, index: int,
                              effect: dict, caster_level: int,
                              combo_points: int = 0) -> int:
        """Damage one target of a direct-damage effect would take."""
        if etype in (spells.SPELL_EFFECT_SCHOOL_DAMAGE,
                     spells.SPELL_EFFECT_HEALTH_LEECH):
            return max(0, self._spell_effect_value(
                template, index, effect, caster_level, combo_points,
                damage=True))
        weapon = self._player_weapon_stats()
        roll = random.uniform(weapon["min"], weapon["max"])
        if etype == spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE:
            percent = (int(effect.get("base_points") or 0) + 1) / 100.0
            return max(1, int(roll * percent))
        if etype == spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG:
            # ``WeaponDamageRange`` normalized to a 2.4 s weapon, then the
            # ability's flat bonus (Mortal Strike, Bloodthirst, ...).
            delay = max(0.5, float(weapon.get("delay", 2.0) or 2.0))
            normalized = (roll / delay) * 2.4
            normalized += (int(self.character.get("attack_power", 0) or 0)
                           / 14.0 * 2.4)
            return max(1, int(normalized + self._spell_effect_value(
                template, index, effect, caster_level, combo_points,
                damage=True)))
        amount = int(roll)
        amount += self._spell_effect_value(
            template, index, effect, caster_level, combo_points,
            damage=True)
        amount += max(1, int(caster_level * 0.5))
        amount += (int(self.character.get("attack_power", 0) or 0) / 14.0
                   * float(weapon.get("delay", 2.0) or 2.0))
        return max(1, int(amount))

    def _group_area_sessions(self, raid: bool):
        """Sessions that receive an ``APPLY_AREA_AURA_PARTY/RAID`` effect."""
        manager = (self._group_manager()
                   if hasattr(self, "_group_manager") else None)
        if manager is None or not self.player_guid:
            return [self]
        group = manager.group_for(int(self.player_guid))
        if group is None:
            return [self]
        map_id = int((self.character or {}).get("map", 0) or 0)
        sessions = list(manager.member_sessions(group, map_id))
        if group.raid and not raid:
            member = group.members.get(int(self.player_guid))
            subgroup = int(getattr(member, "subgroup", 0) or 0)
            sessions = [
                session for session in sessions
                if int(getattr(
                    group.members.get(int(session.player_guid)), "subgroup",
                    subgroup) or 0) == subgroup
            ]
        if self not in sessions:
            sessions.append(self)
        return sessions

    async def _apply_group_area_aura(self, cast, template, index: int,
                                     effect: dict, caster_level: int,
                                     combo_points: int,
                                     etype: int) -> None:
        """Apply one party/raid area aura to eligible group members."""
        if etype == spells.SPELL_EFFECT_APPLY_AREA_AURA_OWNER:
            sessions = [self]
        else:
            sessions = self._group_area_sessions(
                etype == spells.SPELL_EFFECT_APPLY_AREA_AURA_RAID)
        radius = template.effect_radius(index)
        if radius <= 0.0:
            radius = 100.0
        center = self._area_center(template, index, cast, 0)
        for session in sessions:
            target_guid = int(getattr(session, "player_guid", 0) or 0)
            if not target_guid:
                continue
            if session is not self:
                character = getattr(session, "character", None)
                if not character:
                    continue
                pos = (float(character.get("x", 0.0) or 0.0),
                       float(character.get("y", 0.0) or 0.0),
                       float(character.get("z", 0.0) or 0.0))
                if self._distance(center, pos) > radius + 0.5:
                    continue
            if hasattr(session, "_apply_periodic_aura"):
                session._apply_periodic_aura(
                    template, effect, index, caster_level, target_guid,
                    combo_points)
            if hasattr(session, "_apply_aura_from_effect"):
                await session._apply_aura_from_effect(
                    template, effect, index, self.player_guid, target_guid,
                    caster_level, combo_points, target_kind="player")

    async def _apply_nearby_area_aura(self, cast, template, index: int,
                                      effect: dict, caster_level: int,
                                      combo_points: int,
                                      etype: int) -> None:
        """Apply friend/enemy/pet area auras (effects 128/129/119)."""
        radius = template.effect_radius(index)
        if radius <= 0.0:
            radius = 100.0
        center = self._area_center(template, index, cast, 0)

        async def apply(target_kind: str, target_guid: int,
                        session=None) -> None:
            owner = session if session is not None else self
            if hasattr(owner, "_apply_periodic_aura"):
                owner._apply_periodic_aura(
                    template, effect, index, caster_level, target_guid,
                    combo_points)
            if hasattr(owner, "_apply_aura_from_effect"):
                await owner._apply_aura_from_effect(
                    template, effect, index, int(self.player_guid),
                    target_guid, caster_level, combo_points,
                    target_kind=target_kind)

        if etype == spells.SPELL_EFFECT_APPLY_AREA_AURA_PET:
            pet = (self._active_pet() if hasattr(self, "_active_pet")
                   else None)
            if pet is None:
                return
            low, spawn, _state = pet
            guid = self._creature_object_guid(low, int(spawn["entry"]))
            await apply("creature", int(guid))
            return

        if etype == spells.SPELL_EFFECT_APPLY_AREA_AURA_FRIEND:
            await apply("player", int(self.player_guid))
            for peer in (self.peers() if hasattr(self, "peers") else ()):
                peer_pos = (
                    float(peer.character.get("x", 0.0) or 0.0),
                    float(peer.character.get("y", 0.0) or 0.0),
                    float(peer.character.get("z", 0.0) or 0.0))
                if self._distance(center, peer_pos) > radius + 0.5:
                    continue
                await apply("player", int(peer.player_guid), session=peer)
            return

        # Enemy area aura: debuff hostile creatures around the caster.  NPCs
        # with service flags (vendors, trainers, questgivers) are not hostile.
        for low, spawn in self._select_area_targets(center, radius, "enemy"):
            if int(spawn.get("npc_flags") or 0):
                continue
            guid = self._creature_object_guid(low, int(spawn["entry"]))
            await apply("creature", int(guid))

    def _cast_item_entry(self, cast) -> int:
        """Item entry of the cast item, used for inebriation log packets."""
        item_guid = int(getattr(cast, "item_guid", 0) or 0)
        if not item_guid:
            return 0
        for item in ((self.character or {}).get("inventory") or {}).values():
            if int(item.get("guid") or 0) == item_guid:
                return int(item.get("item_entry") or 0)
        return 0

    def _creature_power_state(self, low_guid: int):
        """``(ai_or_like, current, maximum, power_type)`` for a creature."""
        combat = self._get_combat_state()
        state = combat.get("aggro", {}).get(int(low_guid))
        ai = state.get("ai") if state is not None else None
        if ai is not None:
            return ai, int(ai["mana"]), int(ai["max_mana"]), \
                int(ai.get("power_type") or 0)
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None:
            return None, 0, 0, 0
        level = max(1, int(spawn["min_level"] or 1)
                    if "min_level" in spawn.keys() else 1)
        unit_class = int(spawn["unit_class"] or 1) \
            if "unit_class" in spawn.keys() else 1
        max_mana = max(0, int(
            spells.get_creature_base_mana(
                self.config.database.world_path, level, unit_class)
            * float(spawn["energy_multiplier"] or 1.0)))
        current = int(spawn["current_power"] or 0)
        if not 0 < current <= max_mana:
            current = max_mana
        power_type = int(spawn["unit_power_type"] or 0) \
            if "unit_power_type" in spawn.keys() else 0
        return {
            "entry": int(spawn["entry"]),
            "power_type": power_type,
            "mana": current,
            "max_mana": max_mana,
        }, current, max_mana, power_type

    async def _burn_target_power(self, target_guid: int, template,
                                 index: int, effect: dict,
                                 caster_level: int,
                                 combo_points: int) -> int:
        """Burn the effect's power type and return the damage it causes."""
        power_type = int(effect.get("misc_value") or 0)
        kind = self._spell_target_kind(target_guid)
        if kind in ("player", "peer"):
            owner = self if kind == "player" else \
                self._peer_session_for_guid(target_guid)
            if owner is None or not owner.character:
                return 0
            if int(owner._power_type()) != power_type:
                return 0
            current = int(owner.character.get("power", 0))
            maximum = int(owner._max_power())
        else:
            low = creature.creature_low_guid(int(target_guid))
            ai, current, maximum, creature_type = self._creature_power_state(low)
            if ai is None or creature_type != power_type:
                return 0
        burn = max(0, int(self._spell_effect_value(
            template, index, effect, caster_level, combo_points)))
        if int(template.id) == 8129:
            # Mana Burn: burn a percentage of the target's pool, capped at
            # twice that percentage of the caster's own pool.
            pct = max(0, burn)
            burn = int(maximum * pct / 100.0)
            burn = min(burn, int(
                self._max_power() * (pct * 2) / 100.0))
        burn = min(burn, current)
        if burn <= 0:
            return 0
        if kind == "player":
            await self._set_player_power(current - burn)
        elif kind == "peer":
            await self._peer_session_for_guid(target_guid)._set_player_power(
                current - burn)
        else:
            ai["mana"] = current - burn
            self._set_creature_power(low, ai["mana"])
            await self._send_creature_power(low, ai)
        multiplier = float(effect.get("multiple_value") or 1.0)
        if multiplier <= 0.0:
            multiplier = 1.0
        return int(burn * multiplier)

    async def _heal_unit(self, target_guid: int, amount: int, template,
                         critical: bool = False) -> None:
        """Heal a player, peer or creature target and send the heal log."""
        amount = max(0, int(amount))
        if amount <= 0:
            return
        kind = self._spell_target_kind(target_guid)
        player_guid = int(self.player_guid)
        if kind == "peer":
            peer = (self._peer_session_for_guid(target_guid)
                    if hasattr(self, "_peer_session_for_guid") else None)
            if peer is None:
                return
            healed, overheal = await peer._heal_player(
                amount, healer_guid=player_guid)
            await self._send_spell_heal_log(
                target_guid, player_guid, template.id, healed, overheal,
                critical=critical)
            return
        if kind == "player":
            healed, overheal = await self._heal_player(amount)
            await self._send_spell_heal_log(
                target_guid or player_guid, player_guid, template.id,
                healed, overheal, critical=critical)
            return
        low = creature.creature_low_guid(int(target_guid))
        spawn = self._get_creature_spawn(low)
        if spawn is None:
            return
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
            target_guid, player_guid, template.id, max(0, healed),
            max(0, amount - healed), critical=critical)

    async def _charge_target(self, target_guid: int) -> None:
        """``SPELL_EFFECT_CHARGE``: move next to the target and attack it."""
        kind = self._spell_target_kind(target_guid)
        if kind == "peer":
            peer = (self._peer_session_for_guid(target_guid)
                    if hasattr(self, "_peer_session_for_guid") else None)
            if peer is None or not peer.character:
                return
            target = (float(peer.character.get("x", 0.0) or 0.0),
                      float(peer.character.get("y", 0.0) or 0.0),
                      float(peer.character.get("z", 0.0) or 0.0))
            reach = 1.5
        elif kind == "creature":
            low = creature.creature_low_guid(int(target_guid))
            spawn = self._get_creature_spawn(low)
            if spawn is None:
                return
            target = self._creature_position(low)
            reach = max(0.5, self._melee_reach(spawn))
        else:
            return
        caster = self._player_position()
        dx = caster[0] - target[0]
        dy = caster[1] - target[1]
        length = max(0.001, (dx * dx + dy * dy) ** 0.5)
        stop = max(0.4, reach - 0.3)
        x = target[0] + dx / length * stop
        y = target[1] + dy / length * stop
        facing = math.atan2(target[1] - y, target[0] - x)
        await self._teleport_nearby(x, y, target[2], facing)
        if kind == "creature":
            # ``EffectCharge`` attacks once the movement phase completes.
            await self.handle_attack_swing(struct.pack("<Q", int(target_guid)))

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
                                 combo_points: int, caster_level: int,
                                 rolls: dict | None = None) -> None:
        """Apply one direct/aura effect to every unit in its area or cone."""
        etype = int(effect["effect"])
        player_guid = int(self.player_guid)
        rolls = rolls if rolls is not None else {}
        if kind == "ally":
            # Party/raid membership is not modelled, so every friendly player
            # in the area receives the effect (and the caster is always in it).
            radius = max(5.0, template.effect_radius(index) or 10.0)
            center = self._area_center(template, index, cast, target_guid)
            if etype in (spells.SPELL_EFFECT_HEAL,
                         spells.SPELL_EFFECT_HEAL_MAX_HEALTH):
                amount = (self._max_health()
                          if etype == spells.SPELL_EFFECT_HEAL_MAX_HEALTH
                          else template.effect_value(
                              index, caster_level, combo_points)
                          + self._spell_power_bonus(effect, healing=True))
                if amount > 0:
                    healed, overheal = await self._heal_player(amount)
                    await self._send_spell_heal_log(
                        player_guid, player_guid, template.id, healed, overheal)
                    for peer in (self._group_member_sessions(
                            exclude_self=True)
                            if hasattr(self, "_group_member_sessions")
                            else ()):
                        peer_pos = (
                            float(peer.character.get("x", 0.0)),
                            float(peer.character.get("y", 0.0)),
                            float(peer.character.get("z", 0.0)))
                        if self._distance(center, peer_pos) > radius + 0.5:
                            continue
                        peer_healed, peer_overheal = await peer._heal_player(
                            amount, healer_guid=player_guid)
                        await self._send_spell_heal_log(
                            int(peer.player_guid), player_guid, template.id,
                            peer_healed, peer_overheal)
            elif etype == spells.SPELL_EFFECT_APPLY_AURA:
                self._apply_periodic_aura(template, effect, index, caster_level,
                                          player_guid, combo_points)
                if hasattr(self, "_apply_aura_from_effect"):
                    await self._apply_aura_from_effect(
                        template, effect, index, player_guid, player_guid,
                        caster_level, combo_points, target_kind="player")
                    for peer in (self._group_member_sessions(
                            exclude_self=True)
                            if hasattr(self, "_group_member_sessions")
                            else ()):
                        peer_pos = (
                            float(peer.character.get("x", 0.0)),
                            float(peer.character.get("y", 0.0)),
                            float(peer.character.get("z", 0.0)))
                        if self._distance(center, peer_pos) > radius + 0.5:
                            continue
                        if hasattr(peer, "_apply_periodic_aura"):
                            peer._apply_periodic_aura(
                                template, effect, index, caster_level,
                                int(peer.player_guid), combo_points)
                        await peer._apply_aura_from_effect(
                            template, effect, index, player_guid,
                            int(peer.player_guid), caster_level, combo_points,
                            target_kind="player")
            return
        radius = template.effect_radius(index)
        if radius <= 0.0:
            radius = 10.0
        center = self._area_center(template, index, cast, target_guid)
        targets = self._select_area_targets(center, radius, kind,
                                            template.max_targets)
        if not targets:
            return
        base_amount = 0
        if etype in DIRECT_DAMAGE_TYPES:
            base_amount = self._direct_effect_damage(
                etype, template, index, effect, caster_level, combo_points)
            if etype in (spells.SPELL_EFFECT_SCHOOL_DAMAGE,
                         spells.SPELL_EFFECT_HEALTH_LEECH):
                base_amount += self._spell_power_bonus(effect)
            base_amount = self._apply_damage_done(
                int(template.school) or 1, base_amount)
        for low_guid, spawn in targets:
            guid = self._creature_object_guid(low_guid, int(spawn["entry"]))
            if etype in (spells.SPELL_EFFECT_SCHOOL_DAMAGE,
                         spells.SPELL_EFFECT_WEAPON_DAMAGE,
                         spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
                         spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
                         spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE):
                if base_amount <= 0:
                    continue
                roll = await self._resolve_spell_target(
                    guid, template, effect, rolls)
                if roll is None:
                    continue
                amount = int(base_amount * (2.0 if roll.get("crit") else 1.0))
                if roll.get("reflect"):
                    await self._damage_player(
                        amount, f"reflected {template.name}")
                    continue
                await self._deal_spell_damage(
                    guid, amount, template.school or 1, template, player_guid,
                    critical=bool(roll.get("crit")))
            elif etype == spells.SPELL_EFFECT_HEALTH_LEECH:
                if base_amount <= 0:
                    continue
                roll = await self._resolve_spell_target(
                    guid, template, effect, rolls)
                if roll is None:
                    continue
                amount = int(base_amount * (2.0 if roll.get("crit") else 1.0))
                await self._deal_spell_damage(
                    guid, amount, template.school or 1, template, player_guid,
                    critical=bool(roll.get("crit")))
                healed, _ = await self._heal_player(amount)
                await self._send_spell_heal_log(
                    player_guid, player_guid, template.id, healed, 0)
            elif etype == spells.SPELL_EFFECT_APPLY_AURA:
                if self._effect_is_harmful(template, effect):
                    roll = await self._resolve_spell_target(
                        guid, template, effect, rolls)
                    if roll is None:
                        continue
                self._apply_periodic_aura(template, effect, index, caster_level,
                                          guid, combo_points)
                if hasattr(self, "_apply_aura_from_effect"):
                    await self._apply_aura_from_effect(
                        template, effect, index, player_guid, guid,
                        caster_level, combo_points, target_kind="creature")

    async def _apply_chain_effect(self, cast, template, index: int,
                                  effect: dict, target_guid: int,
                                  combo_points: int, caster_level: int,
                                  chain_count: int,
                                  rolls: dict | None = None) -> None:
        """Chain Lightning/Multi-Shot: primary target plus nearby jumps."""
        etype = int(effect["effect"])
        player_guid = int(self.player_guid)
        rolls = rolls if rolls is not None else {}
        if etype not in (spells.SPELL_EFFECT_SCHOOL_DAMAGE,
                         spells.SPELL_EFFECT_WEAPON_DAMAGE,
                         spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
                         spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
                         spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE):
            return
        base = self._direct_effect_damage(
            etype, template, index, effect, caster_level, combo_points)
        if etype == spells.SPELL_EFFECT_SCHOOL_DAMAGE:
            base += self._spell_power_bonus(effect)
        base = self._apply_damage_done(int(template.school) or 1, base)
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
            roll = await self._resolve_spell_target(
                current, template, effect, rolls)
            if roll is not None:
                critical = bool(roll.get("crit"))
                if critical:
                    amount *= 2
                await self._deal_spell_damage(
                    current, amount, template.school or 1, template,
                    player_guid, critical=critical)
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
        duration_ms = int(self._spell_mod_value(
            template, spells.SPELLMOD_DURATION, duration_ms))
        interval = max(250, int(effect.get("amplitude") or 1000)) / 1000.0
        amount = self._spell_effect_value(
            template, index, effect, caster_level,
            damage=int(effect.get("apply_aura") or 0)
            == spells.SPELL_AURA_PERIODIC_DAMAGE)
        school = int(template.school) or 1
        aura = int(effect.get("apply_aura") or 0)
        slow_amount = 0
        slow_spell_id = 0
        if aura == spells.SPELL_AURA_MOD_DECREASE_SPEED:
            slow_amount = abs(amount)
            slow_spell_id = int(template.id)
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
                if int(trigger_effect.get("apply_aura") or 0) == \
                        spells.SPELL_AURA_MOD_DECREASE_SPEED:
                    slow_amount = abs(triggered.effect_value(
                        trigger_index, caster_level))
                    slow_spell_id = int(triggered.id)
            interval = max(250, int(other.get("amplitude") or 1000)) / 1000.0
            break
        if aura in (spells.SPELL_AURA_PERIODIC_DAMAGE,
                    spells.SPELL_AURA_PERIODIC_DUMMY):
            interval = max(250, int(effect.get("amplitude") or 1000)) / 1000.0
        if amount <= 0:
            return
        if caster_guid is None or int(caster_guid) == int(self.player_guid or 0):
            amount = self._apply_damage_done(int(school) or 1, int(amount))
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
            "slow_amount": int(slow_amount or 0),
            "slow_spell_id": int(slow_spell_id or 0),
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
                    if int(area.get("slow_amount") or 0):
                        await self._apply_ground_slow(
                            low_guid, area)
                if (area.get("hostile_to_player")
                        and self._distance(area["center"],
                                           self._player_position())
                        <= area["radius"] + 0.5):
                    trigger = spells.get_spell_template(
                        self.config.database.world_path,
                        int(area["spell_id"]))
                    if trigger is not None and hasattr(self, "_deal_spell_damage"):
                        await self._deal_spell_damage(
                            int(self.player_guid), int(area["amount"]),
                            int(area["school"]) or 1, trigger,
                            int(area["caster_guid"]), periodic=True)
                    else:
                        await self._damage_player(
                            area["amount"], f"spell {area['spell_id']}")
                        await self._send_spell_damage_log(
                            int(self.player_guid), int(area["caster_guid"]),
                            int(area["spell_id"]), int(area["amount"]),
                            int(area["school"]) or 1, periodic=1)
            alive.append(area)
        combat["area_effects"] = alive

    async def _apply_ground_slow(self, low_guid: int, area: dict) -> None:
        """Refresh a short movement-slow aura from a ground effect tick."""
        if not hasattr(self, "_aura_store"):
            return
        now = time.monotonic()
        entries = self._aura_store("creature", low_guid)
        spell_id = int(area.get("slow_spell_id") or area["spell_id"])
        component = {
            "type": spells.SPELL_AURA_MOD_DECREASE_SPEED,
            "amount": -abs(int(area.get("slow_amount") or 0)),
            "misc": 0,
            "misc_b": 0,
            "amplitude": 0,
            "trigger_spell": 0,
        }
        for aura in entries:
            if int(aura["spell_id"]) == spell_id:
                aura["components"] = [component]
                aura["expires"] = now + float(area["interval"]) + 0.5
                aura["max_duration"] = float(area["interval"]) + 0.5
                return
        entry = {
            "spell_id": spell_id,
            "name": "Ground Slow",
            "caster_guid": int(area.get("caster_guid") or self.player_guid),
            "caster_level": int(self.character.get("level", 1)),
            "expires": now + float(area["interval"]) + 0.5,
            "max_duration": float(area["interval"]) + 0.5,
            "components": [component],
            "stacks": 1,
            "max_stacks": 1,
            "dispel_type": 0,
            "mechanic": 0,
            "positive": False,
            "slot": len(entries),
        }
        entries.append(entry)
        self._normalise_aura_slots(entries)

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
                        spells.SPELL_AURA_PERIODIC_ENERGIZE,
                        spells.SPELL_AURA_OBS_MOD_HEALTH,
                        spells.SPELL_AURA_PERIODIC_DAMAGE_PERCENT):
            return
        if not target_guid:
            return
        amplitude = max(250, int(effect["amplitude"] or 2000)) / 1000.0
        amount = template.effect_value(index, caster_level, combo_points)
        percent = aura in (spells.SPELL_AURA_OBS_MOD_HEALTH,
                           spells.SPELL_AURA_PERIODIC_DAMAGE_PERCENT)
        if aura == spells.SPELL_AURA_PERIODIC_HEAL:
            amount += self._spell_power_bonus(effect, healing=True)
        elif aura in (spells.SPELL_AURA_PERIODIC_DAMAGE,
                      spells.SPELL_AURA_PERIODIC_LEECH):
            amount += self._spell_power_bonus(effect)
            amount = self._apply_damage_done(
                int(template.school) or 1, max(0, amount))
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
                dot["is_heal"] = aura in (
                    spells.SPELL_AURA_PERIODIC_HEAL,
                    spells.SPELL_AURA_OBS_MOD_HEALTH)
                dot["leech"] = aura == spells.SPELL_AURA_PERIODIC_LEECH
                dot["percent"] = bool(percent)
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
            "is_heal": aura in (spells.SPELL_AURA_PERIODIC_HEAL,
                                spells.SPELL_AURA_OBS_MOD_HEALTH),
            "leech": aura == spells.SPELL_AURA_PERIODIC_LEECH,
            "percent": bool(percent),
            "energize": (int(effect.get("misc_value") or 0)
                         if aura == spells.SPELL_AURA_PERIODIC_ENERGIZE
                         else None),
        })

    async def _deal_damage_to_target(self, target_guid: int, damage: int,
                                     school: int, spell_id: int,
                                     caster_guid: int,
                                     periodic: bool = False) -> bool:
        """Damage a creature (or the player) and write the combat-log packet."""
        template = spells.get_spell_template(
            self.config.database.world_path, int(spell_id))
        if template is not None and hasattr(self, "_deal_spell_damage"):
            return await self._deal_spell_damage(
                target_guid, damage, school or 1, template, caster_guid,
                periodic=periodic)
        if self._is_player_guid(target_guid):
            await self._damage_player(damage, f"spell {spell_id}")
            return False
        died = await self._deal_damage_to_creature(
            target_guid, damage, caster_guid, threat_kind="spell")
        await self._send_spell_damage_log(
            target_guid, caster_guid, spell_id, damage, school or 1,
            periodic=1 if periodic else 0
        )
        return died

    async def _apply_pending_cast(self, pending: dict) -> None:
        """Finish a cast whose casting time has elapsed."""
        template = pending["template"]
        target_guid = pending["target_guid"]
        cast = spells.SpellCast(self.player_guid, pending["spell_id"], target_guid)
        cast.template = template
        cast.item_guid = int(pending.get("item_guid") or 0)
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
        if not await self._consume_spell_reagents(template):
            await self._cast_failed(pending["cast_id"], template.id,
                                    SPELL_FAILED_REAGENTS)
            return
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
        if template.is_channeled():
            await self._start_channel(cast, template, target_guid,
                                      pending["cast_id"])
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
        if dot.get("percent"):
            maximum = self._target_max_health(target_guid)
            if maximum <= 0:
                return
            if dot["is_heal"]:
                amount = int(maximum * amount / 100.0)
            else:
                amount = int(math.ceil(maximum * amount / 100.0))
        if dot.get("energize") is not None:
            power_type = int(dot["energize"])
            if self._is_player_guid(target_guid) \
                    and power_type == self._power_type():
                current = int(self.character.get("power", 0))
                await self._set_player_power(
                    min(self._max_power(), current + amount))
                await self._send_spell_energize_log(
                    target_guid, caster_guid, dot["spell_id"], power_type,
                    amount)
            return
        if dot["is_heal"]:
            kind = self._spell_target_kind(target_guid)
            if kind == "peer":
                peer = (self._peer_session_for_guid(target_guid)
                        if hasattr(self, "_peer_session_for_guid") else None)
                if peer is not None:
                    healed, overheal = await peer._heal_player(
                        amount, healer_guid=caster_guid)
                    await self._send_spell_heal_log(
                        target_guid, caster_guid, dot["spell_id"], healed,
                        overheal)
                return
            if kind == "player":
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
            await self._deal_damage_to_target(
                target_guid, amount, dot["school"] or 1, dot["spell_id"],
                caster_guid, periodic=True)
            if dot.get("leech") and caster_guid:
                if self._is_player_guid(caster_guid):
                    healed, _ = await self._heal_player(amount)
                    await self._send_spell_heal_log(
                        caster_guid, caster_guid, dot["spell_id"], healed, 0)
                elif hasattr(self, "_heal_creature"):
                    low = creature.creature_low_guid(caster_guid)
                    spawn = self._get_creature_spawn(low)
                    if spawn is not None:
                        await self._heal_creature(
                            low, spawn, amount,
                            spells.get_spell_template(
                                self.config.database.world_path,
                                int(dot["spell_id"])), caster_guid)
            return
        died = await self._deal_damage_to_target(
            target_guid, amount, dot["school"] or 1, dot["spell_id"],
            caster_guid, periodic=True)
        if dot.get("leech") and caster_guid and self._is_player_guid(caster_guid):
            healed, _ = await self._heal_player(amount)
            await self._send_spell_heal_log(
                caster_guid, caster_guid, dot["spell_id"], healed, 0)
        elif dot.get("leech") and caster_guid \
                and not self._is_player_guid(caster_guid) \
                and hasattr(self, "_heal_creature"):
            low = creature.creature_low_guid(caster_guid)
            spawn = self._get_creature_spawn(low)
            if spawn is not None:
                await self._heal_creature(
                    low, spawn, amount,
                    spells.get_spell_template(
                        self.config.database.world_path,
                        int(dot["spell_id"])), caster_guid)
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

    async def handle_cancel_channel(self, data: bytes):
        """``CMSG_CANCEL_CHANNELLING``: stop the player's active channel."""
        if len(data) < 4:
            return
        spell_id = struct.unpack_from("<I", data, 0)[0]
        combat = self._get_combat_state()
        channel = combat.get("channel")
        if channel and int(channel["spell_id"]) == int(spell_id):
            combat["channel"] = None
            await self.send_packet(
                Opcodes.MSG_CHANNEL_UPDATE,
                spells.pack_guid(int(self.player_guid)) + struct.pack("<I", 0),
            )
            self.log.debug(f"Cancelled channel of spell {spell_id}")

    async def handle_cancel_aura(self, data: bytes):
        """``CMSG_CANCEL_AURA``: right-click a buff to remove it."""
        if len(data) < 4:
            return
        spell_id = struct.unpack_from("<I", data, 0)[0]
        combat = self._get_combat_state()
        entries = combat.get("auras", [])
        for aura in list(entries):
            if int(aura["spell_id"]) != int(spell_id):
                continue
            if aura.get("positive"):
                entries.remove(aura)
                self._normalise_aura_slots(entries)
                await self._send_aura_update(
                    "player", self.player_guid, aura, remove=True)
                await self._refresh_player_auras()
                self.log.debug(f"Cancelled aura {spell_id}")
                return

    async def handle_cancel_temp_enchantment(self, data: bytes):
        """``CMSG_CANCEL_TEMP_ENCHANTMENT``: clear a poison/stone."""
        slot = 15
        if len(data) >= 4:
            requested = struct.unpack_from("<I", data, 0)[0]
            if int(requested) in (15, 16):
                slot = int(requested)
        combat = self._get_combat_state()
        entry = combat.setdefault("weapon_enchants", {}).pop(slot, None)
        if entry is None:
            return
        await run_db_write(
            character_db.update_character_item_enchant,
            self.config.database.world_path, int(self.player_guid or 0),
            int(slot), 0,
        )
        inventory = getattr(self, "character", {}).get("inventory") or {}
        item = inventory.get(int(slot))
        if item is not None:
            item["enchantment_2"] = 0
        if hasattr(self, "broadcast_equipment_update"):
            await self.broadcast_equipment_update()
        if hasattr(self, "_send_system_message"):
            await self._send_system_message("Temporary weapon enchant removed.")

    async def handle_set_selection(self, data: bytes):
        """``CMSG_SET_SELECTION``: the client selected a target."""
        if len(data) >= 8:
            guid = struct.unpack_from("<Q", data, 0)[0]
            combat = self._get_combat_state()
            combat["selected_guid"] = guid

    async def _send_spell_damage_log(self, victim_guid: int, caster_guid: int,
                                     spell_id: int, damage: int, school: int,
                                     periodic: int = 0, critical: int = 0,
                                     overkill: int = 0, absorbed: int = 0,
                                     resisted: int = 0) -> None:
        """Send ``SMSG_SPELLNONMELEEDAMAGELOG`` with mitigation fields."""
        out = bytearray()
        out += spells.pack_guid(int(victim_guid))
        out += spells.pack_guid(int(caster_guid))
        out += struct.pack("<ii", int(spell_id) & 0xFFFFFFFF, int(damage))
        out += struct.pack("<i", max(0, int(overkill)))
        out += struct.pack("<B", school & 0xFF)      # school mask
        out += struct.pack("<I", max(0, int(absorbed)) & 0xFFFFFFFF)
        out += struct.pack("<I", max(0, int(resisted)) & 0xFFFFFFFF)
        out += struct.pack("<B", periodic & 0xFF)
        out += struct.pack("<B", 0)                  # unused
        out += struct.pack("<I", 0)                  # shield block
        out += struct.pack("<I", 2 if critical else 0)  # SPELL_HIT_TYPE_CRIT
        out += struct.pack("<B", 0)                  # no debug info
        await self.send_packet(Opcodes.SMSG_SPELLNONMELEEDAMAGELOG, bytes(out))

    async def _send_spell_energize_log(self, victim_guid: int, caster_guid: int,
                                       spell_id: int, power_type: int,
                                       amount: int) -> None:
        out = bytearray()
        out += spells.pack_guid(int(victim_guid))
        out += spells.pack_guid(int(caster_guid))
        out += struct.pack("<III", int(spell_id) & 0xFFFFFFFF,
                           int(power_type) & 0xFFFFFFFF,
                           max(0, int(amount)) & 0xFFFFFFFF)
        await self.send_packet(Opcodes.SMSG_SPELLENERGIZELOG, bytes(out))

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
                                          school: int = 1,
                                          victim_state: int = 0,
                                          hit_info: int = 0) -> None:
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
        flags = int(hit_info)
        if not is_miss:
            flags |= 0x00000002          # HITINFO_AFFECTS_VICTIM
            if victim_state == 0:
                victim_state = 1         # VICTIMSTATE_HIT
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
