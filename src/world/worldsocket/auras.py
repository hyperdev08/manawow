"""Aura application, stacking, modifier aggregation and client updates.

Every aura the server knows about lives in ``combat["auras"]`` (the player) or
``combat["creature_auras"][low_guid]`` (creatures).  One entry is one spell on
one target and may carry several components; Battle Shout, for example, has an
attack-power and a ranged attack-power component.

The modifier registry here is deliberately separate from the periodic DoT list
in :mod:`src.world.worldsocket.spells`.  DoTs/HoTs tick on their own timers;
this module owns everything the client shows as a buff/debuff icon and every
passive number that changes the simulation: movement, crowd control, stats,
resistances, absorbs, damage modifiers, spell/melee haste and ratings.

The packet layout follows 3.3.5 ``AuraApplication::BuildUpdatePacket``:
``slot u8, spell u32, flags u8, caster level u8, stacks u8``, an optional packed
caster when ``AFLAG_CASTER`` is *not* set, and optional ``max duration`` /
``remaining duration`` when ``AFLAG_DURATION`` is set.
"""
from __future__ import annotations

import struct
import time

from src.protocol.opcodes import Opcodes
from src.world import login_packets
from src.world import object_update
from src.world import spells
from src.world import update_fields as F

PLAYER_RUN_SPEED = 7.0
UNIT_FLAG_PLAYER_CONTROLLED = 0x00000008
UNIT_FLAG_SILENCED = 0x00008000
UNIT_FLAG_PACIFIED = 0x00020000
UNIT_FLAG_STUNNED = 0x00040000
UNIT_FLAG_FLEEING = 0x00080000
UNIT_FLAG_CONFUSED = 0x00400000
UNIT_FLAG_DISARMED = 0x00000200

# 3.3.5 ``AURA_FLAGS`` (AzerothCore / CMaNGOS ``SpellAuraDefines.h``).
AFLAG_EFFECT_0 = 0x01
AFLAG_EFFECT_1 = 0x02
AFLAG_EFFECT_2 = 0x04
AFLAG_CASTER = 0x08
AFLAG_POSITIVE = 0x10
AFLAG_DURATION = 0x20
AFLAG_ANY_EFFECT_AMOUNT = 0x40
AFLAG_NEGATIVE = 0x80

PERMANENT_AURA_SECONDS = 10 * 365 * 24 * 3600

# Every aura type this module can turn into a simulation modifier or a piece of
# state.  Aura types not listed here are still stored (so they can be dispelled
# and shown to the client), but they carry no server-side effect yet.
SUPPORTED_AURA_TYPES = frozenset((
    spells.SPELL_AURA_MOD_POSSESS,
    spells.SPELL_AURA_MOD_CONFUSE,
    spells.SPELL_AURA_MOD_CHARM,
    spells.SPELL_AURA_MOD_FEAR,
    spells.SPELL_AURA_PERIODIC_DAMAGE,
    spells.SPELL_AURA_PERIODIC_HEAL,
    spells.SPELL_AURA_MOD_ATTACKSPEED,
    spells.SPELL_AURA_MOD_STUN,
    spells.SPELL_AURA_MOD_DAMAGE_DONE,
    spells.SPELL_AURA_MOD_DAMAGE_TAKEN,
    spells.SPELL_AURA_DAMAGE_SHIELD,
    spells.SPELL_AURA_MOD_RESISTANCE,
    spells.SPELL_AURA_PERIODIC_TRIGGER_SPELL,
    spells.SPELL_AURA_PERIODIC_ENERGIZE,
    spells.SPELL_AURA_MOD_PACIFY,
    spells.SPELL_AURA_MOD_ROOT,
    spells.SPELL_AURA_MOD_SILENCE,
    spells.SPELL_AURA_REFLECT_SPELLS,
    spells.SPELL_AURA_MOD_STAT,
    spells.SPELL_AURA_MOD_INCREASE_SPEED,
    spells.SPELL_AURA_MOD_DECREASE_SPEED,
    spells.SPELL_AURA_MOD_INCREASE_HEALTH,
    spells.SPELL_AURA_MOD_INCREASE_ENERGY,
    spells.SPELL_AURA_EFFECT_IMMUNITY,
    spells.SPELL_AURA_STATE_IMMUNITY,
    spells.SPELL_AURA_SCHOOL_IMMUNITY,
    spells.SPELL_AURA_DAMAGE_IMMUNITY,
    spells.SPELL_AURA_DISPEL_IMMUNITY,
    spells.SPELL_AURA_MOD_PARRY_PERCENT,
    spells.SPELL_AURA_MOD_DODGE_PERCENT,
    spells.SPELL_AURA_MOD_BLOCK_PERCENT,
    spells.SPELL_AURA_MOD_WEAPON_CRIT_PERCENT,
    spells.SPELL_AURA_PERIODIC_LEECH,
    spells.SPELL_AURA_MOD_HIT_CHANCE,
    spells.SPELL_AURA_MOD_SPELL_HIT_CHANCE,
    spells.SPELL_AURA_TRANSFORM,
    spells.SPELL_AURA_MOD_SPELL_CRIT_CHANCE,
    spells.SPELL_AURA_MOD_PACIFY_SILENCE,
    spells.SPELL_AURA_MOD_CASTING_SPEED_NOT_STACK,
    spells.SPELL_AURA_SCHOOL_ABSORB,
    spells.SPELL_AURA_MECHANIC_IMMUNITY,
    spells.SPELL_AURA_MOD_DAMAGE_PERCENT_DONE,
    spells.SPELL_AURA_MOD_PERCENT_STAT,
    spells.SPELL_AURA_MOD_DAMAGE_PERCENT_TAKEN,
    spells.SPELL_AURA_MOD_HEALTH_REGEN_PERCENT,
    spells.SPELL_AURA_MOD_ATTACK_POWER,
    spells.SPELL_AURA_MOD_RESISTANCE_PCT,
    spells.SPELL_AURA_MOD_HEALING,
    spells.SPELL_AURA_MOD_HEALING_PCT,
    spells.SPELL_AURA_MOD_TARGET_RESISTANCE,
    spells.SPELL_AURA_MOD_RANGED_ATTACK_POWER,
    spells.SPELL_AURA_MOD_INCREASE_ENERGY_PERCENT,
    spells.SPELL_AURA_MOD_INCREASE_HEALTH_PERCENT,
    spells.SPELL_AURA_MOD_HEALING_DONE,
    spells.SPELL_AURA_MOD_HEALING_DONE_PERCENT,
    spells.SPELL_AURA_MOD_TOTAL_STAT_PERCENTAGE,
    spells.SPELL_AURA_MOD_MELEE_HASTE,
    spells.SPELL_AURA_MOD_RANGED_HASTE,
    spells.SPELL_AURA_MOD_ATTACK_POWER_PCT,
    spells.SPELL_AURA_MOD_RANGED_ATTACK_POWER_PCT,
    spells.SPELL_AURA_HASTE_SPELLS,
    spells.SPELL_AURA_MECHANIC_DURATION_MOD,
    spells.SPELL_AURA_MOD_MECHANIC_DAMAGE_TAKEN_PERCENT,
))

# State-only auras: their amount is irrelevant for the simulation.
STATE_AURA_TYPES = frozenset((
    spells.SPELL_AURA_MOD_POSSESS,
    spells.SPELL_AURA_MOD_STUN,
    spells.SPELL_AURA_MOD_ROOT,
    spells.SPELL_AURA_MOD_SILENCE,
    spells.SPELL_AURA_MOD_PACIFY,
    spells.SPELL_AURA_MOD_PACIFY_SILENCE,
    spells.SPELL_AURA_MOD_FEAR,
    spells.SPELL_AURA_MOD_CONFUSE,
    spells.SPELL_AURA_MOD_CHARM,
    spells.SPELL_AURA_REFLECT_SPELLS,
    spells.SPELL_AURA_EFFECT_IMMUNITY,
    spells.SPELL_AURA_STATE_IMMUNITY,
    spells.SPELL_AURA_SCHOOL_IMMUNITY,
    spells.SPELL_AURA_DAMAGE_IMMUNITY,
    spells.SPELL_AURA_DISPEL_IMMUNITY,
    spells.SPELL_AURA_MECHANIC_IMMUNITY,
))


class AurasMixin:
    # ------------------------------------------------------------------
    # Storage and lookup
    # ------------------------------------------------------------------
    def _aura_store(self, target_kind: str, target_guid: int) -> list:
        combat = self._get_combat_state()
        if target_kind == "player":
            return combat.setdefault("auras", [])
        low = int(self._creature_low(target_guid))
        manager = (self._creature_combat_manager()
                   if hasattr(self, "_creature_combat_manager") else None)
        state = manager.get(low) if manager is not None else None
        if state is not None:
            entries = state.setdefault("auras", [])
            combat.setdefault("creature_auras", {})[low] = entries
            return entries
        return combat.setdefault("creature_auras", {}).setdefault(low, [])

    @staticmethod
    def _creature_low(guid: int) -> int:
        from src.world import creature
        return creature.creature_low_guid(int(guid))

    def _is_peer_player_guid(self, guid: int) -> bool:
        """True when ``guid`` is another player's character guid."""
        if not guid or int(guid) == int(self.player_guid or 0):
            return False
        for session in getattr(getattr(self, "server", None), "sessions", ()) or ():
            if int(getattr(session, "player_guid", 0) or 0) == int(guid):
                return True
        return False

    def _peer_session_for_guid(self, guid: int):
        for session in getattr(getattr(self, "server", None), "sessions", ()) or ():
            if int(getattr(session, "player_guid", 0) or 0) == int(guid):
                return session
        return None

    # ------------------------------------------------------------------
    # Diminishing returns
    # ------------------------------------------------------------------
    def _diminishing_table(self, target_kind: str, target_guid: int) -> dict:
        combat = self._get_combat_state()
        if target_kind == "player":
            return combat.setdefault("diminishing", {})
        low = int(self._creature_low(target_guid))
        manager = (self._creature_combat_manager()
                   if hasattr(self, "_creature_combat_manager") else None)
        state = manager.get(low) if manager is not None else None
        if state is not None:
            table = state.setdefault("diminishing", {})
            combat.setdefault("creature_diminishing", {})[low] = table
            return table
        return combat.setdefault("creature_diminishing", {}).setdefault(low, {})

    def _diminishing_duration(self, target_kind: str, target_guid: int,
                              mechanic: int, duration_ms: int) -> int:
        """Apply the 3.3.5 DR ladder to a control aura's duration.

        DR counters are per mechanic group and reset after 15 s without a
        matching control effect.  The fourth application in the window is
        immune, which is represented by a zero duration.
        """
        group = spells.DR_GROUPS.get(int(mechanic or 0))
        if not group or duration_ms <= 0:
            return max(0, int(duration_ms))
        table = self._diminishing_table(target_kind, target_guid)
        now = time.monotonic()
        state = table.get(group) or {"stacks": 0, "last": 0.0}
        if now - float(state["last"]) > spells.DR_RESET_SECONDS:
            state["stacks"] = 0
        multiplier = spells.DR_MULTIPLIERS[
            min(int(state["stacks"]), len(spells.DR_MULTIPLIERS) - 1)]
        state["stacks"] = min(int(state["stacks"]) + 1,
                              len(spells.DR_MULTIPLIERS) - 1)
        state["last"] = now
        table[group] = state
        if multiplier <= 0.0:
            return 0
        return max(1, int(duration_ms * multiplier))

    def _reset_diminishing(self, target_kind: str, target_guid: int) -> None:
        table = self._diminishing_table(target_kind, target_guid)
        table.clear()

    # ------------------------------------------------------------------
    # Component construction
    # ------------------------------------------------------------------
    def _aura_component(self, template, effect: dict, index: int,
                        caster_level: int, combo_points: int = 0):
        """Return one supported aura component, or ``None``."""
        aura_type = int(effect.get("apply_aura") or 0)
        if aura_type not in SUPPORTED_AURA_TYPES:
            return None
        if aura_type in STATE_AURA_TYPES:
            amount = 1
        else:
            amount = int(template.effect_value(index, caster_level,
                                               combo_points))
            if aura_type not in (spells.SPELL_AURA_MOD_DECREASE_SPEED,
                                 spells.SPELL_AURA_MOD_INCREASE_SPEED,
                                 spells.SPELL_AURA_MOD_DAMAGE_DONE,
                                 spells.SPELL_AURA_MOD_DAMAGE_TAKEN,
                                 spells.SPELL_AURA_MOD_DAMAGE_PERCENT_DONE,
                                 spells.SPELL_AURA_MOD_DAMAGE_PERCENT_TAKEN,
                                 spells.SPELL_AURA_DAMAGE_SHIELD,
                                 spells.SPELL_AURA_REFLECT_SPELLS) \
                    and amount == 0:
                return None
        return {
            "type": aura_type,
            "amount": amount,
            "misc": int(effect.get("misc_value") or 0),
            "misc_b": int(effect.get("misc_value_b") or 0),
            "amplitude": int(effect.get("amplitude") or 0),
            "trigger_spell": int(effect.get("trigger_spell") or 0),
            "index": int(index),
        }

    def _aura_components(self, template, effect: dict, caster_level: int,
                         combo_points: int = 0) -> list:
        """Collect the components that share the effect's target pattern.

        Battle Shout applies attack power and ranged attack power to the same
        target, so both effects must be merged into one aura entry.  Effects
        aimed at a different target (self buff + enemy debuff) stay separate.
        """
        pattern = (int(effect.get("target_a") or 0),
                   int(effect.get("target_b") or 0))
        components = []
        for index, other in enumerate(template.effects):
            if int(other.get("effect") or 0) != spells.SPELL_EFFECT_APPLY_AURA:
                continue
            if (int(other.get("target_a") or 0),
                    int(other.get("target_b") or 0)) != pattern:
                continue
            component = self._aura_component(
                template, other, index, caster_level, combo_points)
            if component is not None:
                components.append(component)
        return components

    # ------------------------------------------------------------------
    # Application / removal
    # ------------------------------------------------------------------
    async def _apply_aura_from_effect(self, template, effect: dict, index: int,
                                      caster_guid: int, target_guid: int,
                                      caster_level: int,
                                      combo_points: int = 0,
                                      target_kind: str | None = None,
                                      duration_ms: int | None = None) -> bool:
        """Apply the modifier components of one ``SPELL_EFFECT_APPLY_AURA``.

        ``duration_ms`` overrides the DBC duration for hard-coded effects such
        as Resurrection Sickness, whose SpellDuration row is not in the dump.
        """
        if duration_ms is None:
            duration_ms = spells.spell_duration_ms(
                self.config.database.world_path, template.duration_index,
                caster_level)
        if duration_ms < 0 or (duration_ms == 0 and template.is_passive()):
            duration_ms = PERMANENT_AURA_SECONDS * 1000
        if duration_ms <= 0:
            return False
        components = self._aura_components(
            template, effect, caster_level, combo_points)
        if not components:
            return False
        if target_kind is None:
            if (not target_guid or self._is_player_guid(target_guid)
                    or self._is_peer_player_guid(target_guid)):
                target_kind = "player"
            else:
                target_kind = "creature"
        if (target_kind == "player" and target_guid
                and not self._is_player_guid(target_guid)):
            # Apply a friendly buff/heal on another player's own session.  The
            # other session owns its aura list and sends the client packet.
            peer = self._peer_session_for_guid(target_guid)
            if (peer is not None and not template.is_harmful()
                    and hasattr(peer, "_apply_aura_from_effect")):
                return await peer._apply_aura_from_effect(
                    template, effect, index, caster_guid,
                    int(peer.player_guid), caster_level, combo_points,
                    target_kind="player")
            return False

        if target_kind == "creature" and template.is_harmful() \
                and hasattr(self, "_aggro_creature"):
            # A harmful aura is an attack even when the spell has no direct
            # damage effect, so make sure the creature has world-shared aggro.
            self._aggro_creature(int(self._creature_low(target_guid)))

        # Diminishing returns apply to every control effect, not only stuns.
        mechanic = template.cc_mechanic(effect)
        if mechanic in spells.DR_GROUPS:
            duration_ms = self._diminishing_duration(
                target_kind, target_guid, mechanic, duration_ms)
            if duration_ms <= 0:
                self.log.debug(
                    f"Aura {template.id} ({template.name}) is immune by DR")
                if hasattr(self, "_send_spell_miss_log"):
                    await self._send_spell_miss_log(
                        target_guid, caster_guid, template.id,
                        spells.SPELL_MISS_IMMUNE)
                return False

        now = time.monotonic()
        entries = self._aura_store(target_kind, target_guid)
        spell_id = int(template.id)
        max_stacks = max(1, int(getattr(template, "stack_amount", 0) or 1))
        positive = not template.is_harmful()
        refreshed = False
        entry = None
        for existing in entries:
            if existing["spell_id"] != spell_id:
                continue
            entry = existing
            entry["expires"] = now + duration_ms / 1000.0
            entry["max_duration"] = (0 if duration_ms >= PERMANENT_AURA_SECONDS * 1000
                                     else duration_ms / 1000.0)
            entry["components"] = components
            entry["caster_guid"] = int(caster_guid)
            entry["caster_level"] = int(caster_level)
            entry["dispel_type"] = int(template.dispel_type)
            entry["mechanic"] = int(mechanic)
            entry["positive"] = bool(positive)
            entry["max_stacks"] = max_stacks
            if max_stacks > 1:
                entry["stacks"] = min(max_stacks, entry["stacks"] + 1)
            # Refilling an absorb shield restores its pool.
            for component in components:
                if component["type"] == spells.SPELL_AURA_SCHOOL_ABSORB:
                    entry["absorb_remaining"] = int(component["amount"]) * \
                        max(1, int(entry.get("stacks") or 1))
            refreshed = True
            break
        if not refreshed:
            entry = {
                "spell_id": spell_id,
                "name": str(getattr(template, "name", "")),
                "caster_guid": int(caster_guid),
                "caster_level": int(caster_level),
                "expires": now + duration_ms / 1000.0,
                "max_duration": (0 if duration_ms >= PERMANENT_AURA_SECONDS * 1000
                                 else duration_ms / 1000.0),
                "components": components,
                "stacks": 1,
                "max_stacks": max_stacks,
                "dispel_type": int(template.dispel_type),
                "mechanic": int(mechanic),
                "positive": bool(positive),
                "slot": len(entries) if len(entries) < 255 else 254,
            }
            for component in components:
                if component["type"] == spells.SPELL_AURA_SCHOOL_ABSORB:
                    entry["absorb_remaining"] = int(component["amount"])
            entries.append(entry)
        self._normalise_aura_slots(entries)

        if target_kind == "player":
            await self._refresh_player_auras()
        else:
            low = self._creature_low(target_guid)
            await self._sync_creature_flags(low)
            mods = self._aura_mods(entries)
            if mods["rooted"] or mods["stunned"] or mods["feared"] \
                    or mods["confused"] or mods["charmed"]:
                await self._stop_creature_movement(target_guid)
        await self._send_aura_update(target_kind, target_guid, entry)
        return True

    @staticmethod
    def _normalise_aura_slots(entries: list) -> None:
        """Keep visible aura slots stable and fill gaps for new auras.

        3.3.5 ``SMSG_AURA_UPDATE`` identifies an aura by its slot, so removing
        slot 0 must not shift every other aura down: the client still has the
        old slots until it receives updates for them.  Existing slots are kept;
        only missing or duplicate slots are assigned the lowest free index.
        """
        used = set()
        for entry in entries:
            slot = entry.get("slot")
            if isinstance(slot, int) and 0 <= slot < 255 and slot not in used:
                used.add(slot)
                continue
            free = 0
            while free in used and free < 255:
                free += 1
            entry["slot"] = free
            used.add(free)

    async def _sync_creature_stun(self, low_guid: int) -> None:
        """Compatibility alias for the old stun-only sync helper."""
        await self._sync_creature_flags(low_guid)

    async def _sync_creature_flags(self, low_guid: int) -> None:
        """Mirror stun/flee/confuse/pacify/silence flags on a creature."""
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None:
            return
        base_flags = 0
        if "unit_flags" in spawn.keys():
            base_flags = int(spawn["unit_flags"] or 0)
        mods = self._creature_aura_mods(int(low_guid))
        flags = base_flags
        for bit, key in (
            (UNIT_FLAG_STUNNED, "stunned"),
            (UNIT_FLAG_FLEEING, "feared"),
            (UNIT_FLAG_CONFUSED, "confused"),
            (UNIT_FLAG_SILENCED, "silenced"),
            (UNIT_FLAG_PACIFIED, "pacified"),
        ):
            if mods[key]:
                flags |= bit
            else:
                flags &= ~bit
        await self._send_creature_fields(
            int(low_guid), {F.UNIT_FIELD_FLAGS: flags})

    async def _tick_auras(self, now: float) -> None:
        """Expire player and creature auras and tell the client about it."""
        combat = self._get_combat_state()
        changed = False
        auras = combat.get("auras")
        if auras:
            alive = [aura for aura in auras if aura["expires"] > now]
            removed = [aura for aura in auras if aura["expires"] <= now]
            if removed:
                combat["auras"] = alive
                self._normalise_aura_slots(alive)
                changed = True
                for aura in removed:
                    self._remove_dots_for_aura(
                        "player", self.player_guid, int(aura["spell_id"]))
                    await self._send_aura_update("player", self.player_guid,
                                                 aura, remove=True)
        manager = (self._creature_combat_manager()
                   if hasattr(self, "_creature_combat_manager") else None)
        for low, entries in list(combat.get("creature_auras", {}).items()):
            if manager is not None and manager.is_engaged(low):
                state = manager.get(low)
                if state is not None and int(
                        state.get("target_guid") or 0) != int(
                            self.player_guid or 0):
                    continue             # only the target session ticks
            alive = [aura for aura in entries if aura["expires"] > now]
            removed = [aura for aura in entries if aura["expires"] <= now]
            if alive:
                entries[:] = alive       # shared list: mutate, do not replace
                self._normalise_aura_slots(entries)
                if removed:
                    await self._sync_creature_flags(low)
            elif entries:
                entries.clear()
                await self._sync_creature_flags(low)
            for aura in removed:
                self._remove_dots_for_aura(
                    "creature", low, int(aura["spell_id"]))
                await self._send_aura_update("creature", low, aura, remove=True)
        if changed:
            await self._refresh_player_auras()

    def _remove_dots_for_aura(self, target_kind: str, target_guid: int,
                              spell_id: int) -> None:
        """Remove periodic ticks that belong to a dispelled/removed aura."""
        combat = self._get_combat_state()
        if target_kind == "player":
            target = int(self.player_guid or target_guid)
        else:
            target = int(target_guid)
            if target > 0xFFFF:
                target = self._creature_low(target)
        combat["dots"] = [
            dot for dot in combat.get("dots", ())
            if not (int(dot.get("spell_id") or 0) == int(spell_id)
                    and (int(dot.get("target_guid") or 0) == target
                         or self._creature_low(
                             int(dot.get("target_guid") or 0)) == target))
        ]

    async def _clear_player_auras(self) -> None:
        combat = self._get_combat_state()
        if combat.get("auras"):
            removed = list(combat["auras"])
            combat["auras"] = []
            combat["aura_state"] = {}
            combat["diminishing"] = {}
            for aura in removed:
                self._remove_dots_for_aura(
                    "player", self.player_guid, int(aura["spell_id"]))
                await self._send_aura_update("player", self.player_guid,
                                             aura, remove=True)
            await self._refresh_player_auras()

    def _clear_creature_auras(self, low_guid: int) -> None:
        combat = self._get_combat_state()
        store = combat.setdefault("creature_auras", {})
        entries = store.pop(int(low_guid), ())
        for aura in entries or ():
            self._remove_dots_for_aura(
                "creature", int(low_guid), int(aura["spell_id"]))
        if entries:
            entries.clear()              # shared list seen by all sessions
        combat.setdefault("creature_diminishing", {}).pop(int(low_guid), None)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------
    @staticmethod
    def _empty_mods() -> dict:
        return {
            "stats": {name: 0 for name in spells.STAT_AURA_NAMES},
            "stats_pct": {name: 0.0 for name in spells.STAT_AURA_NAMES},
            "total_stat_pct": 0.0,
            "health": 0,
            "health_pct": 0.0,
            "power": 0,
            "power_pct": 0.0,
            "armor": 0,
            "armor_pct": 0.0,
            "resistances": {
                school: 0 for _bit, school in spells.RESISTANCE_AURA_SCHOOLS
            },
            "resistances_pct": {
                school: 0.0 for _bit, school in spells.RESISTANCE_AURA_SCHOOLS
            },
            "attack_power": 0,
            "attack_power_pct": 0.0,
            "ranged_attack_power": 0,
            "ranged_attack_power_pct": 0.0,
            "spell_power": 0,
            "healing": 0,
            "healing_pct": 0.0,
            "hit": 0.0,
            "spell_hit": 0.0,
            "crit": 0.0,
            "spell_crit": 0.0,
            "dodge": 0.0,
            "parry": 0.0,
            "block": 0.0,
            "speed_mult": 1.0,
            "melee_haste": 0.0,
            "ranged_haste": 0.0,
            "spell_haste": 0.0,
            "damage_done_flat": {},
            "damage_done_pct": {},
            "damage_taken_flat": {},
            "damage_taken_pct": {},
            "thorns": 0,
            "reflect": False,
            "spell_penetration": 0,
            "health_regen_pct": 0.0,
            "absorb": [],
            "school_immune": 0,
            "damage_immune": 0,
            "mechanic_immune": 0,
            "effect_immune": set(),
            "state_immune": set(),
            "dispel_immune": 0,
            "rooted": False,
            "stunned": False,
            "silenced": False,
            "pacified": False,
            "feared": False,
            "confused": False,
            "charmed": False,
            "asleep": False,
            "polymorphed": False,
            "banished": False,
        }

    def _aura_mods(self, entries: list) -> dict:
        mods = self._empty_mods()
        for aura in entries:
            stacks = max(1, int(aura.get("stacks") or 1))
            mechanic = int(aura.get("mechanic") or 0)
            for component in aura.get("components", ()):
                aura_type = int(component["type"])
                amount = int(component["amount"]) * stacks
                misc = int(component.get("misc") or 0)
                if aura_type == spells.SPELL_AURA_MOD_STUN:
                    mods["stunned"] = True
                    if mechanic == spells.MECHANIC_SLEEP:
                        mods["asleep"] = True
                    if mechanic in (spells.MECHANIC_POLYMORPH,
                                    spells.MECHANIC_BANISH,
                                    spells.MECHANIC_SHACKLE):
                        mods["polymorphed"] = True
                        if mechanic == spells.MECHANIC_BANISH:
                            mods["banished"] = True
                elif aura_type == spells.SPELL_AURA_MOD_ROOT:
                    mods["rooted"] = True
                elif aura_type == spells.SPELL_AURA_MOD_SILENCE:
                    mods["silenced"] = True
                elif aura_type == spells.SPELL_AURA_MOD_PACIFY:
                    mods["pacified"] = True
                elif aura_type == spells.SPELL_AURA_MOD_PACIFY_SILENCE:
                    mods["pacified"] = True
                    mods["silenced"] = True
                elif aura_type == spells.SPELL_AURA_MOD_FEAR:
                    mods["feared"] = True
                elif aura_type == spells.SPELL_AURA_MOD_CONFUSE:
                    mods["confused"] = True
                    if mechanic == spells.MECHANIC_POLYMORPH:
                        mods["polymorphed"] = True
                        # Polymorph is a full loss of control for movement and
                        # casting, even though the DBC uses MOD_CONFUSE.
                        mods["stunned"] = True
                elif aura_type == spells.SPELL_AURA_MOD_CHARM:
                    mods["charmed"] = True
                elif aura_type == spells.SPELL_AURA_MOD_POSSESS:
                    mods["charmed"] = True
                elif aura_type in (spells.SPELL_AURA_MOD_INCREASE_SPEED,
                                   spells.SPELL_AURA_MOD_DECREASE_SPEED):
                    mods["speed_mult"] *= max(0.1, 1.0 + amount / 100.0)
                elif aura_type in (spells.SPELL_AURA_MOD_STAT,
                                   spells.SPELL_AURA_MOD_PERCENT_STAT):
                    if aura_type == spells.SPELL_AURA_MOD_STAT:
                        if 0 <= misc < len(spells.STAT_AURA_NAMES):
                            mods["stats"][spells.STAT_AURA_NAMES[misc]] += amount
                        elif misc == -1:
                            for key in spells.STAT_AURA_NAMES:
                                mods["stats"][key] += amount
                    elif 0 <= misc < len(spells.STAT_AURA_NAMES):
                        mods["stats_pct"][spells.STAT_AURA_NAMES[misc]] += amount
                    elif misc not in range(len(spells.STAT_AURA_NAMES)):
                        for key in spells.STAT_AURA_NAMES:
                            mods["stats_pct"][key] += amount
                elif aura_type == spells.SPELL_AURA_MOD_TOTAL_STAT_PERCENTAGE:
                    mods["total_stat_pct"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_INCREASE_HEALTH:
                    mods["health"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_INCREASE_HEALTH_PERCENT:
                    mods["health_pct"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_INCREASE_ENERGY:
                    mods["power"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_INCREASE_ENERGY_PERCENT:
                    mods["power_pct"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_RESISTANCE:
                    if misc & 1:
                        mods["armor"] += amount
                    for bit, school in spells.RESISTANCE_AURA_SCHOOLS:
                        if misc & bit:
                            mods["resistances"][school] += amount
                elif aura_type == spells.SPELL_AURA_MOD_RESISTANCE_PCT:
                    if misc & 1:
                        mods["armor_pct"] += amount
                    for bit, school in spells.RESISTANCE_AURA_SCHOOLS:
                        if misc & bit:
                            mods["resistances_pct"][school] += amount
                elif aura_type == spells.SPELL_AURA_MOD_ATTACK_POWER:
                    mods["attack_power"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_ATTACK_POWER_PCT:
                    mods["attack_power_pct"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_RANGED_ATTACK_POWER:
                    mods["ranged_attack_power"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_RANGED_ATTACK_POWER_PCT:
                    mods["ranged_attack_power_pct"] += amount
                elif aura_type in (spells.SPELL_AURA_MOD_HEALING,
                                   spells.SPELL_AURA_MOD_HEALING_DONE):
                    mods["healing"] += amount
                elif aura_type in (spells.SPELL_AURA_MOD_HEALING_PCT,
                                   spells.SPELL_AURA_MOD_HEALING_DONE_PERCENT):
                    mods["healing_pct"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_HIT_CHANCE:
                    mods["hit"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_SPELL_HIT_CHANCE:
                    mods["spell_hit"] += amount
                elif aura_type in (spells.SPELL_AURA_MOD_WEAPON_CRIT_PERCENT,
                                   spells.SPELL_AURA_MOD_WEAPON_CRIT_PERCENT):
                    mods["crit"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_SPELL_CRIT_CHANCE:
                    mods["spell_crit"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_DODGE_PERCENT:
                    mods["dodge"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_PARRY_PERCENT:
                    mods["parry"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_BLOCK_PERCENT:
                    mods["block"] += amount
                elif aura_type in (spells.SPELL_AURA_MOD_MELEE_HASTE,
                                   spells.SPELL_AURA_MOD_ATTACKSPEED):
                    mods["melee_haste"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_RANGED_HASTE:
                    mods["ranged_haste"] += amount
                elif aura_type in (spells.SPELL_AURA_MOD_CASTING_SPEED_NOT_STACK,
                                   spells.SPELL_AURA_HASTE_SPELLS):
                    mods["spell_haste"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_DAMAGE_DONE:
                    for bit, school in spells.RESISTANCE_AURA_SCHOOLS:
                        if misc & bit:
                            mods["damage_done_flat"][school] = \
                                mods["damage_done_flat"].get(school, 0) + amount
                    if misc == 127:
                        mods["damage_done_flat"]["all"] = \
                            mods["damage_done_flat"].get("all", 0) + amount
                elif aura_type == spells.SPELL_AURA_MOD_DAMAGE_PERCENT_DONE:
                    for bit, school in spells.RESISTANCE_AURA_SCHOOLS:
                        if misc & bit:
                            mods["damage_done_pct"][school] = \
                                mods["damage_done_pct"].get(school, 0.0) + amount
                    if misc == 127:
                        mods["damage_done_pct"]["all"] = \
                            mods["damage_done_pct"].get("all", 0.0) + amount
                elif aura_type == spells.SPELL_AURA_MOD_DAMAGE_TAKEN:
                    for bit, school in spells.RESISTANCE_AURA_SCHOOLS:
                        if misc & bit:
                            mods["damage_taken_flat"][school] = \
                                mods["damage_taken_flat"].get(school, 0) + amount
                elif aura_type in (spells.SPELL_AURA_MOD_DAMAGE_PERCENT_TAKEN,
                                   spells.SPELL_AURA_MOD_MECHANIC_DAMAGE_TAKEN_PERCENT):
                    for bit, school in spells.RESISTANCE_AURA_SCHOOLS:
                        if misc & bit:
                            mods["damage_taken_pct"][school] = \
                                mods["damage_taken_pct"].get(school, 0.0) + amount
                    if misc in (0, 127):
                        mods["damage_taken_pct"]["all"] = \
                            mods["damage_taken_pct"].get("all", 0.0) + amount
                elif aura_type == spells.SPELL_AURA_DAMAGE_SHIELD:
                    mods["thorns"] += amount
                elif aura_type == spells.SPELL_AURA_REFLECT_SPELLS:
                    mods["reflect"] = True
                elif aura_type == spells.SPELL_AURA_MOD_TARGET_RESISTANCE:
                    mods["spell_penetration"] += max(0, amount)
                elif aura_type == spells.SPELL_AURA_MOD_HEALTH_REGEN_PERCENT:
                    mods["health_regen_pct"] += amount
                elif aura_type == spells.SPELL_AURA_SCHOOL_ABSORB:
                    mods["absorb"].append(aura)
                elif aura_type == spells.SPELL_AURA_SCHOOL_IMMUNITY:
                    mods["school_immune"] |= misc or 0x7F
                elif aura_type == spells.SPELL_AURA_DAMAGE_IMMUNITY:
                    mods["damage_immune"] |= misc or 0x7F
                elif aura_type == spells.SPELL_AURA_MECHANIC_IMMUNITY:
                    if misc > 0:
                        mods["mechanic_immune"] |= 1 << min(63, misc)
                elif aura_type == spells.SPELL_AURA_EFFECT_IMMUNITY:
                    mods["effect_immune"].add(misc)
                elif aura_type == spells.SPELL_AURA_STATE_IMMUNITY:
                    mods["state_immune"].add(misc)
                elif aura_type == spells.SPELL_AURA_DISPEL_IMMUNITY:
                    if misc == spells.DISPEL_ALL:
                        mods["dispel_immune"] |= spells.DISPEL_ALL_MASK
                    else:
                        mods["dispel_immune"] |= 1 << misc
        mods["speed_mult"] = max(0.05, min(4.0, mods["speed_mult"]))
        return mods

    def _player_aura_mods(self) -> dict:
        return self._aura_mods(self._get_combat_state().get("auras", ()))

    def _creature_aura_mods(self, low_guid: int) -> dict:
        entries = self._get_combat_state().get(
            "creature_auras", {}).get(int(low_guid), ())
        return self._aura_mods(entries)

    def _creature_can_move(self, low_guid: int) -> bool:
        mods = self._creature_aura_mods(low_guid)
        return not (mods["rooted"] or mods["stunned"])

    def _creature_can_attack(self, low_guid: int) -> bool:
        mods = self._creature_aura_mods(low_guid)
        return not (mods["stunned"] or mods["pacified"] or mods["feared"]
                    or mods["confused"] or mods["charmed"])

    def _creature_can_cast(self, low_guid: int) -> bool:
        mods = self._creature_aura_mods(low_guid)
        return not (mods["stunned"] or mods["silenced"] or mods["feared"]
                    or mods["confused"] or mods["charmed"] or mods["asleep"])

    def _creature_speed_mult(self, low_guid: int) -> float:
        return float(self._creature_aura_mods(low_guid)["speed_mult"])

    def _creature_melee_interval(self, low_guid: int) -> float:
        haste = float(self._creature_aura_mods(low_guid)["melee_haste"])
        base = 2.0
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is not None and "melee_attack_speed" in spawn.keys():
            base = max(0.5, float(spawn["melee_attack_speed"] or 2000)
                       / 1000.0)
        return base / max(0.1, 1.0 + haste / 100.0)

    def _player_can_cast(self, spell_id: int = 0) -> bool:
        mods = self._player_aura_mods()
        if (mods["stunned"] or mods["silenced"] or mods["feared"]
                or mods["confused"] or mods["charmed"] or mods["asleep"]):
            return False
        lockouts = self._get_combat_state().get("school_lockout") or {}
        if spell_id and lockouts:
            template = spells.get_spell_template(
                self.config.database.world_path, int(spell_id))
            school = int(template.school) if template is not None else 0
            now = time.monotonic()
            for mask, until in lockouts.items():
                if until > now and int(mask) & (school or 1):
                    return False
        return True

    def _player_can_attack(self) -> bool:
        mods = self._player_aura_mods()
        return not (mods["stunned"] or mods["pacified"] or mods["feared"]
                    or mods["confused"] or mods["charmed"] or mods["asleep"])

    def _player_can_move(self) -> bool:
        mods = self._player_aura_mods()
        return not (mods["rooted"] or mods["stunned"] or mods["charmed"])

    # ------------------------------------------------------------------
    # Damage mitigation helpers
    # ------------------------------------------------------------------
    def _school_name(self, school: int) -> str:
        for _bit, name in spells.RESISTANCE_AURA_SCHOOLS:
            if school & _bit:
                return name
        return ""

    def _spell_immune(self, target_kind: str, target_guid: int,
                      template, effect: dict | None = None) -> bool:
        """Check school/mechanic/effect immunity on a target."""
        mods = (self._player_aura_mods() if target_kind == "player"
                else self._creature_aura_mods(self._creature_low(target_guid)))
        school = int(template.school) or 1
        if mods["school_immune"] & school or mods["damage_immune"] & school:
            return True
        mechanic = template.cc_mechanic(effect)
        if mechanic and (mods["mechanic_immune"] & (1 << min(63, mechanic))):
            return True
        if effect is not None:
            etype = int(effect.get("effect") or 0)
            if etype in mods["effect_immune"]:
                return True
            aura = int(effect.get("apply_aura") or 0)
            if aura in mods["state_immune"]:
                return True
        return False

    def _dispel_immune(self, target_kind: str, target_guid: int,
                       dispel_type: int) -> bool:
        mods = (self._player_aura_mods() if target_kind == "player"
                else self._creature_aura_mods(self._creature_low(target_guid)))
        if not dispel_type:
            return False
        if mods["dispel_immune"] & (1 << int(dispel_type)):
            return True
        return False

    def _absorb_damage(self, target_kind: str, target_guid: int,
                       school: int, damage: int) -> tuple[int, list]:
        """Drain school absorb shields, returning ``(absorbed, depleted)``.

        A depleted shield is removed from the target's aura list, exactly like
        ``AuraEffect::HandleAuraSchoolAbsorb`` when its amount reaches zero.
        """
        if damage <= 0:
            return 0, []
        entries = self._aura_store(target_kind, target_guid)
        remaining = int(damage)
        absorbed = 0
        depleted = []
        for aura in entries:
            for component in aura.get("components", ()):
                if int(component["type"]) != spells.SPELL_AURA_SCHOOL_ABSORB:
                    continue
                mask = int(component.get("misc") or 0)
                if mask and not (mask & int(school or 1)):
                    continue
                pool = int(aura.get("absorb_remaining", 0))
                if pool <= 0:
                    depleted.append(aura)
                    continue
                taken = min(pool, remaining)
                aura["absorb_remaining"] = pool - taken
                absorbed += taken
                remaining -= taken
                if aura["absorb_remaining"] <= 0:
                    depleted.append(aura)
                if remaining <= 0:
                    break
            if remaining <= 0:
                break
        for aura in depleted:
            if aura in entries:
                entries.remove(aura)
        if depleted:
            self._normalise_aura_slots(entries)
        return absorbed, depleted

    def _apply_damage_modifiers(self, target_kind: str, target_guid: int,
                                school: int, damage: int) -> int:
        """Apply percentage damage-taken modifiers and flat resistance."""
        mods = (self._player_aura_mods() if target_kind == "player"
                else self._creature_aura_mods(self._creature_low(target_guid)))
        school_name = self._school_name(school)
        pct = float(mods["damage_taken_pct"].get("all", 0.0))
        pct += float(mods["damage_taken_pct"].get(school_name, 0.0))
        flat = int(mods["damage_taken_flat"].get(school_name, 0))
        value = max(0, int(damage) + flat)
        if pct:
            value = int(value * (1.0 + pct / 100.0))
        return max(0, value)

    def _damage_done_modifier(self, school: int, damage: int) -> int:
        mods = self._player_aura_mods()
        school_name = self._school_name(school)
        flat = int(mods["damage_done_flat"].get("all", 0))
        flat += int(mods["damage_done_flat"].get(school_name, 0))
        pct = float(mods["damage_done_pct"].get("all", 0.0))
        pct += float(mods["damage_done_pct"].get(school_name, 0.0))
        value = max(0, int(damage) + flat)
        if pct:
            value = int(value * (1.0 + pct / 100.0))
        return max(0, value)

    # ------------------------------------------------------------------
    # Player packets / derived stats
    # ------------------------------------------------------------------
    async def _refresh_player_auras(self) -> None:
        """Recompute derived stats and send the movement/stat changes."""
        if not self.character or not self.player_guid:
            return
        # Update-object helpers key on the character row's guid; synthetic
        # sessions (tests, tools) may only populate player_guid.
        self.character.setdefault("guid", int(self.player_guid))
        self._apply_combat_stats()
        fields = object_update.build_combat_stats_values(self.character)
        fields.update(self._derived_player_fields())
        await self._send_player_fields(fields)

        mods = self._player_aura_mods()
        combat = self._get_combat_state()
        state = combat.setdefault("aura_state", {})
        flags = UNIT_FLAG_PLAYER_CONTROLLED
        if mods["stunned"]:
            flags |= UNIT_FLAG_STUNNED
        if mods["feared"]:
            flags |= UNIT_FLAG_FLEEING
        if mods["confused"]:
            flags |= UNIT_FLAG_CONFUSED
        if mods["silenced"]:
            flags |= UNIT_FLAG_SILENCED
        if mods["pacified"]:
            flags |= UNIT_FLAG_PACIFIED
        # Kept from the current flags: UNIT_FLAG_LOOTING and the taxi-flight
        # flags (REMOVE_CLIENT_CONTROL | TAXI_FLIGHT), which auras do not own.
        flags |= int(state.get("flags", 0)) & 0x00000504
        if flags != state.get("flags"):
            state["flags"] = flags
            await self._send_player_fields({F.UNIT_FIELD_FLAGS: flags})

        rooted = bool(mods["rooted"] or mods["stunned"])
        if rooted != bool(state.get("rooted")):
            state["rooted"] = rooted
            await self.send_packet(
                Opcodes.SMSG_FORCE_MOVE_ROOT if rooted
                else Opcodes.SMSG_FORCE_MOVE_UNROOT,
                (login_packets.build_force_move_root(self.player_guid)
                 if rooted else
                 login_packets.build_force_move_unroot(self.player_guid)),
            )
        state["stunned"] = bool(mods["stunned"])

        speed = round(PLAYER_RUN_SPEED * mods["speed_mult"], 3)
        if abs(speed - float(state.get("speed", PLAYER_RUN_SPEED))) > 0.01:
            state["speed"] = speed
            await self.send_packet(
                Opcodes.SMSG_FORCE_RUN_SPEED_CHANGE,
                login_packets.build_force_run_speed(self.player_guid, speed),
            )

    def _aura_packet_flags(self, entry: dict, target_guid: int) -> int:
        flags = 0
        for component in entry.get("components") or ():
            index = int(component.get("index") or 0)
            flags |= 1 << min(2, max(0, index))
        if int(entry.get("caster_guid") or 0) == int(target_guid or 0):
            flags |= AFLAG_CASTER
        if entry.get("positive"):
            flags |= AFLAG_POSITIVE
        else:
            flags |= AFLAG_NEGATIVE
        if float(entry.get("max_duration") or 0.0) > 0.0:
            flags |= AFLAG_DURATION
        return flags

    def _build_aura_packet(self, target_guid: int, entry: dict,
                           remove: bool = False) -> bytes:
        out = bytearray(spells.pack_guid(int(target_guid)))
        out += struct.pack("<B", int(entry.get("slot") or 0) & 0xFF)
        if remove:
            out += struct.pack("<I", 0)
            return bytes(out)
        out += struct.pack("<I", int(entry["spell_id"]) & 0xFFFFFFFF)
        flags = self._aura_packet_flags(entry, target_guid)
        out += struct.pack("<B", flags)
        out += struct.pack("<B", int(entry.get("caster_level") or 1) & 0xFF)
        stacks = max(1, int(entry.get("stacks") or 1))
        out += struct.pack("<B", stacks & 0xFF)
        if not (flags & AFLAG_CASTER):
            out += spells.pack_guid(int(entry.get("caster_guid") or 0))
        if flags & AFLAG_DURATION:
            max_ms = int(float(entry.get("max_duration") or 0.0) * 1000)
            remaining = max(0, int((float(entry["expires"]) - time.monotonic())
                                   * 1000)) if entry.get("expires") else 0
            out += struct.pack("<II", max_ms, remaining)
        return bytes(out)

    async def _send_aura_update(self, target_kind: str, target_guid: int,
                                entry: dict, remove: bool = False) -> None:
        """Send one ``SMSG_AURA_UPDATE`` for a player or creature aura."""
        if not entry:
            return
        low_guid = 0
        if target_kind == "player":
            target_guid = int(self.player_guid or target_guid)
        else:
            low_guid = self._creature_low(target_guid)
            target_guid = low_guid
            spawn = self._get_creature_spawn(target_guid)
            if spawn is not None:
                target_guid = self._creature_object_guid(
                    target_guid, int(spawn["entry"]))
        payload = self._build_aura_packet(target_guid, entry, remove=remove)
        if target_kind == "creature" and low_guid \
                and hasattr(self, "_send_creature_packet"):
            await self._send_creature_packet(
                int(low_guid), Opcodes.SMSG_AURA_UPDATE, payload)
            return
        await self.send_packet(Opcodes.SMSG_AURA_UPDATE, payload)
        if (target_kind == "player" and hasattr(self, "send_packet_to_peers")
                and getattr(self, "server", None) is not None
                and getattr(self, "in_world", False)):
            await self.send_packet_to_peers(Opcodes.SMSG_AURA_UPDATE, payload)

    async def _send_all_auras(self) -> None:
        """Send ``SMSG_AURA_UPDATE`` for every current player aura."""
        for entry in list(self._get_combat_state().get("auras", ())):
            await self._send_aura_update("player", self.player_guid, entry)

    async def _stop_creature_movement(self, guid: int) -> None:
        """Freeze a creature that just became rooted/stunned/feared."""
        low = self._creature_low(guid)
        spawn = self._get_creature_spawn(low)
        if spawn is None:
            return
        combat = self._get_combat_state()
        state = combat["aggro"].get(low) or combat.get("idle", {}).get(low)
        here = self._creature_position(low)
        if state is not None:
            state["pos"] = here
            state["move_until"] = 0.0
            state["chase_until"] = 0.0
            state["next_move"] = time.monotonic() + 3600.0
        if hasattr(self, "_send_creature_packet"):
            await self._send_creature_packet(
                low, Opcodes.SMSG_MONSTER_MOVE,
                object_update.build_monster_move(
                    self._creature_object_guid(low, int(spawn["entry"])),
                    here[0], here[1], here[2],
                    float(spawn["o"] or 0.0),
                    here[0], here[1], here[2],
                    duration_ms=1, move_id=self._next_move_id(),
                ),
            )
        else:
            await self.send_packet(
                Opcodes.SMSG_MONSTER_MOVE,
                object_update.build_monster_move(
                    self._creature_object_guid(low, int(spawn["entry"])),
                    here[0], here[1], here[2],
                    float(spawn["o"] or 0.0),
                    here[0], here[1], here[2],
                    duration_ms=1, move_id=self._next_move_id(),
                ),
            )
