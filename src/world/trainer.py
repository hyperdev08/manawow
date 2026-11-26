"""NPC spell trainers: CMaNGOS trainer data and the 3.3.5a packets.

The class, profession, mount and pet trainers are driven by the CMaNGOS
``npc_trainer`` / ``npc_trainer_template`` tables.  ``creature_templates``
selects the trainer type and, for shared profession lists, the template id.

Wire layouts follow CMaNGOS ``WorldSession::SendTrainerList`` and
``HandleTrainerBuySpellOpcode`` (3.3.5a).  Trainer rows are 38 bytes each:
spell, state, cost, talent cost, primary-profession flag, required level,
required skill/value and up to three required abilities.
"""
from __future__ import annotations

import sqlite3
import struct
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from . import spells

# UNIT_NPC_FLAG_TRAINER / class / profession trainer flags.
NPC_FLAG_TRAINER = 0x10
NPC_FLAG_TRAINER_CLASS = 0x20
NPC_FLAG_TRAINER_PROFESSION = 0x40

# TrainerType in creature_template.
TRAINER_TYPE_CLASS = 0
TRAINER_TYPE_MOUNTS = 1
TRAINER_TYPE_TRADESKILLS = 2
TRAINER_TYPE_PETS = 3

# TrainerSpellState sent to the client.
TRAINER_SPELL_GREEN = 0
TRAINER_SPELL_RED = 1
TRAINER_SPELL_GRAY = 2
TRAINER_SPELL_GREEN_DISABLED = 10

# TrainerBuyFailed reasons (TrinityCore 3.3.5 NPCPackets.h).
TRAINER_FAIL_UNAVAILABLE = 0
TRAINER_FAIL_NOT_ENOUGH_MONEY = 1
TRAINER_FAIL_NOT_ENOUGH_SKILL = 2

# Gossip option type that opens the trainer list.
GOSSIP_OPTION_TRAINER = 5
# Gossip option type that starts the talent-reset confirmation.
GOSSIP_OPTION_UNLEARNTALENTS = 16
# Gossip option type that starts the pet talent-reset confirmation.
GOSSIP_OPTION_UNLEARNPETSKILLS = 17

TRAINER_INTERACT_DISTANCE = 15.0

# Spell effect ids used while resolving trainer rows.
SPELL_EFFECT_LEARN_SPELL = 36
SPELL_EFFECT_SKILL_STEP = 44
SPELL_EFFECT_PROFICIENCY = 60
SPELL_EFFECT_SKILL = 118

# Primary and secondary profession skill lines (SkillLine.dbc).
PRIMARY_PROFESSIONS = frozenset((
    164,   # Blacksmithing
    165,   # Leatherworking
    171,   # Alchemy
    182,   # Herbalism
    186,   # Mining
    197,   # Tailoring
    202,   # Engineering
    333,   # Enchanting
    393,   # Skinning
    755,   # Jewelcrafting
    773,   # Inscription
))
SECONDARY_PROFESSIONS = frozenset((
    129,   # First Aid
    185,   # Cooking
    356,   # Fishing
))
PROFESSION_SKILLS = PRIMARY_PROFESSIONS | SECONDARY_PROFESSIONS

# CMaNGOS SkillTiers equivalent: current cap for a profession rank step.
SKILL_MAX_BY_STEP = {
    1: 75,     # Apprentice
    2: 150,    # Journeyman
    3: 225,    # Expert
    4: 300,    # Artisan
    5: 375,    # Master
    6: 450,    # Grand Master
}

# Minimum character level for a profession rank
# (rank -> gather / craft / fishing), CMaNGOS GetProfessionSpellMinLevel.
_PROFESSION_MIN_LEVEL = {
    0: (0, 5, 5),
    1: (0, 5, 5),
    2: (0, 10, 10),
    3: (10, 20, 10),
    4: (25, 35, 10),
    5: (40, 50, 10),
    6: (55, 65, 10),
    7: (75, 75, 10),
}

_PROFESSION_RANKS = {
    "apprentice": 1,
    "journeyman": 2,
    "expert": 3,
    "artisan": 4,
    "master": 5,
    "grand master": 6,
}

@dataclass(frozen=True)
class TrainerSpell:
    spell: int
    cost: int
    req_skill: int
    req_skill_value: int
    req_level: int
    req_abilities: Tuple[int, int, int]
    learned: Tuple[int, ...]
    is_castable: bool
    primary_profession_first_rank: bool


@lru_cache(maxsize=4096)
def _rank_step(spell_id: int, rank: str) -> int:
    """Return the rank number encoded in a spell's Rank string."""
    text = (rank or "").strip().lower()
    if not text:
        return 0
    if text in _PROFESSION_RANKS:
        return _PROFESSION_RANKS[text]
    if text.startswith("rank "):
        try:
            return max(0, int(text[5:]))
        except ValueError:
            return 0
    return 0


def spell_rank_step(path: str, spell_id: int) -> int:
    template = spells.get_spell_template(path, int(spell_id))
    return _rank_step(int(spell_id), str(getattr(template, "rank", "") or ""))


def is_profession_spell(path: str, spell_id: int) -> bool:
    """CMaNGOS ``SpellMgr::IsProfessionSpell``.

    The profession skill effect lives in the second effect slot
    (``Effect[EFFECT_INDEX_1]`` == ``SPELL_EFFECT_SKILL``).
    """
    template = spells.get_spell_template(path, int(spell_id))
    if template is None or len(template.effects) < 2:
        return False
    effect = template.effects[1]
    return (int(effect.get("effect") or 0) == SPELL_EFFECT_SKILL
            and int(effect.get("misc_value") or 0) in PROFESSION_SKILLS)


def is_primary_profession_spell(path: str, spell_id: int) -> bool:
    template = spells.get_spell_template(path, int(spell_id))
    if template is None or len(template.effects) < 2:
        return False
    effect = template.effects[1]
    return (int(effect.get("effect") or 0) == SPELL_EFFECT_SKILL
            and int(effect.get("misc_value") or 0) in PRIMARY_PROFESSIONS)


def primary_profession_first_rank(path: str, spell_id: int) -> bool:
    return (is_primary_profession_spell(path, spell_id)
            and spell_rank_step(path, spell_id) == 1)


def profession_min_level(path: str, spell_id: int) -> int:
    """CMaNGOS ``SpellMgr::GetProfessionSpellMinLevel`` for one rank."""
    template = spells.get_spell_template(path, int(spell_id))
    if template is None or len(template.effects) < 2:
        return 1
    effect = template.effects[1]
    skill = int(effect.get("misc_value") or 0)
    step = spell_rank_step(path, spell_id)
    row = _PROFESSION_MIN_LEVEL.get(min(7, max(0, step)), (0, 5, 5))
    if skill == 356:                       # Fishing
        value = row[2]
    elif skill in (182, 186, 393):         # Herbalism, Mining, Skinning
        value = row[0]
    else:
        value = row[1]
    return max(1, int(value))


@lru_cache(maxsize=4096)
def _previous_rank(path: str, spell_id: int) -> int:
    """Return the previous rank in the same spell line (0 when rank 1)."""
    template = spells.get_spell_template(path, int(spell_id))
    if template is None:
        return 0
    name = str(getattr(template, "name", "") or "")
    rank = str(getattr(template, "rank", "") or "")
    step = _rank_step(int(spell_id), rank)
    if step <= 1 or not name:
        return 0
    previous_name = None
    for label, value in _PROFESSION_RANKS.items():
        if value == step - 1:
            previous_name = label
            break
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        if previous_name is not None:
            # Profession ranks use words rather than "Rank N".
            for candidate in {previous_name, previous_name.title()}:
                row = con.execute(
                    "SELECT id FROM spell_templates WHERE name = ?"
                    " AND lower(rank) = ? ORDER BY id LIMIT 1",
                    (name, candidate.lower()),
                ).fetchone()
                if row is not None:
                    return int(row[0])
        row = con.execute(
            "SELECT id FROM spell_templates WHERE name = ? AND rank = ?"
            " ORDER BY id LIMIT 1",
            (name, f"Rank {step - 1}"),
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        con.close()


def previous_rank(path: str, spell_id: int) -> int:
    return _previous_rank(path, int(spell_id))


def _row_spell(path: str, row: Mapping) -> Optional[TrainerSpell]:
    spell_id = int(row["spell"] or 0)
    if not spell_id:
        return None
    template = spells.get_spell_template(path, spell_id)
    if template is None:
        return None
    learned = [spell_id]
    for effect in template.effects:
        if int(effect.get("effect") or 0) == SPELL_EFFECT_LEARN_SPELL:
            trigger = int(effect.get("trigger_spell") or 0)
            if trigger:
                learned.append(trigger)
    return TrainerSpell(
        spell=spell_id,
        cost=max(0, int(row["spellcost"] or 0)),
        req_skill=max(0, int(row["reqskill"] or 0)),
        req_skill_value=max(0, int(row["reqskillvalue"] or 0)),
        req_level=max(0, int(row["reqlevel"] or 0)),
        req_abilities=(
            int(row["reqability1"] or 0),
            int(row["reqability2"] or 0),
            int(row["reqability3"] or 0),
        ),
        learned=tuple(learned),
        is_castable=len(learned) > 1,
        primary_profession_first_rank=any(
            primary_profession_first_rank(path, learned_spell)
            for learned_spell in learned),
    )


@lru_cache(maxsize=2048)
def _get_trainer_spells_cached(path: str, entry: int,
                               template_id: int) -> Tuple[TrainerSpell, ...]:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT spell, spellcost, reqskill, reqskillvalue, reqlevel,"
            " reqability1, reqability2, reqability3"
            " FROM npc_trainer WHERE entry = ? ORDER BY spell",
            (int(entry),),
        ).fetchall()
        if template_id:
            rows += con.execute(
                "SELECT spell, spellcost, reqskill, reqskillvalue, reqlevel,"
                " reqability1, reqability2, reqability3"
                " FROM npc_trainer_template WHERE entry = ? ORDER BY spell",
                (int(template_id),),
            ).fetchall()
    except sqlite3.OperationalError:
        return ()
    finally:
        con.close()

    seen = set()
    unique = []
    for row in rows:
        spell_id = int(row["spell"] or 0)
        if spell_id in seen:
            continue
        seen.add(spell_id)
        unique.append(row)
    spells.warm_spell_templates(path, seen)
    result = []
    for row in unique:
        entry_spell = _row_spell(path, row)
        if entry_spell is not None:
            result.append(entry_spell)
    result.sort(key=lambda item: item.spell)
    return tuple(result)


def get_trainer_spells(path: str, entry: int,
                       template_id: int = 0) -> List[TrainerSpell]:
    """Return the creature-specific plus shared trainer spell list.

    CMaNGOS loads ``npc_trainer`` and ``npc_trainer_template`` separately and
    ignores duplicate spells in the creature list when the shared template
    already provides them.  The result is sorted for a stable client order.
    """
    return list(_get_trainer_spells_cached(
        path, int(entry), int(template_id)))


def get_trainer_greeting(path: str, entry: int) -> str:
    """Return the trainer greeting, or CMaNGOS's generic fallback."""
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT text FROM trainer_greeting WHERE entry = ?", (int(entry),)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return str(row[0] or "") if row else "Hello! Ready for some training?"


def is_trainer_of(path: str, spawn: Mapping, char: Mapping,
                  known_spells: Iterable[int]) -> bool:
    """CMaNGOS ``Creature::IsTrainerOf`` for the four trainer types."""
    if not int(spawn["npc_flags"] or 0) & NPC_FLAG_TRAINER:
        return False
    trainer_type = int(spawn["trainer_type"] if "trainer_type" in spawn.keys()
                       else 0)
    if trainer_type == TRAINER_TYPE_CLASS:
        return (int(char.get("class", 0) or 0)
                == int(spawn["trainer_class"] or 0))
    if trainer_type == TRAINER_TYPE_PETS:
        return int(char.get("class", 0) or 0) == 3      # hunter
    if trainer_type == TRAINER_TYPE_MOUNTS:
        trainer_race = int(spawn["trainer_race"] or 0)
        return not trainer_race or int(char.get("race", 0) or 0) == trainer_race
    if trainer_type == TRAINER_TYPE_TRADESKILLS:
        required = int(spawn["trainer_spell"] or 0)
        return not required or required in known_spells
    return False


def can_reset_talents(path: str, spawn: Mapping, char: Mapping) -> bool:
    """CMaNGOS ``Creature::CanTrainAndResetTalentsOf``.

    Only a level-10+ character standing at their own class trainer may respec.
    """
    if not int(spawn["npc_flags"] or 0) & NPC_FLAG_TRAINER:
        return False
    if int(char.get("level", 1) or 1) < 10:
        return False
    trainer_type = int(spawn["trainer_type"] if "trainer_type" in spawn.keys()
                       else 0)
    return (trainer_type == TRAINER_TYPE_CLASS
            and int(char.get("class", 0) or 0)
            == int(spawn["trainer_class"] or 0))


def can_offer_trainer(path: str, spawn: Mapping, char: Mapping,
                      known_spells: Iterable[int]) -> bool:
    """Whether the gossip "Train me." option should be visible."""
    if not is_trainer_of(path, spawn, char, known_spells):
        return False
    return bool(get_trainer_spells(
        path, int(spawn["entry"]),
        int(spawn["trainer_template_id"] if "trainer_template_id"
            in spawn.keys() else 0)))


def _effective_req_level(path: str, trainer_spell: TrainerSpell) -> int:
    if trainer_spell.req_level:
        return trainer_spell.req_level
    primary = (trainer_spell.learned[1] if trainer_spell.is_castable
               else trainer_spell.spell)
    if is_profession_spell(path, primary):
        return profession_min_level(path, primary)
    template = spells.get_spell_template(path, primary)
    return max(1, int(getattr(template, "spell_level", 0) or 1))


def spell_state(path: str, trainer_spell: TrainerSpell, char: Mapping,
                known_spells: set[int], skills: Mapping) -> int:
    """CMaNGOS ``Player::GetTrainerSpellState`` (without GM overrides)."""
    known = known_spells
    if trainer_spell.spell in known:
        return TRAINER_SPELL_GRAY

    level = max(1, int(char.get("level", 1) or 1))
    req_level = _effective_req_level(path, trainer_spell)
    if level < req_level:
        return TRAINER_SPELL_RED

    if trainer_spell.is_castable and all(
            learned in known for learned in trainer_spell.learned
            if learned != trainer_spell.spell):
        return TRAINER_SPELL_GRAY

    for learned in trainer_spell.learned:
        previous = previous_rank(path, learned)
        if previous and previous not in known:
            return TRAINER_SPELL_RED

    if trainer_spell.req_skill:
        skill = skills.get(int(trainer_spell.req_skill)) or {}
        if int(skill.get("value", 0) or 0) < trainer_spell.req_skill_value:
            return TRAINER_SPELL_RED

    for ability in trainer_spell.req_abilities:
        if ability and ability not in known:
            return TRAINER_SPELL_RED

    # Primary profession limit is not enforced yet (no talent/profession slot
    # bookkeeping); every otherwise-valid first rank is trainable.
    return TRAINER_SPELL_GREEN


def _display_requirements(path: str,
                          trainer_spell: TrainerSpell) -> Tuple[int, int, int]:
    explicit = list(trainer_spell.req_abilities)
    if not explicit[0] and not trainer_spell.is_castable:
        previous = previous_rank(path, trainer_spell.spell)
        if previous:
            explicit[0] = previous
    return tuple(explicit)  # type: ignore[return-value]


def build_trainer_list(path: str, guid: int, spawn: Mapping,
                       char: Mapping, known_spells: Iterable[int],
                       skills: Mapping) -> Tuple[bytes, int]:
    """Return ``(SMSG_TRAINER_LIST payload, row count)``.

    An empty trainer list is not sent by CMaNGOS; the caller should skip the
    packet when the count is zero.
    """
    known = set(int(spell_id) for spell_id in known_spells)
    spells_for_sale = get_trainer_spells(
        path, int(spawn["entry"]),
        int(spawn["trainer_template_id"] if "trainer_template_id"
            in spawn.keys() else 0))
    if not spells_for_sale:
        return b"", 0

    rows = []
    for trainer_spell in spells_for_sale:
        state = spell_state(path, trainer_spell, char, known, skills)
        # Red rows stay in the list (the client greys/locks them); this DB has
        # no SkillLineAbility table for CMaNGOS's extra race/class filter.
        req_level = _effective_req_level(path, trainer_spell)
        req_abilities = _display_requirements(path, trainer_spell)
        rows.append((trainer_spell, state, req_level, req_abilities))

    trainer_type = 2 if any(
        is_profession_spell(path, learned)
        for trainer_spell in spells_for_sale
        for learned in trainer_spell.learned) else 0

    out = bytearray()
    out += struct.pack("<QII", int(guid), int(trainer_type), len(rows))
    for trainer_spell, state, req_level, req_abilities in rows:
        sent_state = (TRAINER_SPELL_GREEN
                      if state == TRAINER_SPELL_GREEN_DISABLED else state)
        # CMaNGOS writes the ``IsPrimaryProfessionFirstRank`` flag twice: the
        # client uses both fields to decide whether the Learn button is enabled
        # for an Apprentice/Journeyman profession rank.
        profession_first_rank = (
            1 if trainer_spell.primary_profession_first_rank else 0)
        out += struct.pack(
            "<IBIIIBIIIII",
            int(trainer_spell.spell),
            int(sent_state) & 0xFF,
            int(trainer_spell.cost) & 0xFFFFFFFF,
            profession_first_rank,
            profession_first_rank,
            max(0, min(255, int(req_level))) & 0xFF,
            int(trainer_spell.req_skill) & 0xFFFFFFFF,
            int(trainer_spell.req_skill_value) & 0xFFFFFFFF,
            int(req_abilities[0]) & 0xFFFFFFFF,
            int(req_abilities[1]) & 0xFFFFFFFF,
            int(req_abilities[2]) & 0xFFFFFFFF,
        )
    out += get_trainer_greeting(path, int(spawn["entry"])).encode(
        "utf-8", "replace") + b"\x00"
    return bytes(out), len(rows)


def parse_buy_spell(data: bytes) -> Optional[Tuple[int, int]]:
    """``CMSG_TRAINER_BUY_SPELL``: uint64 trainer guid, uint32 spell."""
    if len(data) < 12:
        return None
    guid, spell_id = struct.unpack_from("<QI", data, 0)
    return int(guid), int(spell_id)


def build_buy_succeeded(guid: int, spell_id: int) -> bytes:
    return struct.pack("<QI", int(guid), int(spell_id))


def build_buy_failed(guid: int, spell_id: int, reason: int) -> bytes:
    """``SMSG_TRAINER_BUY_FAILED``: guid, spell, uint32 reason."""
    return struct.pack("<QII", int(guid), int(spell_id), int(reason))


def build_learned_spell(spell_id: int) -> bytes:
    return struct.pack("<I", int(spell_id))


def build_superceded_spell(old_spell: int, new_spell: int) -> bytes:
    """``SMSG_SUPERCEDED_SPELL``: old rank replaced by the new rank."""
    return struct.pack("<II", int(old_spell), int(new_spell))


def build_removed_spell(spell_id: int) -> bytes:
    """``SMSG_REMOVED_SPELL``: uint32 spell id."""
    return struct.pack("<I", int(spell_id))


def build_play_spell_visual(guid: int, visual_id: int) -> bytes:
    return struct.pack("<QI", int(guid), int(visual_id))


def build_proficiency(item_class: int, subclass_mask: int) -> bytes:
    """``SMSG_SET_PROFICIENCY``."""
    return struct.pack("<BI", int(item_class) & 0xFF,
                       int(subclass_mask) & 0xFFFFFFFF)


def skill_step_from_template(template) -> Tuple[int, int]:
    """Return ``(skill_id, step)`` for the first SKILL_STEP effect, or ``(0, 0)``."""
    if template is None:
        return 0, 0
    for effect in template.effects:
        if int(effect.get("effect") or 0) == SPELL_EFFECT_SKILL_STEP:
            skill_id = int(effect.get("misc_value") or 0)
            if skill_id:
                # CMaNGOS Spell::EffectLearnSkill uses the calculated damage,
                # which is base points + 1 for these rank spells.
                step = max(0, int(effect.get("base_points") or 0) + 1)
                return skill_id, min(6, step)
    return 0, 0


def proficiency_from_template(template) -> Tuple[int, int]:
    """Return ``(item_class, subclass_mask)`` for a proficiency spell."""
    if template is None:
        return 0, 0
    if not any(int(effect.get("effect") or 0) == SPELL_EFFECT_PROFICIENCY
               for effect in template.effects):
        return 0, 0
    return (int(getattr(template, "equipped_item_class", 0) or 0),
            int(getattr(template, "equipped_item_subclass_mask", 0) or 0))


def learn_spells_for(trainer_spell: TrainerSpell) -> Tuple[int, ...]:
    """Spells that enter the spellbook when the trainer row is bought.

    CMaNGOS casts the trainer's wrapper spell for profession/riding ranks; its
    ``SPELL_EFFECT_LEARN_SPELL`` effects teach the real spells.  A normal row
    teaches the row's spell directly.
    """
    if trainer_spell.is_castable:
        return tuple(learned for learned in trainer_spell.learned
                     if learned != trainer_spell.spell)
    return (trainer_spell.spell,)


def clear_caches() -> None:
    """Drop cached trainer rows and rank chains (tests / data re-import)."""
    _previous_rank.cache_clear()
    _rank_step.cache_clear()
    _get_trainer_spells_cached.cache_clear()
