"""Spell system for the 3.3.5a worldserver.

Implements spell casting, spell effects, cooldowns, and the packet handlers
needed for players and creatures to cast spells.
"""

from __future__ import annotations

import math
import time
import random
import struct
import sqlite3
from typing import Dict, List, Optional, Tuple, Any, Mapping

try:  # normal package import (the server adds the project root to sys.path)
    from src.world.rune_costs import RUNE_COST_FALLBACK
except ImportError:  # direct ``python tools/...`` execution
    from rune_costs import RUNE_COST_FALLBACK

def _row_get(row: Mapping, key: str, default=None):
    """Get a value from a Mapping that might be a sqlite3.Row (no .get method)."""
    try:
        if key in row.keys():
            value = row[key]
            return default if value is None else value
    except AttributeError:
        pass
    return row.get(key, default) if hasattr(row, "get") else default

# Power types (``Powers`` in CMaNGOS 3.3.5 SharedDefines.h).  The enum is
# signed: health-cost spells use a negative id, so "not mana" is not the same as
# "has a power bar to predict".
POWER_MANA = 0
POWER_RAGE = 1
POWER_FOCUS = 2
POWER_ENERGY = 3
POWER_HAPPINESS = 4
POWER_RUNE = 5
POWER_RUNIC_POWER = 6
POWER_NONE = -1
POWER_HEALTH = -2

# Rune system (CMaNGOS 3.3.5 ``Player.h``).  Death knights spend runes instead
# of mana; the six slots are two blood, two unholy and two frost runes and each
# spent rune comes back after ``RUNE_COOLDOWN`` milliseconds.  A death rune
# (created by talents/abilities) can pay for any of the three basic types.
RUNE_BLOOD = 0
RUNE_UNHOLY = 1
RUNE_FROST = 2
RUNE_DEATH = 3
NUM_RUNE_TYPES = 4
MAX_RUNES = 6
RUNE_COOLDOWN = 10000
RUNE_REGEN_RATE = 0.1
RUNE_SLOT_TYPES = (RUNE_BLOOD, RUNE_BLOOD,
                   RUNE_UNHOLY, RUNE_UNHOLY,
                   RUNE_FROST, RUNE_FROST)

# Spell effect types (Spell.dbc / SpellDefines.h, 3.3.5a).
SPELL_EFFECT_NONE = 0
SPELL_EFFECT_INSTAKILL = 1
SPELL_EFFECT_SCHOOL_DAMAGE = 2
SPELL_EFFECT_DUMMY = 3
SPELL_EFFECT_APPLY_AURA = 6
SPELL_EFFECT_HEALTH_LEECH = 9
SPELL_EFFECT_HEAL = 10
SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL = 17
SPELL_EFFECT_SUMMON = 28
SPELL_EFFECT_WEAPON_DAMAGE = 58
SPELL_EFFECT_SUMMON_PET = 56
SPELL_EFFECT_TRIGGER_SPELL = 64
SPELL_EFFECT_SUMMON_OBJECT_WILD = 76
SPELL_EFFECT_ATTACK = 78
SPELL_EFFECT_ADD_COMBO_POINTS = 80
SPELL_EFFECT_SUMMON_OBJECT_SLOT1 = 104
SPELL_EFFECT_HEAL_MAX_HEALTH = 120
SPELL_EFFECT_NORMALIZED_WEAPON_DMG = 121

# Aura types (Spell.dbc EffectApplyAuraName).
SPELL_AURA_PERIODIC_DAMAGE = 3
SPELL_AURA_PERIODIC_HEAL = 8
SPELL_AURA_PERIODIC_LEECH = 53

# Aliases kept for callers written against the old names.
SPELL_EFFECT_DMG = SPELL_EFFECT_SCHOOL_DAMAGE
SPELL_EFFECT_SCHOOL_DMG = SPELL_EFFECT_SCHOOL_DAMAGE
SPELL_EFFECT_HEALTH = SPELL_EFFECT_HEAL

# Spell schools
SCHOOL_HOLY = 1
SCHOOL_FIRE = 2
SCHOOL_FROST = 4
SCHOOL_NATURE = 8
SCHOOL_SHADOW = 16
SCHOOL_ARCANE = 32

# Target types
TARGET_SELF = 0
TARGET_TARGET = 1
TARGET_RANDOM = 2
TARGET_RANDOM_TOP = 3
TARGET_ALL_ENEMIES = 4
TARGET_ALL_ALLIES = 5
TARGET_ALL = 6

# Spell categories
SPELL_CATEGORY_MELEE = 1
SPELL_CATEGORY_RANGED = 2
SPELL_CATEGORY_OFFHAND = 3
SPELL_CATEGORY_SPELL = 4

# Spell flags
SPELL_FLAG_NO_TARGETS = 0x00000001
SPELL_FLAG_NO_COOLDOWN = 0x00000004
SPELL_FLAG_NO_MANA = 0x00000008
SPELL_FLAG_NO_REAGENTS = 0x00000010
SPELL_FLAG_NO_INTERRUPT = 0x00000020

# Spell attributes
SPELL_ATTR_NO_TARGETS = 0x00000001
SPELL_ATTR_NO_COOLDOWN = 0x00000004
SPELL_ATTR_NO_MANA = 0x00000008
SPELL_ATTR_NO_REAGENTS = 0x00000010
# ``SPELL_ATTR_TOTEM``: the spell drops a stationary totem (shaman totems).
SPELL_ATTR_TOTEM = 0x00010000
SPELL_ATTR_NO_INTERRUPT = 0x00000020

# ``SpellAttributesEx`` bits (3.3.5a SpellDefines.h).  A spell that carries one
# of the two finishing-move bits is a rogue finisher: it consumes the caster's
# combo points on the current target instead of generating them.
SPELL_ATTR_EX_USE_ALL_MANA = 0x00000002
SPELL_ATTR_EX_NO_AUTOCAST_AI = 0x00020000
SPELL_ATTR_EX_FINISHING_MOVE_DAMAGE = 0x00100000
SPELL_ATTR_EX_FINISHING_MOVE_DURATION = 0x00400000

# ``SpellAttributesEx2`` bits.
SPELL_ATTR_EX2_DONT_BLOCK_MANA_REGEN = 0x02000000

# Target types
TARGET_TYPE_SELF = 0
TARGET_TYPE_TARGET = 1
TARGET_TYPE_RANDOM = 2
TARGET_TYPE_SELF_RANDOM = 3
TARGET_TYPE_ALL_ENEMIES = 4
TARGET_TYPE_ALL_ALLIES = 5
TARGET_TYPE_ALL = 6

# Spell flags
SPELL_FLAG_NO_TARGETS = 1
SPELL_FLAG_NO_CASTING = 2
SPELL_FLAG_NO_COOLDOWN = 3
SPELL_FLAG_NO_MANA = 4
SPELL_FLAG_NO_REAGENTS = 5
SPELL_FLAG_NO_INTERRUPT = 6
SPELL_FLAG_NO_COOLDOWN_2 = 7

# Spell categories
SPELL_CATEGORY_MELEE = 1
SPELL_CATEGORY_RANGED = 2
SPELL_CATEGORY_OFFHAND = 3
SPELL_CATEGORY_SPELL = 4

# Target types
TARGET_TYPE_SELF = 0
TARGET_TYPE_TARGET = 1
TARGET_TYPE_RANDOM = 2
TARGET_TYPE_SELF_RANDOM = 3
TARGET_TYPE_ALL_ENEMIES = 4
TARGET_TYPE_ALL_ALLIES = 5
TARGET_TYPE_ALL = 6

# Effect indices
EFFECT_0 = 0
EFFECT_1 = 1
EFFECT_2 = 2

# Target indices
TARGET_A = 0
TARGET_B = 1

# Spell cast flags
SPELL_CAST_FLAG_NO_TARGETS = 0x00000001
SPELL_CAST_FLAG_NO_COOLDOWN = 0x00000004
SPELL_CAST_FLAG_NO_MANA = 0x00000008
SPELL_CAST_FLAG_NO_REAGENTS = 0x00000010
SPELL_CAST_FLAG_NO_INTERRUPT = 0x00000020
SPELL_CAST_FLAG_NO_COOLDOWN_2 = 0x00000040

# ``SpellCastData`` cast flags from CMaNGOS 3.3.5 (``Spell.h``).  The two
# "base" values are what the core always ORs in: ``CAST_FLAG_UNKNOWN2`` on
# ``SMSG_SPELL_START`` and ``CAST_FLAG_UNKNOWN9`` on ``SMSG_SPELL_GO``.
CAST_FLAG_HIDDEN_COMBATLOG = 0x00000001
CAST_FLAG_SPELL_START = 0x00000002
CAST_FLAG_PERSISTENT_AA = 0x00000010
CAST_FLAG_AMMO = 0x00000020
CAST_FLAG_SPELL_GO = 0x00000100
CAST_FLAG_PREDICTED_POWER = 0x00000800
CAST_FLAG_ADJUST_MISSILE = 0x00020000
CAST_FLAG_NO_GCD = 0x00040000
CAST_FLAG_PREDICTED_RUNES = 0x00200000
CAST_FLAG_IMMUNITY = 0x04000000

# Spell miss types
SPELL_MISS_NONE = 0
SPELL_MISS_MISS = 1
SPELL_MISS_DODGE = 2
SPELL_MISS_PARRY = 3
SPELL_MISS_BLOCK = 4
SPELL_MISS_DEFLECT = 5
SPELL_MISS_EVADE = 6
SPELL_MISS_IMMUNE = 7
SPELL_MISS_IMMUNE_2 = 8
SPELL_MISS_REFLECT = 9
SPELL_MISS_ABSORB = 10
SPELL_MISS_RESIST = 11

# Spell hit types
SPELL_HIT_NORMAL = 0
SPELL_HIT_CRITICAL = 1
SPELL_HIT_CRUSHING = 2
SPELL_HIT_GLANCING = 3

# Spell cast states
CAST_STATE_NONE = 0
CAST_STATE_PREPARING = 1
CAST_STATE_CASTING = 2
CAST_STATE_FINISHED = 3
CAST_STATE_FAILED = 4


class SpellTemplate:
    """A parsed spell from the spell_templates table."""

    def __init__(self, row: Mapping):
        self.id = int(_row_get(row, "id", _row_get(row, "Id", 0)))
        self.name = str(_row_get(row, "name", _row_get(row, "SpellName", "")))
        self.school = int(_row_get(row, "school", _row_get(row, "School", 0)))
        self.mana_cost = int(_row_get(row, "mana_cost", _row_get(row, "ManaCost", 0)))
        self.mana_cost_per_level = int(_row_get(row, "mana_cost_per_level", 0))
        self.mana_cost_percentage = int(_row_get(row, "mana_cost_percentage", 0))
        self.casting_time = int(_row_get(row, "casting_time_index", 0))
        self.recovery_time = int(_row_get(row, "recovery_time", 0))
        self.category_recovery = int(_row_get(row, "category_recovery_time", 0))
        self.start_recovery_time = int(_row_get(row, "start_recovery_time", 0))
        self.range_index = int(_row_get(row, "range_index", 0))
        # ``DurationIndex`` points into ``SpellDuration.dbc``.  Summoning reads
        # it to decide whether a summon is permanent (index 21 = -1) or expires.
        self.duration_index = int(_row_get(row, "duration_index", 0))
        self.category = int(_row_get(row, "category", 0))
        self.speed = float(_row_get(row, "speed", 0.0))
        self.power_type = int(_row_get(row, "power_type", 0))
        self.spell_level = int(_row_get(row, "spell_level", 0))
        self.max_level = int(_row_get(row, "max_level", 0))
        self.base_level = int(_row_get(row, "base_level", 0))
        self.attributes = int(_row_get(row, "attributes", 0))
        self.attributes_ex = int(_row_get(row, "attributes_ex", 0))
        self.attributes_ex2 = int(_row_get(row, "attributes_ex2", 0))
        self.dmg_class = int(_row_get(row, "dmg_class", 0))
        self.spell_family = int(_row_get(row, "spell_family_name", 0))
        self.spell_visual = int(_row_get(row, "spell_visual", 0))
        # ``RuneCostID`` points into ``SpellRuneCost.dbc``.  It is only set for
        # death knight abilities (and a few NPC spells that reuse the field).
        self.rune_cost_id = int(_row_get(row, "rune_cost_id", 0))
        # Effects
        self.effects = []
        for i in (1, 2, 3):
            effect_type = int(_row_get(row, f"effect{i}", 0))
            base_points = int(_row_get(row, f"effect_base_points{i}", 0))
            die_sides = int(_row_get(row, f"effect_die_sides{i}", 0))
            target_a = int(_row_get(row, f"effect_implicit_target_a{i}", 0))
            target_b = int(_row_get(row, f"effect_implicit_target_b{i}", 0))
            misc_value = int(_row_get(row, f"effect_misc_value{i}", 0))
            misc_value_b = int(_row_get(row, f"effect_misc_value_b{i}", 0))
            apply_aura = int(_row_get(row, f"effect_apply_aura_name{i}", 0))
            radius = int(_row_get(row, f"effect_radius_index{i}", 0))
            amplitude = int(_row_get(row, f"effect_amplitude{i}", 0))
            trigger_spell = int(_row_get(row, f"effect_trigger_spell{i}", 0))
            chain_targets = int(_row_get(row, f"effect_chain_target{i}", 0))
            item_type = int(_row_get(row, f"effect_item_type{i}", 0))
            points_per_combo = float(_row_get(row, f"effect_points_per_combo_point{i}", 0.0))
            points_per_level = float(_row_get(row, f"effect_real_points_per_level{i}", 0.0))
            self.effects.append({
                "effect": effect_type,
                "base_points": base_points,
                "die_sides": die_sides,
                "target_a": target_a,
                "target_b": target_b,
                "misc_value": misc_value,
                "misc_value_b": misc_value_b,
                "apply_aura": apply_aura,
                "radius": radius,
                "amplitude": amplitude,
                "trigger_spell": trigger_spell,
                "chain_targets": chain_targets,
                "item_type": item_type,
                "points_per_combo": points_per_combo,
                "points_per_level": points_per_level,
            })

    def has_effect(self, effect_type: int) -> bool:
        return any(e["effect"] == effect_type for e in self.effects)

    def summon_entries(self) -> List[Tuple[int, bool]]:
        """Return ``(creature_entry, is_pet)`` for every summon effect.

        ``is_pet`` is true for ``SPELL_EFFECT_SUMMON_PET`` (warlock demons,
        Call Pet, ...) and false for the generic ``SPELL_EFFECT_SUMMON`` used by
        guardians such as Mirror Image or a quest summon.  A zero entry means
        "summon the caster's stored pet" (Call Pet), which this server has no
        storage for.
        """
        found = []
        for effect in self.effects:
            etype = int(effect["effect"])
            if etype == SPELL_EFFECT_SUMMON_PET:
                found.append((int(effect.get("misc_value") or 0), True))
            elif etype == SPELL_EFFECT_SUMMON:
                found.append((int(effect.get("misc_value") or 0), False))
        return found

    def is_totem(self) -> bool:
        """``SPELL_ATTR_TOTEM``: summon a stationary totem, not a pet."""
        return bool(self.attributes & SPELL_ATTR_TOTEM)

    def needs_combo_points(self) -> bool:
        """``NeedsComboPoints``: a finishing move consumes combo points.

        CMaNGOS marks them with either ``SPELL_ATTR_EX_FINISHING_MOVE_DAMAGE``
        (Eviscerate, Envenom, ...) or ``SPELL_ATTR_EX_FINISHING_MOVE_DURATION``
        (Slice and Dice, Kidney Shot, ...).  Both require the caster to already
        have combo points on the spell's target.
        """
        finishing = (SPELL_ATTR_EX_FINISHING_MOVE_DAMAGE
                     | SPELL_ATTR_EX_FINISHING_MOVE_DURATION)
        return bool(self.attributes_ex & finishing)

    def get_effect(self, index: int) -> Optional[Dict[str, Any]]:
        if 0 <= index < len(self.effects):
            return self.effects[index]
        return None

    def get_range(self) -> float:
        """Return the spell's maximum range in yards (SpellRange.dbc).

        The numbers were checked against the 3.3.5 client data; index 1 is a
        self-only spell and index 13 is "anywhere".
        """
        ranges = {
            1: 0.0, 2: 5.0, 3: 20.0, 4: 30.0, 5: 40.0, 6: 100.0, 7: 10.0,
            8: 20.0, 9: 30.0, 10: 40.0, 11: 15.0, 12: 5.0, 13: 50000.0,
            14: 60.0, 34: 25.0, 35: 35.0, 36: 45.0, 37: 50.0, 54: 30.0,
            74: 30.0, 94: 40.0, 95: 25.0, 96: 2.0, 114: 35.0, 134: 80.0,
            135: 100.0, 137: 8.0, 157: 90.0, 160: 30.0, 161: 20.0,
            176: 70.0,
        }
        return float(ranges.get(self.range_index, 5.0))

    def get_casting_time_ms(self) -> int:
        """Return the cast time in milliseconds (SpellCastTimes.dbc)."""
        times = {
            0: 0, 1: 0, 2: 250, 3: 500, 4: 1000, 5: 2000, 6: 5000,
            7: 10000, 8: 20000, 9: 30000, 14: 3000, 15: 4000, 16: 1500,
            18: 0, 19: 2500, 20: 2500, 21: 2600, 22: 3500, 23: 1800,
            24: 2200, 25: 2900, 28: 3200, 30: 4500, 31: 2300, 32: 7000,
            33: 2000, 34: 3000, 35: 12500, 36: 600, 37: 25000,
            38: 45000, 39: 50000, 50: 1300, 70: 300000, 90: 1700,
            91: 2800, 110: 750, 130: 1600, 170: 8000, 171: 6000,
            190: 100, 193: 12000, 198: 0,
        }
        return int(times.get(self.casting_time, 0))

    def effect_value(self, effect_index: int, caster_level: int = 1,
                     combo_points: int = 0) -> int:
        """Roll one effect's value the way the client's tooltip computes it.

        ``EffectBasePoints`` stores ``base - 1`` and ``EffectDieSides`` the
        number of sides on the damage die, so a Fireball rank 1 with base 13
        and die 9 rolls 14..22.  ``EffectRealPointsPerLevel`` adds a linear
        bonus up to ``MaxLevel``.  ``EffectPointsPerComboPoint`` adds
        ``points_per_combo * combo_points``, which is how Eviscerate's damage
        grows with the rogue's combo points (``WorldObject::
        CalculateSpellEffectValue``).  The caller only passes combo points when
        the spell target *is* the caster's combo target, exactly like the core.
        """
        effect = self.get_effect(effect_index)
        if effect is None:
            return 0
        base = int(effect["base_points"])
        die = int(effect["die_sides"])
        per_level = float(effect.get("points_per_level") or 0.0)
        value = base + (random.randint(1, die) if die > 0 else 1)
        if per_level and caster_level > 0:
            levels = max(0, caster_level - int(self.base_level or 1))
            max_level = int(self.max_level or 0)
            if max_level > 0:
                levels = min(levels, max(0, max_level - int(self.base_level or 1)))
            value += per_level * levels
        per_combo = float(effect.get("points_per_combo") or 0.0)
        if per_combo and combo_points > 0:
            value += per_combo * int(combo_points)
        return int(value)


class SpellCooldowns:
    """Tracks spell cooldowns for a unit."""

    def __init__(self):
        self._cooldowns: Dict[int, float] = {}

    def is_ready(self, spell_id: int) -> bool:
        ready = time.monotonic() - self._cooldowns.get(spell_id, 0)
        return ready >= 0

    def add(self, spell_id: int, duration_ms: int = 0):
        if duration_ms > 0:
            self._cooldowns[spell_id] = time.monotonic() + duration_ms / 1000.0
        else:
            self._cooldowns.pop(spell_id, None)

    def clear(self):
        self._cooldowns.clear()


class SpellEffects:
    """Applies spell effects to targets."""

    @staticmethod
    def calculate_damage(template: SpellTemplate, effect_index: int,
                         attacker_level: int) -> int:
        """Calculate one effect's rolled amount (damage or healing)."""
        return max(0, template.effect_value(effect_index, attacker_level))

    @staticmethod
    def apply_health(session, target_guid: int, template: SpellTemplate,
                     effect_index: int, attacker_level: int) -> int:
        """Apply a health heal effect."""
        effect = template.get_effect(effect_index)
        if effect is None:
            return 0
        heal = SpellEffects.calculate_damage(template, effect_index, attacker_level)
        if heal <= 0:
            return 0
        return heal

    @staticmethod
    def apply_power(session, target_guid: int, template: SpellTemplate,
                    effect_index: int, attacker_level: int) -> int:
        """Apply a power (mana/rage/energy) effect."""
        effect = template.get_effect(effect_index)
        if effect is None:
            return 0
        power = SpellEffects.calculate_damage(template, effect_index, attacker_level)
        if power <= 0:
            return 0
        return power

    @staticmethod
    def apply_school_damage(session, target_guid: int, template: SpellTemplate,
                            effect_index: int, attacker_level: int,
                            school: int = 0) -> int:
        """Apply school damage to a target."""
        effect = template.get_effect(effect_index)
        if effect is None:
            return 0
        damage = SpellEffects.calculate_damage(template, effect_index, attacker_level)
        if damage <= 0:
            return 0
        return damage


class SpellCast:
    """Represents a spell casting attempt."""

    def __init__(self, caster_guid: int, spell_id: int, target_guid: int = 0,
                 x: float = 0.0, y: float = 0.0, z: float = 0.0):
        self.caster_guid = caster_guid
        self.spell_id = spell_id
        self.target_guid = target_guid
        self.x = x
        self.y = y
        self.z = z
        self.state = CAST_STATE_NONE
        self.started_at = time.monotonic()
        self.cast_time_ms = 0
        self.finished = False
        self.template: Optional[SpellTemplate] = None


class SpellResult:
    """Result of a spell cast."""

    def __init__(self):
        self.success = False
        self.damage = 0
        self.heal = 0
        self.power = 0
        self.miss_type = SPELL_MISS_NONE
        self.hit_type = SPELL_HIT_NORMAL
        self.target_guid = 0
        self.spell_id = 0


def get_spell_template(path: str, spell_id: int) -> Optional[SpellTemplate]:
    """Fetch a spell template from the database."""
    import sqlite3
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM spell_templates WHERE id = ?", (int(spell_id),)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return SpellTemplate(row)


# ``SpellDuration.dbc`` values that matter for summoning.  The DBC itself is
# not part of the world dump; ``spell_duration`` is filled from
# ``--spell-duration-dbc`` when the client files are available.  Until then the
# common summon durations are encoded here so a timed guardian still expires.
SUMMON_DURATION_FALLBACK_MS = {
    0: -1,          # no duration field: permanent/script-controlled
    18: 18000,      # 18 s (Mirror Image)
    21: -1,         # until cancelled (warlock pets, most guardians)
    22: 30000,      # 30 s (Force of Nature, elementals)
}


def get_spell_duration(path: str, duration_index: int) -> Optional[Tuple[int, int, int]]:
    """Return ``(duration, duration_per_level, max_duration)`` for an index.

    ``None`` means the index is unknown (the table is missing or has no row).
    """
    if not duration_index:
        return None
    import sqlite3
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT duration, duration_per_level, max_duration"
            " FROM spell_duration WHERE id = ?", (int(duration_index),),
        ).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        con.close()
    if row is None:
        return None
    return (int(row["duration"] or 0), int(row["duration_per_level"] or 0),
            int(row["max_duration"] or 0))


def summon_duration_ms(path: str, duration_index: int, level: int = 1) -> int:
    """How long a summoned creature lives, in milliseconds (``-1`` permanent).

    Mirrors ``Spell::CalculateDuration``: the base duration plus the
    per-level part, capped by ``MaxDuration``.  A negative base duration means
    "until cancelled", which is what warlock pets use.
    """
    index = int(duration_index or 0)
    data = get_spell_duration(path, index)
    if data is None:
        return int(SUMMON_DURATION_FALLBACK_MS.get(index, -1))
    duration, per_level, max_duration = data
    if duration < 0:
        return -1
    total = duration + per_level * max(0, int(level) - 1)
    if max_duration > 0:
        total = min(total, max_duration)
    return total if total > 0 else -1


def get_creature_spells(path: str, entry: int) -> List[int]:
    """Fetch the spell IDs a creature can cast.

    ``creature_template_spells`` is keyed by ``(entry, set_id)``; a normal
    creature uses set 0 (difficulty/instance sets are not simulated).  The
    legacy list is only consulted when ``creature_template.SpellList`` is 0.
    """
    import sqlite3
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM creature_template_spells"
            " WHERE entry = ? AND set_id = 0", (int(entry),)
        ).fetchone()
        if row is None:
            row = con.execute(
                "SELECT * FROM creature_template_spells WHERE entry = ?"
                " ORDER BY set_id LIMIT 1", (int(entry),)
            ).fetchone()
    finally:
        con.close()
    if row is None:
        return []
    spells = []
    for col in ("spell1", "spell2", "spell3", "spell4", "spell5",
                "spell6", "spell7", "spell8", "spell9", "spell10"):
        value = row[col] if col in row.keys() else 0
        if value:
            spells.append(int(value))
    return spells


def get_creature_spell_list(path: str, spell_list_id: int) -> List[dict]:
    """Return a ``creature_spell_list`` as an ordered list of spell entries.

    CMaNGOS 3.3.5 keeps scripted creature spells in ``creature_spell_list``
    (``creature_template.SpellList``) instead of the older
    ``creature_template_spells`` set.  Every row carries the spell's target,
    availability, probability and repeat/initial delay, which is what the AI
    uses to decide whether a spell may be cast right now.
    """
    if not spell_list_id:
        return []
    import sqlite3
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT position, spell_id, flags, combat_condition, target_id,"
            " script_id, availability, probability, initial_min, initial_max,"
            " repeat_min, repeat_max FROM creature_spell_list"
            " WHERE id = ? ORDER BY position",
            (int(spell_list_id),),
        ).fetchall()
    finally:
        con.close()
    entries = []
    for row in rows:
        if not int(row["spell_id"] or 0):
            continue
        entries.append({
            "position": int(row["position"] or 0),
            "spell_id": int(row["spell_id"] or 0),
            "flags": int(row["flags"] or 0),
            "combat_condition": int(row["combat_condition"] or 0),
            "target_id": int(row["target_id"] or 0),
            "script_id": int(row["script_id"] or 0),
            "availability": int(row["availability"] or 0),
            "probability": int(row["probability"] or 0),
            "initial_min": int(row["initial_min"] or 0),
            "initial_max": int(row["initial_max"] or 0),
            "repeat_min": int(row["repeat_min"] or 0),
            "repeat_max": int(row["repeat_max"] or 0),
        })
    return entries


def get_creature_cooldowns(path: str, entry: int) -> Dict[int, Tuple[int, int]]:
    """Return ``{spell_id: (cooldown_min, cooldown_max)}`` for a creature."""
    import sqlite3
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT spell_id, cooldown_min, cooldown_max"
            " FROM creature_cooldowns WHERE entry = ?",
            (int(entry),),
        ).fetchall()
    finally:
        con.close()
    return {
        int(spell_id): (int(low or 0), int(high or 0))
        for spell_id, low, high in rows
    }


def get_creature_spell_list_entry(path: str, spell_list_id: int) -> Dict[str, int]:
    """Return the ``creature_spell_list_entry`` header for a spell list.

    ``ChanceSupportAction`` / ``ChanceRangedAttack`` are rolled once per 1200 ms
    spell-list tick to decide whether a support or ranged action is allowed.
    A missing header simply means both chances are zero.
    """
    if not spell_list_id:
        return {}
    import sqlite3
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT chance_support_action, chance_ranged_attack"
            " FROM creature_spell_list_entry WHERE id = ?",
            (int(spell_list_id),),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return {}
    return {
        "chance_support_action": int(row[0] or 0),
        "chance_ranged_attack": int(row[1] or 0),
    }


def get_creature_spell_targeting(path: str, target_id: int) -> Optional[Dict[str, int]]:
    """Return one ``creature_spell_targeting`` row (``None`` when unknown)."""
    import sqlite3
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT type, param1, param2, param3, unit_condition"
            " FROM creature_spell_targeting WHERE id = ?",
            (int(target_id),),
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return None
    return {
        "type": int(row[0] or 0),
        "param1": int(row[1] or 0),
        "param2": int(row[2] or 0),
        "param3": int(row[3] or 0),
        "unit_condition": int(row[4] if row[4] is not None else -1),
    }


def get_creature_base_mana(path: str, level: int, unit_class: int) -> int:
    """``creature_template_classlevelstats.BaseMana`` for a level/class."""
    import sqlite3
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT base_mana FROM creature_template_classlevelstats"
            " WHERE level = ? AND class = ?",
            (int(level), int(unit_class)),
        ).fetchone()
    finally:
        con.close()
    return int(row[0] or 0) if row else 0


def get_rune_cost(path: str, rune_cost_id: int) -> Tuple[int, int, int, int]:
    """Return ``(blood, unholy, frost, runic_power)`` for a ``RuneCostID``.

    The values live in ``SpellRuneCost.dbc`` and are copied into the
    ``spell_rune_cost`` table by the import tool.  When that table is missing
    (a database built before the rune system existed) the embedded copy of the
    3.3.5a DBC is used instead.
    """
    rune_cost_id = int(rune_cost_id or 0)
    if rune_cost_id <= 0:
        return (0, 0, 0, 0)
    try:
        import sqlite3
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT blood, unholy, frost, runic_power"
                " FROM spell_rune_cost WHERE id = ?",
                (rune_cost_id,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        row = None
    if row is not None:
        return (int(row[0] or 0), int(row[1] or 0),
                int(row[2] or 0), int(row[3] or 0))
    return RUNE_COST_FALLBACK.get(rune_cost_id, (0, 0, 0, 0))


def pack_guid(guid: int) -> bytes:
    """Pack an ObjectGuid the way 3.3.5 packet writers expect.

    The first byte is a mask of the non-zero bytes, followed by those bytes in
    increasing order (``ObjectGuid::WriteAsPacked``).
    """
    guid = int(guid) & 0xFFFFFFFFFFFFFFFF
    mask = 0
    data = bytearray()
    for index in range(8):
        byte = (guid >> (index * 8)) & 0xFF
        if byte:
            mask |= 1 << index
            data.append(byte)
    return bytes([mask]) + bytes(data)


def _target_data(target_guid: int) -> bytes:
    """``SpellTargetData`` for a single unit target (or a self cast).

    ``SpellCastTargets::write`` starts with the ``uint32`` target mask and only
    appends the packed unit guid when ``TARGET_FLAG_UNIT`` (0x2) is set.
    """
    if target_guid:
        return struct.pack("<I", 0x02) + pack_guid(target_guid)
    return struct.pack("<I", 0)


def _go_target_list(target_guid: int, caster_guid: int) -> bytes:
    """The hit/miss target lists of ``SMSG_SPELL_GO``.

    ``Spell::WriteSpellGoTargets`` always writes both counts -- a ``uint8`` hit
    count followed by that many full ``uint64`` guids, then a ``uint8`` miss
    count and the miss entries.  Omitting them makes the client read the
    packet's target mask as the hit count, so every field after it (including
    the target guid) is garbage.

    The hit/miss guids are *not* packed: the core does ``data << targetGUID``
    and the 3.3.5 client reads exactly 8 raw bytes per entry.  Writing a packed
    guid here shifts every following field by two bytes, and the client can end
    up parsing the target mask as ``0xD7DA0000`` -- whose ``0x80000`` bit makes
    it read a guid fragment as an element count and ``alloca(count * 40)``,
    crashing with a stack overflow.  A self cast lists the caster, matching the
    core, which puts the caster into its own hit list.
    """
    guid = int(target_guid or caster_guid) & 0xFFFFFFFFFFFFFFFF
    if not guid:
        return struct.pack("<BB", 0, 0)      # hit count 0, miss count 0
    return struct.pack("<BQ", 1, guid) + struct.pack("<B", 0)


def build_spell_start(cast: SpellCast, caster_guid: int, target_guid: int = 0,
                      cast_id: int = 0, flags: Optional[int] = None) -> bytes:
    """Build ``SMSG_SPELL_START`` (3.3.5 ``SpellCastData``)."""
    out = bytearray()
    out += pack_guid(caster_guid)
    out += pack_guid(caster_guid)          # CasterUnit
    out += struct.pack("<B", int(cast_id) & 0xFF)
    out += struct.pack("<I", int(cast.spell_id))
    out += struct.pack("<I", CAST_FLAG_SPELL_START if flags is None else flags)
    out += struct.pack("<I", int(cast.cast_time_ms))
    out += _target_data(target_guid)
    return bytes(out)


def build_spell_go(cast: SpellCast, caster_guid: int, target_guid: int = 0,
                   cast_id: int = 0, timestamp_ms: int = 0,
                   predicted_power: Optional[int] = None,
                   off_gcd: bool = False, flags: Optional[int] = None,
                   rune_state_before: Optional[int] = None,
                   rune_state_after: Optional[int] = None,
                   rune_progress: Optional[List[int]] = None) -> bytes:
    """Build ``SMSG_SPELL_GO`` (3.3.5 ``SpellCastData``).

    ``Spell::SendSpellGo`` writes the caster twice, the cast id, spell id, cast
    flags and the server tick, then the hit/miss lists, then the target data,
    and finally the caster's remaining power when ``CAST_FLAG_PREDICTED_POWER``
    is set.  ``CAST_FLAG_NO_GCD`` tells the client the spell does not start the
    global cooldown, which is how instant warrior abilities such as Heroic
    Strike stay usable between swings.

    A death knight cast additionally sets ``CAST_FLAG_PREDICTED_RUNES`` and
    appends the rune state before and after the cast (one bit per available
    rune) plus the cooldown progress of every rune that was just spent, so the
    client can grey out the correct runes without waiting for a resync.
    """
    if flags is None:
        flags = CAST_FLAG_SPELL_GO
        if predicted_power is not None:
            flags |= CAST_FLAG_PREDICTED_POWER
        if off_gcd:
            flags |= CAST_FLAG_NO_GCD
        if rune_state_before is not None:
            flags |= CAST_FLAG_PREDICTED_RUNES
    out = bytearray()
    out += pack_guid(caster_guid)
    out += pack_guid(caster_guid)          # CasterUnit
    out += struct.pack("<B", int(cast_id) & 0xFF)
    out += struct.pack("<I", int(cast.spell_id))
    out += struct.pack("<I", flags)
    out += struct.pack("<I", int(timestamp_ms) & 0xFFFFFFFF)
    out += _go_target_list(target_guid, caster_guid)
    out += _target_data(target_guid)
    if flags & CAST_FLAG_PREDICTED_POWER:
        out += struct.pack("<I", int(predicted_power or 0) & 0xFFFFFFFF)
    if flags & CAST_FLAG_PREDICTED_RUNES:
        before = int(rune_state_before or 0) & 0xFF
        after = int(rune_state_after or 0) & 0xFF
        out += struct.pack("<BB", before, after)
        for progress in rune_progress or ():
            out += struct.pack("<B", int(progress) & 0xFF)
    return bytes(out)


def build_resync_runes(runes) -> bytes:
    """``SMSG_RESYNC_RUNES``: authoritative rune types and cooldowns.

    ``runes`` is a sequence of ``(current_type, cooldown_ms)``.  CMaNGOS writes
    ``255 - cooldown * 255 / RUNE_COOLDOWN`` as the "passed" byte, i.e. 255 for
    a ready rune and 0 for one that was just spent.
    """
    out = bytearray(struct.pack("<I", len(runes)))
    for rune_type, cooldown_ms in runes:
        cooldown = max(0, min(RUNE_COOLDOWN, int(cooldown_ms)))
        passed = 255 - (cooldown * 255 // RUNE_COOLDOWN)
        out += struct.pack("<BB", int(rune_type) & 0xFF, passed & 0xFF)
    return bytes(out)


def build_add_rune_power(index: int) -> bytes:
    """``SMSG_ADD_RUNE_POWER``: one bit marks the rune that just came back."""
    return struct.pack("<I", 1 << (int(index) & 0x1F))


def build_cast_failed(cast_id: int, spell_id: int, reason: int) -> bytes:
    """``SMSG_CAST_FAILED``: uint8 cast id, uint32 spell id, uint8 reason."""
    return struct.pack("<BIB", int(cast_id) & 0xFF, int(spell_id), int(reason) & 0xFF)


def build_spell_failure(caster_guid: int, cast_id: int, spell_id: int,
                        reason: int) -> bytes:
    """``SMSG_SPELL_FAILURE``: packed caster, cast id, spell id, reason."""
    return (pack_guid(caster_guid) + struct.pack("<BIB", int(cast_id) & 0xFF,
                                                 int(spell_id),
                                                 int(reason) & 0xFF))