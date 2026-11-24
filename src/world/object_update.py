"""Build the 3.3.5a ``SMSG_UPDATE_OBJECT`` create block for a player.

Wire layout mirrors CMaNGOS/TrinityCore 3.3.5 (``Object::BuildCreateUpdateBlockForPlayer``
+ ``Object::BuildMovementUpdate`` + ``Object::BuildValuesUpdate``):

    uint32 blockCount
    block:
        uint8  updateType       (3 = UPDATETYPE_CREATE_OBJECT2)
        packed guid
        uint8  objectTypeId     (4 = TYPEID_PLAYER)
        uint16 updateFlags
        movement info           (flags, flags2, time, x, y, z, o, fall time)
        float  speed[9]          (walk, run, run back, swim, swim back,
                                  flight, flight back, turn rate, pitch rate)
        uint32 0x2F              (HIGHGUID payload; only for the client's own
                                  player object)
        uint8  maskBlockCount
        uint32 mask[maskBlockCount]
        uint32 value[...]        (one per set bit, ascending field index)

Packets larger than 100 bytes are zlib-compressed and sent as
``SMSG_COMPRESSED_UPDATE_OBJECT`` with a leading uint32 uncompressed size,
exactly like TrinityCore's ``UpdateData::BuildPacket``.
"""
from __future__ import annotations

import math
import struct
import zlib
from typing import Dict, List, Mapping, Optional, Tuple

from src.protocol.opcodes import Opcodes
from src.world import update_fields as F
from src.world.creature import creature_guid
from src.world.update_fields import TYPEMASK_OBJECT, TYPEMASK_ITEM

# 3.3.5 UpdateType.  The client's SMSG_UPDATE_OBJECT dispatcher switches on
# these values directly (ObjectMgrClient.cpp, ObjectUpdateFirstPass/SecondPass):
#   0 -> values (CallMirrorHandlers), 1 -> movement, 2/3 -> create object,
#   4 -> out of range, 5 -> near objects.
# Sending 2 here makes the client parse the block as a create and drop the
# whole packet, which silently swallows every inventory update.
UPDATETYPE_VALUES = 0
UPDATETYPE_MOVEMENT = 1
UPDATETYPE_CREATE_OBJECT = 2
UPDATETYPE_CREATE_OBJECT2 = 3
UPDATETYPE_OUT_OF_RANGE_OBJECTS = 4
UPDATETYPE_NEAR_OBJECTS = 5
TYPEID_ITEM = 1
TYPEID_UNIT = 3
TYPEID_PLAYER = 4
TYPEID_CORPSE = 7

# ``Movement::MonsterMoveType`` (CMaNGOS 3.3.5, MoveSpline.h).  This byte
# replaces the final-facing spline flags inside ``SMSG_MONSTER_MOVE``; note that
# the facing variants are 2/3/4, not 1/2/3, and each has its own payload size.
MONSTER_MOVE_NORMAL = 0
MONSTER_MOVE_STOP = 1
MONSTER_MOVE_FACING_SPOT = 2
MONSTER_MOVE_FACING_TARGET = 3
MONSTER_MOVE_FACING_ANGLE = 4

UPDATEFLAG_SELF = 0x0001
UPDATEFLAG_HIGHGUID = 0x0010
UPDATEFLAG_LOWGUID = UPDATEFLAG_HIGHGUID
UPDATEFLAG_LIVING = 0x0020
UPDATEFLAG_HAS_POSITION = 0x0040
UPDATEFLAG_STATIONARY_POSITION = UPDATEFLAG_HAS_POSITION
UPDATEFLAG_POSITION = 0x0100

# HIGHGUID payload the client expects for its own player object.
PLAYER_SELF_HIGHGUID = 0x2F

# Default player movement speeds (TrinityCore ``Player::InitDisplayIds``/Unit base).
_WALK, _RUN, _RUN_BACK = 2.5, 7.0, 4.5
_SWIM, _SWIM_BACK = 4.722222, 2.5
_FLIGHT, _FLIGHT_BACK = 7.0, 4.5
_TURN_RATE = 3.141594
_PITCH_RATE = 3.141594

_MAX_POWER = 100

# ``UNIT_FIELD_POWER1``/``MAXPOWER1`` are not in displayed points for every
# power type: rage (1) and runic power (6) use tenths, energy (3) and focus (2)
# use 0..100.  Matches CMaNGOS ``Unit::GetCreatePowerValue``.
_MAX_POWER_BY_TYPE = {1: 1000, 6: 1000}
_MAX_HEALTH = 100

# ``ratio`` column of the 3.3.5 client's ``GtRegenMPPerSpt.dbc`` (build 12340),
# indexed by ``(class - 1) * 100 + level - 1``.  Every mana-using class (paladin,
# hunter, priest, shaman, mage, warlock, druid) has the same 100-row block;
# warriors, rogues and death knights have an all-zero block.  The spirit-based
# mana regeneration is ``sqrt(Intellect) * Spirit * ratio`` *per second*
# (CMaNGOS ``Player::UpdateManaRegen`` + ``OCTRegenMPPerSpirit``); the client
# shows the same number as mana per five seconds by multiplying it by five.
# Values for levels 81+ repeat the level-80 ratio, as in the client table.
_BASE_REGEN = (
    0.062937, 0.056900, 0.051488, 0.046267, 0.041637,  # 1-5
    0.037784, 0.034309, 0.031172, 0.028158, 0.025460,  # 6-10
    0.022654, 0.019904, 0.017817, 0.015771, 0.014008,  # 11-15
    0.013650, 0.013175, 0.012832, 0.012475, 0.012073,  # 16-20
    0.011840, 0.011494, 0.011292, 0.010990, 0.010761,  # 21-25
    0.010546, 0.010321, 0.010151, 0.009949, 0.009740,  # 26-30
    0.009597, 0.009425, 0.009278, 0.009123, 0.008974,  # 31-35
    0.008847, 0.008698, 0.008581, 0.008457, 0.008338,  # 36-40
    0.008235, 0.008113, 0.008018, 0.007906, 0.007798,  # 41-45
    0.007713, 0.007612, 0.007524, 0.007430, 0.007340,  # 46-50
    0.007268, 0.007184, 0.007116, 0.007029, 0.006945,  # 51-55
    0.006884, 0.006805, 0.006747, 0.006667, 0.006600,  # 56-60
    0.006421, 0.006314, 0.006175, 0.006072, 0.005981,  # 61-65
    0.005885, 0.005791, 0.005732, 0.005668, 0.005596,  # 66-70
    0.005316, 0.005049, 0.004796, 0.004555, 0.004327,  # 71-75
    0.004110, 0.003903, 0.003708, 0.003522, 0.003345,  # 76-80
)


def base_regen_for_level(level: int) -> float:
    """``BASE_REGEN`` coefficient for ``level`` (clamped to the table)."""
    level = max(1, min(len(_BASE_REGEN) + 5, int(level or 1)))
    if level > len(_BASE_REGEN):
        return 0.003345
    return _BASE_REGEN[level - 1]


def mana_regen_per_second(stats: Optional[Mapping], level: int) -> float:
    """Flat spirit/intellect mana regeneration, in mana per second.

    This is exactly the value the real core publishes in
    ``UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER`` (a per-second float).
    """
    if not stats:
        return 0.0
    intellect = max(0, int(stats.get("inte", 0) or 0))
    spirit = max(0, int(stats.get("spi", 0) or 0))
    if not intellect or not spirit:
        return 0.0
    return math.sqrt(intellect) * spirit * base_regen_for_level(level)


def mana_regen_rates(stats: Optional[Mapping],
                     level: int) -> Tuple[int, int]:
    """The two regen update-fields as ``uint32`` float bits.

    Returns ``(UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER,
    UNIT_FIELD_POWER_REGEN_INTERRUPTED_FLAT_MODIFIER)`` for the mana slot.
    ``Player::UpdateManaRegen`` writes::

        interrupted = mp5 + spirit_regen * modManaRegenInterrupt / 100
        flat        = mp5 + spirit_regen

    With no ``SPELL_AURA_MOD_POWER_REGEN`` gear bonus and no
    ``SPELL_AURA_MOD_MANA_REGEN_INTERRUPT`` talent -- which is every character
    this server has -- both terms collapse to the spirit regen and zero, which
    is the five-second-rule behaviour the client animates on its own.  Keep this
    as the single source of truth: the regen loop in ``realmsocket`` reads the
    same two numbers back so the bar the client draws and the mana the server
    credits can never disagree.
    """
    return _float_bits(mana_regen_per_second(stats, level)), 0


def float_from_bits(bits: int) -> float:
    """Inverse of :func:`float_bits` for a float update-field value."""
    return struct.unpack("<f", struct.pack("<I", int(bits) & 0xFFFFFFFF))[0]

# Race -> (male display id, female display id), taken from the client's
# ChrRaces.dbc.  Both ids must exist in the client's CreatureDisplayInfo.dbc:
# the client dereferences the display entry while building the player object and
# crashes with a null-pointer access if it is missing.
_DISPLAY_IDS = {
    1: (49, 50),          # Human
    2: (51, 52),          # Orc
    3: (53, 54),          # Dwarf
    4: (55, 56),          # Night Elf
    5: (57, 58),          # Undead
    6: (59, 60),          # Tauren
    7: (1563, 1564),      # Gnome
    8: (1478, 1479),      # Troll
    10: (15476, 15475),   # Blood Elf
    11: (16125, 16126),   # Draenei
}

# Race -> friendly faction template, taken from the client's ChrRaces.dbc
# ("FactionID" column).  The client looks this id up in FactionTemplate.dbc
# while initialising the player's faction lists and crashes if the row is
# missing, so these values must match the client's DBC data exactly.
_FACTION_TEMPLATES = {
    1: 1,       # Human
    2: 2,       # Orc
    3: 3,       # Dwarf
    4: 4,       # Night Elf
    5: 5,       # Undead
    6: 6,       # Tauren
    7: 115,     # Gnome
    8: 116,     # Troll
    10: 1610,   # Blood Elf
    11: 1629,   # Draenei
}


def faction_template_for_race(race: int) -> int:
    """Return the ``FactionTemplate.dbc`` id the client uses for ``race``.

    The 3.3.5 client dereferences the race's faction template while building a
    player, and the same id makes a summoned unit friendly to its owner.
    """
    return int(_FACTION_TEMPLATES.get(int(race), 1))

# Class -> power type (0 mana, 1 rage, 3 energy, 6 runic power)
_POWER_TYPES = {1: 1, 4: 3, 6: 6}

# All known language skill lines for 3.3.5a (from SkillLine.dbc / TrinityCore).
# Give ALL of them to EVERY character so that regardless of which skill line
# SpellTableLookupAbility(race, class, languageSpell) returns for a given
# race/class/spell combination, the player has it at max rank (300).
# This avoids the "you can't speak that language" error.
#
# The skill line IDs from SkillLineAbility.dbc for language spells can be
# different from the base SkillLine.dbc IDs.  Spell 673 (Tauren/Taurahe)
# maps to skill_line=10021, so we include high IDs that language spells
# commonly map to.
_ALL_LANGUAGE_SKILLS = [
    (98, 300, 300),      # Common
    (109, 300, 300),     # Orcish
    (111, 300, 300),     # Dwarven
    (113, 300, 300),     # Darnassian
    (115, 300, 300),     # Taurahe (base ID)
    (124, 300, 300),     # Gnomish
    (126, 300, 300),     # Zandali (Troll)
    (553, 300, 300),     # Thalassian (Blood Elf)
    (554, 300, 300),     # Draenei
    (555, 300, 300),     # Zombie/Gutterspeak (Forsaken)
    # High skill line IDs from SkillLineAbility.dbc for language spells:
    (10021, 300, 300),   # Spell 673 -> Taurahe (actual mapping)
    (10022, 300, 300),   # Common
    (10023, 300, 300),   # Orcish
    (10024, 300, 300),   # Dwarven
    (10025, 300, 300),   # Darnassian
    (10026, 300, 300),   # Gutterspeak
    (10027, 300, 300),   # Gnomish
    (10028, 300, 300),   # Zandali
    (10029, 300, 300),   # Thalassian
    (10030, 300, 300),   # Draenei
    (10031, 300, 300),   # Zombie/Gutterspeak
]

_LANGUAGE_SKILLS_BY_RACE = {
    race: _ALL_LANGUAGE_SKILLS for race in range(1, 22)
}


def language_skill_count(char: Mapping) -> int:
    race = int(_char_get(char, "race", 1) or 1)
    return len(_LANGUAGE_SKILLS_BY_RACE.get(race, _ALL_LANGUAGE_SKILLS))


def assign_skill_slots(char: Mapping) -> None:
    """Give every learned skill a stable PLAYER_SKILL_INFO slot.

    Language skills occupy the first slots in the client's array; professions
    and weapon skills must never overwrite them.
    """
    skills = _char_get(char, "skills", None)
    if not skills:
        return
    start = language_skill_count(char)
    used = set()
    next_slot = start
    for skill_id in sorted(skills):
        data = skills[skill_id]
        slot = int(data.get("slot", -1) or -1)
        if slot >= start and slot not in used:
            used.add(slot)
            next_slot = max(next_slot, slot + 1)
        else:
            data["slot"] = None
    for skill_id in sorted(skills):
        data = skills[skill_id]
        if data.get("slot") is not None:
            continue
        while next_slot in used:
            next_slot += 1
        data["slot"] = next_slot
        used.add(next_slot)
        next_slot += 1


def next_skill_slot(char: Mapping) -> int:
    assign_skill_slots(char)
    skills = _char_get(char, "skills", None) or {}
    if not skills:
        return language_skill_count(char)
    return max(int(data.get("slot", 0) or 0) for data in skills.values()) + 1


def skill_field_values(char: Mapping, skill_id: int) -> Dict[int, int]:
    """Return the three PLAYER_SKILL_INFO fields for one skill line."""
    skills = _char_get(char, "skills", None) or {}
    data = skills.get(int(skill_id))
    if not data:
        return {}
    slot = int(data.get("slot", -1) or -1)
    if slot < 0 or slot >= F.PLAYER_SKILL_COUNT:
        return {}
    base = F.PLAYER_SKILL_INFO_1_1 + slot * 3
    step = max(0, int(data.get("step", 0) or 0)) & 0xFFFF
    value = max(0, int(data.get("value", 0) or 0)) & 0xFFFF
    maximum = max(0, int(data.get("max_value", 0) or 0)) & 0xFFFF
    return {
        base: (int(skill_id) & 0xFFFF) | (step << 16),
        base + 1: value | (maximum << 16),
        base + 2: 0,
    }


def skill_value(char: Mapping, skill_id: int) -> int:
    skills = _char_get(char, "skills", None) or {}
    return int((skills.get(int(skill_id)) or {}).get("value", 0) or 0)


# UNIT_FLAG_PLAYER_CONTROLLED in 3.3.5 (called UNIT_FLAG_PVP_ATTACKABLE in
# TrinityCore): every real player object carries it.  Without it the client
# treats the character as a plain unit that is not player-controlled.
UNIT_FLAG_PLAYER_CONTROLLED = 0x00000008
# UNIT_FLAG2_REGENERATE_POWER - TrinityCore marks it "must be set" on players.
UNIT_FLAG2_REGENERATE_POWER = 0x00000800
# UNIT_FLAG2_MIRROR_IMAGE - the client must not build this unit's appearance from
# its own PLAYER_BYTES/visible items.  ``CGUnit_C::InitializeComponent`` sees the
# bit, sends ``CMSG_GET_MIRRORIMAGE_DATA`` and waits for
# ``SMSG_MIRRORIMAGE_DATA``; that handler allocates the character component and
# calls ``AddItemBySlot`` for all 11 armour slots, which is the only reliable
# way a 3.3.5 client puts armour on another player.  The bit is checked before
# the display-info branch that otherwise aborts ``InitializeComponent`` for
# peers (see the README "Other players' equipment" note).
UNIT_FLAG2_MIRROR_IMAGE = 0x00000010


def pack_guid(guid: int) -> bytes:
    """ObjectGuid.WriteAsPacked(): mask byte + only the non-zero bytes."""
    mask = 0
    body = bytearray()
    for i in range(8):
        byte = (guid >> (8 * i)) & 0xFF
        if byte:
            mask |= 1 << i
            body.append(byte)
    return bytes([mask]) + bytes(body)


def _movement_info(x: float, y: float, z: float, o: float) -> bytes:
    # MOVEMENTFLAG_NONE, MOVEFLAG2_NONE, time 0, position/orientation, fall time.
    # MovementInfo::Write() always appends fallTime (float) after the position.
    return struct.pack("<IHIfffff", 0, 0, 0, x, y, z, o, 0.0)


def _speeds() -> bytes:
    return struct.pack(
        "<9f", _WALK, _RUN, _RUN_BACK, _SWIM, _SWIM_BACK,
        _FLIGHT, _FLIGHT_BACK, _TURN_RATE, _PITCH_RATE,
    )


def player_display_id(char: Mapping) -> int:
    """Return the ``CreatureDisplayInfo`` id for a character (race/gender based)."""
    race = int(char["race"])
    gender = int(char["gender"])
    display_ids = _DISPLAY_IDS.get(race, _DISPLAY_IDS[1])
    return display_ids[gender if gender in (0, 1) else 0]


def _char_get(char: Mapping, key: str, default):
    """``Mapping.get`` that also works on a ``sqlite3.Row``."""
    try:
        if key in char.keys():
            value = char[key]
            return default if value is None else value
    except AttributeError:
        pass
    return default


def build_combat_stats_values(char: Mapping) -> Dict[int, int]:
    """Update fields describing the character's current stats and pools.

    Shared by the create block and the ``UPDATETYPE_VALUES`` update sent after
    equipping/unequipping, so the character sheet (attributes, armor, attack
    power) and the health/mana bars always match the server-side character.
    """
    char_class = int(char["class"])
    level = max(1, int(_char_get(char, "level", 1) or 1))
    power_type = _POWER_TYPES.get(char_class, 0)

    max_health = int(_char_get(char, "max_health", _MAX_HEALTH)) or _MAX_HEALTH
    max_power = int(_char_get(char, "max_power", _MAX_POWER)) or _MAX_POWER
    health = min(max_health, int(_char_get(char, "health", max_health)))
    power = min(max_power, int(_char_get(char, "power", max_power)))

    values: Dict[int, int] = {}
    values[F.UNIT_FIELD_HEALTH] = health
    values[F.UNIT_FIELD_MAXHEALTH] = max_health
    # Power lives in the per-power-type slot: mana in POWER1, rage in POWER2,
    # energy in POWER4, runic power in POWER7.  Writing every class into
    # POWER1 is what made the client grey out Heroic Strike ("Not enough
    # rage") even when the server had a full rage bar: it never saw POWER2.
    values[F.UNIT_FIELD_POWER1 + power_type] = power
    values[F.UNIT_FIELD_MAXPOWER1 + power_type] = max_power
    values[F.UNIT_FIELD_LEVEL] = level

    # Current attributes, already including equipped-item bonuses.  The client's
    # character sheet reads these five fields; positive/negative aura modifiers
    # are not simulated, so they stay zero.
    stats = _char_get(char, "stats", None)
    if stats:
        for index, key in enumerate(("str", "agi", "sta", "inte", "spi")):
            values[F.UNIT_FIELD_STAT0 + index] = int(stats.get(key, 0) or 0)
            values[F.UNIT_FIELD_POSSTAT0 + index] = 0
            values[F.UNIT_FIELD_NEGSTAT0 + index] = 0

    # ``UNIT_FIELD_BASE_MANA``/``BASE_HEALTH`` are the unmodified class/level
    # values used by the client for tooltips and regen calculations.
    values[F.UNIT_FIELD_BASE_MANA] = int(_char_get(char, "base_mana", 0) or 0)
    values[F.UNIT_FIELD_BASE_HEALTH] = int(
        _char_get(char, "base_health", max_health) or max_health
    )

    # Armor + the six magic resistances.  Armor is item armor plus the
    # agility contribution (``Player::UpdateArmor``), computed by the session.
    values[F.UNIT_FIELD_RESISTANCES] = int(_char_get(char, "armor", 0) or 0)
    for index, key in enumerate(
            ("holy", "fire", "nature", "frost", "shadow", "arcane"), start=1):
        values[F.UNIT_FIELD_RESISTANCES + index] = int(
            _char_get(char, "res_" + key, 0) or 0
        )

    # ``Player::UpdateAttackPowerAndDamage``: the base field carries the
    # class/level/stat formula plus flat item attack power.  MODS is a packed
    # int16 pair and MULTIPLIER is the bonus fraction, both zero without auras.
    values[F.UNIT_FIELD_ATTACK_POWER] = int(
        _char_get(char, "attack_power", 0) or 0
    )
    values[F.UNIT_FIELD_ATTACK_POWER_MODS] = 0
    values[F.UNIT_FIELD_ATTACK_POWER_MULTIPLIER] = _float_bits(0.0)
    values[F.UNIT_FIELD_RANGED_ATTACK_POWER] = int(
        _char_get(char, "ranged_attack_power", 0) or 0
    )
    values[F.UNIT_FIELD_RANGED_ATTACK_POWER_MODS] = 0
    values[F.UNIT_FIELD_RANGED_ATTACK_POWER_MULTIPLIER] = _float_bits(0.0)
    return values


def build_player_values(char: Mapping, peer: bool = False) -> Dict[int, int]:
    """Return ``{field_index: uint32_value}`` for the player's create block.

    ``peer`` is true for another player a client can see but does not control.
    Those objects carry ``UNIT_FLAG2_MIRROR_IMAGE`` so the client asks for
    ``SMSG_MIRRORIMAGE_DATA`` and builds the armour/weapon visuals from the
    server's reply (see ``login_packets.build_mirror_image_data``).
    """
    race = int(char["race"])
    char_class = int(char["class"])
    gender = int(char["gender"])
    skin = int(char["skin"])
    face = int(char["face"])
    hair_style = int(char["hair_style"])
    hair_color = int(char["hair_color"])
    facial_style = int(char["facial_style"])
    level = max(1, int(char["level"]))
    guid = int(char["guid"])

    power_type = _POWER_TYPES.get(char_class, 0)
    display_id = player_display_id(char)
    faction = faction_template_for_race(race)

    next_xp = int(_char_get(char, "next_level_xp", 400)) or 400

    values: Dict[int, int] = {}
    values[F.OBJECT_FIELD_GUID] = guid & 0xFFFFFFFF
    values[F.OBJECT_FIELD_GUID + 1] = (guid >> 32) & 0xFFFFFFFF
    values[F.OBJECT_FIELD_TYPE] = 0x19          # TYPEMASK_UNIT | TYPEMASK_PLAYER
    values[F.OBJECT_FIELD_ENTRY] = 0
    values[F.OBJECT_FIELD_SCALE_X] = _float_bits(1.0)

    values[F.UNIT_FIELD_BYTES_0] = (
        (race & 0xFF) | ((char_class & 0xFF) << 8)
        | ((gender & 0xFF) << 16) | ((power_type & 0xFF) << 24)
    )
    # Health, power, level, attributes, base pools, armor and attack power all
    # come from one helper so the create block and the post-equip update agree.
    values.update(build_combat_stats_values(char))
    values[F.UNIT_FIELD_FACTIONTEMPLATE] = faction
    values[F.UNIT_FIELD_FLAGS] = UNIT_FLAG_PLAYER_CONTROLLED
    values[F.UNIT_FIELD_FLAGS_2] = UNIT_FLAG2_REGENERATE_POWER | (
        UNIT_FLAG2_MIRROR_IMAGE if peer else 0
    )
    values[F.UNIT_FIELD_BOUNDINGRADIUS] = _float_bits(0.389)
    values[F.UNIT_FIELD_COMBATREACH] = _float_bits(1.5)
    values[F.UNIT_FIELD_DISPLAYID] = display_id
    values[F.UNIT_FIELD_NATIVEDISPLAYID] = display_id
    values[F.UNIT_FIELD_MOUNTDISPLAYID] = 0
    values[F.UNIT_FIELD_BYTES_1] = 0
    values[F.UNIT_DYNAMIC_FLAGS] = 0
    values[F.UNIT_FIELD_BYTES_2] = 0

    # ``UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER`` / ``..._INTERRUPTED_FLAT_MODIFIER``
    # are float[7] blocks (one per power type) the owner's client reads to
    # animate the power bar and print the "mana per 5" number.  CMaNGOS stores
    # a *per-second* rate there.  Without talents the interrupted (recently
    # cast) spirit regen is zero, so only the flat block is filled in.
    stats = _char_get(char, "stats", None)
    if stats:
        level = max(1, int(char.get("level", 1) or 1))
        power_type = _POWER_TYPES.get(int(char.get("class", 1)), 0)
        if power_type == 0:
            flat_bits, interrupted_bits = mana_regen_rates(stats, level)
            values[F.UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER + 0] = flat_bits
            values[F.UNIT_FIELD_POWER_REGEN_INTERRUPTED_FLAT_MODIFIER + 0] = \
                interrupted_bits

    values[F.PLAYER_FLAGS] = int(_char_get(char, "player_flags", 0)) & 0xFFFFFFFF
    values[F.PLAYER_BYTES] = (
        (skin & 0xFF) | ((face & 0xFF) << 8)
        | ((hair_style & 0xFF) << 16) | ((hair_color & 0xFF) << 24)
    )
    values[F.PLAYER_BYTES_2] = facial_style & 0xFF
    values[F.PLAYER_BYTES_3] = gender & 0xFF
    values[F.PLAYER_XP] = int(_char_get(char, "xp", 0)) & 0xFFFFFFFF
    values[F.PLAYER_NEXT_LEVEL_XP] = next_xp
    values[F.PLAYER_FIELD_COINAGE] = int(_char_get(char, "money", 0)) & 0xFFFFFFFF
    values[F.PLAYER_FIELD_MAX_LEVEL] = 80

    # Language skills: populate the player's skill array so the client knows
    # which languages the character can speak/understand.
    #
    # The client's skill array (Mirror.cpp PLAYER_SKILL_INFO_1_1) packs each skill
    # as THREE packed uint32s:
    #   [0] = skillId | (step << 16)
    #   [1] = rank    | (maxRank << 16)
    #   [2] = tempBonus | (permBonus << 16)
    # This matches CGPlayer_C::GetSkillValue() and SkillRankChangeHandler().
    # We set step=0, rank=maxRank=300, tempBonus=permBonus=0 for all languages.
    race = int(char["race"])
    language_skills = _LANGUAGE_SKILLS_BY_RACE.get(race, [(98, 300, 300)])
    for i, (skill_id, rank, max_rank) in enumerate(language_skills):
        if i >= F.PLAYER_SKILL_COUNT:
            break
        base = F.PLAYER_SKILL_INFO_1_1 + i * 3
        values[base] = skill_id | (0 << 16)          # skillId | step
        values[base + 1] = rank | (max_rank << 16)   # rank | maxRank
        values[base + 2] = 0                         # tempBonus | permBonus

    # Learned professions/weapon skills follow the language entries.
    assign_skill_slots(char)
    for skill_id in sorted(_char_get(char, "skills", None) or {}):
        values.update(skill_field_values(char, skill_id))

    # Inventory: populate visible items + inv slots if we have character items.
    # The caller is expected to set ``char["inventory"]`` = {slot: item dict}.
    # ``char`` may be a sqlite3.Row, which has no ``get``.
    inventory = char["inventory"] if "inventory" in char.keys() else None
    if inventory:
        values.update(build_equipment_values(inventory))

    # The quest log fields are private to the owner (UF_FLAG_PRIVATE).
    quest_fields = char["quest_fields"] if "quest_fields" in char.keys() else None
    if quest_fields and not peer:
        values.update({int(k): int(v) for k, v in quest_fields.items() if int(v)})

    return values


def build_equipment_values(inventory: Mapping) -> Dict[int, int]:
    """Return the player fields that describe equipment and bag contents.

    Used both in the create block and in the ``UPDATETYPE_VALUES`` update that is
    broadcast after a swap/auto-equip/destroy, so peers re-run the client's
    ``OnUpdateInventoryComponent`` handler and update the visible model.
    """
    values: Dict[int, int] = {}

    # Visible items: 19 pairs of (entry_id, enchant_visual)
    for slot in range(19):
        entry_id = 0
        enchant = 0
        # equipment slots: 0..18 map to INVSLOT_HEAD..INVSLOT_TABARD
        item = inventory.get(slot)
        if item:
            entry_id = int(item.get("item_entry", 0))
            enchant = int(item.get("enchantment_1", 0))
        base = F.PLAYER_VISIBLE_ITEM_1_ENTRYID + slot * 2
        values[base] = entry_id
        values[base + 1] = enchant

    # Inventory slots: 23 uint64 guids (equip 0..18 + bag 19..22)
    for slot in range(23):
        item = inventory.get(slot)
        item_guid = int(item["guid"]) if item else 0
        base = F.PLAYER_FIELD_INV_SLOT_HEAD + slot * 2
        values[base] = item_guid & 0xFFFFFFFF
        values[base + 1] = (item_guid >> 32) & 0xFFFFFFFF

    # Backpack slots 0..15 (16 slots)
    for slot in range(16):
        item = inventory.get(256 + slot)
        item_guid = int(item["guid"]) if item else 0
        base = F.PLAYER_FIELD_PACK_SLOT_1 + slot * 2
        values[base] = item_guid & 0xFFFFFFFF
        values[base + 1] = (item_guid >> 32) & 0xFFFFFFFF

    return values


def float_bits(value: float) -> int:
    """Wire representation of a float update-field value (``SetFloatValue``)."""
    return _float_bits(value)


def _float_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def build_player_create_block(char: Mapping, self_player: bool = True) -> bytes:
    """Build one ``UPDATETYPE_CREATE_OBJECT2`` block for a player.

    ``self_player`` is true only for the character the client controls.  The
    client marks the active player when ``UPDATEFLAG_SELF`` (0x1) is set and
    reads its own high-guid word (0x2F) behind ``UPDATEFLAG_HIGHGUID`` (0x10);
    another player must be sent without either flag, or the client would
    overwrite its active-player guid with the other character.
    """
    guid = int(char["guid"])
    x = float(char["x"])
    y = float(char["y"])
    z = float(char["z"])
    orientation = float(char["o"]) if "o" in char.keys() else 0.0

    values = build_player_values(char, peer=not self_player)

    blocks = (F.PLAYER_VALUES_COUNT + 31) // 32
    mask = bytearray(blocks * 4)
    for index in values:
        mask[(index // 32) * 4 + (index % 32) // 8] |= 1 << (index % 8)

    block = bytearray()
    block.append(UPDATETYPE_CREATE_OBJECT2)
    block += pack_guid(guid)
    block.append(TYPEID_PLAYER)

    flags = UPDATEFLAG_LIVING | UPDATEFLAG_HAS_POSITION
    if self_player:
        flags |= UPDATEFLAG_SELF | UPDATEFLAG_HIGHGUID
    block += struct.pack("<H", flags)
    block += _movement_info(x, y, z, orientation)
    block += _speeds()
    if self_player:
        block += struct.pack("<I", PLAYER_SELF_HIGHGUID)

    block.append(blocks)
    block += mask
    for index in sorted(values):
        block += struct.pack("<I", values[index] & 0xFFFFFFFF)

    return bytes(block)


def build_update_object(char: Mapping, self_player: bool = True,
                        extra_blocks: Optional[List[bytes]] = None) -> Tuple[int, bytes]:
    """Return ``(opcode, payload)`` for ``SMSG_UPDATE_OBJECT`` (possibly compressed).

    ``char["inventory"]`` (only meaningful for the player's own session) adds one
    create block per item *before* the player block, like TrinityCore's
    ``Player::BuildCreateUpdateBlockForPlayer``, so the object guid referenced by
    ``PLAYER_FIELD_INV_SLOT_HEAD`` already exists when the client reads it.
    Other players do not need the item objects: their appearance comes from
    ``PLAYER_VISIBLE_ITEM_*``, and their inventory is private.

    ``extra_blocks`` are appended after the player block.  Static creatures are
    delivered in the same update packet on login, which keeps the world-entry
    burst's packet order unchanged and gives the client every visible object in
    one atomic update.
    """
    blocks = []
    inventory = char["inventory"] if "inventory" in char.keys() else None
    if self_player and inventory:
        for slot in sorted(inventory):
            blocks.append(build_item_create_block(inventory[slot]))
    blocks.append(build_player_create_block(char, self_player=self_player))
    if extra_blocks:
        blocks.extend(extra_blocks)

    body = struct.pack("<I", len(blocks)) + b"".join(blocks)

    if len(body) > 100:
        return Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT, struct.pack("<I", len(body)) + zlib.compress(body)
    return Opcodes.SMSG_UPDATE_OBJECT, body


def build_update_blocks(blocks: List[bytes]) -> Tuple[int, bytes]:
    """Wrap already-built update blocks in SMSG_UPDATE_OBJECT (compressed if large)."""
    body = struct.pack("<I", len(blocks)) + b"".join(blocks)
    if len(body) > 100:
        return (Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT,
                struct.pack("<I", len(body)) + zlib.compress(body))
    return Opcodes.SMSG_UPDATE_OBJECT, body


def corpse_guid(owner_guid: int) -> int:
    return F.CORPSE_HIGH_GUID | (int(owner_guid) & 0xFFFFFFFF)


def build_corpse_values(corpse: Mapping) -> Dict[int, int]:
    """Return ``{field_index: uint32_value}`` for a player corpse object."""
    owner_guid = int(corpse["owner_guid"])
    guid = corpse_guid(owner_guid)
    values: Dict[int, int] = {}
    values[F.OBJECT_FIELD_GUID] = guid & 0xFFFFFFFF
    values[F.OBJECT_FIELD_GUID + 1] = (guid >> 32) & 0xFFFFFFFF
    values[F.OBJECT_FIELD_TYPE] = F.TYPEMASK_OBJECT | F.TYPEMASK_CORPSE
    values[F.OBJECT_FIELD_ENTRY] = 0
    values[F.OBJECT_FIELD_SCALE_X] = _float_bits(1.0)
    values[F.CORPSE_FIELD_OWNER] = owner_guid & 0xFFFFFFFF
    values[F.CORPSE_FIELD_OWNER + 1] = (owner_guid >> 32) & 0xFFFFFFFF
    values[F.CORPSE_FIELD_PARTY] = 0
    values[F.CORPSE_FIELD_PARTY + 1] = 0
    values[F.CORPSE_FIELD_DISPLAY_ID] = int(corpse.get("display_id", 0) or 0)
    items = list(corpse.get("items") or [])
    for slot in range(19):
        values[F.CORPSE_FIELD_ITEM + slot] = (
            int(items[slot]) if slot < len(items) else 0)
    values[F.CORPSE_FIELD_BYTES_1] = int(corpse.get("bytes1", 0) or 0)
    values[F.CORPSE_FIELD_BYTES_2] = int(corpse.get("bytes2", 0) or 0)
    values[F.CORPSE_FIELD_GUILD] = int(corpse.get("guild", 0) or 0)
    values[F.CORPSE_FIELD_FLAGS] = 0
    values[F.CORPSE_FIELD_DYNAMIC_FLAGS] = 0
    return values


def build_corpse_create_block(corpse: Mapping) -> bytes:
    """Build one ``UPDATETYPE_CREATE_OBJECT2`` block for a player corpse.

    Corpses are stationary WorldObjects: TrinityCore uses
    ``LOWGUID | STATIONARY_POSITION | POSITION`` and writes the position twice
    (plus the orientation twice) before the low-guid word.
    """
    owner_guid = int(corpse["owner_guid"])
    guid = corpse_guid(owner_guid)
    values = build_corpse_values(corpse)
    blocks = (F.CORPSE_END + 31) // 32
    mask = bytearray(blocks * 4)
    for index in values:
        mask[(index // 32) * 4 + (index % 32) // 8] |= 1 << (index % 8)

    x = float(corpse["x"])
    y = float(corpse["y"])
    z = float(corpse["z"])
    o = float(corpse.get("o", 0.0) or 0.0)
    block = bytearray()
    block.append(UPDATETYPE_CREATE_OBJECT2)
    block += pack_guid(guid)
    block.append(TYPEID_CORPSE)
    block += struct.pack(
        "<H", UPDATEFLAG_LOWGUID | UPDATEFLAG_STATIONARY_POSITION
        | UPDATEFLAG_POSITION)
    block += b"\x00"                     # no transport
    block += struct.pack("<8f", x, y, z, x, y, z, o, o)
    block += struct.pack("<I", owner_guid & 0xFFFFFFFF)
    block.append(blocks)
    block += mask
    for index in sorted(values):
        block += struct.pack("<I", values[index] & 0xFFFFFFFF)
    return bytes(block)


def build_corpse_update(corpse: Mapping) -> Tuple[int, bytes]:
    return build_update_blocks([build_corpse_create_block(corpse)])


# ---------------------------------------------------------------------------
# Creature create blocks
# ---------------------------------------------------------------------------
def build_creature_values(creature: Mapping) -> Dict[int, int]:
    """Return ``{field_index: uint32_value}`` for a static creature.

    ``creature`` is one joined ``creature_spawns``/``creature_templates`` row
    (see ``creature.get_creature_spawns``).  Creatures share the UNIT field
    block with players but stop at ``UNIT_END``; they never carry player fields.
    """
    entry = int(creature["entry"])
    guid = creature_guid(creature["guid"], entry)
    level = max(1, int(creature["min_level"]))
    max_health = max(1, int(creature["base_health"]))
    dead_since = int(creature["dead_since"] or 0) if "dead_since" in creature.keys() else 0
    saved_health = int(creature["current_health"] or 0) if "current_health" in creature.keys() else 0
    dead = dead_since > 0
    if dead:
        health = 0
    elif saved_health > 0:
        health = min(saved_health, max_health)
    else:
        health = max_health
    # Real templates occasionally leave DisplayId1 empty and put the model in
    # one of the later slots; pick the first populated one.
    display_id = int(creature["spawn_display_id"])
    if not display_id:
        for index in (1, 2, 3, 4):
            display_id = int(creature[f"display_id{index}"])
            if display_id:
                break

    values: Dict[int, int] = {}
    values[F.OBJECT_FIELD_GUID] = guid & 0xFFFFFFFF
    values[F.OBJECT_FIELD_GUID + 1] = (guid >> 32) & 0xFFFFFFFF
    # Creature = OBJECT | UNIT; no PLAYER bit.
    values[F.OBJECT_FIELD_TYPE] = (F.TYPEMASK_OBJECT | F.TYPEMASK_UNIT) & 0xFFFFFFFF
    values[F.OBJECT_FIELD_ENTRY] = entry
    values[F.OBJECT_FIELD_SCALE_X] = _float_bits(max(0.01, float(creature["scale"])))

    # ``UNIT_FIELD_BYTES_0`` is [race, class, gender, power] for every unit.
    # A creature has no race byte (0); its class comes from
    # ``creature_template.UnitClass`` and decides the power bar (warriors use
    # rage, rogues energy, death knights runes, everyone else mana).
    power_type = int(creature["unit_power_type"])
    values[F.UNIT_FIELD_BYTES_0] = (
        ((int(creature["unit_class"]) & 0xFF) << 8)
        | ((int(creature["unit_gender"]) & 0xFF) << 16)
        | ((power_type & 0xFF) << 24)
    )
    values[F.UNIT_FIELD_HEALTH] = health
    values[F.UNIT_FIELD_MAXHEALTH] = max_health
    # Rage/runic-power units carry their bar in tenths, like players do, so the
    # client's own "is this ability usable" maths sees a sane maximum.  As with
    # players, the value must go in the slot for the unit's own power type.
    max_power = _MAX_POWER_BY_TYPE.get(power_type, _MAX_POWER)
    values[F.UNIT_FIELD_POWER1 + power_type] = max_power if power_type == 0 else 0
    values[F.UNIT_FIELD_MAXPOWER1 + power_type] = max_power
    values[F.UNIT_FIELD_LEVEL] = level
    values[F.UNIT_FIELD_FACTIONTEMPLATE] = int(creature["faction"])
    # A summon carries ``summoned_by`` (the owner's full guid).  The client
    # draws such a unit as the player's pet/guardian, and the low bits tell
    # every combat/pet UI who owns it.  Template flags such as the imp's
    # 0x8000 PACIFIED bit describe the wild NPC of that entry, not the pet,
    # so a summon uses the player-controlled flag instead.
    summoned_by = 0
    if "summoned_by" in creature.keys():
        summoned_by = int(creature["summoned_by"] or 0)
    if summoned_by:
        values[F.UNIT_FIELD_SUMMONEDBY] = summoned_by & 0xFFFFFFFF
        values[F.UNIT_FIELD_SUMMONEDBY + 1] = (summoned_by >> 32) & 0xFFFFFFFF
        values[F.UNIT_FIELD_CREATEDBY] = summoned_by & 0xFFFFFFFF
        values[F.UNIT_FIELD_CREATEDBY + 1] = (summoned_by >> 32) & 0xFFFFFFFF
        values[F.UNIT_FIELD_FLAGS] = UNIT_FLAG_PLAYER_CONTROLLED
    else:
        values[F.UNIT_FIELD_FLAGS] = int(creature["unit_flags"])
    values[F.UNIT_FIELD_FLAGS_2] = int(creature["unit_flags2"])
    values[F.UNIT_FIELD_BOUNDINGRADIUS] = _float_bits(float(creature["bounding_radius"]))
    values[F.UNIT_FIELD_COMBATREACH] = _float_bits(float(creature["combat_reach"]))
    values[F.UNIT_FIELD_DISPLAYID] = display_id
    values[F.UNIT_FIELD_NATIVEDISPLAYID] = display_id
    values[F.UNIT_FIELD_MOUNTDISPLAYID] = 0
    values[F.UNIT_FIELD_BYTES_1] = 0
    values[F.UNIT_DYNAMIC_FLAGS] = 1 if dead else 0  # UNIT_DYNFLAG_LOOTABLE
    values[F.UNIT_NPC_FLAGS] = int(creature["npc_flags"])
    values[F.UNIT_NPC_EMOTESTATE] = 0
    values[F.UNIT_FIELD_BYTES_2] = 0
    return values


def build_creature_create_block(creature: Mapping) -> bytes:
    """Build one ``UPDATETYPE_CREATE_OBJECT2`` block for a creature.

    Creatures are world objects the client does not control, so the block uses
    ``UPDATEFLAG_LIVING | UPDATEFLAG_HAS_POSITION`` (0x60) with movement info and
    the nine speed floats, but neither ``UPDATEFLAG_SELF`` nor
    ``UPDATEFLAG_HIGHGUID``.
    """
    guid = creature_guid(creature["guid"], creature["entry"])
    values = build_creature_values(creature)

    blocks = (F.UNIT_END + 31) // 32
    mask = bytearray(blocks * 4)
    for index in values:
        mask[(index // 32) * 4 + (index % 32) // 8] |= 1 << (index % 8)

    block = bytearray()
    block.append(UPDATETYPE_CREATE_OBJECT2)
    block += pack_guid(guid)
    block.append(TYPEID_UNIT)
    block += struct.pack("<H", UPDATEFLAG_LIVING | UPDATEFLAG_HAS_POSITION)
    block += _movement_info(
        float(creature["x"]), float(creature["y"]), float(creature["z"]),
        float(creature["o"]),
    )
    block += _speeds()

    block.append(blocks)
    block += mask
    for index in sorted(values):
        block += struct.pack("<I", values[index] & 0xFFFFFFFF)
    return bytes(block)


def build_creature_create_blocks(creatures: List[Mapping]) -> List[bytes]:
    """Return one create block per creature spawn."""
    return [build_creature_create_block(creature) for creature in creatures]


def build_creature_update(creatures: List[Mapping]) -> Optional[Tuple[int, bytes]]:
    """Return ``(opcode, payload)`` announcing newly visible creatures.

    Used after login when the player walks far enough to load a new part of the
    zone.  ``None`` means there is nothing to send.
    """
    creatures = list(creatures)
    if not creatures:
        return None
    body = struct.pack("<I", len(creatures)) + b"".join(
        build_creature_create_block(creature) for creature in creatures
    )
    if len(body) > 100:
        return Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT, struct.pack("<I", len(body)) + zlib.compress(body)
    return Opcodes.SMSG_UPDATE_OBJECT, body


# ---------------------------------------------------------------------------
# Item create blocks
# ---------------------------------------------------------------------------
def build_item_values(item: Mapping) -> Dict[int, int]:
    """Return ``{field_index: uint32_value}`` for an item create block.

    An item has no movement info, so the block uses ``UPDATETYPE_CREATE_OBJECT``
    with no position flags and only the item fields.
    """
    values: Dict[int, int] = {}
    values[F.OBJECT_FIELD_GUID] = int(item["guid"]) & 0xFFFFFFFF
    values[F.OBJECT_FIELD_GUID + 1] = (int(item["guid"]) >> 32) & 0xFFFFFFFF
    values[F.OBJECT_FIELD_TYPE] = TYPEMASK_OBJECT | TYPEMASK_ITEM
    values[F.OBJECT_FIELD_ENTRY] = int(item["item_entry"])
    values[F.OBJECT_FIELD_SCALE_X] = _float_bits(1.0)

    values[F.ITEM_FIELD_OWNER] = int(item["owner_guid"]) & 0xFFFFFFFF
    values[F.ITEM_FIELD_OWNER + 1] = (int(item["owner_guid"]) >> 32) & 0xFFFFFFFF
    values[F.ITEM_FIELD_CONTAINED] = int(item["owner_guid"]) & 0xFFFFFFFF
    values[F.ITEM_FIELD_CONTAINED + 1] = (int(item["owner_guid"]) >> 32) & 0xFFFFFFFF
    values[F.ITEM_FIELD_STACK_COUNT] = max(1, int(item.get("stack_count", 1)))
    values[F.ITEM_FIELD_DURABILITY] = int(item.get("durability", 0))
    values[F.ITEM_FIELD_MAXDURABILITY] = int(item.get("max_durability", 0))
    return values


def build_item_create_block(item: Mapping) -> bytes:
    """Build one create block for an item.

    An item has no movement info and no low/high-guid word because its update
    flags are ``0`` (TrinityCore leaves ``Item::m_updateFlag`` at
    ``UPDATEFLAG_NONE``), so the values mask follows the type id immediately.
    """
    guid = int(item["guid"])
    values = build_item_values(item)

    blocks = (F.ITEM_END + 31) // 32
    mask = bytearray(blocks * 4)
    for index in values:
        mask[(index // 32) * 4 + (index % 32) // 8] |= 1 << (index % 8)

    block = bytearray()
    block.append(UPDATETYPE_CREATE_OBJECT2)
    block += pack_guid(guid)
    block.append(TYPEID_ITEM)
    block += struct.pack("<H", 0)            # UPDATEFLAG_NONE: no movement data
    block.append(blocks)
    block += mask
    for index in sorted(values):
        block += struct.pack("<I", values[index] & 0xFFFFFFFF)
    return bytes(block)


def build_item_update(item: Mapping) -> Tuple[int, bytes]:
    """Return ``(opcode, payload)`` for ``SMSG_UPDATE_OBJECT`` (possibly compressed)."""
    body = struct.pack("<I", 1) + build_item_create_block(item)
    if len(body) > 100:
        return Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT, struct.pack("<I", len(body)) + zlib.compress(body)
    return Opcodes.SMSG_UPDATE_OBJECT, body


def build_player_values_update(char: Mapping, changed_fields: Dict[int, int],
                               force_uncompressed: bool = False) -> Tuple[int, bytes]:
    """Build a ``UPDATETYPE_VALUES`` block for the player.

    ``changed_fields`` maps field_index -> new_value.  Used after inventory
    swaps so the client's mirror of INV_SLOT_HEAD / PACK_SLOT_1 guids stays
    in sync with our server-side state.
    """
    return build_values_update(int(char["guid"]), F.PLAYER_VALUES_COUNT, changed_fields,
                               force_uncompressed=force_uncompressed)


def build_item_values_update(item: Mapping, changed_fields: Dict[int, int]) -> Tuple[int, bytes]:
    """Build a ``UPDATETYPE_VALUES`` block for one item object.

    ``changed_fields`` maps absolute field_index -> new_value.  After a swap the
    client's bag UI listens for ``ITEM_FIELD_CONTAINED`` / ``ITEM_FIELD_OWNER``
    changes on the item object itself, so those must be re-sent.
    """
    return build_values_update(int(item["guid"]), F.ITEM_END, changed_fields)


def build_values_update(guid: int, max_field: int, changed_fields: Dict[int, int],
                        force_uncompressed: bool = False) -> Tuple[int, bytes]:
    """Build a ``UPDATETYPE_VALUES`` block for an arbitrary object.

    ``guid`` is the object whose fields changed, ``max_field`` the object type's
    field count, ``changed_fields`` the field_index -> value pairs.
    """
    blocks = (max_field + 31) // 32
    mask = bytearray(blocks * 4)
    for index in changed_fields:
        mask[(index // 32) * 4 + (index % 32) // 8] |= 1 << (index % 8)

    block = bytearray()
    block.append(UPDATETYPE_VALUES)
    block += pack_guid(guid)
    block.append(blocks)
    block += mask
    for index in sorted(changed_fields):
        block += struct.pack("<I", changed_fields[index] & 0xFFFFFFFF)

    body = struct.pack("<I", 1) + bytes(block)
    # Send uncompressed when requested so a large values update is still a plain
    # SMSG_UPDATE_OBJECT, which some 3.3.5 clients parse more reliably.
    if not force_uncompressed and len(body) > 100:
        return Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT, struct.pack("<I", len(body)) + zlib.compress(body)
    return Opcodes.SMSG_UPDATE_OBJECT, body


def _monster_move_header(guid: int, x: float, y: float, z: float,
                         move_id: int, face_mode: int) -> bytearray:
    """Everything ``SMSG_MONSTER_MOVE`` shares, up to and including the face mode.

    Layout of ``Movement::PacketBuilder::WriteCommonMonsterMovePart`` (CMaNGOS
    3.3.5): packed guid, ``uint8`` vehicle/spline-toggle, the spline *start*
    position as a plain Vector3, the spline id and the ``MonsterMoveType`` byte.
    The orientation is **not** on the wire -- the client derives it from the
    spline -- so writing a fourth float here desynchronizes every following
    field and the client drops or misparses the packet.
    """
    out = bytearray()
    out += pack_guid(int(guid))
    out += b"\x00"                                    # toggleMoveflagUnk4
    out += struct.pack("<fff", float(x), float(y), float(z))
    out += struct.pack("<I", int(move_id) & 0xFFFFFFFF)   # spline id
    out += struct.pack("<B", int(face_mode) & 0xFF)
    return out


def build_monster_move(guid: int, x: float, y: float, z: float, o: float,
                       dest_x: float, dest_y: float, dest_z: float,
                       duration_ms: int = 1000, move_id: int = 0) -> bytes:
    """Build ``SMSG_MONSTER_MOVE`` for a straight two-point spline.

    ``Pos`` is the object's current position; the spline carries facing mode 0
    (normal), no flags, the duration and the single absolute destination point,
    which is exactly what CMaNGOS' ``WriteLinearPath`` emits for a one-segment
    path (``last_idx == 1``).  The client animates the creature along it.
    ``o`` is kept in the signature because callers track facing alongside the
    position, but it is deliberately not serialised.
    """
    out = _monster_move_header(guid, x, y, z, move_id, MONSTER_MOVE_NORMAL)
    out += struct.pack("<I", 0)                       # spline flags
    out += struct.pack("<I", max(1, int(duration_ms)))
    out += struct.pack("<I", 1)                       # point count
    out += struct.pack("<fff", float(dest_x), float(dest_y), float(dest_z))
    return bytes(out)


def _append_pack_xyz(out: bytearray, x: float, y: float, z: float) -> None:
    """CMaNGOS ``ByteBuffer::appendPackXYZ`` (11/11/10-bit quarter yards)."""
    packed = (int(x / 0.25) & 0x7FF)
    packed |= (int(y / 0.25) & 0x7FF) << 11
    packed |= (int(z / 0.25) & 0x3FF) << 22
    out += struct.pack("<I", packed & 0xFFFFFFFF)


def build_monster_move_path(guid: int, x: float, y: float, z: float,
                            o: float, points: list[tuple[float, float, float]],
                            duration_ms: int = 1000, move_id: int = 0) -> bytes:
    """Build a linear ``SMSG_MONSTER_MOVE`` spline with intermediate points.

    CMaNGOS' ``WriteLinearPath`` writes the destination as a full Vector3 and
    then packs each intermediate point as an offset from the midpoint between
    the start and destination.  Writing raw floats for the intermediates (the
    obvious but wrong interpretation) makes the 3.3.5 client misparse the
    packet, which shows up as units teleporting into the air or drifting away.
    ``build_monster_move`` remains the one-point fast path.
    """
    cleaned: list[tuple[float, float, float]] = []
    for point in points:
        destination = (float(point[0]), float(point[1]), float(point[2]))
        if cleaned and math.dist(cleaned[-1], destination) < 0.05:
            continue
        cleaned.append(destination)
    if not cleaned:
        cleaned = [(float(x), float(y), float(z))]
    if len(cleaned) == 1:
        return build_monster_move(
            guid, x, y, z, o, cleaned[0][0], cleaned[0][1], cleaned[0][2],
            duration_ms=duration_ms, move_id=move_id)

    start = (float(x), float(y), float(z))
    destination = cleaned[-1]
    middle = ((start[0] + destination[0]) * 0.5,
              (start[1] + destination[1]) * 0.5,
              (start[2] + destination[2]) * 0.5)
    out = _monster_move_header(guid, x, y, z, move_id, MONSTER_MOVE_NORMAL)
    out += struct.pack("<I", 0)                       # spline flags
    out += struct.pack("<I", max(1, int(duration_ms)))
    out += struct.pack("<I", len(cleaned))            # last_idx
    out += struct.pack("<fff", destination[0], destination[1],
                       destination[2])
    for point in cleaned[:-1]:
        _append_pack_xyz(out, middle[0] - point[0],
                         middle[1] - point[1], middle[2] - point[2])
    return bytes(out)


def build_monster_face(guid: int, x: float, y: float, z: float, o: float,
                       angle: float, move_id: int = 0) -> bytes:
    """Build a ``SMSG_MONSTER_MOVE`` that only turns a creature in place.

    CMaNGOS implements ``Unit::SetFacingTo`` as a zero-length spline carrying
    ``MoveSplineFlag::Final_Angle``.  On the wire the final-facing bits are
    masked out of the spline flags and become the ``MonsterMoveType`` byte
    instead: 0 normal, 1 stop, 2 facing spot, 3 facing target, **4 facing
    angle** (``MonsterMoveFacingAngle``).  Mode 4 is followed by the ``float``
    angle; mode 3 would be read as a full 8-byte object guid, which shifts the
    rest of the packet and is what previously made the client ignore (and
    sometimes misparse) the turn.  The angle is wrapped to ``[0, 2pi)`` exactly
    like ``MoveSplineInit::SetFacing`` does.  The spline repeats the current
    position as its only point, so the creature turns without travelling.
    """
    # CMaNGOS floors every spline segment at ``minimal_duration`` (1 ms), which
    # is what a zero-length turn reports; a duration of 0 makes some clients
    # treat the spline as finished before the facing is applied.
    duration_ms = 1
    out = _monster_move_header(guid, x, y, z, move_id, MONSTER_MOVE_FACING_ANGLE)
    out += struct.pack("<f", float(angle) % (2.0 * math.pi))
    out += struct.pack("<I", 0)                       # spline flags
    out += struct.pack("<I", duration_ms)
    out += struct.pack("<I", 1)                       # point count
    out += struct.pack("<fff", float(x), float(y), float(z))
    return bytes(out)


def build_item_destroy_object(guid: int) -> bytes:
    """``SMSG_DESTROY_OBJECT`` for an item: full guid + death flag."""
    return struct.pack("<QB", int(guid), 0)
