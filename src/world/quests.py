"""Quest data, quest-log rules and quest packet builders (3.3.5a, 12340).

Everything here is socket-free so it can be unit tested on its own; the
packet handlers live in :mod:`src.world.worldsocket.quests`.

Static data comes from the CMaNGOS WoTLK dump (``quest_template``,
``creature_questrelation``, ``creature_involvedrelation`` and
``areatrigger_involvedrelation``) and is imported by
``tools/import_wotlk_db.py``.  Packet layouts follow TrinityCore 3.3.5
(``PlayerMenu::SendQuest*`` in ``GossipDef.cpp``) and the quest rules follow
CMaNGOS/TrinityCore ``Player::CanTakeQuest`` / ``GetQuestDialogStatus``.
"""
from __future__ import annotations

import math
import re
import sqlite3
import struct
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from src.world import update_fields as F

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_QUEST_LOG_SIZE = 25
QUEST_OBJECTIVES_COUNT = 4          # creature/GO kill objectives
QUEST_ITEM_OBJECTIVES_COUNT = 6
QUEST_SOURCE_ITEM_COUNT = 4
QUEST_REWARD_CHOICES_COUNT = 6
QUEST_REWARDS_COUNT = 4
QUEST_REPUTATIONS_COUNT = 5
QUEST_EMOTE_COUNT = 4

# ``PLAYER_QUEST_LOG_1_1`` = PLAYER_START + 0x000A; five fields per slot:
# quest id, state flags, two uint32 holding four uint16 kill counters, timer.
PLAYER_QUEST_LOG_1_1 = F.PLAYER_START + 0x000A
QUEST_FIELDS_PER_SLOT = 5
QUEST_ID_OFFSET = 0
QUEST_STATE_OFFSET = 1
QUEST_COUNTS_OFFSET = 2
QUEST_TIME_OFFSET = 4

# Quest-log state flags stored in PLAYER_QUEST_LOG_x_2.
QUEST_STATE_NONE = 0
QUEST_STATE_COMPLETE = 1
QUEST_STATE_FAIL = 2

# ``QuestStatus`` (character_quests.status).
QUEST_STATUS_NONE = 0
QUEST_STATUS_COMPLETE = 1
QUEST_STATUS_INCOMPLETE = 3
QUEST_STATUS_FAILED = 5

# ``QuestGiverStatus`` for 3.3.5a (the "!"/"?" over an NPC's head).
DIALOG_STATUS_NONE = 0
DIALOG_STATUS_UNAVAILABLE = 1          # grey "!": level too low
DIALOG_STATUS_LOW_LEVEL_AVAILABLE = 2  # trivial quest (hidden by default)
DIALOG_STATUS_LOW_LEVEL_REWARD_REP = 3
DIALOG_STATUS_LOW_LEVEL_AVAILABLE_REP = 4
DIALOG_STATUS_INCOMPLETE = 5           # grey "?"
DIALOG_STATUS_REWARD_REP = 6
DIALOG_STATUS_AVAILABLE_REP = 7
DIALOG_STATUS_AVAILABLE = 8            # yellow "!"
DIALOG_STATUS_REWARD2 = 9              # yellow "?" without minimap blip
DIALOG_STATUS_REWARD = 10              # yellow "?"

# Gossip/quest-list menu icons (TrinityCore ``PrepareQuestMenu``).
QUEST_ICON_AVAILABLE = 2
QUEST_ICON_ACTIVE = 4

# QuestFlags / SpecialFlags bits used here.
QUEST_FLAGS_HIDDEN_REWARDS = 0x00000200
QUEST_FLAGS_AUTO_COMPLETE = 0x00010000
# The CMaNGOS dump marks the 178 starting-zone quests with this flag, but no
# 3.3.5a quest carried it on retail (TrinityCore QuestDef.h; auto-accept
# arrived with Cataclysm).  The 3.3.5a client still honours it: its Accept
# button would only close the window and never send ACCEPT_QUEST.  It is
# therefore stripped from every packet (see ``client_quest_flags``).
QUEST_FLAGS_AUTO_ACCEPT = 0x00080000
QUEST_SPECIAL_FLAG_REPEATABLE = 0x01
QUEST_SPECIAL_FLAG_EXPLORATION_OR_EVENT = 0x02

# ``SMSG_QUESTGIVER_QUEST_INVALID`` reasons (``QuestFailedReason``).
INVALIDREASON_DONT_HAVE_REQ = 0
INVALIDREASON_QUEST_FAILED_LOW_LEVEL = 1
INVALIDREASON_QUEST_FAILED_WRONG_RACE = 6
INVALIDREASON_QUEST_ALREADY_DONE = 7
INVALIDREASON_QUEST_ALREADY_ON = 13
INVALIDREASON_QUEST_FAILED_MISSING_ITEMS = 21
INVALIDREASON_QUEST_FAILED_NOT_ENOUGH_MONEY = 23

# Level differences that hide/grey quests (TrinityCore config defaults).
QUEST_LOW_LEVEL_HIDE_DIFF = 4
QUEST_HIGH_LEVEL_HIDE_DIFF = 7

# NPC flag that marks a quest giver.
UNIT_NPC_FLAG_QUESTGIVER = 0x2

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# CMaNGOS ``quest_template`` columns we keep, in dump spelling.  The SQLite
# column name is the snake_case form (see :func:`column_name`).
TEXT_COLUMNS = (
    "Title", "Details", "Objectives", "OfferRewardText", "RequestItemsText",
    "EndText", "CompletedText",
    "ObjectiveText1", "ObjectiveText2", "ObjectiveText3", "ObjectiveText4",
)
FLOAT_COLUMNS = ("RewHonorMultiplier", "PointX", "PointY")


def _numbered(prefix: str, count: int) -> Tuple[str, ...]:
    return tuple(f"{prefix}{index}" for index in range(1, count + 1))


QUEST_DUMP_COLUMNS: Tuple[str, ...] = (
    "entry", "Method", "ZoneOrSort", "MinLevel", "MaxLevel", "QuestLevel",
    "Type", "RequiredClasses", "RequiredRaces",
    "RepObjectiveFaction", "RepObjectiveValue",
    "SuggestedPlayers", "LimitTime", "QuestFlags", "SpecialFlags",
    "CharTitleId", "PlayersSlain", "BonusTalents",
    "PrevQuestId", "NextQuestId", "ExclusiveGroup", "NextQuestInChain",
    "RewXPId", "SrcItemId", "SrcItemCount", "SrcSpell",
    *TEXT_COLUMNS,
    *_numbered("ReqItemId", QUEST_ITEM_OBJECTIVES_COUNT),
    *_numbered("ReqItemCount", QUEST_ITEM_OBJECTIVES_COUNT),
    *_numbered("ReqSourceId", QUEST_SOURCE_ITEM_COUNT),
    *_numbered("ReqSourceCount", QUEST_SOURCE_ITEM_COUNT),
    *_numbered("ReqCreatureOrGOId", QUEST_OBJECTIVES_COUNT),
    *_numbered("ReqCreatureOrGOCount", QUEST_OBJECTIVES_COUNT),
    *_numbered("RewChoiceItemId", QUEST_REWARD_CHOICES_COUNT),
    *_numbered("RewChoiceItemCount", QUEST_REWARD_CHOICES_COUNT),
    *_numbered("RewItemId", QUEST_REWARDS_COUNT),
    *_numbered("RewItemCount", QUEST_REWARDS_COUNT),
    *_numbered("RewRepFaction", QUEST_REPUTATIONS_COUNT),
    *_numbered("RewRepValueId", QUEST_REPUTATIONS_COUNT),
    *_numbered("RewRepValue", QUEST_REPUTATIONS_COUNT),
    "RewHonorAddition", "RewHonorMultiplier", "RewOrReqMoney",
    "RewMoneyMaxLevel", "RewSpell", "RewSpellCast",
    "PointMapId", "PointX", "PointY", "PointOpt",
    *_numbered("DetailsEmote", QUEST_EMOTE_COUNT),
    *_numbered("DetailsEmoteDelay", QUEST_EMOTE_COUNT),
    "IncompleteEmote", "CompleteEmote",
    *_numbered("OfferRewardEmote", QUEST_EMOTE_COUNT),
    *_numbered("OfferRewardEmoteDelay", QUEST_EMOTE_COUNT),
)


def column_name(dump_column: str) -> str:
    """``ReqCreatureOrGOId1`` -> ``req_creature_or_go_id1``."""
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", dump_column)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower()


def _column_declaration(dump_column: str) -> str:
    name = column_name(dump_column)
    if dump_column == "entry":
        return "entry INTEGER PRIMARY KEY"
    if dump_column in TEXT_COLUMNS:
        return f"{name} TEXT NOT NULL DEFAULT ''"
    if dump_column in FLOAT_COLUMNS:
        return f"{name} REAL NOT NULL DEFAULT 0.0"
    return f"{name} INTEGER NOT NULL DEFAULT 0"


QUEST_COLUMNS: Tuple[str, ...] = tuple(column_name(c) for c in QUEST_DUMP_COLUMNS)

CREATE_QUEST_SQL = (
    "CREATE TABLE IF NOT EXISTS quest_templates(\n  "
    + ",\n  ".join(_column_declaration(c) for c in QUEST_DUMP_COLUMNS)
    + "\n);\n"
    "CREATE INDEX IF NOT EXISTS idx_quest_next ON quest_templates(next_quest_id);\n"
    "CREATE INDEX IF NOT EXISTS idx_quest_group ON quest_templates(exclusive_group);\n"
    """
-- creature_questrelation: creature entry -> quest it starts.
CREATE TABLE IF NOT EXISTS creature_quest_starters(
  entry INTEGER NOT NULL,
  quest INTEGER NOT NULL,
  PRIMARY KEY(entry, quest)
);
-- creature_involvedrelation: creature entry -> quest it ends.
CREATE TABLE IF NOT EXISTS creature_quest_enders(
  entry INTEGER NOT NULL,
  quest INTEGER NOT NULL,
  PRIMARY KEY(entry, quest)
);
CREATE INDEX IF NOT EXISTS idx_quest_enders_quest ON creature_quest_enders(quest);
-- gameobject_questrelation / gameobject_involvedrelation: wanted posters,
-- quest books and other QUESTGIVER objects.
CREATE TABLE IF NOT EXISTS gameobject_quest_starters(
  entry INTEGER NOT NULL,
  quest INTEGER NOT NULL,
  PRIMARY KEY(entry, quest)
);
CREATE TABLE IF NOT EXISTS gameobject_quest_enders(
  entry INTEGER NOT NULL,
  quest INTEGER NOT NULL,
  PRIMARY KEY(entry, quest)
);
-- areatrigger_involvedrelation: AreaTrigger.dbc id -> exploration quest.
CREATE TABLE IF NOT EXISTS areatrigger_quests(
  trigger_id INTEGER PRIMARY KEY,
  quest INTEGER NOT NULL
);

-- Active quest log, one row per quest (max 25 per character).
CREATE TABLE IF NOT EXISTS character_quests(
  character_guid INTEGER NOT NULL,
  quest_id INTEGER NOT NULL,
  slot INTEGER NOT NULL,
  status INTEGER NOT NULL DEFAULT 3,
  explored INTEGER NOT NULL DEFAULT 0,
  count1 INTEGER NOT NULL DEFAULT 0,
  count2 INTEGER NOT NULL DEFAULT 0,
  count3 INTEGER NOT NULL DEFAULT 0,
  count4 INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(character_guid, quest_id)
);
-- Quests the character has turned in.
CREATE TABLE IF NOT EXISTS character_quests_rewarded(
  character_guid INTEGER NOT NULL,
  quest_id INTEGER NOT NULL,
  PRIMARY KEY(character_guid, quest_id)
);
"""
)


def init_quest_db(path: str) -> None:
    """Create/upgrade the quest tables (idempotent)."""
    con = sqlite3.connect(path)
    try:
        con.executescript(CREATE_QUEST_SQL)
        existing = {row[1] for row in con.execute("PRAGMA table_info(quest_templates)")}
        for dump_column in QUEST_DUMP_COLUMNS:
            if column_name(dump_column) not in existing:
                con.execute(
                    "ALTER TABLE quest_templates ADD COLUMN "
                    + _column_declaration(dump_column)
                )
        con.commit()
    finally:
        con.close()
    clear_cache()


# ---------------------------------------------------------------------------
# Static data access
# ---------------------------------------------------------------------------

_TEMPLATE_CACHE: Dict[Tuple[str, int], Optional[dict]] = {}
_RELATION_CACHE: Dict[Tuple[str, str, int], Tuple[int, ...]] = {}


def clear_cache() -> None:
    _TEMPLATE_CACHE.clear()
    _RELATION_CACHE.clear()


def get_quest_template(path: str, quest_id: int) -> Optional[dict]:
    key = (path, int(quest_id))
    if key not in _TEMPLATE_CACHE:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT * FROM quest_templates WHERE entry = ?", (int(quest_id),)
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            con.close()
        _TEMPLATE_CACHE[key] = dict(row) if row is not None else None
    return _TEMPLATE_CACHE[key]


# Who can give/take quests: creatures, game objects and quest-starting items
# (``item_template.startquest``; items never end quests).
GIVER_CREATURE = "creature"
GIVER_GAMEOBJECT = "gameobject"
GIVER_ITEM = "item"
_STARTER_SQL = {
    GIVER_CREATURE: "SELECT quest FROM creature_quest_starters WHERE entry = ? ORDER BY quest",
    GIVER_GAMEOBJECT: "SELECT quest FROM gameobject_quest_starters WHERE entry = ? ORDER BY quest",
    GIVER_ITEM: "SELECT start_quest FROM item_templates WHERE entry = ? AND start_quest <> 0",
}
_ENDER_SQL = {
    GIVER_CREATURE: "SELECT quest FROM creature_quest_enders WHERE entry = ? ORDER BY quest",
    GIVER_GAMEOBJECT: "SELECT quest FROM gameobject_quest_enders WHERE entry = ? ORDER BY quest",
}


def _relation(path: str, sql: str, entry: int) -> Tuple[int, ...]:
    key = (path, sql, int(entry))
    if key not in _RELATION_CACHE:
        con = sqlite3.connect(path)
        try:
            rows = con.execute(sql, (int(entry),)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        finally:
            con.close()
        _RELATION_CACHE[key] = tuple(int(r[0]) for r in rows)
    return _RELATION_CACHE[key]


def quests_started_by(path: str, entry: int, kind: str = GIVER_CREATURE) -> Tuple[int, ...]:
    return _relation(path, _STARTER_SQL[kind], entry)


def quests_ended_by(path: str, entry: int, kind: str = GIVER_CREATURE) -> Tuple[int, ...]:
    sql = _ENDER_SQL.get(kind)
    return _relation(path, sql, entry) if sql else ()


def is_quest_giver(path: str, entry: int, kind: str = GIVER_CREATURE) -> bool:
    return bool(quests_started_by(path, entry, kind) or quests_ended_by(path, entry, kind))


def prerequisite_quests(path: str, quest: Mapping) -> List[int]:
    """``Quest::prevQuests``: PrevQuestId plus every quest naming us as next.

    A positive id must have been rewarded; a negative id must be active.
    """
    prereqs: List[int] = []
    prev = int(quest.get("prev_quest_id") or 0)
    if prev:
        prereqs.append(prev)
    con = sqlite3.connect(path)
    try:
        for (entry,) in con.execute(
            "SELECT entry FROM quest_templates WHERE next_quest_id = ?",
            (int(quest["entry"]),),
        ):
            prereqs.append(int(entry))
        for (entry,) in con.execute(
            "SELECT entry FROM quest_templates WHERE next_quest_id = ?",
            (-int(quest["entry"]),),
        ):
            prereqs.append(-int(entry))
    finally:
        con.close()
    return prereqs


def exclusive_group_members(path: str, group: int) -> List[int]:
    if not group:
        return []
    con = sqlite3.connect(path)
    try:
        return [int(r[0]) for r in con.execute(
            "SELECT entry FROM quest_templates WHERE exclusive_group = ?",
            (int(group),),
        )]
    finally:
        con.close()


def quest_for_areatrigger(path: str, trigger_id: int) -> int:
    con = sqlite3.connect(path)
    try:
        row = con.execute(
            "SELECT quest FROM areatrigger_quests WHERE trigger_id = ?",
            (int(trigger_id),),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    return int(row[0]) if row else 0


def item_display_ids(path: str, entries: Iterable[int]) -> Dict[int, int]:
    entries = [int(e) for e in entries if int(e)]
    if not entries:
        return {}
    con = sqlite3.connect(path)
    try:
        marks = ",".join("?" * len(entries))
        return {int(e): int(d or 0) for e, d in con.execute(
            f"SELECT entry, display_id FROM item_templates WHERE entry IN ({marks})",
            entries,
        )}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Character quest log persistence
# ---------------------------------------------------------------------------

def load_quest_log(path: str, character_guid: int) -> Dict[int, dict]:
    """Return ``{quest_id: {slot, status, explored, counts[4]}}``."""
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT quest_id, slot, status, explored, count1, count2, count3, "
            "count4 FROM character_quests WHERE character_guid = ?",
            (int(character_guid),),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return {
        int(q): {
            "slot": int(slot), "status": int(status), "explored": int(explored),
            "counts": [int(c1), int(c2), int(c3), int(c4)],
        }
        for q, slot, status, explored, c1, c2, c3, c4 in rows
    }


def load_rewarded_quests(path: str, character_guid: int) -> Set[int]:
    con = sqlite3.connect(path)
    try:
        return {int(r[0]) for r in con.execute(
            "SELECT quest_id FROM character_quests_rewarded WHERE character_guid = ?",
            (int(character_guid),),
        )}
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()


def save_quest(path: str, character_guid: int, quest_id: int, entry: Mapping) -> None:
    counts = list(entry["counts"]) + [0] * 4
    con = sqlite3.connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO character_quests(character_guid, quest_id, "
            "slot, status, explored, count1, count2, count3, count4) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (int(character_guid), int(quest_id), int(entry["slot"]),
             int(entry["status"]), int(entry["explored"]), *counts[:4]),
        )
        con.commit()
    finally:
        con.close()


def delete_quest(path: str, character_guid: int, quest_id: int) -> None:
    con = sqlite3.connect(path)
    try:
        con.execute(
            "DELETE FROM character_quests WHERE character_guid = ? AND quest_id = ?",
            (int(character_guid), int(quest_id)),
        )
        con.commit()
    finally:
        con.close()


def reward_quest(path: str, character_guid: int, quest_id: int) -> None:
    """Move a quest from the log to the rewarded set in one transaction."""
    con = sqlite3.connect(path)
    try:
        con.execute(
            "DELETE FROM character_quests WHERE character_guid = ? AND quest_id = ?",
            (int(character_guid), int(quest_id)),
        )
        con.execute(
            "INSERT OR IGNORE INTO character_quests_rewarded(character_guid, quest_id)"
            " VALUES(?, ?)",
            (int(character_guid), int(quest_id)),
        )
        con.commit()
    finally:
        con.close()


def delete_character_quests(con: sqlite3.Connection, character_guid: int) -> None:
    """Drop all quest rows of a deleted character (caller commits)."""
    for table in ("character_quests", "character_quests_rewarded"):
        try:
            con.execute(f"DELETE FROM {table} WHERE character_guid = ?",
                        (int(character_guid),))
        except sqlite3.OperationalError:
            pass


# ---------------------------------------------------------------------------
# Quest rules (pure)
# ---------------------------------------------------------------------------

def _q(quest: Mapping, name: str) -> int:
    value = quest.get(name)
    return int(value) if value is not None else 0


def quest_level(quest: Mapping, player_level: int) -> int:
    """``QuestLevel`` of -1 means "the player's level"."""
    level = _q(quest, "quest_level")
    return int(player_level) if level <= 0 else level


def kill_objectives(quest: Mapping) -> List[Tuple[int, int, int]]:
    """``[(index, creature_or_go_id, required)]`` for each kill objective.

    Negative ids are game objects; they are still listed so the counters line
    up, but only positive ids are credited by creature kills.
    """
    out = []
    for index in range(QUEST_OBJECTIVES_COUNT):
        target = _q(quest, f"req_creature_or_go_id{index + 1}")
        required = _q(quest, f"req_creature_or_go_count{index + 1}")
        if target and required > 0:
            out.append((index, target, required))
    return out


def object_objectives(quest: Mapping) -> List[Tuple[int, int, int]]:
    """``[(index, gameobject_entry, required)]``: "use the object" objectives.

    They are stored as negative ``ReqCreatureOrGOId`` values.
    """
    return [(index, -target, required) for index, target, required in kill_objectives(quest)
            if target < 0]


def item_objectives(quest: Mapping) -> List[Tuple[int, int]]:
    out = []
    for index in range(1, QUEST_ITEM_OBJECTIVES_COUNT + 1):
        item = _q(quest, f"req_item_id{index}")
        count = _q(quest, f"req_item_count{index}")
        if item and count > 0:
            out.append((item, count))
    return out


def reward_choices(quest: Mapping) -> List[Tuple[int, int]]:
    out = []
    for index in range(1, QUEST_REWARD_CHOICES_COUNT + 1):
        item = _q(quest, f"rew_choice_item_id{index}")
        if item:
            out.append((item, max(1, _q(quest, f"rew_choice_item_count{index}"))))
    return out


def reward_items(quest: Mapping) -> List[Tuple[int, int]]:
    out = []
    for index in range(1, QUEST_REWARDS_COUNT + 1):
        item = _q(quest, f"rew_item_id{index}")
        if item:
            out.append((item, max(1, _q(quest, f"rew_item_count{index}"))))
    return out


def needs_exploration(quest: Mapping) -> bool:
    return bool(_q(quest, "special_flags") & QUEST_SPECIAL_FLAG_EXPLORATION_OR_EVENT)


def is_repeatable(quest: Mapping) -> bool:
    return bool(_q(quest, "special_flags") & QUEST_SPECIAL_FLAG_REPEATABLE)


def client_quest_flags(quest: Mapping) -> int:
    """``QuestFlags`` as sent to the client: WotLK quests are never auto-accept."""
    return _q(quest, "quest_flags") & ~QUEST_FLAGS_AUTO_ACCEPT & 0xFFFFFFFF


def is_auto_complete(quest: Mapping) -> bool:
    # ``Quest::IsAutoComplete``: Method 0 quests finish without objectives.
    return _q(quest, "method") == 0


def has_hidden_rewards(quest: Mapping) -> bool:
    return bool(_q(quest, "quest_flags") & QUEST_FLAGS_HIDDEN_REWARDS)


def required_money(quest: Mapping) -> int:
    money = _q(quest, "rew_or_req_money")
    return -money if money < 0 else 0


def reward_money(quest: Mapping) -> int:
    return max(0, _q(quest, "rew_or_req_money"))


def quest_xp(quest: Mapping, player_level: int) -> int:
    """Quest XP for ``player_level``.

    The exact value lives in the client's QuestXP.dbc (indexed by RewXPId),
    which is not part of the SQL dump.  CMaNGOS derives the same number from
    ``RewMoneyMaxLevel`` (the money paid instead of XP at the level cap), and
    reduces it for quests far below the player.
    """
    money = _q(quest, "rew_money_max_level")
    if money <= 0:
        return 0
    player_level = int(player_level)
    q_level = quest_level(quest, player_level)
    if q_level >= 65:
        full = money / 6.0
    elif q_level == 64:
        full = money / 4.8
    elif q_level == 63:
        full = money / 3.6
    elif q_level == 62:
        full = money / 2.4
    elif q_level == 61:
        full = money / 1.2
    else:
        full = money / 0.6
    diff = player_level - q_level
    if diff <= 5:
        factor = 1.0
    elif diff == 6:
        factor = 0.8
    elif diff == 7:
        factor = 0.6
    elif diff == 8:
        factor = 0.4
    elif diff == 9:
        factor = 0.2
    else:
        factor = 0.1
    return int(math.ceil(full * factor - 1e-9))


def satisfies_race_class(quest: Mapping, race: int, char_class: int) -> bool:
    races = _q(quest, "required_races")
    if races and not races & (1 << (int(race) - 1)):
        return False
    classes = _q(quest, "required_classes")
    if classes and not classes & (1 << (int(char_class) - 1)):
        return False
    return True


def satisfies_level(quest: Mapping, player_level: int) -> bool:
    if int(player_level) < _q(quest, "min_level"):
        return False
    max_level = _q(quest, "max_level")
    return not max_level or int(player_level) <= max_level


def can_see_quest(quest: Mapping, player_level: int) -> bool:
    """High-level quests show a grey "!" a few levels before they open."""
    return int(player_level) + QUEST_HIGH_LEVEL_HIDE_DIFF >= _q(quest, "min_level")


class QuestState:
    """What the rules need to know about one player."""

    def __init__(self, race: int, char_class: int, level: int,
                 log: Optional[Dict[int, dict]] = None,
                 rewarded: Optional[Set[int]] = None):
        self.race = int(race)
        self.char_class = int(char_class)
        self.level = int(level)
        self.log = log if log is not None else {}
        self.rewarded = rewarded if rewarded is not None else set()

    def status(self, quest_id: int) -> int:
        entry = self.log.get(int(quest_id))
        return int(entry["status"]) if entry else QUEST_STATUS_NONE


def can_take_quest(path: str, quest: Mapping, state: QuestState,
                   check_level: bool = True) -> bool:
    """``Player::CanTakeQuest`` without skills, reputation and conditions."""
    quest_id = int(quest["entry"])
    if quest_id in state.log:
        return False
    if quest_id in state.rewarded and not is_repeatable(quest):
        return False
    if not satisfies_race_class(quest, state.race, state.char_class):
        return False
    if check_level and not satisfies_level(quest, state.level):
        return False
    prereqs = prerequisite_quests(path, quest)
    if prereqs:
        ok = False
        for prev in prereqs:
            if prev > 0 and prev in state.rewarded:
                ok = True
            elif prev < 0 and -prev in state.log:
                ok = True
        if not ok:
            return False
    group = _q(quest, "exclusive_group")
    if group > 0:
        for other in exclusive_group_members(path, group):
            if other != quest_id and (other in state.rewarded or other in state.log):
                return False
    return True


def objectives_complete(quest: Mapping, entry: Mapping,
                        item_counts: Mapping[int, int]) -> bool:
    """``Player::CanCompleteQuest`` for kill, item and exploration objectives."""
    counts = entry.get("counts") or [0, 0, 0, 0]
    for index, _target, required in kill_objectives(quest):
        if int(counts[index]) < required:
            return False
    for item, required in item_objectives(quest):
        if int(item_counts.get(item, 0)) < required:
            return False
    if needs_exploration(quest) and not int(entry.get("explored") or 0):
        return False
    return True


def giver_status(path: str, creature_entry: int, state: QuestState,
                 kind: str = GIVER_CREATURE) -> int:
    """``Player::GetQuestDialogStatus`` for one creature or game object."""
    result = DIALOG_STATUS_NONE
    for quest_id in quests_ended_by(path, creature_entry, kind):
        quest = get_quest_template(path, quest_id)
        if quest is None:
            continue
        status = state.status(quest_id)
        if status == QUEST_STATUS_COMPLETE:
            result = max(result, DIALOG_STATUS_REWARD)
        elif status == QUEST_STATUS_INCOMPLETE:
            result = max(result, DIALOG_STATUS_INCOMPLETE)
    for quest_id in quests_started_by(path, creature_entry, kind):
        quest = get_quest_template(path, quest_id)
        if quest is None or state.status(quest_id) != QUEST_STATUS_NONE:
            continue
        if not can_take_quest(path, quest, state, check_level=False):
            continue
        if satisfies_level(quest, state.level):
            if state.level <= quest_level(quest, state.level) + QUEST_LOW_LEVEL_HIDE_DIFF:
                result = max(result, DIALOG_STATUS_AVAILABLE)
            else:
                result = max(result, DIALOG_STATUS_LOW_LEVEL_AVAILABLE)
        elif can_see_quest(quest, state.level):
            result = max(result, DIALOG_STATUS_UNAVAILABLE)
    return result


def quest_menu(path: str, creature_entry: int, state: QuestState,
               kind: str = GIVER_CREATURE) -> List[Tuple[int, int]]:
    """``PlayerMenu::PrepareQuestMenu``: ``[(quest_id, icon)]``."""
    menu: List[Tuple[int, int]] = []
    seen: Set[int] = set()
    for quest_id in quests_ended_by(path, creature_entry, kind):
        if state.status(quest_id) in (QUEST_STATUS_COMPLETE, QUEST_STATUS_INCOMPLETE):
            if get_quest_template(path, quest_id) is not None:
                menu.append((quest_id, QUEST_ICON_ACTIVE))
                seen.add(quest_id)
    for quest_id in quests_started_by(path, creature_entry, kind):
        if quest_id in seen:
            continue
        quest = get_quest_template(path, quest_id)
        if quest is None or not can_take_quest(path, quest, state):
            continue
        menu.append((quest_id, QUEST_ICON_AVAILABLE))
    return menu


def free_log_slot(log: Mapping[int, Mapping]) -> int:
    used = {int(e["slot"]) for e in log.values()}
    for slot in range(MAX_QUEST_LOG_SIZE):
        if slot not in used:
            return slot
    return -1


def new_log_entry(slot: int) -> dict:
    return {"slot": int(slot), "status": QUEST_STATUS_INCOMPLETE,
            "explored": 0, "counts": [0, 0, 0, 0]}


# ---------------------------------------------------------------------------
# Update fields
# ---------------------------------------------------------------------------

def slot_fields(slot: int, quest_id: int, entry: Optional[Mapping]) -> Dict[int, int]:
    """The five ``PLAYER_QUEST_LOG_<slot>_*`` values for one slot."""
    base = PLAYER_QUEST_LOG_1_1 + int(slot) * QUEST_FIELDS_PER_SLOT
    if entry is None or not quest_id:
        return {base + offset: 0 for offset in range(QUEST_FIELDS_PER_SLOT)}
    counts = [min(0xFFFF, max(0, int(c))) for c in (list(entry["counts"]) + [0] * 4)[:4]]
    state = QUEST_STATE_NONE
    if int(entry["status"]) == QUEST_STATUS_COMPLETE:
        state = QUEST_STATE_COMPLETE
    elif int(entry["status"]) == QUEST_STATUS_FAILED:
        state = QUEST_STATE_FAIL
    return {
        base + QUEST_ID_OFFSET: int(quest_id),
        base + QUEST_STATE_OFFSET: state,
        base + QUEST_COUNTS_OFFSET: counts[0] | (counts[1] << 16),
        base + QUEST_COUNTS_OFFSET + 1: counts[2] | (counts[3] << 16),
        base + QUEST_TIME_OFFSET: 0,
    }


def quest_log_fields(log: Mapping[int, Mapping]) -> Dict[int, int]:
    fields: Dict[int, int] = {}
    for quest_id, entry in log.items():
        fields.update(slot_fields(int(entry["slot"]), int(quest_id), entry))
    return fields


# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------

def _cstring(value) -> bytes:
    return (str(value or "")).encode("utf-8") + b"\x00"


def build_questgiver_status(guid: int, status: int) -> bytes:
    return struct.pack("<QB", int(guid) & 0xFFFFFFFFFFFFFFFF, int(status) & 0xFF)


def build_questgiver_status_multiple(statuses: Sequence[Tuple[int, int]]) -> bytes:
    out = bytearray(struct.pack("<I", len(statuses)))
    for guid, status in statuses:
        out += build_questgiver_status(guid, status)
    return bytes(out)


def _quest_list_entry(quest: Mapping, icon: int, player_level: int) -> bytes:
    return (
        struct.pack("<IIiIB", int(quest["entry"]), int(icon),
                    quest_level(quest, player_level),
                    client_quest_flags(quest), 0)
        + _cstring(quest.get("title"))
    )


def gossip_quest_items(path: str, menu: Sequence[Tuple[int, int]],
                       player_level: int) -> List[dict]:
    """Quest rows in the shape ``login_packets.build_gossip_message`` reads."""
    out = []
    for quest_id, icon in menu:
        quest = get_quest_template(path, quest_id)
        if quest is None:
            continue
        out.append({
            "quest_id": int(quest_id), "icon": int(icon),
            "level": quest_level(quest, player_level),
            "flags": client_quest_flags(quest), "repeatable": 0,
            "title": quest.get("title") or "",
        })
    return out


def build_quest_list(guid: int, greeting: str, quests: Sequence[Tuple[Mapping, int]],
                     player_level: int, emote: int = 0, emote_delay: int = 0) -> bytes:
    """``SMSG_QUESTGIVER_QUEST_LIST``."""
    out = bytearray(struct.pack("<Q", int(guid) & 0xFFFFFFFFFFFFFFFF))
    out += _cstring(greeting)
    out += struct.pack("<IIB", int(emote_delay), int(emote), len(quests) & 0xFF)
    for quest, icon in quests:
        out += _quest_list_entry(quest, icon, player_level)
    return bytes(out)


def _reward_item_block(items: Sequence[Tuple[int, int]], displays: Mapping[int, int]) -> bytes:
    out = bytearray(struct.pack("<I", len(items)))
    for item, count in items:
        out += struct.pack("<III", int(item), int(count), int(displays.get(item, 0)))
    return bytes(out)


def _reputation_block(quest: Mapping) -> bytes:
    out = bytearray()
    for index in range(1, QUEST_REPUTATIONS_COUNT + 1):
        out += struct.pack("<I", _q(quest, f"rew_rep_faction{index}") & 0xFFFFFFFF)
    for index in range(1, QUEST_REPUTATIONS_COUNT + 1):
        out += struct.pack("<i", _q(quest, f"rew_rep_value_id{index}"))
    for index in range(1, QUEST_REPUTATIONS_COUNT + 1):
        # RewardFactionValueIdOverride; CMaNGOS stores the flat value here.
        out += struct.pack("<i", _q(quest, f"rew_rep_value{index}"))
    return bytes(out)


def build_quest_details(guid: int, quest: Mapping, player_level: int,
                        displays: Mapping[int, int], activate_accept: bool = True) -> bytes:
    """``SMSG_QUESTGIVER_QUEST_DETAILS`` (TrinityCore 3.3.5 layout)."""
    out = bytearray(struct.pack("<QQ", int(guid) & 0xFFFFFFFFFFFFFFFF, 0))
    out += struct.pack("<I", int(quest["entry"]))
    out += _cstring(quest.get("title"))
    out += _cstring(quest.get("details"))
    out += _cstring(quest.get("objectives"))
    out += struct.pack("<BIIB", 1 if activate_accept else 0,
                       client_quest_flags(quest),
                       _q(quest, "suggested_players"), 0)
    if has_hidden_rewards(quest):
        out += struct.pack("<IIII", 0, 0, 0, 0)
    else:
        out += _reward_item_block(reward_choices(quest), displays)
        out += _reward_item_block(reward_items(quest), displays)
        out += struct.pack("<II", reward_money(quest), quest_xp(quest, player_level))
    out += struct.pack("<If", _q(quest, "rew_honor_addition"),
                       float(quest.get("rew_honor_multiplier") or 0.0))
    out += struct.pack("<Ii", _q(quest, "rew_spell"), _q(quest, "rew_spell_cast"))
    out += struct.pack("<IIII", _q(quest, "char_title_id"), _q(quest, "bonus_talents"),
                       0, 0)                    # arena points, unknown
    out += _reputation_block(quest)
    out += struct.pack("<I", QUEST_EMOTE_COUNT)
    for index in range(1, QUEST_EMOTE_COUNT + 1):
        out += struct.pack("<II", _q(quest, f"details_emote{index}"),
                           _q(quest, f"details_emote_delay{index}"))
    return bytes(out)


def build_quest_query_response(quest: Mapping) -> bytes:
    """``SMSG_QUEST_QUERY_RESPONSE`` (TrinityCore 3.3.5 layout)."""
    hidden = has_hidden_rewards(quest)
    out = bytearray()
    out += struct.pack(
        "<IIiIiIIIIIIII",
        int(quest["entry"]), _q(quest, "method"), _q(quest, "quest_level"),
        _q(quest, "min_level"), _q(quest, "zone_or_sort"), _q(quest, "type"),
        _q(quest, "suggested_players"),
        _q(quest, "rep_objective_faction"), _q(quest, "rep_objective_value"),
        0, 0,                                   # second rep objective
        _q(quest, "next_quest_in_chain"), _q(quest, "rew_xp_id"),
    )
    out += struct.pack("<i", 0 if hidden else _q(quest, "rew_or_req_money"))
    out += struct.pack("<IIi", _q(quest, "rew_money_max_level"),
                       _q(quest, "rew_spell"), _q(quest, "rew_spell_cast"))
    out += struct.pack("<If", _q(quest, "rew_honor_addition"),
                       float(quest.get("rew_honor_multiplier") or 0.0))
    out += struct.pack("<IIIIIII", _q(quest, "src_item_id"),
                       client_quest_flags(quest) & 0xFFFF, _q(quest, "char_title_id"),
                       _q(quest, "players_slain"), _q(quest, "bonus_talents"), 0, 0)
    for index in range(1, QUEST_REWARDS_COUNT + 1):
        item = 0 if hidden else _q(quest, f"rew_item_id{index}")
        count = 0 if hidden else _q(quest, f"rew_item_count{index}")
        out += struct.pack("<II", item, count)
    for index in range(1, QUEST_REWARD_CHOICES_COUNT + 1):
        item = 0 if hidden else _q(quest, f"rew_choice_item_id{index}")
        count = 0 if hidden else _q(quest, f"rew_choice_item_count{index}")
        out += struct.pack("<II", item, count)
    out += _reputation_block(quest)
    out += struct.pack("<IffI", _q(quest, "point_map_id"),
                       float(quest.get("point_x") or 0.0),
                       float(quest.get("point_y") or 0.0), _q(quest, "point_opt"))
    for name in ("title", "objectives", "details", "end_text", "completed_text"):
        out += _cstring(quest.get(name))
    for index in range(1, QUEST_OBJECTIVES_COUNT + 1):
        target = _q(quest, f"req_creature_or_go_id{index}")
        if target < 0:
            target = (-target) | 0x80000000
        source = (_q(quest, f"req_source_id{index}")
                  if index <= QUEST_SOURCE_ITEM_COUNT else 0)
        out += struct.pack("<IIII", target & 0xFFFFFFFF,
                           _q(quest, f"req_creature_or_go_count{index}"),
                           source, _q(quest, f"req_source_count{index}"))
    for index in range(1, QUEST_ITEM_OBJECTIVES_COUNT + 1):
        out += struct.pack("<II", _q(quest, f"req_item_id{index}"),
                           _q(quest, f"req_item_count{index}"))
    for index in range(1, QUEST_OBJECTIVES_COUNT + 1):
        out += _cstring(quest.get(f"objective_text{index}"))
    return bytes(out)


def build_request_items(guid: int, quest: Mapping, can_complete: bool,
                        displays: Mapping[int, int], close_on_cancel: bool = False) -> bytes:
    """``SMSG_QUESTGIVER_REQUEST_ITEMS``: the "bring me X" window."""
    out = bytearray(struct.pack("<QI", int(guid) & 0xFFFFFFFFFFFFFFFF, int(quest["entry"])))
    out += _cstring(quest.get("title"))
    out += _cstring(quest.get("request_items_text"))
    emote = _q(quest, "complete_emote") if can_complete else _q(quest, "incomplete_emote")
    out += struct.pack("<IIIIII", 0, emote, 1 if close_on_cancel else 0,
                       client_quest_flags(quest),
                       _q(quest, "suggested_players"), required_money(quest))
    out += _reward_item_block(item_objectives(quest), displays)
    out += struct.pack("<IIII", 0x03 if can_complete else 0x00, 0x04, 0x08, 0x10)
    return bytes(out)


def build_offer_reward(guid: int, quest: Mapping, player_level: int,
                       displays: Mapping[int, int], enable_next: bool = True) -> bytes:
    """``SMSG_QUESTGIVER_OFFER_REWARD``: the reward/choice window."""
    out = bytearray(struct.pack("<QI", int(guid) & 0xFFFFFFFFFFFFFFFF, int(quest["entry"])))
    out += _cstring(quest.get("title"))
    out += _cstring(quest.get("offer_reward_text"))
    out += struct.pack("<BII", 1 if enable_next else 0,
                       client_quest_flags(quest),
                       _q(quest, "suggested_players"))
    emotes = []
    for index in range(1, QUEST_EMOTE_COUNT + 1):
        emote = _q(quest, f"offer_reward_emote{index}")
        if emote <= 0:
            break
        emotes.append((_q(quest, f"offer_reward_emote_delay{index}"), emote))
    out += struct.pack("<I", len(emotes))
    for delay, emote in emotes:
        out += struct.pack("<II", delay, emote)
    out += _reward_item_block(reward_choices(quest), displays)
    out += _reward_item_block(reward_items(quest), displays)
    out += struct.pack("<II", reward_money(quest), quest_xp(quest, player_level))
    out += struct.pack("<If", _q(quest, "rew_honor_addition"),
                       float(quest.get("rew_honor_multiplier") or 0.0))
    out += struct.pack("<IIi", 0x08, _q(quest, "rew_spell"), _q(quest, "rew_spell_cast"))
    out += struct.pack("<IIII", _q(quest, "char_title_id"), _q(quest, "bonus_talents"), 0, 0)
    out += _reputation_block(quest)
    return bytes(out)


def build_quest_complete(quest_id: int, xp: int, money: int, honor: int = 0,
                         talents: int = 0, arena: int = 0) -> bytes:
    """``SMSG_QUESTGIVER_QUEST_COMPLETE`` (``Player::SendQuestReward``)."""
    return struct.pack("<IIIIII", int(quest_id), int(xp), int(money),
                       10 * int(honor), int(talents), int(arena))


def build_questupdate_add_kill(quest_id: int, target: int, count: int,
                               required: int, guid: int) -> bytes:
    return struct.pack("<IIIIQ", int(quest_id), int(target) & 0xFFFFFFFF,
                       int(count), int(required), int(guid) & 0xFFFFFFFFFFFFFFFF)


def build_questupdate_complete(quest_id: int) -> bytes:
    return struct.pack("<I", int(quest_id))


def build_quest_invalid(reason: int) -> bytes:
    return struct.pack("<I", int(reason))


def build_quest_poi_response(quest_ids: Sequence[int]) -> bytes:
    """``SMSG_QUEST_POI_QUERY_RESPONSE`` with no POI data for each quest."""
    out = bytearray(struct.pack("<I", len(quest_ids)))
    for quest_id in quest_ids:
        out += struct.pack("<II", int(quest_id), 0)
    return bytes(out)
