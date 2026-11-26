#!/usr/bin/env python3
"""Report spell effect and aura coverage for the imported 3.3.5a spell data.

This is the audit half of the full-parity spell program.  It does not cast
anything; it compares every imported ``spell_templates`` row against the effect
and aura families the worldserver currently implements, so unsupported spells
are visible instead of failing silently.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

from src.world import spells  # noqa: E402
from src.world.worldsocket.auras import SUPPORTED_AURA_TYPES  # noqa: E402


# Effects with an explicit handler in ``worldsocket/spells.py``.  Effects that
# are deliberately ignored (summon object, dummy/script effects) are not listed
# because their spells still do not produce the real client-visible behaviour.
SUPPORTED_EFFECTS = frozenset((
    spells.SPELL_EFFECT_INSTAKILL,
    spells.SPELL_EFFECT_SCHOOL_DAMAGE,
    spells.SPELL_EFFECT_APPLY_AURA,
    spells.SPELL_EFFECT_HEALTH_LEECH,
    spells.SPELL_EFFECT_HEAL,
    spells.SPELL_EFFECT_WEAPON_DAMAGE_NOSCHOOL,
    spells.SPELL_EFFECT_QUEST_COMPLETE,
    spells.SPELL_EFFECT_CREATE_ITEM,
    spells.SPELL_EFFECT_CREATE_ITEM_2,
    spells.SPELL_EFFECT_TRADE_SKILL,
    spells.SPELL_EFFECT_PERSISTENT_AREA_AURA,
    spells.SPELL_EFFECT_SUMMON,
    spells.SPELL_EFFECT_ENERGIZE,
    spells.SPELL_EFFECT_WEAPON_PERCENT_DAMAGE,
    spells.SPELL_EFFECT_TRIGGER_MISSILE,
    spells.SPELL_EFFECT_APPLY_AREA_AURA_PARTY,
    spells.SPELL_EFFECT_APPLY_AREA_AURA_PET,
    spells.SPELL_EFFECT_APPLY_AREA_AURA_FRIEND,
    spells.SPELL_EFFECT_APPLY_AREA_AURA_ENEMY,
    spells.SPELL_EFFECT_JUMP_DEST,
    spells.SPELL_EFFECT_SPAWN,
    spells.SPELL_EFFECT_TRANSMITTED_OBJECT,
    spells.SPELL_EFFECT_ACTIVATE_OBJECT,
    spells.SPELL_EFFECT_GAMEOBJECT_DAMAGE,
    spells.SPELL_EFFECT_POWER_DRAIN,
    spells.SPELL_EFFECT_LEAP,
    spells.SPELL_EFFECT_JUMP,
    spells.SPELL_EFFECT_REPUTATION,
    spells.SPELL_EFFECT_PULL_TOWARDS,
    spells.SPELL_EFFECT_ENERGIZE_PCT,
    spells.SPELL_EFFECT_POWER_BURN,
    spells.SPELL_EFFECT_CHARGE,
    spells.SPELL_EFFECT_INEBRIATE,
    spells.SPELL_EFFECT_DISPEL_MECHANIC,
    spells.SPELL_EFFECT_KILL_CREDIT,
    spells.SPELL_EFFECT_KILL_CREDIT_2,
    spells.SPELL_EFFECT_HEAL_PCT,
    spells.SPELL_EFFECT_OPEN_LOCK,
    spells.SPELL_EFFECT_LEARN_SPELL,
    spells.SPELL_EFFECT_SKILL,
    spells.SPELL_EFFECT_SKILL_STEP,
    spells.SPELL_EFFECT_DISPEL,
    spells.SPELL_EFFECT_ENCHANT_ITEM,
    spells.SPELL_EFFECT_ENCHANT_ITEM_TEMPORARY,
    spells.SPELL_EFFECT_SUMMON_PET,
    spells.SPELL_EFFECT_WEAPON_DAMAGE,
    spells.SPELL_EFFECT_PROFICIENCY,
    spells.SPELL_EFFECT_TELEPORT_UNITS,
    spells.SPELL_EFFECT_TELEPORT_UNITS_FACE_CASTER,
    spells.SPELL_EFFECT_TELEPORT_UNITS_SET_FACING,
    spells.SPELL_EFFECT_SEND_EVENT,
    spells.SPELL_EFFECT_THREAT,
    spells.SPELL_EFFECT_TRIGGER_SPELL,
    spells.SPELL_EFFECT_APPLY_AREA_AURA_RAID,
    spells.SPELL_EFFECT_SUMMON_OBJECT_WILD,
    spells.SPELL_EFFECT_SUMMON_OBJECT_SLOT1,
    spells.SPELL_EFFECT_SUMMON_OBJECT_SLOT2,
    spells.SPELL_EFFECT_SUMMON_OBJECT_SLOT3,
    spells.SPELL_EFFECT_SUMMON_OBJECT_SLOT4,
    spells.SPELL_EFFECT_HEAL_MAX_HEALTH,
    spells.SPELL_EFFECT_INTERRUPT_CAST,
    spells.SPELL_EFFECT_APPLY_GLYPH,
    spells.SPELL_EFFECT_FORCE_CAST,
    spells.SPELL_EFFECT_FORCE_CAST_WITH_VALUE,
    spells.SPELL_EFFECT_FORCE_CAST_2,
    spells.SPELL_EFFECT_ATTACK,
    spells.SPELL_EFFECT_ADD_COMBO_POINTS,
    spells.SPELL_EFFECT_KNOCK_BACK,
    spells.SPELL_EFFECT_RESURRECT,
    spells.SPELL_EFFECT_ATTACK_ME,
    spells.SPELL_EFFECT_NORMALIZED_WEAPON_DMG,
    spells.SPELL_EFFECT_MODIFY_THREAT_PERCENT,
    spells.SPELL_EFFECT_STEAL_BENEFICIAL,
    spells.SPELL_EFFECT_REDIRECT_THREAT,
    spells.SPELL_EFFECT_TRIGGER_SPELL_WITH_VALUE,
    spells.SPELL_EFFECT_APPLY_AREA_AURA_OWNER,
    spells.SPELL_EFFECT_TRIGGER_MISSILE_SPELL_WITH_VALUE,
    spells.SPELL_EFFECT_TRIGGER_SPELL_2,
))

EFFECT_NAMES = {
    1: "Instakill", 2: "School damage", 3: "Dummy/script", 5: "Teleport units",
    6: "Apply aura", 7: "Environmental damage", 8: "Power drain",
    9: "Health leech", 10: "Heal", 16: "Quest complete", 17: "Weapon damage",
    18: "Resurrect (old)", 19: "Extra attacks", 24: "Create item",
    27: "Persistent area aura", 28: "Summon", 29: "Leap", 30: "Energize",
    41: "Jump",
    31: "Weapon % damage", 32: "Trigger missile", 33: "Open lock",
    35: "Area aura party", 36: "Learn spell", 38: "Dispel",
    42: "Jump dest", 44: "Skill step", 46: "Spawn",
    50: "Transmitted object", 53: "Enchant item",
    54: "Temporary enchant", 56: "Summon pet",
    58: "Weapon damage (no school)", 60: "Proficiency",
    61: "Send event", 62: "Power burn", 63: "Threat", 64: "Trigger spell",
    65: "Area aura raid", 67: "Heal max health", 68: "Interrupt cast",
    74: "Apply glyph", 75: "Heal mechanical", 76: "Summon object",
    77: "Script effect", 78: "Attack", 80: "Add combo points",
    86: "Activate object", 87: "Gameobject damage", 90: "Kill credit",
    96: "Charge",
    98: "Knock back", 100: "Inebriate", 103: "Reputation",
    104: "Summon object slot 1",
    105: "Summon object slot 2", 106: "Summon object slot 3",
    107: "Summon object slot 4", 108: "Dispel mechanic",
    113: "Resurrect", 114: "Attack me", 119: "Area aura pet",
    121: "Normalized weapon damage", 124: "Pull towards",
    125: "Modify threat %", 126: "Steal beneficial",
    128: "Area aura friend",
    129: "Area aura enemy", 130: "Redirect threat", 134: "Kill credit 2",
    136: "Heal pct", 137: "Energize pct", 140: "Force cast",
    146: "Activate rune",
}


def _effect_name(effect: int) -> str:
    return EFFECT_NAMES.get(effect, "")


def audit(path: str) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, name, effect1, effect2, effect3,"
            " effect_item_type1, effect_item_type2, effect_item_type3,"
            " effect_apply_aura_name1, effect_apply_aura_name2,"
            " effect_apply_aura_name3"
            " FROM spell_templates").fetchall()
    finally:
        con.close()

    effects = collections.Counter()
    aura_types = collections.Counter()
    effect_examples: dict[int, tuple[int, str]] = {}
    aura_examples: dict[int, tuple[int, str]] = {}
    fully_supported = 0
    unsupported_spells = 0
    reasons = collections.Counter()

    for row in rows:
        spell_id = int(row["id"])
        name = str(row["name"] or "")
        spell_unsupported = False
        for index in (1, 2, 3):
            effect = int(row[f"effect{index}"] or 0)
            if not effect:
                continue
            effects[effect] += 1
            effect_examples.setdefault(effect, (spell_id, name))
            if effect not in SUPPORTED_EFFECTS:
                spell_unsupported = True
                reasons[f"effect {effect} {_effect_name(effect)}".strip()] += 1
                continue
            if effect in (spells.SPELL_EFFECT_CREATE_ITEM,
                          spells.SPELL_EFFECT_CREATE_ITEM_2):
                item_type = int(row[f"effect_item_type{index}"] or 0)
                if not item_type:
                    # Random-loot research spells need spell_loot_template.
                    spell_unsupported = True
                    reasons[f"effect {effect} without item"] += 1
                    continue
            if effect in (spells.SPELL_EFFECT_APPLY_AURA,
                          spells.SPELL_EFFECT_PERSISTENT_AREA_AURA):
                aura = int(row[f"effect_apply_aura_name{index}"] or 0)
                aura_types[aura] += 1
                aura_examples.setdefault(aura, (spell_id, name))
                if aura in SUPPORTED_AURA_TYPES:
                    continue
                # Drink spells carry the real regeneration value on a
                # PERIODIC_DUMMY effect and copy it into the MOD_REGEN /
                # MOD_POWER_REGEN aura; other periodic-dummy spells are
                # script markers and stay unsupported.
                if aura == spells.SPELL_AURA_PERIODIC_DUMMY and any(
                        int(row[f"effect_apply_aura_name{i}"] or 0) in (
                            spells.SPELL_AURA_MOD_REGEN,
                            spells.SPELL_AURA_MOD_POWER_REGEN)
                        for i in (1, 2, 3)):
                    continue
                spell_unsupported = True
                reasons[f"aura {aura}"] += 1
        if spell_unsupported:
            unsupported_spells += 1
        else:
            fully_supported += 1

    return {
        "database": path,
        "spells": len(rows),
        "fully_supported": fully_supported,
        "unsupported": unsupported_spells,
        "effects": dict(sorted(effects.items())),
        "effect_examples": effect_examples,
        "unsupported_effects": {
            effect: count for effect, count in effects.items()
            if effect not in SUPPORTED_EFFECTS
        },
        "auras": dict(sorted(aura_types.items())),
        "aura_examples": aura_examples,
        "unsupported_auras": {
            aura: count for aura, count in aura_types.items()
            if aura not in SUPPORTED_AURA_TYPES
        },
        "reasons": dict(reasons.most_common()),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", default=os.path.join(root, "data", "world.db"),
        help="world database containing spell_templates")
    parser.add_argument("--json", action="store_true",
                        help="print the complete report as JSON")
    parser.add_argument("--limit", type=int, default=20,
                        help="number of unsupported effects/auras to print")
    args = parser.parse_args(argv)

    report = audit(args.db)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    print(f"Spell database: {report['database']}")
    print(f"Imported spells: {report['spells']:,}")
    print(f"Fully supported: {report['fully_supported']:,}")
    print(f"Unsupported/partial: {report['unsupported']:,}")
    print()
    print("Most common unsupported effect families:")
    unsupported = sorted(report["unsupported_effects"].items(),
                         key=lambda item: item[1], reverse=True)
    for effect, count in unsupported[:max(1, args.limit)]:
        example_id, example_name = report["effect_examples"][effect]
        name = _effect_name(effect)
        print(f"  {effect:4d} {count:7d}  {name:<26} "
              f"e.g. {example_id} {example_name}")
    if len(unsupported) > args.limit:
        print(f"  ... {len(unsupported) - args.limit} more effect families")
    print()
    print("Most common unsupported aura families:")
    unsupported_auras = sorted(report["unsupported_auras"].items(),
                               key=lambda item: item[1], reverse=True)
    for aura, count in unsupported_auras[:max(1, args.limit)]:
        example_id, example_name = report["aura_examples"][aura]
        print(f"  {aura:4d} {count:7d}  e.g. {example_id} {example_name}")
    if len(unsupported_auras) > args.limit:
        print(f"  ... {len(unsupported_auras) - args.limit} more aura families")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
