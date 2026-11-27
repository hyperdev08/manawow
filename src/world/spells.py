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
import threading
from functools import lru_cache
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
SPELL_EFFECT_POWER_DRAIN = 8
SPELL_EFFECT_HEALTH_LEECH = 9
SPELL_EFFECT_HEAL = 10
SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL = 17
SPELL_EFFECT_QUEST_COMPLETE = 16
SPELL_EFFECT_CREATE_ITEM = 24
SPELL_EFFECT_PERSISTENT_AREA_AURA = 27
SPELL_EFFECT_SUMMON = 28
SPELL_EFFECT_ENERGIZE = 30
SPELL_EFFECT_WEAPON_PERCENT_DAMAGE = 31
SPELL_EFFECT_TRIGGER_MISSILE = 32
SPELL_EFFECT_APPLY_AREA_AURA_PARTY = 35
SPELL_EFFECT_DISPEL = 38
SPELL_EFFECT_JUMP_DEST = 42
SPELL_EFFECT_SPAWN = 46
SPELL_EFFECT_TRADE_SKILL = 47
SPELL_EFFECT_TRANSMITTED_OBJECT = 50
SPELL_EFFECT_POWER_BURN = 62
SPELL_EFFECT_TELEPORT_UNITS = 5
SPELL_EFFECT_RESURRECT_OLD = 18
SPELL_EFFECT_ENCHANT_ITEM = 53
SPELL_EFFECT_ENCHANT_ITEM_TEMPORARY = 54
SPELL_EFFECT_LEARN_SPELL = 36
SPELL_EFFECT_SKILL_STEP = 44
SPELL_EFFECT_PROFICIENCY = 60
SPELL_EFFECT_SUMMON_PET = 56
SPELL_EFFECT_WEAPON_DAMAGE = 58
SPELL_EFFECT_THREAT = 63
SPELL_EFFECT_TRIGGER_SPELL = 64
SPELL_EFFECT_APPLY_AREA_AURA_RAID = 65
SPELL_EFFECT_ACTIVATE_OBJECT = 86
SPELL_EFFECT_GAMEOBJECT_DAMAGE = 87
SPELL_EFFECT_KILL_CREDIT = 90
SPELL_EFFECT_CHARGE = 96
SPELL_EFFECT_INEBRIATE = 100
SPELL_EFFECT_DISPEL_MECHANIC = 108
SPELL_EFFECT_FORCE_CAST = 140
SPELL_EFFECT_FORCE_CAST_WITH_VALUE = 141
SPELL_EFFECT_FORCE_CAST_2 = 160
SPELL_EFFECT_TALENT_SPEC_COUNT = 161
SPELL_EFFECT_TALENT_SPEC_SELECT = 162
SPELL_EFFECT_INTERRUPT_CAST = 68
SPELL_EFFECT_APPLY_GLYPH = 74
SPELL_EFFECT_SUMMON_OBJECT_WILD = 76
SPELL_EFFECT_SEND_EVENT = 61
# Opening, Herb Gathering, Mining, Pick Lock: unlock and use a game object.
SPELL_EFFECT_OPEN_LOCK = 33
SPELL_EFFECT_ATTACK = 78
SPELL_EFFECT_ADD_COMBO_POINTS = 80
SPELL_EFFECT_LEAP = 29
SPELL_EFFECT_JUMP = 41
SPELL_EFFECT_ADD_HONOR = 45
SPELL_EFFECT_LEARN_PET_SPELL = 57
SPELL_EFFECT_SANCTUARY = 79
SPELL_EFFECT_REPUTATION = 103
SPELL_EFFECT_UNLEARN_SPECIALIZATION = 133
SPELL_EFFECT_CREATE_TAMED_PET = 153
SPELL_EFFECT_PULL_TOWARDS = 124
SPELL_EFFECT_ENERGIZE_PCT = 137
SPELL_EFFECT_KNOCK_BACK = 98
SPELL_EFFECT_TELEPORT_UNITS_FACE_CASTER = 43
SPELL_EFFECT_TELEPORT_UNITS_SET_FACING = 95
SPELL_EFFECT_SUMMON_OBJECT_SLOT1 = 104
SPELL_EFFECT_SUMMON_OBJECT_SLOT2 = 105
SPELL_EFFECT_SUMMON_OBJECT_SLOT3 = 106
SPELL_EFFECT_SUMMON_OBJECT_SLOT4 = 107
SPELL_EFFECT_SKILL = 118
SPELL_EFFECT_APPLY_AREA_AURA_PET = 119
SPELL_EFFECT_APPLY_AREA_AURA_FRIEND = 128
SPELL_EFFECT_APPLY_AREA_AURA_ENEMY = 129
SPELL_EFFECT_HEAL_PCT = 136
SPELL_EFFECT_TRIGGER_SPELL_WITH_VALUE = 142
SPELL_EFFECT_APPLY_AREA_AURA_OWNER = 143
SPELL_EFFECT_TRIGGER_MISSILE_SPELL_WITH_VALUE = 148
SPELL_EFFECT_CREATE_ITEM_2 = 157
SPELL_EFFECT_KILL_CREDIT_2 = 134
SPELL_EFFECT_TRIGGER_SPELL_2 = 151
SPELL_EFFECT_ATTACK_ME = 114
SPELL_EFFECT_RESURRECT = 113
SPELL_EFFECT_MODIFY_THREAT_PERCENT = 125
SPELL_EFFECT_REDIRECT_THREAT = 130
SPELL_EFFECT_HEAL_MAX_HEALTH = 67
SPELL_EFFECT_NORMALIZED_WEAPON_DMG = 121
SPELL_EFFECT_STEAL_BENEFICIAL = 126

# Aura types (Spell.dbc EffectApplyAuraName).
SPELL_AURA_NONE = 0
SPELL_AURA_MOD_POSSESS = 2
SPELL_AURA_MOD_CONFUSE = 5
SPELL_AURA_MOD_CHARM = 6
SPELL_AURA_MOD_FEAR = 7
SPELL_AURA_PERIODIC_DAMAGE = 3
SPELL_AURA_DUMMY = 4
SPELL_AURA_PERIODIC_HEAL = 8
SPELL_AURA_MOD_TAUNT = 11
SPELL_AURA_MOD_STEALTH = 16
SPELL_AURA_MOD_DETECT = 17
SPELL_AURA_MOD_INVISIBILITY = 18
SPELL_AURA_MOD_INVISIBILITY_DETECT = 19
SPELL_AURA_OBS_MOD_HEALTH = 20
SPELL_AURA_MOD_ATTACKSPEED = 9
SPELL_AURA_MOD_STUN = 12
SPELL_AURA_MOD_DAMAGE_DONE = 13
SPELL_AURA_MOD_DAMAGE_TAKEN = 14
SPELL_AURA_DAMAGE_SHIELD = 15
SPELL_AURA_MOD_RESISTANCE = 22
SPELL_AURA_PERIODIC_TRIGGER_SPELL = 23
SPELL_AURA_PERIODIC_ENERGIZE = 24
SPELL_AURA_MOD_PACIFY = 25
SPELL_AURA_MOD_ROOT = 26
SPELL_AURA_MOD_SILENCE = 27
SPELL_AURA_REFLECT_SPELLS = 28
SPELL_AURA_MOD_STAT = 29
SPELL_AURA_MOD_INCREASE_SPEED = 31
SPELL_AURA_MOD_INCREASE_MOUNTED_SPEED = 32
SPELL_AURA_MOD_DECREASE_SPEED = 33
SPELL_AURA_PROC_TRIGGER_SPELL = 42
SPELL_AURA_PROC_TRIGGER_DAMAGE = 43
SPELL_AURA_MOD_DISARM = 67
SPELL_AURA_MOUNTED = 78
SPELL_AURA_ADD_TARGET_TRIGGER = 109
SPELL_AURA_FORCE_REACTION = 139
SPELL_AURA_MOD_INCREASE_MOUNTED_FLIGHT_SPEED = 207
SPELL_AURA_ADD_FLAT_MODIFIER = 107
SPELL_AURA_ADD_PCT_MODIFIER = 108
SPELL_AURA_MOD_PET_TALENT_POINTS = 145
SPELL_AURA_MOD_RATING = 189
SPELL_AURA_MELEE_SLOW = 193
SPELL_AURA_MOD_INCREASE_HEALTH = 34
SPELL_AURA_MOD_INCREASE_ENERGY = 35
SPELL_AURA_EFFECT_IMMUNITY = 37
SPELL_AURA_STATE_IMMUNITY = 38
SPELL_AURA_SCHOOL_IMMUNITY = 39
SPELL_AURA_DAMAGE_IMMUNITY = 40
SPELL_AURA_DISPEL_IMMUNITY = 41
SPELL_AURA_MOD_PARRY_PERCENT = 47
SPELL_AURA_MOD_DODGE_PERCENT = 49
SPELL_AURA_MOD_BLOCK_PERCENT = 51
SPELL_AURA_MOD_WEAPON_CRIT_PERCENT = 52
SPELL_AURA_PERIODIC_LEECH = 53
SPELL_AURA_MOD_HIT_CHANCE = 54
SPELL_AURA_MOD_SPELL_HIT_CHANCE = 55
SPELL_AURA_TRANSFORM = 56
SPELL_AURA_MOD_SPELL_CRIT_CHANCE = 57
SPELL_AURA_MOD_PACIFY_SILENCE = 60
SPELL_AURA_MOD_CASTING_SPEED_NOT_STACK = 65
SPELL_AURA_SCHOOL_ABSORB = 69
SPELL_AURA_MECHANIC_IMMUNITY = 77
SPELL_AURA_MOD_DAMAGE_PERCENT_DONE = 79
SPELL_AURA_MOD_PERCENT_STAT = 80
SPELL_AURA_MOD_DAMAGE_PERCENT_TAKEN = 87
SPELL_AURA_MOD_HEALTH_REGEN_PERCENT = 88
SPELL_AURA_PERIODIC_DAMAGE_PERCENT = 89
SPELL_AURA_MOD_REGEN = 84
SPELL_AURA_MOD_POWER_REGEN = 85
SPELL_AURA_MOD_HEALTH_REGEN_IN_COMBAT = 161
SPELL_AURA_MOD_SCALE = 61
SPELL_AURA_MOD_BASE_RESISTANCE_PCT = 142
SPELL_AURA_MOD_SHIELD_BLOCKVALUE = 158
SPELL_AURA_MOD_SKILL = 30
SPELL_AURA_PERIODIC_DUMMY = 226
SPELL_AURA_MOD_ATTACK_POWER = 99
SPELL_AURA_MOD_RESISTANCE_PCT = 101
SPELL_AURA_MOD_HEALING = 115
SPELL_AURA_MOD_HEALING_PCT = 118
SPELL_AURA_MOD_TARGET_RESISTANCE = 123
SPELL_AURA_MOD_RANGED_ATTACK_POWER = 124
SPELL_AURA_MOD_INCREASE_ENERGY_PERCENT = 132
SPELL_AURA_MOD_INCREASE_HEALTH_PERCENT = 133
SPELL_AURA_MOD_HEALING_DONE = 135
SPELL_AURA_MOD_HEALING_DONE_PERCENT = 136
SPELL_AURA_MOD_TOTAL_STAT_PERCENTAGE = 137
SPELL_AURA_MOD_MELEE_HASTE = 138
SPELL_AURA_MOD_RANGED_HASTE = 140
SPELL_AURA_MOD_ATTACK_POWER_PCT = 166
SPELL_AURA_MOD_RANGED_ATTACK_POWER_PCT = 167
SPELL_AURA_HASTE_SPELLS = 216
SPELL_AURA_MECHANIC_DURATION_MOD = 232
SPELL_AURA_MOD_MECHANIC_DAMAGE_TAKEN_PERCENT = 255

# ``SpellModOp`` (SpellDefines.h).  Aura 107/108 stores one of these in its
# MiscValue and the affected spell-family mask in EffectSpellClassMask.
SPELLMOD_DAMAGE = 0
SPELLMOD_DURATION = 1
SPELLMOD_THREAT = 2
SPELLMOD_EFFECT1 = 3
SPELLMOD_RANGE = 5
SPELLMOD_RADIUS = 6
SPELLMOD_CRITICAL_CHANCE = 7
SPELLMOD_ALL_EFFECTS = 8
SPELLMOD_CASTING_TIME = 10
SPELLMOD_COOLDOWN = 11
SPELLMOD_EFFECT2 = 12
SPELLMOD_IGNORE_ARMOR = 13
SPELLMOD_COST = 14
SPELLMOD_EFFECT3 = 23
SPELLMOD_GLOBAL_COOLDOWN = 21
SPELLMOD_DAMAGE_MULTIPLIER = 20
# 3.3.5 Death and Decay uses this periodic dummy aura for its ground ticks.
SPELL_AURA_PERIODIC_DUMMY = 226
SPELL_AURA_PERIODIC_TRIGGER_SPELL_WITH_VALUE = 227

# ``SpellMechanic`` ids used for crowd control and immunity checks.
MECHANIC_NONE = 0
MECHANIC_CHARM = 1
MECHANIC_DISORIENTED = 2
MECHANIC_DISARM = 3
MECHANIC_DISTRACT = 4
MECHANIC_FEAR = 5
MECHANIC_GRIP = 6
MECHANIC_ROOT = 7
MECHANIC_SLOW_ATTACK = 8
MECHANIC_SILENCE = 9
MECHANIC_SLEEP = 10
MECHANIC_SNARE = 11
MECHANIC_STUN = 12
MECHANIC_FREEZE = 13
MECHANIC_KNOCKOUT = 14
MECHANIC_BLEED = 15
MECHANIC_BANDAGE = 16
MECHANIC_POLYMORPH = 17
MECHANIC_BANISH = 18
MECHANIC_SHIELD = 19
MECHANIC_SHACKLE = 20
MECHANIC_MOUNT = 21
MECHANIC_INFECTED = 22
MECHANIC_TURN = 23
MECHANIC_HORROR = 24
MECHANIC_INVULNERABILITY = 25
MECHANIC_INTERRUPT = 26
MECHANIC_DAZE = 27
MECHANIC_DISCOVERY = 28
MECHANIC_IMMUNE_SHIELD = 29
MECHANIC_SAPPED = 30
MECHANIC_ENRAGED = 31
MECHANIC_TAUNTED = 36

# ``DispelType`` ids.
DISPEL_NONE = 0
DISPEL_MAGIC = 1
DISPEL_CURSE = 2
DISPEL_DISEASE = 3
DISPEL_POISON = 4
DISPEL_ALL = 7
DISPEL_ALL_MASK = (1 << DISPEL_MAGIC) | (1 << DISPEL_CURSE) \
    | (1 << DISPEL_DISEASE) | (1 << DISPEL_POISON)

# Diminishing-return groups.  A repeated control spell in the same group is
# reduced to 1/2, 1/4 and then immune; the counter resets after 15 seconds.
DR_GROUPS = {
    MECHANIC_STUN: "stun", MECHANIC_FREEZE: "stun",
    MECHANIC_KNOCKOUT: "stun", MECHANIC_SLEEP: "sleep",
    MECHANIC_ROOT: "root", MECHANIC_SNARE: "root",
    MECHANIC_FEAR: "fear", MECHANIC_HORROR: "fear",
    MECHANIC_POLYMORPH: "polymorph", MECHANIC_BANISH: "polymorph",
    MECHANIC_SHACKLE: "polymorph", MECHANIC_CHARM: "charm",
    MECHANIC_DISORIENTED: "disorient", MECHANIC_SILENCE: "silence",
    MECHANIC_TAUNTED: "taunt",
}
DR_MULTIPLIERS = (1.0, 0.5, 0.25, 0.0)
DR_RESET_SECONDS = 15.0


# ``SPELL_AURA_MOD_STAT`` misc value -> primary attribute.
STAT_AURA_NAMES = ("str", "agi", "sta", "inte", "spi")
# ``SPELL_AURA_MOD_RESISTANCE`` misc value is a school mask (bit 0 = physical).
RESISTANCE_AURA_SCHOOLS = (
    (2, "holy"), (4, "fire"), (8, "nature"), (16, "frost"),
    (32, "shadow"), (64, "arcane"),
)

# ``EffectImplicitTarget`` values used by area selection (3.3.5a).
TARGET_UNIT_CASTER = 1
TARGET_UNIT_TARGET_ENEMY = 6
TARGET_UNIT_SRC_AREA_ENEMY = 15
TARGET_UNIT_DEST_AREA_ENEMY = 16
TARGET_DEST_CASTER = 18
TARGET_UNIT_CASTER_AREA_PARTY = 20
TARGET_UNIT_TARGET_ALLY = 21
TARGET_SRC_CASTER = 22
TARGET_UNIT_CONE_ENEMY_24 = 24
TARGET_UNIT_TARGET_ANY = 25
TARGET_DEST_DYNOBJ_ENEMY = 28
TARGET_UNIT_SRC_AREA_ALLY = 30
TARGET_UNIT_DEST_AREA_ALLY = 31
TARGET_UNIT_SRC_AREA_PARTY = 33
TARGET_UNIT_DEST_AREA_PARTY = 34
TARGET_DEST_DEST = 87
TARGET_UNIT_CONE_ENEMY_54 = 54
TARGET_UNIT_CONE_ENEMY_104 = 104

AREA_ENEMY_TARGETS = frozenset((TARGET_UNIT_SRC_AREA_ENEMY,
                                TARGET_UNIT_DEST_AREA_ENEMY))
AREA_ALLY_TARGETS = frozenset((TARGET_UNIT_CASTER_AREA_PARTY,
                               TARGET_UNIT_SRC_AREA_ALLY,
                               TARGET_UNIT_DEST_AREA_ALLY,
                               TARGET_UNIT_SRC_AREA_PARTY,
                               TARGET_UNIT_DEST_AREA_PARTY))
CONE_TARGETS = frozenset((TARGET_UNIT_CONE_ENEMY_24,
                          TARGET_UNIT_CONE_ENEMY_54,
                          TARGET_UNIT_CONE_ENEMY_104))

# ``SpellRadius.dbc`` build 12340.  The server only needs the maximum radius
# for area selection; every 3.3.5 row has RadiusPerLevel = 0.
SPELL_RADII = {
    7: 2.0, 8: 5.0, 9: 20.0, 10: 30.0, 11: 45.0, 12: 100.0, 13: 10.0,
    14: 8.0, 15: 3.0, 16: 1.0, 17: 13.0, 18: 15.0, 19: 18.0, 20: 25.0,
    21: 35.0, 22: 200.0, 23: 40.0, 24: 65.0, 25: 70.0, 26: 4.0, 27: 50.0,
    28: 50000.0, 29: 6.0, 30: 500.0, 31: 80.0, 32: 12.0, 33: 99.0,
    35: 55.0, 36: 0.0, 37: 7.0, 38: 21.0, 39: 34.0, 40: 9.0, 41: 150.0,
    42: 11.0, 43: 16.0, 44: 0.5, 45: 10.0, 46: 5.0, 47: 15.0, 48: 60.0,
    49: 90.0, 50: 15.0, 51: 60.0, 52: 5.0, 53: 60.0, 54: 50000.0,
    55: 130.0, 56: 38.0, 57: 45.0, 58: 50000.0, 59: 32.0, 60: 44.0,
    61: 14.0, 62: 47.0, 63: 23.0, 64: 3.5, 65: 80.0,
}


def spell_radius(radius_index: int) -> float:
    """Yards for a ``SpellRadius.dbc`` index (0 when unknown)."""
    return float(SPELL_RADII.get(int(radius_index or 0), 0.0))


def item_enchant_proc(name: str, points: int, spell_id: int) -> dict:
    """Build a weapon-enchant proc entry from a SpellItemEnchantment row.

    Instant/Wound Poison are PPM effects; Deadly/Crippling/Mind-numbing use a
    flat chance.  The DBC's EffectPointsMin is a flat chance for the latter but
    not for the PPM poisons, so use the known 3.3.5 poison rates.
    """
    label = str(name or "").lower()
    if "instant poison" in label:
        return {"spell_id": int(spell_id), "ppm": 8.57}
    if "wound poison" in label:
        return {"spell_id": int(spell_id), "ppm": 21.4}
    if "deadly poison" in label:
        return {"spell_id": int(spell_id), "chance": 30.0}
    if "crippling poison" in label or "mind-numbing" in label \
            or "anesthetic poison" in label:
        return {"spell_id": int(spell_id), "chance": 50.0}
    chance = float(points) if 0 < points <= 100 else 30.0
    return {"spell_id": int(spell_id), "chance": chance}

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
SPELL_ATTR_USES_RANGED_SLOT = 0x00000002
SPELL_ATTR_ON_NEXT_SWING = 0x00000400
SPELL_ATTR_PASSIVE = 0x00000040
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
SPELL_ATTR_EX_CHANNELED = 0x00000004
SPELL_ATTR_EX_NO_THREAT = 0x00000400
SPELL_ATTR_EX_NO_AUTOCAST_AI = 0x00020000
SPELL_ATTR_EX_FINISHING_MOVE_DAMAGE = 0x00100000
SPELL_ATTR_EX_FINISHING_MOVE_DURATION = 0x00400000
# ``SPELL_ATTR_EX3_CANT_MISS``: a few internal/triggered spells skip the roll.
SPELL_ATTR_EX3_CANT_MISS = 0x00000010
SPELL_ATTR_EX3_NO_THREAT = 0x00000400

# ``Spell.dbc`` ``FacingCasterFlags``.
SPELL_FACING_FLAG_INFRONT = 0x00000001

# ``ItemEnchantmentType`` values used by temporary weapon enchants/poisons.
ITEM_ENCHANTMENT_COMBAT_SPELL = 1
ITEM_ENCHANTMENT_DAMAGE = 2
ITEM_ENCHANTMENT_EQUIP_SPELL = 3
ITEM_ENCHANTMENT_USE_SPELL = 7

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

# Spell miss types (3.3.5 ``SpellMissInfo`` order).
SPELL_MISS_NONE = 0
SPELL_MISS_MISS = 1
SPELL_MISS_RESIST = 2
SPELL_MISS_DODGE = 3
SPELL_MISS_PARRY = 4
SPELL_MISS_BLOCK = 5
SPELL_MISS_EVADE = 6
SPELL_MISS_IMMUNE = 7
SPELL_MISS_IMMUNE_2 = 8
SPELL_MISS_DEFLECT = 9
SPELL_MISS_ABSORB = 10
SPELL_MISS_REFLECT = 11

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
        self.rank = str(_row_get(row, "rank", _row_get(row, "Rank", "")) or "")
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
        # ``SpellTargetRestrictions.MaxTargets``; 0 means "no cap".
        self.max_targets = int(_row_get(row, "max_affected_targets", 0))
        # ``Spell.dbc`` StackAmount; 0/1 means the aura does not stack.
        self.stack_amount = int(_row_get(row, "stack_amount", 0))
        self.category = int(_row_get(row, "category", 0))
        self.speed = float(_row_get(row, "speed", 0.0))
        self.power_type = int(_row_get(row, "power_type", 0))
        self.spell_level = int(_row_get(row, "spell_level", 0))
        self.max_level = int(_row_get(row, "max_level", 0))
        self.base_level = int(_row_get(row, "base_level", 0))
        self.attributes = int(_row_get(row, "attributes", 0))
        self.attributes_ex = int(_row_get(row, "attributes_ex", 0))
        self.attributes_ex2 = int(_row_get(row, "attributes_ex2", 0))
        self.attributes_ex3 = int(_row_get(row, "attributes_ex3", 0))
        self.attributes_ex4 = int(_row_get(row, "attributes_ex4", 0))
        self.attributes_ex5 = int(_row_get(row, "attributes_ex5", 0))
        self.attributes_ex6 = int(_row_get(row, "attributes_ex6", 0))
        self.attributes_ex7 = int(_row_get(row, "attributes_ex7", 0))
        self.dispel_type = int(_row_get(row, "dispel_type", 0))
        self.mechanic = int(_row_get(row, "mechanic", 0))
        self.caster_aura_state = int(_row_get(row, "caster_aura_state", 0))
        self.target_aura_state = int(_row_get(row, "target_aura_state", 0))
        self.caster_aura_state_not = int(_row_get(row, "caster_aura_state_not", 0))
        self.target_aura_state_not = int(_row_get(row, "target_aura_state_not", 0))
        self.caster_aura_spell = int(_row_get(row, "caster_aura_spell", 0))
        self.target_aura_spell = int(_row_get(row, "target_aura_spell", 0))
        self.requires_spell_focus = int(_row_get(row, "requires_spell_focus", 0))
        # Tool requirements: ``TotemCategory`` (crafting hammer, mining pick,
        # skinning knife, ...) and literal totem items consumed by shaman spells.
        self.totem_category = (
            int(_row_get(row, "totem_category1", 0)),
            int(_row_get(row, "totem_category2", 0)),
        )
        self.totem_items = (
            int(_row_get(row, "totem1", 0)),
            int(_row_get(row, "totem2", 0)),
        )
        self.facing_caster_flags = int(_row_get(row, "facing_caster_flags", 0))
        self.mana_per_second = int(_row_get(row, "mana_per_second", 0))
        self.mana_per_second_per_level = int(
            _row_get(row, "mana_per_second_per_level", 0))
        self.interrupt_flags = int(_row_get(row, "interrupt_flags", 0))
        self.channel_interrupt_flags = int(
            _row_get(row, "channel_interrupt_flags", 0))
        self.dmg_class = int(_row_get(row, "dmg_class", 0))
        self.spell_family = int(_row_get(row, "spell_family_name", 0))
        self.spell_family_flags = (
            int(_row_get(row, "spell_family_flags", 0)),
            int(_row_get(row, "spell_family_flags2", 0)),
            0,
        )
        self.spell_visual = int(_row_get(row, "spell_visual", 0))
        # Weapon/armour proficiency spells (SMSG_SET_PROFICIENCY).
        self.equipped_item_class = int(_row_get(row, "equipped_item_class", 0))
        self.equipped_item_subclass_mask = int(
            _row_get(row, "equipped_item_subclass_mask", 0))
        # ``RuneCostID`` points into ``SpellRuneCost.dbc``.  It is only set for
        # death knight abilities (and a few NPC spells that reuse the field).
        self.rune_cost_id = int(_row_get(row, "rune_cost_id", 0))
        # Reagent requirements, checked at cast start and consumed when the cast
        # finishes (``Spell::CheckCast`` / ``Spell::TakeReagents``).
        self.reagents = []
        for i in range(1, 9):
            reagent = int(_row_get(row, f"reagent{i}", 0))
            count = int(_row_get(row, f"reagent_count{i}", 0))
            if reagent:
                self.reagents.append((reagent, max(1, count)))
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
            effect_mechanic = int(_row_get(row, f"effect_mechanic{i}", 0))
            radius = int(_row_get(row, f"effect_radius_index{i}", 0))
            amplitude = int(_row_get(row, f"effect_amplitude{i}", 0))
            trigger_spell = int(_row_get(row, f"effect_trigger_spell{i}", 0))
            chain_targets = int(_row_get(row, f"effect_chain_target{i}", 0))
            multiple_value = float(
                _row_get(row, f"effect_multiple_value{i}", 0.0) or 0.0)
            item_type = int(_row_get(row, f"effect_item_type{i}", 0))
            points_per_combo = float(_row_get(row, f"effect_points_per_combo_point{i}", 0.0))
            points_per_level = float(_row_get(row, f"effect_real_points_per_level{i}", 0.0))
            bonus_coefficient = float(_row_get(row, f"effect_bonus_coefficient{i}", 0.0))
            class_mask = tuple(
                int(_row_get(row, f"effect_spell_class_mask{i}_{j}", 0))
                for j in (1, 2, 3))
            self.effects.append({
                "effect": effect_type,
                "base_points": base_points,
                "die_sides": die_sides,
                "target_a": target_a,
                "target_b": target_b,
                "misc_value": misc_value,
                "misc_value_b": misc_value_b,
                "apply_aura": apply_aura,
                "mechanic": effect_mechanic,
                "radius": radius,
                "amplitude": amplitude,
                "trigger_spell": trigger_spell,
                "chain_targets": chain_targets,
                "multiple_value": multiple_value,
                "class_mask": class_mask,
                "item_type": item_type,
                "points_per_combo": points_per_combo,
                "points_per_level": points_per_level,
                "bonus_coefficient": bonus_coefficient,
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

    def has_weapon_effect(self) -> bool:
        """True for weapon-damage abilities (Heroic Strike, Sinister Strike...)."""
        return any(int(effect.get("effect") or 0) in (
            SPELL_EFFECT_WEAPON_DAMAGE,
            SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
            SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
            SPELL_EFFECT_WEAPON_PERCENT_DAMAGE,
            SPELL_EFFECT_ATTACK,
        ) for effect in self.effects)

    def is_self_targeted(self) -> bool:
        """Whether the spell's primary target is the caster.

        Item procs and poisons use both self buffs (Destiny) and enemy damage
        spells (Fiery War Axe), so the proc code needs this distinction.
        """
        if int(self.range_index) == 1:
            return True
        has_self = False
        for effect in self.effects:
            a = int(effect.get("target_a") or 0)
            b = int(effect.get("target_b") or 0)
            if a in (TARGET_UNIT_TARGET_ENEMY, TARGET_UNIT_SRC_AREA_ENEMY,
                     TARGET_UNIT_DEST_AREA_ENEMY, TARGET_UNIT_CONE_ENEMY_24,
                     TARGET_UNIT_CONE_ENEMY_54, TARGET_UNIT_CONE_ENEMY_104) \
                    or b in (TARGET_UNIT_TARGET_ENEMY,
                             TARGET_UNIT_SRC_AREA_ENEMY,
                             TARGET_UNIT_DEST_AREA_ENEMY,
                             TARGET_UNIT_CONE_ENEMY_24,
                             TARGET_UNIT_CONE_ENEMY_54,
                             TARGET_UNIT_CONE_ENEMY_104):
                return False
            if a in (TARGET_UNIT_CASTER, TARGET_SRC_CASTER, TARGET_DEST_CASTER) \
                    or b in (TARGET_UNIT_CASTER, TARGET_SRC_CASTER,
                             TARGET_DEST_CASTER):
                has_self = True
        return has_self

    def is_channeled(self) -> bool:
        """``SPELL_ATTR_EX_IS_CHANNELED``: cast a channel instead of a one-shot."""
        return bool(self.attributes_ex & SPELL_ATTR_EX_CHANNELED)

    def is_passive(self) -> bool:
        return bool(self.attributes & SPELL_ATTR_PASSIVE)

    def uses_ranged_slot(self) -> bool:
        return bool(self.attributes & SPELL_ATTR_USES_RANGED_SLOT)

    def is_harmful(self) -> bool:
        """Whether this is an offensive spell (used for aura colour/flags)."""
        # Spells explicitly marked as a debuff by the DBC.
        if self.attributes & 0x40000000:            # SPELL_ATTR0_AURA_IS_DEBUFF
            return True
        # An enemy implicit target is the strongest signal, and catches curses
        # and debuffs whose aura types look neutral on their own.
        for effect in self.effects:
            if int(effect.get("target_a") or 0) in (
                    TARGET_UNIT_TARGET_ENEMY, TARGET_UNIT_SRC_AREA_ENEMY,
                    TARGET_UNIT_DEST_AREA_ENEMY, TARGET_UNIT_CONE_ENEMY_24,
                    TARGET_UNIT_CONE_ENEMY_54, TARGET_UNIT_CONE_ENEMY_104):
                return True
            if int(effect.get("target_b") or 0) in (
                    TARGET_UNIT_TARGET_ENEMY, TARGET_UNIT_SRC_AREA_ENEMY,
                    TARGET_UNIT_DEST_AREA_ENEMY, TARGET_UNIT_CONE_ENEMY_24,
                    TARGET_UNIT_CONE_ENEMY_54, TARGET_UNIT_CONE_ENEMY_104):
                return True
        negative_effects = 0
        positive_effects = 0
        for effect in self.effects:
            etype = int(effect.get("effect") or 0)
            aura = int(effect.get("apply_aura") or 0)
            if etype in (SPELL_EFFECT_SCHOOL_DAMAGE, SPELL_EFFECT_HEALTH_LEECH,
                         SPELL_EFFECT_INSTAKILL, SPELL_EFFECT_INTERRUPT_CAST,
                         SPELL_EFFECT_DISPEL, SPELL_EFFECT_STEAL_BENEFICIAL):
                negative_effects += 1
            elif aura in (SPELL_AURA_MOD_STUN, SPELL_AURA_MOD_ROOT,
                          SPELL_AURA_MOD_FEAR, SPELL_AURA_MOD_CHARM,
                          SPELL_AURA_MOD_CONFUSE, SPELL_AURA_MOD_SILENCE,
                          SPELL_AURA_MOD_PACIFY, SPELL_AURA_MOD_PACIFY_SILENCE,
                          SPELL_AURA_MOD_DECREASE_SPEED,
                          SPELL_AURA_MOD_DAMAGE_PERCENT_TAKEN,
                          SPELL_AURA_PERIODIC_DAMAGE, SPELL_AURA_PERIODIC_LEECH):
                negative_effects += 1
            elif etype in (SPELL_EFFECT_HEAL, SPELL_EFFECT_HEAL_MAX_HEALTH) \
                    or aura in (SPELL_AURA_PERIODIC_HEAL,
                                SPELL_AURA_MOD_INCREASE_HEALTH,
                                SPELL_AURA_MOD_INCREASE_ENERGY,
                                SPELL_AURA_MOD_STAT,
                                SPELL_AURA_MOD_ATTACK_POWER,
                                SPELL_AURA_MOD_INCREASE_SPEED):
                positive_effects += 1
        return negative_effects > positive_effects

    def cc_mechanic(self, effect: dict | None = None) -> int:
        """Mechanic id for crowd-control detection.

        ``Spell.dbc`` carries the mechanic on the spell and sometimes on the
        individual effect.  Fall back to the aura type for the many CC spells
        whose mechanic column is zero (Frost Nova is the classic example).
        """
        mechanic = int((effect or {}).get("mechanic") or 0) or int(self.mechanic)
        if mechanic:
            return mechanic
        aura = int((effect or {}).get("apply_aura") or 0)
        return {
            SPELL_AURA_MOD_STUN: MECHANIC_STUN,
            SPELL_AURA_MOD_ROOT: MECHANIC_ROOT,
            SPELL_AURA_MOD_FEAR: MECHANIC_FEAR,
            SPELL_AURA_MOD_CHARM: MECHANIC_CHARM,
            SPELL_AURA_MOD_CONFUSE: MECHANIC_DISORIENTED,
            SPELL_AURA_MOD_SILENCE: MECHANIC_SILENCE,
            SPELL_AURA_MOD_PACIFY: MECHANIC_SILENCE,
            SPELL_AURA_MOD_DECREASE_SPEED: MECHANIC_SNARE,
        }.get(aura, MECHANIC_NONE)

    def get_effect(self, index: int) -> Optional[Dict[str, Any]]:
        if 0 <= index < len(self.effects):
            return self.effects[index]
        return None

    def effect_radius(self, effect_index: int) -> float:
        """Area radius in yards for one effect (``SpellRadius.dbc``)."""
        effect = self.get_effect(effect_index)
        if effect is None:
            return 0.0
        return spell_radius(effect.get("radius") or 0)

    def effect_area_kind(self, effect_index: int) -> Optional[str]:
        """Classify an effect's implicit targets.

        Returns ``"enemy"`` for area enemy effects, ``"ally"`` for party/ally
        areas, ``"cone"`` for cone effects and ``None`` for single-target
        effects.  The 3.3.5 DBC puts the selector in ``EffectImplicitTargetB``
        for most spells (e.g. Arcane Explosion is ``22, 15``) and occasionally
        in A; the ground anchors (``TARGET_SRC_CASTER``/``DEST_CASTER``) are not
        selectors themselves.
        """
        effect = self.get_effect(effect_index)
        if effect is None:
            return None
        a = int(effect.get("target_a") or 0)
        b = int(effect.get("target_b") or 0)
        if a in CONE_TARGETS or b in CONE_TARGETS:
            return "cone"
        if b in AREA_ENEMY_TARGETS or a in AREA_ENEMY_TARGETS:
            return "enemy"
        if b in AREA_ALLY_TARGETS or a in AREA_ALLY_TARGETS:
            return "ally"
        return None

    def is_ground_area(self, effect_index: int) -> bool:
        """True for a persistent ground effect (Blizzard, Consecration, ...)."""
        effect = self.get_effect(effect_index)
        if effect is None:
            return False
        if int(effect.get("effect") or 0) != SPELL_EFFECT_PERSISTENT_AREA_AURA:
            return False
        a = int(effect.get("target_a") or 0)
        b = int(effect.get("target_b") or 0)
        return (a in (TARGET_DEST_CASTER, TARGET_DEST_DYNOBJ_ENEMY)
                or b in AREA_ENEMY_TARGETS or b in AREA_ALLY_TARGETS
                or self.effect_area_kind(effect_index) is not None)

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

    def get_min_range(self) -> float:
        """Return the spell's minimum range in yards (SpellRange.dbc)."""
        ranges = {
            1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0, 7: 0.0,
            8: 10.0, 9: 10.0, 10: 10.0, 11: 0.0, 12: 0.0, 13: 0.0,
            14: 0.0, 34: 0.0, 35: 0.0, 36: 0.0, 37: 0.0, 38: 10.0,
            54: 5.0, 74: 0.0, 94: 8.0, 95: 8.0, 96: 0.0, 114: 0.0,
            134: 0.0, 135: 0.0, 136: 30.0, 137: 0.0, 139: 5.0,
            140: 0.0, 141: 0.0, 150: 8.0, 151: 5.0, 152: 0.0,
            153: 0.0, 154: 10.0, 155: 0.0, 156: 30.0, 157: 0.0,
            159: 0.0, 160: 0.0, 161: 0.0, 162: 0.0, 163: 8.0,
            164: 10.0, 167: 40.0, 169: 0.0, 170: 0.0, 172: 0.0,
            173: 5.0, 174: 0.0, 176: 0.0, 180: 5.0, 181: 0.0,
            184: 5.0, 187: 0.0,
        }
        return float(ranges.get(self.range_index, 0.0))

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
        # Item guid carried by TARGET_FLAG_ITEM (temporary weapon enchants).
        self.item_guid = 0
        # True when the client sent a destination location (ground-targeted
        # spells such as Blizzard, Flamestrike or Death and Decay).
        self.has_dest = False
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


_SPELL_TEMPLATE_CACHE: Dict[Tuple[str, int], Optional[SpellTemplate]] = {}
_SPELL_TEMPLATE_LOCK = threading.RLock()
_SPELL_TEMPLATE_LIMIT = 8192


def _cache_spell_template(key: Tuple[str, int],
                          template: Optional[SpellTemplate]) -> None:
    with _SPELL_TEMPLATE_LOCK:
        if key not in _SPELL_TEMPLATE_CACHE \
                and len(_SPELL_TEMPLATE_CACHE) >= _SPELL_TEMPLATE_LIMIT:
            # Cheap FIFO eviction: enough to bound memory for a client dataset
            # with tens of thousands of spells.
            _SPELL_TEMPLATE_CACHE.pop(next(iter(_SPELL_TEMPLATE_CACHE)))
        _SPELL_TEMPLATE_CACHE[key] = template


def get_spell_template(path: str, spell_id: int) -> Optional[SpellTemplate]:
    """Fetch a spell template from the database (cached).

    Client spell data is immutable for the lifetime of the server.  Combat,
    aura ticks and creature AI look the same templates up several times per
    second; opening a fresh SQLite connection for each lookup measured 6-8 ms
    on Windows and produced visible combat pauses.
    """
    key = (path, int(spell_id))
    with _SPELL_TEMPLATE_LOCK:
        if key in _SPELL_TEMPLATE_CACHE:
            return _SPELL_TEMPLATE_CACHE[key]
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM spell_templates WHERE id = ?", (int(spell_id),)
        ).fetchone()
    finally:
        con.close()
    template = SpellTemplate(row) if row is not None else None
    _cache_spell_template(key, template)
    return template


def warm_spell_templates(path: str, spell_ids) -> None:
    """Load many spell templates in one query and put them in the cache.

    Opening the trainer window needs every spell in the list; doing that as
    dozens of separate connections was a visible first-open pause.
    """
    wanted = sorted({int(spell_id) for spell_id in spell_ids if spell_id})
    if not wanted:
        return
    with _SPELL_TEMPLATE_LOCK:
        missing = [spell_id for spell_id in wanted
                   if (path, spell_id) not in _SPELL_TEMPLATE_CACHE]
    if not missing:
        return
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        found: Dict[int, SpellTemplate] = {}
        for start in range(0, len(missing), 500):
            chunk = missing[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = con.execute(
                f"SELECT * FROM spell_templates WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows:
                found[int(row["id"])] = SpellTemplate(row)
        for spell_id in missing:
            _cache_spell_template((path, spell_id), found.get(spell_id))
    finally:
        con.close()


# ``SpellDuration.dbc`` values used when the table has not been imported from
# the client files.  Only the entries the server can reach are listed; the full
# table is filled by ``--spell-duration-dbc``.
SPELL_DURATION_FALLBACK_MS = {
    0: 0,
    1: 10000,
    3: 60000,
    4: 120000,
    5: 300000,
    6: 600000,
    8: 15000,
    9: 30000,
    18: 20000,
    21: -1,
    22: 45000,
    23: 90000,
    24: 160000,
    25: 180000,
    26: 240000,
    27: 3000,
    28: 5000,
    29: 12000,
    30: 1800000,
    31: 8000,
    32: 6000,
    35: 4000,
    36: 1000,
    38: 11000,
    39: 2000,
    40: 1200000,
    41: 360000,
    42: 3600000,
    62: 75000,
    63: 25000,
    64: 40000,
    65: 1500,
    66: 2500,
    85: 18000,
    86: 21000,
    105: 9000,
    106: 24000,
    125: 35000,
    165: 7000,
    185: 6000,
    186: 2000,
    187: 5000,
    205: 27000,
    225: 604800000,
    245: 50000,
    265: 55000,
    285: 6000,
    305: 14000,
    325: 36000,
    326: 44000,
    327: 500,
    328: 250,
    347: 900000,
    367: 7200000,
    387: 16000,
    407: 100,
    427: 600000,
    447: 6000,
    467: 22000,
    468: 26000,
    487: 1700,
    507: 1100,
    508: 1100,
    527: 14400000,
    547: 5400000,
    548: 10800000,
    549: 3800,
    550: 2147483647,
    551: 3500,
    552: 210000,
    553: 16000,
    554: 155000,
    555: 4500,
    556: 28000,
    557: 165000,
    558: 114000,
    559: 53000,
    560: 299000,
    561: 3300000,
    562: 150000,
    563: 20500,
    564: 13000,
    565: 70000,
    566: 0,
    567: 135000,
    568: 1250,
    569: 280000,
    570: 32000,
    571: 5500,
    572: 100000,
    573: 11999900,
    574: 200,
    575: 17000,
    576: 43200000,
    577: 160000,
    578: 14250,
    579: 170000,
    580: 64800000,
    581: 34000,
    582: 3200,
    583: 600,
    584: 800,
    585: 0,
    586: 25000,
    587: 31000,
    588: 30000,
    589: 6500,
    590: 330000,
    591: 80000,
    592: 400,
    593: 300,
    594: 660000,
    596: 900,
    597: 4700,
    598: 3600,
    600: 42000,
    602: 480000,
}

# Old name kept for the summoning module.
SUMMON_DURATION_FALLBACK_MS = SPELL_DURATION_FALLBACK_MS


@lru_cache(maxsize=2048)
def get_spell_duration(path: str, duration_index: int) -> Optional[Tuple[int, int, int]]:
    """Return ``(duration, duration_per_level, max_duration)`` for an index.

    ``None`` means the index is unknown (the table is missing or has no row).
    Durations are immutable DBC data, so the row is cached.
    """
    if not duration_index:
        return None
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


def spell_duration_ms(path: str, duration_index: int, level: int = 1) -> int:
    """Duration for a ``SpellDuration.dbc`` index, in milliseconds.

    Mirrors ``Spell::CalculateDuration``: the base duration plus the per-level
    part, capped by ``MaxDuration``.  ``-1`` means "until cancelled" and ``0``
    means "no duration".
    """
    index = int(duration_index or 0)
    data = get_spell_duration(path, index)
    if data is None:
        return int(SPELL_DURATION_FALLBACK_MS.get(index, 0))
    duration, per_level, max_duration = data
    if duration < 0:
        return -1
    total = duration + per_level * max(0, int(level) - 1)
    if max_duration > 0:
        total = min(total, max_duration)
    return total if total > 0 else 0


def summon_duration_ms(path: str, duration_index: int, level: int = 1) -> int:
    """How long a summoned creature lives, in milliseconds (``-1`` permanent).

    Summons treat "no duration" as "until dismissed" (most warlock pets use
    index 21, which is explicitly -1).
    """
    total = spell_duration_ms(path, duration_index, level)
    return total if total > 0 else -1


@lru_cache(maxsize=4096)
def _creature_spells_cached(path: str, entry: int) -> Tuple[int, ...]:
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
        return ()
    spells = []
    for col in ("spell1", "spell2", "spell3", "spell4", "spell5",
                "spell6", "spell7", "spell8", "spell9", "spell10"):
        value = row[col] if col in row.keys() else 0
        if value:
            spells.append(int(value))
    return tuple(spells)


@lru_cache(maxsize=2048)
def _spell_target_position_cached(path: str, spell_id: int):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT target_map, target_position_x, target_position_y,"
            " target_position_z, target_orientation"
            " FROM spell_target_position WHERE id = ?", (int(spell_id),)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return row


def get_spell_target_position(path: str, spell_id: int) -> Optional[dict]:
    """Destination of a teleport spell (``spell_target_position``)."""
    row = _spell_target_position_cached(path, int(spell_id))
    if row is None:
        return None
    return {
        "map": int(row["target_map"]),
        "x": float(row["target_position_x"]),
        "y": float(row["target_position_y"]),
        "z": float(row["target_position_z"]),
        "o": float(row["target_orientation"]),
    }


@lru_cache(maxsize=1024)
def _glyph_properties_cached(path: str, glyph_id: int):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT id, spell_id, slot_flags, icon_id"
            " FROM glyph_properties WHERE id = ?", (int(glyph_id),)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return row


def get_glyph_properties(path: str, glyph_id: int) -> Optional[dict]:
    """Return GlyphProperties.dbc data for an effect-74 glyph id."""
    row = _glyph_properties_cached(path, int(glyph_id))
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "spell_id": int(row["spell_id"]),
        "slot_flags": int(row["slot_flags"]),
        "icon_id": int(row["icon_id"]),
    }


@lru_cache(maxsize=4096)
def _skill_line_ability_cached(path: str, spell_id: int):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT spell_id, skill_line, race_mask, class_mask, min_rank,"
            " trivial_high, trivial_low FROM skill_line_ability"
            " WHERE spell_id = ?", (int(spell_id),)
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return tuple(rows)


def get_skill_line_ability(path: str, spell_id: int) -> List[dict]:
    """Profession SkillLineAbility rows for a recipe spell."""
    return [dict(row) for row in _skill_line_ability_cached(path, int(spell_id))]


@lru_cache(maxsize=1024)
def _totem_categories_cached(path: str):
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT id, category_type, category_mask FROM totem_categories"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return {int(row[0]): (int(row[1]), int(row[2])) for row in rows}


@lru_cache(maxsize=4096)
def _item_tool_fields_cached(path: str, entry: int) -> tuple:
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT totem_category, stackable FROM item_templates"
            " WHERE entry = ?", (int(entry),)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    if row is None:
        return (0, 1)
    return (int(row[0] or 0), max(1, int(row[1] or 1)))


def item_tool_fields(path: str, entry: int) -> tuple:
    """``(totem_category, max_stack)`` for an item template."""
    return _item_tool_fields_cached(path, int(entry))


def totem_category_compatible(item_category: int, required_category: int,
                              path: str) -> bool:
    """``IsTotemCategoryCompatiableWith`` for crafted-tool requirements."""
    if not required_category:
        return True
    if not item_category:
        return False
    categories = _totem_categories_cached(path)
    item = categories.get(int(item_category))
    required = categories.get(int(required_category))
    if item is None or required is None:
        return int(item_category) == int(required_category)
    item_type, item_mask = item
    required_type, required_mask = required
    if item_type != required_type:
        return False
    return (item_mask & required_mask) == required_mask


def get_creature_spells(path: str, entry: int) -> List[int]:
    """Fetch the spell IDs a creature can cast.

    ``creature_template_spells`` is keyed by ``(entry, set_id)``; a normal
    creature uses set 0 (difficulty/instance sets are not simulated).  The
    legacy list is only consulted when ``creature_template.SpellList`` is 0.
    """
    return list(_creature_spells_cached(path, int(entry)))


@lru_cache(maxsize=2048)
def _creature_spell_list_cached(path: str, spell_list_id: int
                                ) -> Tuple[dict, ...]:
    if not spell_list_id:
        return ()
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
    return tuple(entries)


def get_creature_spell_list(path: str, spell_list_id: int) -> List[dict]:
    """Return a ``creature_spell_list`` as an ordered list of spell entries.

    CMaNGOS 3.3.5 keeps scripted creature spells in ``creature_spell_list``
    (``creature_template.SpellList``) instead of the older
    ``creature_template_spells`` set.  Every row carries the spell's target,
    availability, probability and repeat/initial delay, which is what the AI
    uses to decide whether a spell may be cast right now.
    """
    return [dict(entry) for entry in
            _creature_spell_list_cached(path, int(spell_list_id))]


@lru_cache(maxsize=2048)
def _creature_cooldowns_cached(path: str, entry: int
                               ) -> Tuple[Tuple[int, int, int], ...]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT spell_id, cooldown_min, cooldown_max"
            " FROM creature_cooldowns WHERE entry = ?",
            (int(entry),),
        ).fetchall()
    finally:
        con.close()
    return tuple((int(spell_id), int(low or 0), int(high or 0))
                 for spell_id, low, high in rows)


def get_creature_cooldowns(path: str, entry: int) -> Dict[int, Tuple[int, int]]:
    """Return ``{spell_id: (cooldown_min, cooldown_max)}`` for a creature."""
    return {spell_id: (low, high)
            for spell_id, low, high in _creature_cooldowns_cached(
                path, int(entry))}


@lru_cache(maxsize=1024)
def _creature_spell_list_entry_cached(path: str, spell_list_id: int
                                      ) -> Tuple[int, int]:
    if not spell_list_id:
        return (0, 0)
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
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


def get_creature_spell_list_entry(path: str, spell_list_id: int) -> Dict[str, int]:
    """Return the ``creature_spell_list_entry`` header for a spell list.

    ``ChanceSupportAction`` / ``ChanceRangedAttack`` are rolled once per 1200 ms
    spell-list tick to decide whether a support or ranged action is allowed.
    A missing header simply means both chances are zero.
    """
    support, ranged = _creature_spell_list_entry_cached(
        path, int(spell_list_id))
    if not spell_list_id:
        return {}
    return {"chance_support_action": support,
            "chance_ranged_attack": ranged}


@lru_cache(maxsize=2048)
def _creature_spell_targeting_cached(path: str, target_id: int
                                     ) -> Optional[Tuple[int, int, int, int, int]]:
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
    return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0),
            int(row[3] or 0), int(row[4] if row[4] is not None else -1))


def get_creature_spell_targeting(path: str, target_id: int) -> Optional[Dict[str, int]]:
    """Return one ``creature_spell_targeting`` row (``None`` when unknown)."""
    data = _creature_spell_targeting_cached(path, int(target_id))
    if data is None:
        return None
    return {"type": data[0], "param1": data[1], "param2": data[2],
            "param3": data[3], "unit_condition": data[4]}


@lru_cache(maxsize=1024)
def get_creature_base_mana(path: str, level: int, unit_class: int) -> int:
    """``creature_template_classlevelstats.BaseMana`` for a level/class."""
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


def clear_caches() -> None:
    """Drop cached client data (used by tests and data-import tools)."""
    with _SPELL_TEMPLATE_LOCK:
        _SPELL_TEMPLATE_CACHE.clear()
    for cached in (get_spell_duration,
                   _creature_spells_cached, _creature_spell_list_cached,
                   _creature_cooldowns_cached,
                   _creature_spell_list_entry_cached,
                   _creature_spell_targeting_cached,
                   _spell_target_position_cached,
                   _glyph_properties_cached,
                   _skill_line_ability_cached,
                   _totem_categories_cached, _item_tool_fields_cached,
                   get_creature_base_mana, get_rune_cost):
        cached.cache_clear()


@lru_cache(maxsize=512)
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


def _target_data(target_guid: int, cast: Optional["SpellCast"] = None) -> bytes:
    """``SpellTargetData`` for a unit target and/or destination.

    ``SpellCastTargets::Write`` starts with the ``uint32`` target mask, appends
    the packed unit guid for ``TARGET_FLAG_UNIT`` (0x2), and for a destination
    (0x40) appends an empty packed transport guid followed by three floats.
    """
    flags = 0
    if target_guid:
        # Game objects (0xF110 high guid) go in the same packed-guid slot but
        # under TARGET_FLAG_GAMEOBJECT, or the client looks for a unit.
        flags |= 0x800 if (int(target_guid) >> 48) == 0xF110 else 0x02
    has_dest = bool(cast is not None and cast.has_dest)
    if has_dest:
        flags |= 0x40
    out = bytearray(struct.pack("<I", flags))
    if flags & 0x802:
        out += pack_guid(target_guid)
    if has_dest:
        out += b"\x00"                      # empty transport guid
        out += struct.pack("<fff", float(cast.x), float(cast.y), float(cast.z))
    return bytes(out)


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
    out += _target_data(target_guid, cast)
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
    out += _target_data(target_guid, cast)
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