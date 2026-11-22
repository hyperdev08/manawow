"""Aura application, stacking and modifier aggregation.

Periodic damage/heal/leech auras already live in ``combat["dots"]``; this
module adds the *modifier* components the client and the simulation need:
movement slows, roots and stuns, silence/pacify, primary stats, health/mana,
armor/resistances, attack power and melee haste.

Player auras are stored in ``combat["auras"]``; creature auras live in
``combat["creature_auras"][low_guid]``.  Every entry is one spell cast on one
target and can carry several components (Battle Shout has attack power and
ranged attack power, for example).  Recasting the same spell refreshes its
duration (and increments stacks up to ``Spell.dbc`` StackAmount).
"""
from __future__ import annotations

import time

from src.protocol.opcodes import Opcodes
from src.world import login_packets
from src.world import object_update
from src.world import spells
from src.world import update_fields as F

PLAYER_RUN_SPEED = 7.0
UNIT_FLAG_PLAYER_CONTROLLED = 0x00000008
UNIT_FLAG_STUNNED = 0x00040000

# Components this server understands.  Periodic/dummy auras are handled by the
# DoT list or the ground-effect code instead.
SUPPORTED_AURA_TYPES = frozenset((
    spells.SPELL_AURA_MOD_STUN,
    spells.SPELL_AURA_MOD_RESISTANCE,
    spells.SPELL_AURA_MOD_PACIFY,
    spells.SPELL_AURA_MOD_ROOT,
    spells.SPELL_AURA_MOD_SILENCE,
    spells.SPELL_AURA_MOD_STAT,
    spells.SPELL_AURA_MOD_INCREASE_SPEED,
    spells.SPELL_AURA_MOD_DECREASE_SPEED,
    spells.SPELL_AURA_MOD_INCREASE_HEALTH,
    spells.SPELL_AURA_MOD_INCREASE_ENERGY,
    spells.SPELL_AURA_MOD_ATTACK_POWER,
    spells.SPELL_AURA_MOD_RANGED_ATTACK_POWER,
    spells.SPELL_AURA_MOD_MELEE_HASTE,
))

# State-only auras: their amount is irrelevant.
STATE_AURA_TYPES = frozenset((
    spells.SPELL_AURA_MOD_STUN,
    spells.SPELL_AURA_MOD_ROOT,
    spells.SPELL_AURA_MOD_SILENCE,
    spells.SPELL_AURA_MOD_PACIFY,
))


class AurasMixin:
    # ------------------------------------------------------------------
    # Storage and lookup
    # ------------------------------------------------------------------
    def _aura_store(self, target_kind: str, target_guid: int) -> list:
        combat = self._get_combat_state()
        if target_kind == "player":
            return combat.setdefault("auras", [])
        low = self._creature_low(target_guid)
        return combat.setdefault("creature_auras", {}).setdefault(int(low), [])

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
                                 spells.SPELL_AURA_MOD_INCREASE_SPEED) \
                    and amount == 0:
                return None
        return {
            "type": aura_type,
            "amount": amount,
            "misc": int(effect.get("misc_value") or 0),
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
                                      target_kind: str | None = None) -> bool:
        """Apply the modifier components of one ``SPELL_EFFECT_APPLY_AURA``."""
        duration_ms = spells.spell_duration_ms(
            self.config.database.world_path, template.duration_index,
            caster_level)
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
            # Cross-session auras are not synchronised yet; do not apply a
            # party member's buff to ourselves.
            return False
        now = time.monotonic()
        entries = self._aura_store(target_kind, target_guid)
        spell_id = int(template.id)
        max_stacks = max(1, int(getattr(template, "stack_amount", 0) or 1))
        refreshed = False
        for entry in entries:
            if entry["spell_id"] != spell_id:
                continue
            entry["expires"] = now + duration_ms / 1000.0
            entry["components"] = components
            entry["caster_guid"] = int(caster_guid)
            entry["max_stacks"] = max_stacks
            if max_stacks > 1:
                entry["stacks"] = min(max_stacks, entry["stacks"] + 1)
            refreshed = True
            break
        if not refreshed:
            entries.append({
                "spell_id": spell_id,
                "name": str(getattr(template, "name", "")),
                "caster_guid": int(caster_guid),
                "expires": now + duration_ms / 1000.0,
                "components": components,
                "stacks": 1,
                "max_stacks": max_stacks,
            })
        if target_kind == "player":
            await self._refresh_player_auras()
        else:
            low = self._creature_low(target_guid)
            mods = self._aura_mods(entries)
            await self._sync_creature_stun(low)
            if mods["rooted"] or mods["stunned"]:
                await self._stop_creature_movement(target_guid)
        return True

    async def _sync_creature_stun(self, low_guid: int) -> None:
        """Mirror the stun flag on a creature's update fields."""
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None:
            return
        flags = 0
        if "unit_flags" in spawn.keys():
            flags = int(spawn["unit_flags"] or 0)
        if self._creature_aura_mods(int(low_guid))["stunned"]:
            flags |= UNIT_FLAG_STUNNED
        await self._send_creature_fields(
            int(low_guid), {F.UNIT_FIELD_FLAGS: flags})

    async def _tick_auras(self, now: float) -> None:
        """Expire player and creature auras."""
        combat = self._get_combat_state()
        changed = False
        auras = combat.get("auras")
        if auras:
            alive = [aura for aura in auras if aura["expires"] > now]
            if len(alive) != len(auras):
                combat["auras"] = alive
                changed = True
        for low, entries in list(combat.get("creature_auras", {}).items()):
            alive = [aura for aura in entries if aura["expires"] > now]
            if alive:
                combat["creature_auras"][low] = alive
                if len(alive) != len(entries):
                    await self._sync_creature_stun(low)
            else:
                del combat["creature_auras"][low]
                await self._sync_creature_stun(low)
        if changed:
            await self._refresh_player_auras()

    async def _clear_player_auras(self) -> None:
        combat = self._get_combat_state()
        if combat.get("auras"):
            combat["auras"] = []
            combat["aura_state"] = {}
            await self._refresh_player_auras()

    def _clear_creature_auras(self, low_guid: int) -> None:
        combat = self._get_combat_state()
        combat.setdefault("creature_auras", {}).pop(int(low_guid), None)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------
    @staticmethod
    def _empty_mods() -> dict:
        return {
            "stats": {name: 0 for name in spells.STAT_AURA_NAMES},
            "health": 0,
            "power": 0,
            "armor": 0,
            "resistances": {
                school: 0 for _bit, school in spells.RESISTANCE_AURA_SCHOOLS
            },
            "attack_power": 0,
            "ranged_attack_power": 0,
            "speed_mult": 1.0,
            "melee_haste": 0.0,
            "rooted": False,
            "stunned": False,
            "silenced": False,
            "pacified": False,
        }

    def _aura_mods(self, entries: list) -> dict:
        mods = self._empty_mods()
        for aura in entries:
            stacks = max(1, int(aura.get("stacks") or 1))
            for component in aura.get("components", ()):
                aura_type = int(component["type"])
                amount = int(component["amount"]) * stacks
                misc = int(component.get("misc") or 0)
                if aura_type == spells.SPELL_AURA_MOD_STUN:
                    mods["stunned"] = True
                elif aura_type == spells.SPELL_AURA_MOD_ROOT:
                    mods["rooted"] = True
                elif aura_type == spells.SPELL_AURA_MOD_SILENCE:
                    mods["silenced"] = True
                elif aura_type == spells.SPELL_AURA_MOD_PACIFY:
                    mods["pacified"] = True
                elif aura_type in (spells.SPELL_AURA_MOD_INCREASE_SPEED,
                                   spells.SPELL_AURA_MOD_DECREASE_SPEED):
                    mods["speed_mult"] *= max(0.1, 1.0 + amount / 100.0)
                elif aura_type == spells.SPELL_AURA_MOD_STAT:
                    if 0 <= misc < len(spells.STAT_AURA_NAMES):
                        mods["stats"][spells.STAT_AURA_NAMES[misc]] += amount
                elif aura_type == spells.SPELL_AURA_MOD_INCREASE_HEALTH:
                    mods["health"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_INCREASE_ENERGY:
                    mods["power"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_RESISTANCE:
                    if misc & 1:
                        mods["armor"] += amount
                    for bit, school in spells.RESISTANCE_AURA_SCHOOLS:
                        if misc & bit:
                            mods["resistances"][school] += amount
                elif aura_type == spells.SPELL_AURA_MOD_ATTACK_POWER:
                    mods["attack_power"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_RANGED_ATTACK_POWER:
                    mods["ranged_attack_power"] += amount
                elif aura_type == spells.SPELL_AURA_MOD_MELEE_HASTE:
                    mods["melee_haste"] += amount
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
        return not (mods["stunned"] or mods["pacified"])

    def _creature_can_cast(self, low_guid: int) -> bool:
        mods = self._creature_aura_mods(low_guid)
        return not (mods["stunned"] or mods["silenced"])

    def _creature_speed_mult(self, low_guid: int) -> float:
        return float(self._creature_aura_mods(low_guid)["speed_mult"])

    def _creature_melee_interval(self, low_guid: int) -> float:
        haste = float(self._creature_aura_mods(low_guid)["melee_haste"])
        return 2.0 / max(0.1, 1.0 + haste / 100.0)

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

    async def _stop_creature_movement(self, guid: int) -> None:
        """Freeze a creature that just became rooted/stunned."""
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
