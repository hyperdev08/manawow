#!/usr/bin/env python3
"""Import a full WotLK 3.3.5a world database into the SQLite NPC tables.

The experimental worldserver keeps only the data it needs to place static NPCs
in front of the client: creature templates, spawn points, gossip menus, gossip
options and NPC text.  This tool reads the canonical CMaNGOS ``WoTLKDB`` dump
(``Full_DB/WoTLKDB_*.sql`` from ``cmangos/wotlk-db``) and fills those tables.

Usage::

    python tools/import_wotlk_db.py --dump C:\\path\\WoTLKDB.sql [--db data/world.db]

The import is idempotent: by default the creature/gossip/npc-text tables are
cleared first, so running it again always produces the same data and never
leaves the old nine-NPC demo population behind.  Character and account data are
never touched.  Stop the worldserver before importing so SQLite does not have to
contend with a live writer.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "tools"))

import wotlk_dump

try:  # importing the package is only needed for the optional start-position report
    from src.world.character_db import _START_POSITIONS
except Exception:  # pragma: no cover - tool still works without the server package
    _START_POSITIONS = {}


# Menu-option text that CMaNGOS stores as a client global-string token instead
# of a literal.  These tokens mostly live in the default menu 0; translating
# them keeps the few real menus that use them readable in the client.
OPTION_TEXT_TOKENS = {
    "GOSSIP_OPTION_QUESTGIVER": "I have a quest for you.",
    "GOSSIP_OPTION_SPIRITHEALER": "Return me to life.",
    "GOSSIP_OPTION_SPIRITGUIDE": "I need to find my corpse.",
    "GOSSIP_OPTION_BANKER": "I would like to make a deposit.",
    "GOSSIP_OPTION_BATTLEFIELD": "I would like to enter a battleground.",
    "GOSSIP_OPTION_AUCTIONEER": "I would like to browse your auctions.",
    "GOSSIP_OPTION_ARMORER": "I would like to repair my equipment.",
    "GOSSIP_OPTION_TABARDVENDOR": "I would like to buy a tabard.",
    "GOSSIP_OPTION_STABLEPET": "I would like to stable my pet.",
    "GOSSIP_OPTION_BOT": "Bot commands.",
}

# Blizzard's world database also ships the content their tools need: invisible
# stalkers, mining/fire/"bunny" helpers, quest-effect triggers, camera shakers,
# target dummies and "[DND]" test props.  They are real ``creature`` rows but not
# creatures a player is meant to see.  Rendering them is what looked like random
# invisible units and, for the "[DND] TAR Pedestal" set, a Karazhan doodad model
# standing in the starting zones.  Every name below was checked against the whole
# dump: none of them has a gossip menu and none is a vendor, trainer or quest
# giver.  ``^old\b`` is deliberately *not* in the list, it would delete real
# quest-givers such as "Old Man Heming" and "Old Orok".
DEV_CONTENT_RE = re.compile(
    r"\[DND\]|\[UNUSED\]|\bbunny\b|\btrigger\b|\binvisible\b|\bwaypoint\b|\bdummy\b"
    r"|\bvfx\b|\bvisual\b|\bdoodad\b|\bmarker\b|\bcamera\b|\bteleporter\b|\btarget\b",
    re.IGNORECASE,
)


def is_player_visible(name) -> int:
    """Return 0 for developer/tooling creatures that should never be spawned."""
    return 0 if DEV_CONTENT_RE.search(name or "") else 1


# SQLite stores integers as signed 64-bit values.  Several CMaNGOS spell columns
# (SpellFamilyFlags, AttributesEx*) are unsigned bitmasks that can exceed that
# range, so wrap them into the signed range instead of aborting the import.
_SQLITE_INT_MAX = 9223372036854775807
_SQLITE_INT_MIN = -9223372036854775808


def _sqlite_int(value) -> int:
    """Coerce a dump value to an integer SQLite can store."""
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        number = int(text)
    except (TypeError, ValueError):
        try:
            number = int(float(text))
        except (TypeError, ValueError):
            return 0
    if number > _SQLITE_INT_MAX or number < _SQLITE_INT_MIN:
        number &= 0xFFFFFFFFFFFFFFFF
        if number > _SQLITE_INT_MAX:
            number -= 0x10000000000000000
    return number


def _sqlite_float(value) -> float:
    """Coerce a dump value to a float, defaulting to 0.0."""
    if value is None:
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


# Creature class -> UNIT_FIELD_BYTES_0 power type.  Warriors use rage, rogues
# and druids (cat form) use energy, death knights use runes; everything else
# uses mana.
_POWER_TYPE_BY_CLASS = {
    1: 1,   # warrior -> rage
    4: 3,   # rogue -> energy
    6: 6,   # death knight -> runes
}

TEMPLATE_COLUMNS = (
    "entry", "name", "subname", "cursor_name", "gossip_menu_id",
    "min_level", "max_level", "faction", "npc_flags", "unit_flags",
    "unit_flags2", "unit_class", "unit_gender", "unit_power_type", "scale",
    "type", "family", "classification", "display_id1", "display_id2",
    "display_id3", "display_id4", "hp_multiplier", "energy_multiplier",
    "leader", "movement_info_id", "movement_type", "base_health", "flags", "visible",
    "kill_credit1", "kill_credit2", "quest_item1", "quest_item2", "quest_item3",
    "quest_item4", "quest_item5", "quest_item6", "loot_id",
    # Combat / AI data the creature AI needs at runtime.
    "spell_list_id", "ai_name", "aggro_radius", "chase_distance",
    "speed_walk", "speed_run", "min_damage", "max_damage",
    "attack_power", "ranged_attack_power", "armor",
    "melee_attack_speed", "ranged_attack_speed", "crit_chance",
)

SPAWN_COLUMNS = ("guid", "entry", "map", "zone", "x", "y", "z", "o",
                 "display_id", "spawn_time", "movement_type", "spawndist")

# Spell template columns as named in the CMaNGOS ``spell_template`` table.
SPELL_TEMPLATE_COLUMNS = (
    "Id", "Category", "Dispel", "Mechanic", "Attributes", "AttributesEx", "AttributesEx2",
    "AttributesEx3", "AttributesEx4", "AttributesEx5", "AttributesEx6", "AttributesEx7",
    "Stances", "Stances2", "StancesNot", "StancesNot2", "Targets", "TargetCreatureType",
    "RequiresSpellFocus", "FacingCasterFlags", "CasterAuraState", "TargetAuraState",
    "CasterAuraStateNot", "TargetAuraStateNot", "CasterAuraSpell", "TargetAuraSpell",
    "ExcludeCasterAuraSpell", "ExcludeTargetAuraSpell", "CastingTimeIndex", "RecoveryTime",
    "CategoryRecoveryTime", "InterruptFlags", "AuraInterruptFlags", "ChannelInterruptFlags",
    "ProcFlags", "ProcChance", "ProcCharges", "MaxLevel", "BaseLevel", "SpellLevel",
    "DurationIndex", "PowerType", "ManaCost", "ManaCostPerlevel", "ManaPerSecond",
    "ManaPerSecondPerLevel", "RangeIndex", "Speed", "ModalNextSpell", "StackAmount",
    "Totem1", "Totem2", "Reagent1", "Reagent2", "Reagent3", "Reagent4", "Reagent5",
    "Reagent6", "Reagent7", "Reagent8", "ReagentCount1", "ReagentCount2", "ReagentCount3",
    "ReagentCount4", "ReagentCount5", "ReagentCount6", "ReagentCount7", "ReagentCount8",
    "EquippedItemClass", "EquippedItemSubClassMask", "EquippedItemInventoryTypeMask",
    "Effect1", "Effect2", "Effect3", "EffectDieSides1", "EffectDieSides2", "EffectDieSides3",
    "EffectRealPointsPerLevel1", "EffectRealPointsPerLevel2", "EffectRealPointsPerLevel3",
    "EffectBasePoints1", "EffectBasePoints2", "EffectBasePoints3", "EffectMechanic1",
    "EffectMechanic2", "EffectMechanic3", "EffectImplicitTargetA1", "EffectImplicitTargetA2",
    "EffectImplicitTargetA3", "EffectImplicitTargetB1", "EffectImplicitTargetB2",
    "EffectImplicitTargetB3", "EffectRadiusIndex1", "EffectRadiusIndex2", "EffectRadiusIndex3",
    "EffectApplyAuraName1", "EffectApplyAuraName2", "EffectApplyAuraName3", "EffectAmplitude1",
    "EffectAmplitude2", "EffectAmplitude3", "EffectMultipleValue1", "EffectMultipleValue2",
    "EffectMultipleValue3", "EffectChainTarget1", "EffectChainTarget2", "EffectChainTarget3",
    "EffectItemType1", "EffectItemType2", "EffectItemType3", "EffectMiscValue1",
    "EffectMiscValue2", "EffectMiscValue3", "EffectMiscValueB1", "EffectMiscValueB2",
    "EffectMiscValueB3", "EffectTriggerSpell1", "EffectTriggerSpell2", "EffectTriggerSpell3",
    "EffectPointsPerComboPoint1", "EffectPointsPerComboPoint2", "EffectPointsPerComboPoint3",
    "EffectSpellClassMask1_1", "EffectSpellClassMask1_2", "EffectSpellClassMask1_3",
    "EffectSpellClassMask2_1", "EffectSpellClassMask2_2", "EffectSpellClassMask2_3",
    "EffectSpellClassMask3_1", "EffectSpellClassMask3_2", "EffectSpellClassMask3_3",
    "SpellVisual", "SpellVisual2", "SpellIconID", "ActiveIconID", "SpellPriority", "SpellName",
    "Rank1", "Rank2", "Rank3", "Rank4", "Rank5", "Rank6", "Rank7", "Rank8", "Rank9",
    "Rank10", "Rank11", "Rank12", "Rank13", "Rank14", "Rank15", "Rank16",
    "ManaCostPercentage", "StartRecoveryCategory", "StartRecoveryTime", "MaxTargetLevel",
    "SpellFamilyName", "SpellFamilyFlags", "SpellFamilyFlags2", "MaxAffectedTargets",
    "DmgClass", "PreventionType", "StanceBarOrder", "DmgMultiplier1", "DmgMultiplier2",
    "DmgMultiplier3", "MinFactionId", "MinReputation", "RequiredAuraVision",
    "TotemCategory1", "TotemCategory2", "AreaId", "SchoolMask", "RuneCostID", "SpellMissileID",
    "PowerDisplayId", "EffectBonusCoefficient1", "EffectBonusCoefficient2",
    "EffectBonusCoefficient3", "EffectBonusCoefficientFromAP1",
    "EffectBonusCoefficientFromAP2", "EffectBonusCoefficientFromAP3",
    "SpellDescriptionVariableID", "SpellDifficultyId", "IsServerSide", "AttributesServerside",
)


def _snake_case(name: str) -> str:
    """Convert CMaNGOS CamelCase to the server's lower_snake_case."""
    name = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name)
    name = re.sub(r"(?<=[A-Z])([A-Z][a-z])", r"_\1", name)
    return name.lower()


# Column names that are not plain snake_case conversions.
_SPELL_COLUMN_OVERRIDES = {
    "Id": "id",
    "Dispel": "dispel_type",
    "SpellName": "name",
    "ManaCostPerlevel": "mana_cost_per_level",
    "SpellIconID": "spell_icon_id",
    "ActiveIconID": "active_icon_id",
    "SpellMissileID": "spell_missile_id",
    "RuneCostID": "rune_cost_id",
    "SpellDescriptionVariableID": "spell_description_variable_id",
    "EquippedItemSubClassMask": "equipped_item_subclass_mask",
}

# (SQLite column, dump column) pairs.  Columns the server schema does not
# declare are skipped at insert time by _insert_mapped().
SPELL_TEMPLATE_MAP = tuple(
    (_SPELL_COLUMN_OVERRIDES.get(dump_col) or _snake_case(dump_col), dump_col)
    for dump_col in SPELL_TEMPLATE_COLUMNS
) + (
    # The schema keeps both the display name and the base rank text.
    ("spell_name", "SpellName"),
    ("rank", "Rank1"),
)

# SQLite columns in spell_templates that hold text / floating point values.
SPELL_TEXT_SQLITE_COLUMNS = frozenset(
    ("name", "spell_name", "rank") + tuple(f"rank{i}" for i in range(1, 17))
)
SPELL_FLOAT_SQLITE_COLUMNS = frozenset((
    "speed",
    "effect_real_points_per_level1", "effect_real_points_per_level2",
    "effect_real_points_per_level3",
    "effect_multiple_value1", "effect_multiple_value2", "effect_multiple_value3",
    "effect_points_per_combo_point1", "effect_points_per_combo_point2",
    "effect_points_per_combo_point3",
    "dmg_multiplier1", "dmg_multiplier2", "dmg_multiplier3",
    "effect_bonus_coefficient1", "effect_bonus_coefficient2",
    "effect_bonus_coefficient3",
    "effect_bonus_coefficient_from_ap1", "effect_bonus_coefficient_from_ap2",
    "effect_bonus_coefficient_from_ap3",
))

CREATURE_TEMPLATE_SPELLS_MAP = (
    ("entry", "entry"), ("set_id", "setId"),
    ("spell1", "spell1"), ("spell2", "spell2"), ("spell3", "spell3"),
    ("spell4", "spell4"), ("spell5", "spell5"), ("spell6", "spell6"),
    ("spell7", "spell7"), ("spell8", "spell8"), ("spell9", "spell9"),
    ("spell10", "spell10"),
)

CREATURE_SPELL_LIST_MAP = (
    ("id", "Id"), ("position", "Position"), ("spell_id", "SpellId"),
    ("flags", "Flags"), ("combat_condition", "CombatCondition"),
    ("target_id", "TargetId"), ("script_id", "ScriptId"),
    ("availability", "Availability"), ("probability", "Probability"),
    ("initial_min", "InitialMin"), ("initial_max", "InitialMax"),
    ("repeat_min", "RepeatMin"), ("repeat_max", "RepeatMax"),
    ("comments", "Comments"),
)

CREATURE_SPELL_LIST_ENTRY_MAP = (
    ("id", "Id"), ("name", "Name"),
    ("chance_support_action", "ChanceSupportAction"),
    ("chance_ranged_attack", "ChanceRangedAttack"),
)

CREATURE_SPELL_TARGETING_MAP = (
    ("id", "Id"), ("type", "Type"), ("param1", "Param1"),
    ("param2", "Param2"), ("param3", "Param3"),
    ("unit_condition", "UnitCondition"), ("comments", "Comments"),
)

CREATURE_MODEL_INFO_MAP = (
    ("modelid", "modelid"), ("bounding_radius", "bounding_radius"),
    ("combat_reach", "combat_reach"), ("speed_walk", "SpeedWalk"),
    ("speed_run", "SpeedRun"), ("gender", "gender"),
    ("modelid_other_gender", "modelid_other_gender"),
    ("modelid_alternative", "modelid_alternative"),
)

CREATURE_CLASSLEVELSTATS_MAP = (
    ("level", "Level"), ("class", "Class"),
    ("base_health_exp0", "BaseHealthExp0"),
    ("base_health_exp1", "BaseHealthExp1"),
    ("base_health_exp2", "BaseHealthExp2"),
    ("base_mana", "BaseMana"),
    ("base_damage_exp0", "BaseDamageExp0"),
    ("base_damage_exp1", "BaseDamageExp1"),
    ("base_damage_exp2", "BaseDamageExp2"),
    ("base_melee_attack_power", "BaseMeleeAttackPower"),
    ("base_ranged_attack_power", "BaseRangedAttackPower"),
    ("base_armor", "BaseArmor"),
    ("strength", "Strength"), ("agility", "Agility"),
    ("stamina", "Stamina"), ("intellect", "Intellect"),
    ("spirit", "Spirit"),
)
CREATURE_CLASSLEVELSTATS_FLOATS = frozenset(
    ("base_damage_exp0", "base_damage_exp1", "base_damage_exp2")
)

PLAYER_CLASSLEVELSTATS_MAP = (
    ("class", "class"),
    ("level", "level"),
    ("basehp", "basehp"),
    ("basemana", "basemana"),
)

PLAYER_LEVELSTATS_MAP = (
    ("race", "race"),
    ("class", "class"),
    ("level", "level"),
    ("str", "str"),
    ("agi", "agi"),
    ("sta", "sta"),
    ("inte", "inte"),
    ("spi", "spi"),
)

CREATURE_LOOT_TEMPLATE_MAP = (
    ("entry", "entry"), ("item", "item"),
    ("chance_or_quest_chance", "ChanceOrQuestChance"),
    ("group_id", "groupid"), ("mincount_or_ref", "mincountOrRef"),
    ("maxcount", "maxcount"), ("condition_id", "condition_id"),
    ("comments", "comments"),
)

REFERENCE_LOOT_TEMPLATE_MAP = CREATURE_LOOT_TEMPLATE_MAP

CREATURE_EQUIP_TEMPLATE_MAP = (
    ("entry", "entry"), ("equipentry1", "equipentry1"),
    ("equipentry2", "equipentry2"), ("equipentry3", "equipentry3"),
)

CREATURE_IMMUNITIES_MAP = (
    ("entry", "Entry"), ("set_id", "SetId"),
    ("type", "Type"), ("value", "Value"),
)

CREATURE_COOLDOWNS_MAP = (
    ("entry", "Entry"), ("spell_id", "SpellId"),
    ("cooldown_min", "CooldownMin"), ("cooldown_max", "CooldownMax"),
)

SPELL_CONE_MAP = (("id", "Id"), ("cone_degrees", "ConeDegrees"))

SPELL_TARGET_POSITION_MAP = (
    ("id", "id"), ("target_map", "target_map"),
    ("target_position_x", "target_position_x"),
    ("target_position_y", "target_position_y"),
    ("target_position_z", "target_position_z"),
    ("target_orientation", "target_orientation"),
)

PLAYERCREATEINFO_SPELL_MAP = (
    ("race", "race"), ("class", "class"),
    ("spell", "Spell"), ("note", "Note"),
)

PLAYERCREATEINFO_ACTION_MAP = (
    ("race", "race"), ("class", "class"), ("button", "button"),
    ("action", "action"), ("type", "type"),
)


def read_rune_cost_dbc(path: str) -> list[tuple[int, int, int, int, int]]:
    """Read ``SpellRuneCost.dbc`` (WDBC) into ``(id, blood, unholy, frost, rp)``.

    The 3.3.5a file has five ``uint32`` fields per record: ``Id`` and the blood,
    unholy and frost rune costs, followed by the runic power gain.
    """
    import struct

    with open(path, "rb") as handle:
        data = handle.read()
    magic, count, field_count, record_size, _string_size = struct.unpack(
        "<4sIIII", data[:20]
    )
    if magic != b"WDBC" or field_count != 5:
        raise ValueError(f"{path} is not a 3.3.5a SpellRuneCost.dbc")
    rows = []
    for index in range(count):
        offset = 20 + index * record_size
        fields = struct.unpack("<5I", data[offset:offset + record_size])
        rows.append(tuple(int(value) for value in fields))
    return rows


def read_spell_duration_dbc(path: str) -> list[tuple[int, int, int, int]]:
    """Read ``SpellDuration.dbc`` (WDBC) into ``(id, duration, per_level, max)``.

    The 3.3.5a file has four ``int32`` fields per record: ``Id``, the base
    duration in milliseconds, the per-level part and the maximum duration.  A
    negative base (usually ``-1``) means "until cancelled".
    """
    import struct

    with open(path, "rb") as handle:
        data = handle.read()
    magic, count, field_count, record_size, _string_size = struct.unpack(
        "<4sIIII", data[:20]
    )
    if magic != b"WDBC" or field_count != 4:
        raise ValueError(f"{path} is not a 3.3.5a SpellDuration.dbc")
    rows = []
    for index in range(count):
        offset = 20 + index * record_size
        fields = struct.unpack("<4i", data[offset:offset + record_size])
        rows.append(tuple(int(value) for value in fields))
    return rows

# ``item_template`` -> ``item_templates``.  Loot and equipment are useless
# without real names, icons and weapon damage, so the whole table is imported;
# the hand-written starter rows in character_db are re-seeded afterwards.
# Column names that are not plain CamelCase -> snake_case conversions:
#   name -> name1 (the server keeps the four locale slots), displayid ->
#   display_id, Flags/Flags2 -> flags1/flags2, armor -> resistance_armor,
#   *_res -> resistance_*, sheath -> sheath_type, itemset -> item_set,
#   spellid_N -> spell_idN, spellcategorycooldown_N -> spell_category_cooldownN.
ITEM_TEMPLATE_OVERRIDES = {
    "name": "name1",
    "displayid": "display_id",
    "Flags": "flags1",
    "Flags2": "flags2",
    "requiredspell": "required_spell",
    "requiredhonorrank": "required_honor_rank",
    "maxcount": "max_count",
    "armor": "resistance_armor",
    "holy_res": "resistance_holy",
    "fire_res": "resistance_fire",
    "nature_res": "resistance_nature",
    "frost_res": "resistance_frost",
    "shadow_res": "resistance_shadow",
    "arcane_res": "resistance_arcane",
    "startquest": "start_quest",
    "lockid": "lock_id",
    "sheath": "sheath_type",
    "itemset": "item_set",
}
ITEM_TEMPLATE_FLOATS = frozenset((
    "damage_min1", "damage_max1", "damage_min2", "damage_max2",
    "ranged_mod_range", "armor_damage_modifier",
))
ITEM_TEMPLATE_TEXTS = frozenset(("name1", "description"))


def _item_template_column(dump_col: str) -> str:
    """Map one CMaNGOS ``item_template`` column onto the server's schema."""
    if dump_col in ITEM_TEMPLATE_OVERRIDES:
        return ITEM_TEMPLATE_OVERRIDES[dump_col]
    match = re.fullmatch(r"dmg_(min|max|type)(\d)", dump_col)
    if match:
        return f"damage_{match.group(1)}{match.group(2)}"
    match = re.fullmatch(
        r"spell(id|trigger|charges|cooldown|category|categorycooldown)_(\d)",
        dump_col,
    )
    if match:
        words = {
            "id": "spell_id", "trigger": "spell_trigger",
            "charges": "spell_charges", "cooldown": "spell_cooldown",
            "category": "spell_category",
            "categorycooldown": "spell_category_cooldown",
        }
        return words[match.group(1)] + match.group(2)
    return _snake_case(dump_col)


def _item_template_map(dump_columns) -> tuple:
    """Build the (sqlite, dump) pairs for every dump column we can store.

    Columns the schema does not know (``unk0``, ``ScriptName``, ``FoodType``,
    the ``minMoneyLoot`` pair, ...) are dropped; ``_insert_mapped`` filters the
    result against the live table anyway.
    """
    return tuple((_item_template_column(col), col) for col in dump_columns)

# Spell tables owned by the worldserver schema.  They are recreated from
# src/world/spell_tables.sql on every import so schema changes take effect.
SPELL_TABLES = (
    "spell_templates", "creature_template_spells", "creature_spell_list",
    "creature_spell_list_entry", "creature_spell_targeting",
    "creature_model_info", "creature_template_classlevelstats",
    "creature_loot_template", "creature_equip_template",
    "creature_immunities", "creature_cooldowns", "spell_cone",
    "spell_target_position", "spell_rune_cost", "spell_duration",
    "playercreateinfo_spell", "playercreateinfo_action",
)


def _insert_rune_costs(con, rows) -> int:
    """Fill ``spell_rune_cost`` from ``SpellRuneCost.dbc`` records."""
    con.executemany(
        "INSERT OR REPLACE INTO spell_rune_cost("
        "id, blood, unholy, frost, runic_power) VALUES(?,?,?,?,?)",
        rows,
    )
    return len(rows)


def _insert_spell_durations(con, rows) -> int:
    """Fill ``spell_duration`` from ``SpellDuration.dbc`` records."""
    con.executemany(
        "INSERT OR REPLACE INTO spell_duration("
        "id, duration, duration_per_level, max_duration) VALUES(?,?,?,?)",
        rows,
    )
    return len(rows)


def _insert_mapped(con, table, pairs, raw_rows, float_columns=frozenset(),
                   text_columns=frozenset()) -> int:
    """Insert dump rows into ``table`` using (sqlite_column, dump_column) pairs."""
    present = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    pairs = [(sqlite_col, dump_col) for sqlite_col, dump_col in pairs
             if sqlite_col in present]
    if not pairs:
        return 0
    columns = list(dict.fromkeys(sqlite_col for sqlite_col, _ in pairs))
    source = {sqlite_col: dump_col for sqlite_col, dump_col in pairs}
    sql = (f"INSERT OR REPLACE INTO {table}({', '.join(columns)}) "
           f"VALUES({', '.join('?' for _ in columns)})")
    rows = []
    count = 0
    for raw in raw_rows:
        values = []
        for column in columns:
            value = raw.get(source[column])
            if column in text_columns:
                values.append(_text_or_empty(value))
            elif column in float_columns:
                values.append(_sqlite_float(value))
            else:
                values.append(_sqlite_int(value))
        rows.append(tuple(values))
        count += 1
        if len(rows) >= 2000:
            con.executemany(sql, rows)
            rows.clear()
    if rows:
        con.executemany(sql, rows)
    return count


def _text_or_empty(value) -> str:
    return "" if value is None else str(value)


def _choose_menu_rows(menus: list[dict]) -> dict[int, dict]:
    """Pick one row per menu id (conditions are not simulated)."""
    chosen: dict[int, dict] = {}
    for row in menus:
        menu_id = int(row.get("entry") or 0)
        if not menu_id:
            continue
        candidate_score = (
            0 if int(row.get("condition_id") or 0) == 0 else 1,
            int(row.get("text_id") or 0),
        )
        current = chosen.get(menu_id)
        if current is None or candidate_score < current[0]:
            chosen[menu_id] = (candidate_score, row)
    return {menu_id: row for menu_id, (_, row) in chosen.items()}


def _choose_options(options: list[dict]) -> dict[tuple[int, int], dict]:
    """Pick one row per ``(menu, option index)``, preferring unconditional rows."""
    chosen: dict[tuple[int, int], tuple[tuple[int, int], dict]] = {}
    for row in options:
        key = (int(row.get("menu_id") or 0), int(row.get("id") or 0))
        if key[0] <= 0:
            continue
        score = (
            0 if int(row.get("condition_id") or 0) == 0 else 1,
            int(row.get("action_menu_id") or 0),
        )
        current = chosen.get(key)
        if current is None or score < current[0]:
            chosen[key] = (score, row)
    return {key: row for key, (_, row) in chosen.items()}


def _normalise_option_text(row: dict) -> str:
    text = _text_or_empty(row.get("option_text")).strip()
    return OPTION_TEXT_TOKENS.get(text, text)


def _choose_npc_text(row: dict) -> tuple[str, str, int, int, list[tuple[int, int]]]:
    """Return the first populated text variant of an ``npc_text`` row.

    CMaNGOS stores eight independent male/female variants with a probability
    and three emote pairs each.  The experimental server only has room for one
    variant, so the first non-empty one is used; probability is left at 1.0
    because it is a relative weight and the seven unused slots are zero.
    """
    for index in range(8):
        male = _text_or_empty(row.get(f"text{index}_0"))
        female = _text_or_empty(row.get(f"text{index}_1"))
        if not male and not female:
            continue
        emotes = []
        for pair in range(3):
            delay = int(row.get(f"em{index}_{pair * 2}") or 0)
            emote = int(row.get(f"em{index}_{pair * 2 + 1}") or 0)
            emotes.append((delay, emote))
        return (
            male or female,
            female or male,
            int(row.get(f"lang{index}") or 0),
            1,
            emotes,
        )
    return "", "", 0, 1, [(0, 0), (0, 0), (0, 0)]


def _template_row(template: dict) -> tuple:
    entry = int(template["Entry"])
    unit_class = int(template.get("UnitClass") or 1)
    power_type = _POWER_TYPE_BY_CLASS.get(unit_class, 0)
    scale = float(template.get("Scale") or 1.0)
    health = max(1, int(template.get("MinLevelHealth") or 1))
    return (
        entry,
        _text_or_empty(template.get("Name")),
        _text_or_empty(template.get("SubName")),
        _text_or_empty(template.get("IconName")),
        int(template.get("GossipMenuId") or 0),
        max(1, int(template.get("MinLevel") or 1)),
        max(1, int(template.get("MaxLevel") or 1)),
        int(template.get("Faction") or 0),
        int(template.get("NpcFlags") or 0),
        int(template.get("UnitFlags") or 0),
        int(template.get("UnitFlags2") or 0),
        unit_class,
        0,
        power_type,
        max(0.01, scale),
        int(template.get("CreatureType") or 0),
        int(template.get("Family") or 0),
        int(template.get("Rank") or 0),
        int(template.get("DisplayId1") or 0),
        int(template.get("DisplayId2") or 0),
        int(template.get("DisplayId3") or 0),
        int(template.get("DisplayId4") or 0),
        float(template.get("HealthMultiplier") or 1.0),
        float(template.get("PowerMultiplier") or 1.0),
        int(template.get("RacialLeader") or 0),
        int(template.get("MovementTemplateId") or 0),
        int(template.get("MovementType") if template.get("MovementType") is not None else -1),
        health,
        int(template.get("CreatureTypeFlags") or 0),
        is_player_visible(template.get("Name")),
        int(template.get("KillCredit1") or 0),
        int(template.get("KillCredit2") or 0),
        int(template.get("QuestItem1") or 0),
        int(template.get("QuestItem2") or 0),
        int(template.get("QuestItem3") or 0),
        int(template.get("QuestItem4") or 0),
        int(template.get("QuestItem5") or 0),
        int(template.get("QuestItem6") or 0),
        int(template.get("LootId") or 0),
        # Combat / AI data.
        int(template.get("SpellList") or 0),
        _text_or_empty(template.get("AIName")),
        float(template.get("Detection") or 20.0),
        float(template.get("Pursuit") or 5000.0) / 100.0,
        float(template.get("SpeedWalk") or 2.5),
        float(template.get("SpeedRun") or 8.0),
        float(template.get("MinMeleeDmg") or 0.0),
        float(template.get("MaxMeleeDmg") or 0.0),
        int(template.get("MeleeAttackPower") or 0),
        int(template.get("RangedAttackPower") or 0),
        int(template.get("Armor") or 0),
        int(template.get("MeleeBaseAttackTime") or 2000),
        int(template.get("RangedBaseAttackTime") or 3000),
        0.0,
    )


def import_world(dump_path: str, db_path: str, clear: bool = True,
                 include_instances: bool = True,
                 rune_cost_dbc: str | None = None,
                 spell_duration_dbc: str | None = None) -> dict[str, int]:
    started = time.time()
    text = wotlk_dump.read_dump(dump_path)
    print(f"loaded {len(text):,} bytes from {dump_path}")

    _, raw_templates = wotlk_dump.rows(text, "creature_template")
    _, raw_spawns = wotlk_dump.rows(text, "creature")
    _, raw_menus = wotlk_dump.rows(text, "gossip_menu")
    _, raw_options = wotlk_dump.rows(text, "gossip_menu_option")
    _, raw_npc_text = wotlk_dump.rows(text, "npc_text")
    _, raw_spell_template = wotlk_dump.rows(text, "spell_template")
    _, raw_creature_template_spells = wotlk_dump.rows(text, "creature_template_spells")
    _, raw_creature_spell_list = wotlk_dump.rows(text, "creature_spell_list")
    _, raw_creature_spell_list_entry = wotlk_dump.rows(
        text, "creature_spell_list_entry")
    _, raw_creature_spell_targeting = wotlk_dump.rows(
        text, "creature_spell_targeting")
    _, raw_creature_model_info = wotlk_dump.rows(text, "creature_model_info")
    _, raw_creature_template_classlevelstats = wotlk_dump.rows(text, "creature_template_classlevelstats")
    _, raw_player_classlevelstats = wotlk_dump.rows(text, "player_classlevelstats")
    _, raw_player_levelstats = wotlk_dump.rows(text, "player_levelstats")
    _, raw_creature_loot_template = wotlk_dump.rows(text, "creature_loot_template")
    _, raw_reference_loot_template = wotlk_dump.rows(text, "reference_loot_template")
    _, raw_creature_equip_template = wotlk_dump.rows(text, "creature_equip_template")
    _, raw_creature_immunities = wotlk_dump.rows(text, "creature_immunities")
    _, raw_creature_cooldowns = wotlk_dump.rows(text, "creature_cooldowns")
    _, raw_spell_cone = wotlk_dump.rows(text, "spell_cone")
    _, raw_spell_target_position = wotlk_dump.rows(text, "spell_target_position")
    _, raw_playercreateinfo_spell = wotlk_dump.rows(
        text, "playercreateinfo_spell")
    _, raw_playercreateinfo_action = wotlk_dump.rows(
        text, "playercreateinfo_action")
    _, raw_creature_movement = wotlk_dump.rows(text, "creature_movement")
    _, raw_creature_movement_template = wotlk_dump.rows(
        text, "creature_movement_template")
    item_columns, raw_item_template = wotlk_dump.rows(text, "item_template")
    item_map = _item_template_map(item_columns)
    del text

    rune_cost_rows = read_rune_cost_dbc(rune_cost_dbc) if rune_cost_dbc else []
    duration_rows = (read_spell_duration_dbc(spell_duration_dbc)
                     if spell_duration_dbc else [])

    template_rows = [_template_row(row) for row in raw_templates]
    known_entries = {row[0] for row in template_rows}

    spawn_rows = []
    skipped_missing = 0
    skipped_phase = 0
    skipped_mask = 0
    for row in raw_spawns:
        if int(row.get("spawnMask") or 0) & 1 == 0:
            skipped_mask += 1
            continue
        if int(row.get("phaseMask") or 0) != 1:
            skipped_phase += 1
            continue
        entry = int(row.get("id") or 0)
        if entry not in known_entries:
            skipped_missing += 1
            continue
        spawn_rows.append((
            int(row["guid"]),
            entry,
            int(row.get("map") or 0),
            0,
            float(row.get("position_x") or 0.0),
            float(row.get("position_y") or 0.0),
            float(row.get("position_z") or 0.0),
            float(row.get("orientation") or 0.0),
            0,
            int(row.get("spawntimesecsmin") or 0),
            # ``MovementType`` and ``spawndist`` are per-spawn overrides.  A
            # missing value stays -1/0.0 so the runtime can fall back to the
            # template or a heuristic instead of forcing every creature idle.
            int(row.get("MovementType") if row.get("MovementType") is not None else -1),
            float(row.get("spawndist") or 0.0),
        ))

    # Waypoint paths.  ``creature_movement.Id`` is the spawn GUID, so rows for
    # spawns that were filtered out (other phases, missing templates) are
    # dropped as well.  ``creature_movement_template`` is keyed by entry and
    # optional path id.
    spawn_guids = {row[0] for row in spawn_rows}
    movement_rows = []
    for row in raw_creature_movement:
        guid = int(row.get("Id") or 0)
        if guid not in spawn_guids:
            continue
        movement_rows.append((
            guid,
            int(row.get("Point") or 0),
            float(row.get("PositionX") or 0.0),
            float(row.get("PositionY") or 0.0),
            float(row.get("PositionZ") or 0.0),
            float(row.get("Orientation") or 0.0),
            int(row.get("WaitTime") or 0),
            int(row.get("ScriptId") or 0),
            _text_or_empty(row.get("Comment")),
        ))

    movement_template_rows = []
    for row in raw_creature_movement_template:
        entry = int(row.get("Entry") or 0)
        if entry not in known_entries:
            continue
        movement_template_rows.append((
            entry,
            int(row.get("PathId") or 0),
            int(row.get("Point") or 0),
            float(row.get("PositionX") or 0.0),
            float(row.get("PositionY") or 0.0),
            float(row.get("PositionZ") or 0.0),
            float(row.get("Orientation") or 0.0),
            int(row.get("WaitTime") or 0),
            int(row.get("ScriptId") or 0),
            _text_or_empty(row.get("Comment")),
        ))

    menu_rows = []
    for menu_id, row in sorted(_choose_menu_rows(raw_menus).items()):
        menu_rows.append((menu_id, int(row.get("text_id") or 0)))
    option_rows = []
    for (menu_id, option_index), row in sorted(_choose_options(raw_options).items()):
        action_menu = int(row.get("action_menu_id") or 0)
        if action_menu < 0:
            action_menu = 0
        option_rows.append((
            menu_id,
            option_index,
            int(row.get("option_id") or 0),
            int(row.get("option_icon") or 0),
            int(row.get("box_coded") or 0),
            int(row.get("box_money") or 0),
            _normalise_option_text(row),
            _text_or_empty(row.get("box_text")),
            action_menu,
            0,
        ))

    npc_text_rows = []
    for row in raw_npc_text:
        text_id = int(row.get("ID") or 0)
        if not text_id:
            continue
        male, female, language, probability, emotes = _choose_npc_text(row)
        if not male and not female:
            continue
        npc_text_rows.append((
            text_id, male, female, language, probability,
            emotes[0][1], emotes[0][0],
            emotes[1][1], emotes[1][0],
            emotes[2][1], emotes[2][0],
        ))

    # The worldserver's schema lives in src/world/creature.py.  Drop the spell
    # tables first so a changed spell_tables.sql is applied to existing data
    # instead of silently keeping the old columns.
    import sqlite3

    pre = sqlite3.connect(db_path)
    try:
        for table in SPELL_TABLES:
            pre.execute(f"DROP TABLE IF EXISTS {table}")
        pre.commit()
    finally:
        pre.close()

    # Import lazily so this tool also works from a source checkout without
    # starting the server.
    from src.world.creature import init_creature_db

    init_creature_db(db_path)
    # ``item_templates`` lives in the character schema; loot rows and equipped
    # gear are useless without it, so make sure it exists before filling it.
    from src.world.character_db import init_character_db

    init_character_db(db_path)

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA synchronous=OFF")
        if clear:
            con.execute("DELETE FROM creature_spawns")
            con.execute("DELETE FROM creature_movement")
            con.execute("DELETE FROM creature_movement_template")
            con.execute("DELETE FROM gossip_options")
            con.execute("DELETE FROM gossip_menus")
            con.execute("DELETE FROM npc_text")
            con.execute("DELETE FROM creature_templates")

        template_sql = (
            f"INSERT OR REPLACE INTO creature_templates({', '.join(TEMPLATE_COLUMNS)}) "
            f"VALUES({', '.join('?' for _ in TEMPLATE_COLUMNS)})"
        )
        spawn_sql = (
            f"INSERT OR REPLACE INTO creature_spawns({', '.join(SPAWN_COLUMNS)}) "
            f"VALUES({', '.join('?' for _ in SPAWN_COLUMNS)})"
        )
        con.executemany(template_sql, template_rows)
        con.executemany(
            "INSERT OR REPLACE INTO gossip_menus(menu_id, text_id) VALUES(?,?)",
            menu_rows,
        )
        con.executemany(
            "INSERT OR REPLACE INTO gossip_options("
            "menu_id, option_index, option_type, icon, coded, box_money, text, "
            "box_text, action_menu_id, action_text_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
            option_rows,
        )
        con.executemany(
            "INSERT OR REPLACE INTO npc_text("
            "id, text, female_text, language, probability, emote1, emote_delay1, "
            "emote2, emote_delay2, emote3, emote_delay3) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            npc_text_rows,
        )
        con.executemany(spawn_sql, spawn_rows)
        con.executemany(
            "INSERT OR REPLACE INTO creature_movement("
            "id, point, position_x, position_y, position_z, orientation,"
            " wait_time, script_id, comment) VALUES(?,?,?,?,?,?,?,?,?)",
            movement_rows,
        )
        con.executemany(
            "INSERT OR REPLACE INTO creature_movement_template("
            "entry, path_id, point, position_x, position_y, position_z,"
            " orientation, wait_time, script_id, comment)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            movement_template_rows,
        )

        counts = {
            "spells": _insert_mapped(
                con, "spell_templates", SPELL_TEMPLATE_MAP, raw_spell_template,
                float_columns=SPELL_FLOAT_SQLITE_COLUMNS,
                text_columns=SPELL_TEXT_SQLITE_COLUMNS,
            ),
            "creature_template_spells": _insert_mapped(
                con, "creature_template_spells", CREATURE_TEMPLATE_SPELLS_MAP,
                raw_creature_template_spells,
            ),
            "creature_spell_list": _insert_mapped(
                con, "creature_spell_list", CREATURE_SPELL_LIST_MAP,
                raw_creature_spell_list, text_columns=frozenset(("comments",)),
            ),
            "creature_spell_list_entry": _insert_mapped(
                con, "creature_spell_list_entry",
                CREATURE_SPELL_LIST_ENTRY_MAP,
                raw_creature_spell_list_entry,
                text_columns=frozenset(("name",)),
            ),
            "creature_spell_targeting": _insert_mapped(
                con, "creature_spell_targeting",
                CREATURE_SPELL_TARGETING_MAP,
                raw_creature_spell_targeting,
                text_columns=frozenset(("comments",)),
            ),
            "creature_model_info": _insert_mapped(
                con, "creature_model_info", CREATURE_MODEL_INFO_MAP,
                raw_creature_model_info,
            ),
            "creature_template_classlevelstats": _insert_mapped(
                con, "creature_template_classlevelstats",
                CREATURE_CLASSLEVELSTATS_MAP,
                raw_creature_template_classlevelstats,
                float_columns=CREATURE_CLASSLEVELSTATS_FLOATS,
            ),
            "player_classlevelstats": _insert_mapped(
                con, "player_classlevelstats",
                PLAYER_CLASSLEVELSTATS_MAP,
                raw_player_classlevelstats,
            ),
            "player_levelstats": _insert_mapped(
                con, "player_levelstats",
                PLAYER_LEVELSTATS_MAP,
                raw_player_levelstats,
            ),
            "creature_loot_template": _insert_mapped(
                con, "creature_loot_template", CREATURE_LOOT_TEMPLATE_MAP,
                raw_creature_loot_template, text_columns=frozenset(("comments",)),
            ),
            "reference_loot_template": _insert_mapped(
                con, "reference_loot_template", REFERENCE_LOOT_TEMPLATE_MAP,
                raw_reference_loot_template, text_columns=frozenset(("comments",)),
            ),
            "creature_equip_template": _insert_mapped(
                con, "creature_equip_template", CREATURE_EQUIP_TEMPLATE_MAP,
                raw_creature_equip_template,
            ),
            "creature_immunities": _insert_mapped(
                con, "creature_immunities", CREATURE_IMMUNITIES_MAP,
                raw_creature_immunities,
            ),
            "creature_cooldowns": _insert_mapped(
                con, "creature_cooldowns", CREATURE_COOLDOWNS_MAP,
                raw_creature_cooldowns,
            ),
            "spell_cone": _insert_mapped(
                con, "spell_cone", SPELL_CONE_MAP, raw_spell_cone,
                float_columns=frozenset(("cone_degrees",)),
            ),
            "spell_target_position": _insert_mapped(
                con, "spell_target_position", SPELL_TARGET_POSITION_MAP,
                raw_spell_target_position,
            ),
            "spell_rune_cost": _insert_rune_costs(con, rune_cost_rows),
            "spell_duration": _insert_spell_durations(con, duration_rows),
            "playercreateinfo_spell": _insert_mapped(
                con, "playercreateinfo_spell", PLAYERCREATEINFO_SPELL_MAP,
                raw_playercreateinfo_spell,
                text_columns=frozenset(("note",)),
            ),
            "playercreateinfo_action": _insert_mapped(
                con, "playercreateinfo_action", PLAYERCREATEINFO_ACTION_MAP,
                raw_playercreateinfo_action,
            ),
            "item_templates": _insert_mapped(
                con, "item_templates", item_map, raw_item_template,
                float_columns=ITEM_TEMPLATE_FLOATS,
                text_columns=ITEM_TEMPLATE_TEXTS,
            ),
        }

        # CMaNGOS spell_template only ships SchoolMask, while the server stores
        # the single school index used by the client packets.  Derive it from
        # the lowest set bit of the mask (bit 0 = physical, 1 = holy, 2 = fire,
        # 3 = nature, 4 = frost, 5 = shadow, 6 = arcane).
        school_updates = [
            ((mask & -mask).bit_length() - 1, spell_id)
            for spell_id, mask in con.execute(
                "SELECT id, school_mask FROM spell_templates WHERE school_mask <> 0"
            )
        ]
        con.executemany(
            "UPDATE spell_templates SET school = ? WHERE id = ?", school_updates
        )

        # The import replaced the hand-written starter rows, and the dump has no
        # usable sound column, so restore the sheath/sound overrides the client
        # needs to attach an equipped weapon.
        from src.world.character_db import _seed_starter_items

        _seed_starter_items(con)

        con.commit()
        con.execute("PRAGMA optimize")
    finally:
        con.close()

    stats = {
        "templates": len(template_rows),
        "spawns": len(spawn_rows),
        "movement_rows": len(movement_rows),
        "movement_template_rows": len(movement_template_rows),
        "menus": len(menu_rows),
        "options": len(option_rows),
        "npc_text": len(npc_text_rows),
        "skipped_missing_template": skipped_missing,
        "skipped_phase": skipped_phase,
        "skipped_spawn_mask": skipped_mask,
        "spells": counts["spells"],
        "rune_costs": counts["spell_rune_cost"],
        "spell_durations": counts["spell_duration"],
        "starter_spells": counts["playercreateinfo_spell"],
        "starter_actions": counts["playercreateinfo_action"],
        "loot_rows": counts["creature_loot_template"],
        "reference_loot_rows": counts["reference_loot_template"],
        "items": counts["item_templates"],
    }
    print(
        f"imported {stats['templates']:,} templates, {stats['spawns']:,} spawns, "
        f"{stats['movement_rows']:,} waypoint rows, "
        f"{stats['movement_template_rows']:,} waypoint template rows, "
        f"{stats['menus']:,} gossip menus, {stats['options']:,} options, "
        f"{stats['npc_text']:,} NPC texts, {stats['spells']:,} spells, "
        f"{stats['rune_costs']:,} rune costs, "
        f"{stats['spell_durations']:,} spell durations, "
        f"{stats['starter_spells']:,} starter spells, "
        f"{stats['starter_actions']:,} starter actions, "
        f"{stats['loot_rows']:,} loot rows, {stats['items']:,} item templates "
        f"in {time.time() - started:.1f}s"
    )
    print(
        f"skipped {skipped_missing:,} spawns with a missing template, "
        f"{skipped_phase:,} non-default phase and {skipped_mask:,} non-normal "
        "spawn-mask rows"
    )
    return stats


def report_start_zones(db_path: str, radius: float = 150.0) -> None:
    """Print how many visible spawns each playable race has after the import."""
    import sqlite3
    if not _START_POSITIONS:
        return
    con = sqlite3.connect(db_path)
    try:
        for (race, char_class), (map_id, zone, x, y, z, _o) in sorted(_START_POSITIONS.items()):
            if char_class != 1 and (race, 1) in _START_POSITIONS:
                continue
            # Mirrors creature.get_creature_spawns_near(): bounding box first, then
            # the exact 3D distance, and never a tooling creature.
            count = 0
            for sx, sy, sz in con.execute(
                "SELECT s.x, s.y, s.z FROM creature_spawns s"
                " JOIN creature_templates t ON t.entry = s.entry"
                " WHERE s.map = ? AND t.visible <> 0"
                " AND s.x BETWEEN ? AND ? AND s.y BETWEEN ? AND ?",
                (map_id, x - radius, x + radius, y - radius, y + radius),
            ):
                if (sx - x) ** 2 + (sy - y) ** 2 + (sz - z) ** 2 <= radius * radius:
                    count += 1
            print(f"  race {race:2d} class {char_class:2d} map {map_id:3d} zone {zone:5d}: "
                  f"{count:4d} spawns within {radius:.0f}y")
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", required=True, help="CMaNGOS/TrinityCore world .sql dump")
    parser.add_argument("--db", help="target SQLite world database (default: config/config.yaml)")
    parser.add_argument("--rune-cost-dbc", help="path to the client's SpellRuneCost.dbc")
    parser.add_argument("--spell-duration-dbc",
                        help="path to the client's SpellDuration.dbc")
    parser.add_argument("--keep-existing", action="store_true",
                        help="do not clear the NPC tables before importing")
    args = parser.parse_args()

    db_path = args.db
    if not db_path:
        # Reuse the server configuration so the tool works from any directory.
        from src.common.config import Config
        config_path = os.path.join(root, "config", "config.yaml")
        if not os.path.exists(config_path):
            config_path = os.path.join(root, "config", "config.example.yaml")
        db_path = Config.load(config_path).database.world_path

    import_world(args.dump, db_path, clear=not args.keep_existing,
                 rune_cost_dbc=args.rune_cost_dbc,
                 spell_duration_dbc=args.spell_duration_dbc)
    print(f"\nvisible spawns at each starting position ({os.path.abspath(db_path)}):")
    report_start_zones(db_path)


if __name__ == "__main__":
    main()
